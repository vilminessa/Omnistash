"""Каталог площадки (site-like browsing) - карточки с самой площадки.

НЕ путать с tests/test_catalog.py: тот - mesh-зеркало индекса участников
(B3). Здесь - просмотр трендов/поиска, кэш, пагинация и возрастной гейт.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import catalog
from app import legal as legal_mod
from app import repo
from app.catalog import CACHE_TTL, CatalogError, PAGE_SIZE
from app.gui import Api
from app.util import QuietLogger


def entry(remote_id="vid1", **overrides):
    item = {"id": remote_id, "title": "Клип", "duration": 213,
            "view_count": 1000, "uploader": "Автор",
            "url": f"https://www.youtube.com/watch?v={remote_id}",
            "thumbnail": "https://i.ytimg.com/vi/" + remote_id + "/maxres.jpg"}
    item.update(overrides)
    return item


PH_HTML = """
<div class="phimage">
  <a href="/view_video.php?viewkey=abc123" title="Первый клип"
     class="linkVideoThumb img">
    <img src="https://pix.phncdn.com/th.jpg" alt="Первый клип">
  </a>
  <span class="title"><a href="/view_video.php?viewkey=abc123">Первый клип</a></span>
  <div class="tags">
    <a href="/video/tag/uncut">uncut</a>
    <a href="/video/tag/glasses">glasses</a>
    <a href="/model/made-of-two">Made of Two</a>
  </div>
</div>
<div class="phimage">
  <a href="/view_video.php?viewkey=def456" title="Второй &quot;клип&quot;">
    <img src="https://pix.phncdn.com/th2.jpg">
  </a>
