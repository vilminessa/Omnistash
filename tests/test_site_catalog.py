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


def entry(remote_id="vid1", **overrides):
    item = {"id": remote_id, "title": "Клип", "duration": 213,
            "view_count": 1000, "uploader": "Автор",
            "url": f"https://www.youtube.com/watch?v={remote_id}",
            "thumbnail": "https://i.ytimg.com/vi/" + remote_id + "/maxres.jpg"}
    item.update(overrides)
    return item


def scripted(responses):
    """Подмена yt_dlp.YoutubeDL: ответы по порядку вызовов.

    Ответ - dict (info площадки) или исключение (бросается). Вызовы
    пишутся в Ydl.calls: (url, opts) - проверяем адрес и диапазон записей.
    """
    seq = list(responses)

    class Ydl:
        calls = []

        def __init__(self, opts):
            self.opts = dict(opts)

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
        raw = entry(remote_id="698a3d3b0e335",
                    url="https://rt.pornhub.org/view_video.php"
                        "?viewkey=698a3d3b0e335")
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [raw]}])):
            res = catalog.fetch(platform="pornhub")
        self.assertEqual(res["items"][0]["url"],
                         "https://www.pornhub.com/view_video.php"
                         "?viewkey=698a3d3b0e335")

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

    def test_short_page_ends(self):
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry(), entry("vid2")]}])):
            res = catalog.fetch(platform="youtube")
        self.assertFalse(res["has_more"])

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
        fake = scripted([{"entries": [entry()]}, {"entries": [entry("vid2")]}])
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", fake):
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
        err = catalog.yt_dlp.utils.DownloadError("Connection aborted.")
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", scripted([err])):
            with self.assertRaises(CatalogError) as ctx:
                catalog.fetch(platform="pornhub")
        self.assertEqual(ctx.exception.kind, "blocked")
        self.assertIn("недоступна", str(ctx.exception))

    def test_empty_kind(self):
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": []}, {"entries": []},
                                         {"entries": []}])):
            with self.assertRaises(CatalogError) as ctx:
                catalog.fetch(platform="youtube")
        self.assertEqual(ctx.exception.kind, "empty")

    def test_unsupported_platform(self):
        with self.assertRaises(CatalogError) as ctx:
            catalog.fetch(platform="vimeo")
        self.assertEqual(ctx.exception.kind, "unsupported")

    def test_vk_only_search(self):
        with self.assertRaises(CatalogError) as ctx:
            catalog.fetch(platform="vk")
        self.assertEqual(ctx.exception.kind, "unsupported")

    def test_failure_is_not_cached(self):
        """Пустая страница не записывается в кэш: повтор пробует снова."""
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

    def test_both_catalog_endpoints_alive(self):
        """Регрессия затмевания: site_catalog и mesh catalog_list разные."""
        self.assertTrue(hasattr(self.api, "site_catalog"))
        self.assertTrue(hasattr(self.api, "site_catalog_more"))
        self.assertTrue(hasattr(self.api, "site_catalog_clear"))
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])):
            site = self.api.site_catalog({"platform": "youtube"})
        self.assertTrue(site.get("ok"), site)
        self.assertIn("items", site)
        mesh = self.api.catalog_list({"query": "клип"})
        self.assertTrue(mesh.get("ok"))
        self.assertIn("rows", mesh)
        self.assertIn("origins", mesh)
        self.assertNotIn("items", mesh)

    def test_adult_gate_blocks_pornhub(self):
        fake = scripted([{"entries": [entry()]}] * 2)
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", fake):
            denied = self.api.site_catalog({"platform": "pornhub"})
            self.assertTrue(denied.get("need_adult"))
            self.assertEqual(fake.calls, [],
                             "без 18+ запрос к площадке не идёт")
            legal_mod.accept(adult=True, aup=True)
            allowed = self.api.site_catalog({"platform": "pornhub"})
        self.assertTrue(allowed.get("ok"), allowed)
        self.assertEqual(len(fake.calls), 1,
                         "после подтверждения - один запрос к площадке")

    def test_added_marks_library_row(self):
        repo.upsert_video(self.api.db.conn,
                          {"platform": "youtube", "remote_id": "vid1",
                           "title": "Мой клип"}, full=False)
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])):
            res = self.api.site_catalog({"platform": "youtube"})
        self.assertTrue(res["items"][0]["added"])

    def test_blocked_reports_retry(self):
        err = catalog.yt_dlp.utils.DownloadError("Connection reset by peer.")
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", scripted([err])):
            res = self.api.site_catalog({"platform": "youtube"})
        self.assertEqual(res["kind"], "blocked")
        self.assertTrue(res.get("can_retry"))

    def test_more_endpoint_pages_forward(self):
        fake = scripted([{"entries": [entry()]}, {"entries": [entry("vid2")]}])
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL", fake):
            self.api.site_catalog({"platform": "youtube", "page": 1})
            self.api.site_catalog_more({"platform": "youtube", "page": 2})
        self.assertEqual(fake.calls[1][1]["playlist_items"],
                         f"{PAGE_SIZE + 1}-{2 * PAGE_SIZE}")

    def test_site_catalog_clear_endpoint(self):
        with mock.patch.object(catalog.yt_dlp, "YoutubeDL",
                               scripted([{"entries": [entry()]}])):
            self.api.site_catalog({"platform": "youtube"})
        res = self.api.site_catalog_clear({"platform": "youtube"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 1)


if __name__ == "__main__":
    unittest.main()