</div>
"""


def scripted(responses):
    """Подмена yt_dlp.YoutubeDL: ответы по порядку вызовов.

    Ответ - dict (info площадки) или исключение (бросается). Вызовы
    пишутся в Ydl.calls: (url, opts) - проверяем адрес и диапазон записей.
    """
    seq = list(responses)

    class Ydl:
        calls = []

        def __init__(self, opts, **kwargs):
            self.opts = dict(opts)      # logger и playlist_items - в params
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            Ydl.calls.append((url, self.opts))
            item = seq.pop(0) if seq else {"entries": []}
            if isinstance(item, BaseException):
                raise item
            return item

    return Ydl


def raising(exc):
    """Функция-заглушка, которая всегда бросает переданный отказ."""
    def _fail(url, settings):
        raise exc
    return _fail


class CatalogCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        patcher = mock.patch("app.catalog.profile_dir", lambda: self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class TestCards(CatalogCase):
    def test_normalize_card(self):
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])):
            res = catalog.fetch(platform="youtube", mode="trending")
        self.assertEqual(len(res["items"]), 1)
        card = res["items"][0]
        self.assertEqual(card["video_id"], "vid1")
        self.assertEqual(card["title"], "Клип")
        self.assertEqual(card["duration_s"], 213)
        self.assertEqual(card["views"], 1000)
        self.assertEqual(card["author"], "Автор")
        self.assertEqual(card["url"], "https://www.youtube.com/watch?v=vid1")
        self.assertFalse(card["added"])
        self.assertEqual(res["source"], "site")

    def test_missing_url_built_from_id(self):
        raw = entry(url=None, webpage_url=None)
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [raw]}])):
            res = catalog.fetch(platform="youtube")
        self.assertEqual(res["items"][0]["url"],
                         "https://www.youtube.com/watch?v=vid1")

    def test_ph_mirror_canonicalized(self):
        """Зеркало rt.pornhub.org -> www.pornhub.com (тот же viewkey)."""
        url = catalog._canonical(
            "https://rt.pornhub.org/view_video.php?viewkey=698a3d3b0e335",
            "pornhub", "698a3d3b0e335")
        self.assertEqual(url, "https://www.pornhub.com/view_video.php"
                              "?viewkey=698a3d3b0e335")

    def test_ph_cards_from_page(self):
        """Карточки PH читаются со страницы площадки (yt-dlp списки PH не умеет)."""
        with mock.patch.object(catalog, "_http_get",
                               lambda url, settings: PH_HTML):
            res = catalog.fetch(platform="pornhub", mode="trending")
        ids = [item["video_id"] for item in res["items"]]
        self.assertEqual(ids, ["abc123", "def456"],
                         "дубли ссылки (обложка и заголовок) схлопнуты")
        first = res["items"][0]
        self.assertEqual(first["title"], "Первый клип")
        self.assertEqual(first["thumb"], "https://pix.phncdn.com/th.jpg")
        self.assertEqual(first["url"],
                         "https://www.pornhub.com/view_video.php?viewkey=abc123")
        # длительность и просмотры в HTML страницы не лежат (их подгружает JS)
        self.assertIsNone(first["duration_s"])
        self.assertIsNone(first["views"])
        self.assertEqual(res["items"][1]["title"], 'Второй "клип"',
                         "html-escape в title разворачивается")
        self.assertEqual(first["tags"], ["uncut", "glasses"],
                         "теги карточки - ссылки /video/tag/... рядом")
        self.assertEqual(first["people"], ["Made of Two"],
                         "исполнители карточки - ссылки /model/... рядом")
        self.assertEqual(res["items"][1]["tags"], [],
                         "теги берутся из своей карточки, не со всей страницы")
        self.assertEqual(res["items"][1]["people"], [],
                         "исполнители - тоже из своей карточки")

    def test_ph_tag_page(self):
        """Тег из карточек - страница тега площадки (тот же разбор карточек)."""
        urls = []

        def record(url, settings):
            urls.append(url)
            return PH_HTML

        with mock.patch.object(catalog, "_http_get", record):
            res = catalog.fetch(platform="pornhub", mode="tag", query="uncut",
                                force=True)
        self.assertIn("/video/tag/uncut", urls[0])
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["items"]), 2)

    def test_ph_search_page_param(self):
        urls = []

        def record(url, settings):
            urls.append(url)
            return PH_HTML

        with mock.patch.object(catalog, "_http_get", record):
            catalog.fetch(platform="pornhub", mode="search", query="кошки",
                          force=True)
            catalog.fetch(platform="pornhub", mode="search", query="кошки",
                          page=2, force=True)
        self.assertIn("search=кошки", urls[0])
        self.assertIn("page=2", urls[1], "вторая страница - параметр страницы")

    def test_dedup_by_video_id(self):
        """Одна и та же запись из нескольких адресов - одна карточка."""
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry(), entry()]}])):
            res = catalog.fetch(platform="youtube")
        self.assertEqual(len(res["items"]), 1)

    def test_unavailable_entry_skipped(self):
        """Записи без id (приватное/удалённое) в карточки не попадают."""
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry(), None]}])):
            res = catalog.fetch(platform="youtube")
        self.assertEqual(len(res["items"]), 1)

    def test_thumbnail_prefers_last(self):
        raw = entry(thumbnail=None,
                    thumbnails=[{"url": "small.jpg"}, {"url": "big.jpg"}])
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [raw]}])):
            res = catalog.fetch(platform="youtube")
        self.assertEqual(res["items"][0]["thumb"], "big.jpg")


class TestPagination(CatalogCase):
    def test_range_passed_to_extractor(self):
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])) as fake:
            catalog.fetch(platform="youtube", page=3)
        _url, opts = fake.calls[0]
        self.assertEqual(opts["playlist_items"],
                         f"{2 * PAGE_SIZE + 1}-{3 * PAGE_SIZE}")
        self.assertEqual(opts["extract_flat"], "in_playlist")
        self.assertTrue(opts["skip_download"], "каталог не качает файлы")

    def test_full_page_means_more(self):
        many = [{"id": f"v{i}", "title": f"Клип {i}"} for i in range(PAGE_SIZE)]
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": many}])):
            res = catalog.fetch(platform="youtube")
        self.assertTrue(res["has_more"])
        self.assertEqual(len(res["items"]), PAGE_SIZE)

    def test_trending_search_are_open_pages(self):
        """Тренды/поиск открыты: короткая страница ещё не значит «конец».

        YouTube не отдаёт признак конца списка - листание заканчивает UI,
        когда страница пришла пустой или полностью дублированной.
        """
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry(), entry("vid2")]}])):
            res = catalog.fetch(platform="youtube", mode="trending",
                                force=True)
        self.assertTrue(res["has_more"])

    def test_channel_page_ends_on_short_page(self):
        """Страница загрузок канала конечна: короткая страница = конец."""
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry(), entry("vid2")]}])):
            res = catalog.fetch(platform="youtube", mode="channel",
                                query="https://www.youtube.com/channel/UC"
                                      + "a" * 22 + "/videos", force=True)
        self.assertFalse(res["has_more"])

    def test_ph_has_more_by_page_length(self):
        """PH отдаёт ~44 карточки на страницу: has_more по длине страницы,
        а не по нашему PAGE_SIZE (иначе «Загрузить ещё» вставал бы)."""
        big = "\n".join(
            f'<div class="phimage"><a href="/view_video.php?viewkey=k{i}"'
            f' title="К {i}"><img src="t{i}.jpg"></a></div>'
            for i in range(31))
        urls = []

        def record(url, settings):
            urls.append(url)
            return big

        with mock.patch.object(catalog, "_http_get", record):
            first = catalog.fetch(platform="pornhub", force=True)
            self.assertTrue(first["has_more"])
            self.assertEqual(len(first["items"]), 31)
            catalog.fetch(platform="pornhub", page=2, force=True)
        self.assertIn("page=2", urls[1], "вторая страница - параметр страницы")

    def test_empty_next_page_is_end_not_error(self):
        """Следняя страница пустая - это конец листа, а не ошибка «повтори»."""
        with mock.patch.object(catalog, "_probe",
                               lambda url, settings: "ok"):
            with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                                   scripted([{"entries": []}, {"entries": []},
                                             {"entries": []}])):
                res = catalog.fetch(platform="youtube", page=2, force=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["items"], [])
        self.assertFalse(res["has_more"])
        self.assertTrue(res.get("end"))

    def test_trending_falls_back_to_results(self):
        """feed/trending в плоском режиме часто пуст - пробуем results."""
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": []},
                                         {"entries": [entry()]}])) as fake:
            res = catalog.fetch(platform="youtube")
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(len(fake.calls), 2,
                         "первый адрес пуст - попытан второй")

    def test_search_uses_results_url(self):
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry("vid2")]}])) as fake:
            res = catalog.fetch(platform="youtube", mode="search",
                                query="кошки")
        self.assertEqual(fake.calls[0][0],
                         "https://www.youtube.com/results?search_query=кошки")
        self.assertEqual(res["mode"], "search")

    def test_empty_query_falls_to_trending(self):
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])):
            res = catalog.fetch(platform="youtube", mode="search", query="")
        self.assertEqual(res["mode"], "trending")


class TestCache(CatalogCase):
    def test_cache_hit_skips_network(self):
        fake = scripted([{"entries": [entry()]}])
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", fake):
            first = catalog.fetch(platform="youtube")
        self.assertEqual(first["source"], "site")
        second = catalog.fetch(platform="youtube")
        self.assertEqual(second["source"], "cache")
        self.assertEqual(len(fake.calls), 1, "кэш не должен трогать сеть")
        self.assertEqual(second["items"][0]["video_id"], "vid1")

    def test_force_bypasses_cache(self):
        fake = scripted([{"entries": [entry()]}, {"entries": [entry("vid2")]}])
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", fake):
            catalog.fetch(platform="youtube")
            forced = catalog.fetch(platform="youtube", force=True)
        self.assertEqual(forced["source"], "site")
        self.assertEqual(len(fake.calls), 2)

    def test_ttl_expires(self):
        fake = scripted([{"entries": [entry()]}, {"entries": [entry("vid2")]}])
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", fake):
            catalog.fetch(platform="youtube", mode="trending")
            later = time.time() + CACHE_TTL["trending"] + 1
            with mock.patch.object(catalog.time, "time", lambda: later):
                res = catalog.fetch(platform="youtube", mode="trending")
        self.assertEqual(res["source"], "site")
        self.assertEqual(len(fake.calls), 2)

    def test_queries_cached_separately(self):
        fake = scripted([{"entries": [entry()]}, {"entries": [entry("vid2")]}])
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", fake):
            catalog.fetch(platform="youtube", mode="search", query="кошки")
            catalog.fetch(platform="youtube", mode="search", query="собаки")
        self.assertEqual(len(fake.calls), 2,
                         "разные запросы - разные кэш-ключи")

    def test_clear_cache_by_platform(self):
        with mock.patch.object(catalog, "_http_get",
                               lambda url, settings: PH_HTML):
            with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                                   scripted([{"entries": [entry()]}])):
                catalog.fetch(platform="youtube")
                catalog.fetch(platform="pornhub")
        files = list((self.dir / "catalog_cache").glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertEqual(catalog.clear_cache("youtube"), 1)
        self.assertEqual(list((self.dir / "catalog_cache").glob("youtube.*")),
                         [])
        self.assertEqual(len(list((self.dir / "catalog_cache").glob("pornhub.*"))),
                         1)
        self.assertEqual(catalog.clear_cache(), 1)


class TestFailures(CatalogCase):
    def test_blocked_kind(self):
        """Сетевой отказ страницы PH - kind=blocked доходит до вызывающего."""
        with mock.patch.object(catalog, "_http_get",
                               raising(CatalogError("Площадка не ответила",
                                                    kind="blocked"))):
            with self.assertRaises(CatalogError) as ctx:
                catalog.fetch(platform="pornhub")
        self.assertEqual(ctx.exception.kind, "blocked")

    def test_http_get_classifies_block(self):
        """403/410 на страницу - blocked; сетевой отказ тоже blocked.

        ignoreerrors/плоский режим не различают блокировку, поэтому её
        решает именно этот путь (PH списки читаем напрямую).
        """
        import urllib.request

        def fail_403(*args, **kwargs):
            # HTTPError мусорит при GC; текст ответа решает не меньше
            raise OSError("HTTP Error 403: Forbidden")

        with mock.patch("urllib.request.urlopen", fail_403):
            with self.assertRaises(CatalogError) as ctx:
                catalog._http_get("https://www.pornhub.com/video", {})
        self.assertEqual(ctx.exception.kind, "blocked")

        def fail_reset(*args, **kwargs):
            raise OSError("Connection reset by peer")

        with mock.patch("urllib.request.urlopen", fail_reset):
            with self.assertRaises(CatalogError) as ctx:
                catalog._http_get("https://www.pornhub.com/video", {})
        self.assertEqual(ctx.exception.kind, "blocked")

    def test_empty_kind(self):
        with mock.patch.object(catalog, "_probe",
                               lambda url, settings: "ok"):
            with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                                   scripted([{"entries": []}, {"entries": []},
                                             {"entries": []}])):
                with self.assertRaises(CatalogError) as ctx:
                    catalog.fetch(platform="youtube")
        self.assertEqual(ctx.exception.kind, "empty")

    def test_probe_blocked_kind(self):
        """Площадка отвечает 403/410 на страницу: kinds=blocked, не «пусто».

        ignoreerrors съедает HTTP-отказ внутри экстрактора, поэтому
        различаем блокировку отдельной пробой.
        """
        with mock.patch.object(catalog, "_probe",
                               lambda url, settings: "blocked"):
            with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                                   scripted([{"entries": []},
                                             {"entries": []},
                                             {"entries": []}])):
                with self.assertRaises(CatalogError) as ctx:
                    catalog.fetch(platform="youtube")
        self.assertEqual(ctx.exception.kind, "blocked")
        self.assertIn("VPN", str(ctx.exception))

    def test_unsupported_platform(self):
        with self.assertRaises(CatalogError) as ctx:
            catalog.fetch(platform="vimeo")
        self.assertEqual(ctx.exception.kind, "unsupported")

    def test_vk_has_no_catalog(self):
        """VK: списки площадок yt-dlp не разбирает - каталога нет (P5)."""
        self.assertNotIn("vk", catalog.FETCHERS)
        with self.assertRaises(CatalogError) as ctx:
            catalog.fetch(platform="vk")
        self.assertEqual(ctx.exception.kind, "unsupported")

    def test_unsupported_page_url(self):
        """Страница, которую yt-dlp не разбирает, - unsupported, не «пусто»."""
        with mock.patch.object(catalog, "_supported", lambda url: False):
            with self.assertRaises(CatalogError) as ctx:
                catalog.fetch(platform="youtube")
        self.assertEqual(ctx.exception.kind, "unsupported")

    def test_failure_is_not_cached(self):
        """Пустая страница не записывается в кэш: повтор пробует снова."""
        with mock.patch.object(catalog, "_probe",
                               lambda url, settings: "ok"):
            with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                                   scripted([{"entries": []}, {"entries": []},
                                             {"entries": []}])):
                with self.assertRaises(CatalogError):
                    catalog.fetch(platform="youtube")
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])) as fake:
            res = catalog.fetch(platform="youtube")
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(len(fake.calls), 1, "кэш ошибки не блокирует повтор")


class TestSilence(CatalogCase):
    """Шум: логгер yt-dlp молчит, текст отказов - без ANSI."""

    def test_extractor_logger_is_silent(self):
        """Логгер передаётся молчаливый: ERROR yt-dlp не печатает в консоль.

        quiet=True логгер не глушит - без SilentLogger строки
        «ERROR: [youtube:tab] …» забивают консоль и журнал.
        """
        fake = scripted([{"entries": [entry()]}])
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", fake):
            catalog.fetch(platform="youtube", mode="trending", force=True)
        logger = fake.calls[0][1]["logger"]
        self.assertIsInstance(logger, QuietLogger)
        self.assertTrue(hasattr(logger, "error") and hasattr(logger, "warning")
                        and hasattr(logger, "info"))

    def test_logger_error_goes_to_journal_without_ansi(self):
        """Логгер пишет ошибку в журнал (без ANSI) и молчит в остальном."""
        from app.util import QuietLogger as QL
        messages = []
        logger = QL(messages.append)
        logger.error("\x1b[0;31mERROR:\x1b[0m 410 Gone")
        logger.warning("warn me")
        logger.info("progress")
        self.assertEqual(messages, ["ERROR: 410 Gone"])

    def test_clean_text_strips_ansi(self):
        self.assertEqual(catalog._clean("\x1b[0;31mERROR:\x1b[0m 410 Gone"),
                         "ERROR: 410 Gone")
        self.assertEqual(catalog._clean(None), "")

    def test_failure_logs_clean_message(self):
        """Отказ страницы пишется в журнал без escape-кодов."""
        err = catalog.yt_dlp.utils.DownloadError(
            "\x1b[0;31mERROR:\x1b[0m [youtube:tab] redirect")
        messages = []
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([err, {"entries": [entry()]}])), \
             mock.patch.object(catalog, "_supported", lambda url: True):
            catalog.fetch(platform="youtube", mode="trending", force=True,
                          log=messages.append)
        joined = "\n".join(messages)
        self.assertNotIn("\x1b", joined)
        self.assertIn("redirect", joined)


RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://schemas.microsoft.com/2005/Atom"
      xmlns:media="http://search.yahoo.com/mrss/">
  <title>Канал</title>
  <entry>
    <yt:videoId>old1</yt:videoId>
    <title>Старое видео</title>
    <published>2026-10-01T10:00:00+00:00</published>
    <media:group>
      <media:thumbnail url="https://i.ytimg.com/vi/old1/hqdefault.jpg"/>
      <media:statistics views="700"/>
    </media:group>
  </entry>
  <entry>
    <yt:videoId>new2</yt:videoId>
    <title>Новое видео</title>
    <published>2026-10-25T10:00:00+00:00</published>
    <media:group>
      <media:thumbnail url="https://i.ytimg.com/vi/new2/hqdefault.jpg"/>
      <media:statistics views="900"/>
    </media:group>
  </entry>
</feed>
"""

CHANNELS = [{"remote_id": "UC" + "a" * 22, "title": "Канал",
             "url": "https://www.youtube.com/channel/UC" + "a" * 22}]


class TestChannelsMode(CatalogCase):
    """Режим «новое от каналов»: лента канала (RSS) с датой публикации.

    Flat-список страницы канала дат не несёт, поэтому свежее от каналов
    берётся из RSS: один дешёвый GET на канал, сортировка по дате честная.
    """

    def test_rss_cards_sorted_by_date(self):
        urls = []

        def record(url, settings):
            urls.append(url)
            return RSS_XML

        with mock.patch.object(catalog, "_http_get", record):
            res = catalog.fetch(platform="youtube", mode="channels",
                                channels=CHANNELS, force=True)
        self.assertIn("feeds/videos.xml", urls[0])
        self.assertIn("channel_id=UC", urls[0])
        self.assertEqual([item["video_id"] for item in res["items"]],
                         ["new2", "old1"], "новые сверху")
        self.assertEqual(res["items"][0]["uploaded_at"], "2026-10-25")
        self.assertEqual(res["items"][0]["views"], 900)
        self.assertEqual(res["items"][0]["thumb"],
                         "https://i.ytimg.com/vi/new2/hqdefault.jpg")
        self.assertFalse(res["has_more"], "лента отдаёт последние ~15 - дальше листать нечем")

    def test_channels_scope_cached_separately(self):
        """Список каналов - часть кэш-ключа: другой список не берётся из кэша."""
        calls = []

        def record(url, settings):
            calls.append(url)
            return RSS_XML

        with mock.patch.object(catalog, "_http_get", record):
            catalog.fetch(platform="youtube", mode="channels",
                          channels=CHANNELS)
            catalog.fetch(platform="youtube", mode="channels",
                          channels=CHANNELS)         # кэш
            other = [{"remote_id": "UC" + "b" * 22, "title": "Другой"}]
            catalog.fetch(platform="youtube", mode="channels",
                          channels=other, force=False)
        self.assertEqual(len(calls), 2,
                         "тот же список - из кэша, новый список - запрос")

    def test_channels_blocked_propagates(self):
        with mock.patch.object(catalog, "_http_get",
                               raising(CatalogError("Площадка не ответила",
                                                    kind="blocked"))):
            with self.assertRaises(CatalogError) as ctx:
                catalog.fetch(platform="youtube", mode="channels",
                              channels=CHANNELS, force=True)
        self.assertEqual(ctx.exception.kind, "blocked")

    def test_channels_without_channels(self):
        """Ни одного канала - честный текст, а не пустой экран молча."""
        with self.assertRaises(CatalogError) as ctx:
            catalog.fetch(platform="youtube", mode="channels", channels=[])
        self.assertEqual(ctx.exception.kind, "empty")
        self.assertIn("канал", str(ctx.exception))

    def test_channel_page_mode_paginates(self):
        """Один канал: страница загрузок диапазонно (глубже, но без дат)."""
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry("vid1")]}])) as fake:
            catalog.fetch(platform="youtube", mode="channel",
                          query="https://www.youtube.com/channel/UC" + "a" * 22
                                + "/videos", force=True)
        self.assertEqual(fake.calls[0][0],
                         "https://www.youtube.com/channel/UC" + "a" * 22
                         + "/videos")
        self.assertEqual(fake.calls[0][1]["playlist_items"],
                         f"1-{PAGE_SIZE}")


class ApiCatalogCase(unittest.TestCase):
    """Api: site-каталог не конфликтует с mesh-каталогом (catalog_* заняты)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        for target, value in (
                ("app.settings.settings_path", self.dir / "settings.json"),
                ("app.db.db_path", self.dir / "library.db"),
                ("app.gui.log_path", self.dir / "omnistash.log"),
                ("app.legal.legal_path", self.dir / "legal.json"),
                ("app.catalog.profile_dir", self.dir)):
            patcher = mock.patch(target, lambda v=value: v)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.api = Api()
        self.addCleanup(self.api.close)

    def load(self, request, method="site_catalog", timeout=5.0) -> dict:
        """Запрос каталога: воркер + опрос статуса (как в интерфейсе)."""
        res = getattr(self.api, method)(request)
        if res.get("done") or not res.get("running"):
            return res
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.api.site_catalog_status()
            if not status.get("running"):
                return status
            time.sleep(0.02)
        raise AssertionError("загрузка каталога не завершилась")

    def test_both_catalog_endpoints_alive(self):
        """Регрессия затмевания: site_catalog и mesh catalog_list разные."""
        self.assertTrue(hasattr(self.api, "site_catalog"))
        self.assertTrue(hasattr(self.api, "site_catalog_more"))
        self.assertTrue(hasattr(self.api, "site_catalog_clear"))
        self.assertTrue(hasattr(self.api, "site_catalog_status"))
        self.assertTrue(hasattr(self.api, "site_catalog_cancel"))
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])):
            site = self.load({"platform": "youtube"})
        self.assertTrue(site.get("ok"), site)
        self.assertIn("items", site)
        mesh = self.api.catalog_list({"query": "клип"})
        self.assertTrue(mesh.get("ok"))
        self.assertIn("rows", mesh)
        self.assertIn("origins", mesh)
        self.assertNotIn("items", mesh)

    def test_cache_returns_immediately(self):
        """Свежий кэш отдаётся без воркера: ответ сразу и source=cache."""
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])):
            first = self.load({"platform": "youtube"})
        self.assertEqual(first["source"], "site")
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])) as fake:
            second = self.api.site_catalog({"platform": "youtube"})
        self.assertTrue(second.get("done"), "кэш - без воркера и без сети")
        self.assertEqual(second["source"], "cache")
        self.assertEqual(fake.calls, [])

    def test_adult_gate_blocks_pornhub(self):
        urls = []

        def record(url, settings):
            urls.append(url)
            return PH_HTML

        with mock.patch.object(catalog, "_http_get", record):
            denied = self.api.site_catalog({"platform": "pornhub"})
            self.assertTrue(denied.get("need_adult"))
            self.assertEqual(urls, [], "без 18+ запрос к площадке не идёт")
            legal_mod.accept(adult=True, aup=True)
            allowed = self.load({"platform": "pornhub"})
        self.assertTrue(allowed.get("ok"), allowed)
        self.assertEqual(len(urls), 1,
                         "после подтверждения - один запрос к площадке")

    def test_added_marks_library_row(self):
        repo.upsert_video(self.api.db.conn,
                          {"platform": "youtube", "remote_id": "vid1",
                           "title": "Мой клип"}, full=False)
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])):
            res = self.load({"platform": "youtube"})
        self.assertTrue(res["items"][0]["added"])

    def test_blocked_reports_kind(self):
        err = catalog.yt_dlp.utils.DownloadError("Connection reset by peer.")
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", scripted([err])):
            res = self.load({"platform": "youtube"})
        self.assertEqual(res["kind"], "blocked")
        self.assertTrue(res.get("error"))

    def test_cancel_drops_result(self):
        """Отмена: карточки не применяются, статус говорит «отменено»."""
        class Slow:
            def __init__(self, opts, **kwargs):
                self.opts = dict(opts)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                time.sleep(0.3)               # запрос «идёт» долго
                return {"entries": [entry()]}

        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", Slow):
            started = self.api.site_catalog({"platform": "youtube",
                                             "force": True})
            self.assertTrue(started.get("running"))
            self.api.site_catalog_cancel()
            deadline = time.time() + 5.0
            status = None
            while time.time() < deadline:
                status = self.api.site_catalog_status()
                if not status.get("running"):
                    break
                time.sleep(0.02)
        self.assertEqual(status.get("kind"), "cancelled", status)
        self.assertEqual(status.get("items") or [], [],
                         "отменённая загрузка не отдаёт карточки")

    def test_more_endpoint_pages_forward(self):
        fake = scripted([{"entries": [entry()]}, {"entries": [entry("vid2")]}])
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", fake):
            first = self.load({"platform": "youtube", "page": 1})
            self.assertTrue(first.get("ok"), first)
            second = self.load({"platform": "youtube", "page": 2},
                               method="site_catalog_more")
            self.assertTrue(second.get("ok"), second)
        self.assertEqual(fake.calls[1][1]["playlist_items"],
                         f"{PAGE_SIZE + 1}-{2 * PAGE_SIZE}")

    def test_site_catalog_clear_endpoint(self):
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])):
            self.load({"platform": "youtube"})
        res = self.api.site_catalog_clear({"platform": "youtube"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 1)

    def test_channels_endpoint_lists_library_channels(self):
        repo.upsert_video(self.api.db.conn,
                          {"platform": "youtube", "remote_id": "vid1",
                           "title": "Клип",
                           "channel": {"platform": "youtube",
                                       "remote_id": "UC" + "a" * 22,
                                       "title": "Канал", "handle": None,
                                       "url": "https://www.youtube.com/channel/UC"
                                              + "a" * 22, "fallback": False}},
                          full=False)
        res = self.api.site_catalog_channels({"platform": "youtube"})
        self.assertTrue(res["ok"])
        self.assertEqual([item["remote_id"] for item in res["channels"]],
                         ["UC" + "a" * 22])
        self.assertEqual(res["channels"][0]["n"], 1)

    def test_site_catalog_channels_mode_from_library(self):
        """Режим channels: список каналов берётся из библиотеки, даты из RSS."""
        repo.upsert_video(self.api.db.conn,
                          {"platform": "youtube", "remote_id": "new2",
                           "title": "Мой клип",
                           "channel": {"platform": "youtube",
                                       "remote_id": "UC" + "a" * 22,
                                       "title": "Канал", "handle": None,
                                       "url": None, "fallback": False}},
                          full=False)
        with mock.patch.object(catalog, "_http_get",
                               lambda url, settings: RSS_XML):
            res = self.load({"platform": "youtube", "mode": "channels"})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual([item["video_id"] for item in res["items"]],
                         ["new2", "old1"])
        self.assertTrue(res["items"][0]["added"],
                        "карточка канала, который уже сохранялся, помечена")
        self.assertFalse(res["items"][1]["added"])
        self.assertEqual(res["items"][0]["uploaded_at"], "2026-10-25")
        self.assertFalse(res["has_more"])

    def test_platforms_report_catalog_support(self):
        """Переключатель «Каталог» показывают только поддерживаемые
        площадки (VK без списков страниц - без каталога)."""
        res = self.api.platforms_list()
        by = {item["id"]: item for item in res["platforms"]}
        self.assertTrue(by["youtube"]["catalog"])
        self.assertTrue(by["pornhub"]["catalog"])
        self.assertFalse(by["vk"]["catalog"])


if __name__ == "__main__":
    unittest.main()
