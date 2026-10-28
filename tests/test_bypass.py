"""Дороги выкачки (M13): устойчивость к отказам САМОЙ площадки.

Офлайн, без сети: yt_dlp.YoutubeDL подменяется фикстурой (как в
test_download_retry.py и test_impersonate.py). Проверяется поведение цепочки
app/bypass.py, а не YouTube:

  * первая дорога падает -> вторая выкачивает;
  * все дороги падают -> kind последней честной причины + подсказка;
  * лимит попыток (bypass_max_attempts) - не бесконечный повтор;
  * цепочка выключена -> одна дорога (обычный запрос);
  * журнал чистый: без ANSI и с итогом прогона;
  * порядок дорог детерминирован, кэш выигравшей дороги по площадке;
  * attempt_chain («попробовать другую дорогу») forgets the cache.

Обход DPI здесь не используется и не тестится: сеть пользователя не меняется.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import bypass, downloader, google_auth, paths
from app.settings_schema import defaults, field
from tests.test_gui import GuiCase
from yt_dlp.utils import DownloadCancelled, DownloadError

VIDEO = {"webpage_url": "https://example.com/v1", "id": "v1",
         "platform": "youtube"}

BOT_TEXT = ("ERROR: [youtube] Requesting a URL but got a HTTP 403 response, "
            "please sign in to confirm you're not a bot")
BLOCK_TEXT = ("ERROR: [PornHub] Unable to download webpage: "
              "HTTP Error 403: Forbidden (caused by HTTPError)")


class FakeYDL:
    """YoutubeDL-заглушка: очередь действий -> успех с фейковым info.

    Снимает и опции, и ссылку: дорога «зеркало» меняет url, и это видно
    в журнале/захвате (обычный запрос идёт на исходный домен).
    """

    def __init__(self, captured: list, plan: list):
        self._captured = captured
        self._plan = plan

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def add_post_processor(self, *args, **kwargs):
        pass

    def extract_info(self, url, download=True):
        self._captured.append(url)
        action = self._plan.pop(0) if self._plan else "ok"
        if action == "err403":
            raise DownloadError(BLOCK_TEXT)
        if action == "errbot":
            raise DownloadError(BOT_TEXT)
        return {"id": "v1", "title": "Тест", "ext": "mp4",
                "requested_downloads": []}


class ChainCase(unittest.TestCase):
    """Профиль под тестовым: кэш дорог и токены не пишут в реальный профиль."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name)
        self._profile = mock.patch.object(paths, "profile_dir",
                                          lambda: self.dest)
        self._profile.start()
        self.addCleanup(self._profile.stop)

    def run_plan(self, plan, settings=None, *, video=None, log=None,
                 browsers=None, target_of=None):
        """Выкачка фикстурой: снимает опции каждой дороги и ссылки."""
        captured: list = []
        urls: list = []
        shared_plan = list(plan)

        def factory(opts):
            captured.append(dict(opts))
            return FakeYDL(urls, shared_plan)

        settings = {"retries": 1, "dest_dir": str(self.dest), "quality": "high",
                    **(settings or {})}
        no_browsers = browsers if browsers is not None else bypass.browser_order(settings)
        patchers = [
            mock.patch.object(downloader.yt_dlp, "YoutubeDL", side_effect=factory),
            mock.patch.object(downloader, "impersonate_target",
                              target_of or (lambda s: None)),
        ]
        if no_browsers is not None:
            patchers.append(mock.patch.object(google_auth, "BROWSER_CHOICES",
                                              tuple(no_browsers)))
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        result = downloader._download(dict(video or VIDEO), settings,
                                      stop=threading.Event(), log=log)
        return result, captured, urls


class TestChainThroughDownloader(ChainCase):
    def test_first_road_fails_second_wins(self):
        result, captured, _ = self.run_plan(["err403", "ok"])
        self.assertIsNone(result["error"], result)
        self.assertFalse(result["cancelled"])
        self.assertEqual(len(captured), 2)
        self.assertNotIn("cookiesfrombrowser", captured[0])
        self.assertEqual(captured[1].get("cookiesfrombrowser"),
                         ("firefox",), "следующая дорога - куки браузера")
        routes = [r["route"] for r in result["routes"]] if result.get("routes") else []
        self.assertEqual(routes, [], "успех - routes в итоге только у отказа")

    def test_all_roads_fail_keeps_kind_and_hint(self):
        result, captured, urls = self.run_plan(
            ["err403"] * 9, browsers=[],
            video={**VIDEO, "webpage_url": "https://www.youtube.com/watch?v=x"})
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["kind"], "blocked",
                         "kind - последняя честная причина (403 = blocked)")
        self.assertIn("403", result["error"])
        self.assertIn("имитацию", result["error"],
                      "подсказка лечит отказ: куки/имитация, не обход сети")
        self.assertLessEqual(len(captured), 4,
                             "не бесконечный перебор, дефолт 4")
        ids = [r["route"] for r in result["routes"]]
        self.assertEqual(ids, ["plain", "mirror"],
                         "без кук и имитации - зеркало домена площадки")
        self.assertIn("youtu.be", urls[-1], "дорога меняет домен ссылки")

    def test_bot_hint_and_kind_from_downloader_chain(self):
        result, captured, _ = self.run_plan(["errbot"] * 9, browsers=[])
        self.assertEqual(result["kind"], "bot")
        self.assertIn("не бот", result["error"])
        self.assertIn("участников сети", result["error"],
                      "альтернатива: mesh существует")
        self.assertIn("откройте страницу на площадке", result["error"])
        self.assertLessEqual(len(captured), 4)

    def test_attempt_limit_respects_setting(self):
        result, captured, _ = self.run_plan(
            ["err403"] * 9, settings={"bypass_max_attempts": 2},
            browsers=["firefox"])
        self.assertEqual(len(captured), 2,
                         "ровно две дороги по настройке, дефолт не шире")
        self.assertEqual(result["tally"]["of"], 2)
        self.assertEqual(result["kind"], "blocked")

    def test_bypass_disabled_single_plain_request(self):
        result, captured, _ = self.run_plan(["err403", "ok"],
                                            settings={"bypass_enabled": False})
        self.assertEqual(len(captured), 1, "выключено - только обычный запрос")
        self.assertIsNotNone(result["error"])
        self.assertIn("цепочка дорог выключена", result["error"],
                      "подсказка: где включить")

    def test_journal_is_clean_and_shows_every_road(self):
        messages: list = []

        def boom(opts, url):
            raise DownloadError(
                "\x1b[0;31munable to open for writing: "
                "[Errno 22] Invalid argument\x1b[0m")

        ctx = bypass.make_context({"bypass_max_attempts": 2}, {}, "https://x",
                                  platform="youtube", browsers=[])
        outcome = bypass.run_chain(boom, ctx,
                                   classify=lambda t: "blocked", log=messages.append)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["kind"], "blocked")
        self.assertTrue(any("дорога [1/2]" in m for m in messages), messages)
        self.assertTrue(any("дорога [2/2]" in m for m in messages), messages)
        self.assertTrue(any("дороги выкачки" in m for m in messages),
                        "сводка прогона в журнале")
        for m in messages:
            self.assertNotIn("\x1b", m, "ANSI в журнал не попадает")
        for row in outcome["routes"]:
            self.assertNotIn("\x1b", row["detail"])
        self.assertEqual(outcome["tally"], {"ok": 0, "fail": 2, "of": 2})

    def test_summary_counts_only_failures(self):
        messages: list = []
        calls = {"n": 0}

        def attempt(opts, url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DownloadError(BLOCK_TEXT)
            return {"id": "v1"}

        ctx = bypass.make_context({}, {}, "https://x", platform="youtube",
                                  browsers=["firefox"])
        outcome = bypass.run_chain(attempt, ctx,
                                   classify=lambda t: "blocked",
                                   log=messages.append)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["tally"], {"ok": 1, "fail": 1, "of": 2},
                         "вышедшая дорога не считается «не вышло»")

    def test_mirror_road_swaps_platform_domain(self):
        urls: list = []

        def attempt(opts, url):
            urls.append(url)
            if len(urls) == 1:
                raise DownloadError(BLOCK_TEXT)
            return {"id": "v1"}

        ctx = bypass.make_context({}, {}, "https://www.youtube.com/watch?v=x",
                                  platform="youtube", purpose="meta",
                                  browsers=[])
        outcome = bypass.run_chain(attempt, ctx, classify=lambda t: "blocked")
        self.assertTrue(outcome["ok"])
        self.assertIn("youtu.be", urls[1], "дорога «зеркало» - домен реестра")
        self.assertEqual([r["route"] for r in outcome["routes"]],
                         ["plain", "mirror"])

    def test_foreign_url_not_rewritten(self):
        urls: list = []

        def attempt(opts, url):
            urls.append(url)
            raise DownloadError(BLOCK_TEXT)

        ctx = bypass.make_context({"bypass_max_attempts": 12}, {},
                                  "https://example.com/v1", platform="youtube",
                                  purpose="meta", browsers=[])
        outcome = bypass.run_chain(attempt, ctx, classify=lambda t: "blocked")
        self.assertTrue(all(u == "https://example.com/v1" for u in urls),
                        "чужую ссылку переписать под другой сайт нельзя")

    def test_not_a_download_error_is_not_retried(self):
        calls = {"n": 0}

        def attempt(opts, url):
            calls["n"] += 1
            raise ValueError("это не отказ площадки")

        ctx = bypass.make_context({}, {}, "https://x", platform="youtube")
        with self.assertRaises(ValueError):
            bypass.run_chain(attempt, ctx, classify=lambda t: "network",
                             retryable=lambda exc: isinstance(exc, DownloadError))
        self.assertEqual(calls["n"], 1, "прочий исключение - без дорог")

    def test_stop_between_roads_is_cancel(self):
        stop = threading.Event()

        def attempt(opts, url):
            raise DownloadError(BLOCK_TEXT)

        ctx = bypass.make_context({}, {}, "https://x", platform="youtube")
        stop.set()                       # «Стоп» до первой дороги
        outcome = bypass.run_chain(attempt, ctx, classify=lambda t: "blocked",
                                   stop=stop)
        self.assertTrue(outcome["cancelled"])
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["routes"], [], "остановлено - дорог не прогонено")

    def test_cancel_inside_road_propagates(self):
        def attempt(opts, url):
            raise DownloadCancelled("остановлено человеком")

        ctx = bypass.make_context({}, {}, "https://x", platform="youtube")
        with self.assertRaises(DownloadCancelled):
            bypass.run_chain(attempt, ctx, classify=lambda t: "network",
                             retryable=lambda exc: isinstance(exc, DownloadError))


class TestRouteOrderAndCache(ChainCase):
    def make_ctx(self, settings=None, url="https://x", browsers=[], **kw):
        return bypass.make_context(settings or {}, {}, url,
                                   platform="youtube", purpose="file",
                                   browsers=browsers, target_of=lambda s: None,
                                   height_limit=1080, ladder=[720, 480], **kw)

    def test_route_order_is_deterministic(self):
        ids = [r["id"] for r in bypass.ROUTES]
        self.assertEqual(ids[:4], ["plain", "cookies_browser", "cookies_file",
                                   "impersonate"])
        self.assertEqual(ids[-3:], ["names_ascii", "format_hls", "quality_down"])
        self.assertEqual(len(ids), len(set(ids)), "дороги не повторяются")

    def test_winning_cache_moves_ahead(self):
        calls: list = []

        def attempt(opts, url):
            calls.append(dict(opts))
            if len(calls) == 1:
                raise DownloadError("requested format is not available")
            return {"id": "v1"}

        bypass.remember_winning("youtube", "format_hls")
        outcome = bypass.run_chain(attempt, self.make_ctx(),
                                   classify=lambda t: "format")
        self.assertTrue(outcome["ok"])
        self.assertEqual([r["route"] for r in outcome["routes"]],
                         ["plain", "format_hls"],
                         "выигравшая ранее дорога пробывается раньше прочих")
        self.assertEqual(bypass.read_winning("youtube"), "format_hls")

    def test_attempt_chain_forgets_cache(self):
        """«Повторить упавшие» с другой дорогой: кэш не поднимает прежнюю."""
        def attempt(o, u):
            if o.get("cookiesfrombrowser"):
                return {"id": "v1"}
            raise DownloadError(BLOCK_TEXT)

        bypass.remember_winning("youtube", "mirror")
        outcome = bypass.run_chain(
            attempt, self.make_ctx(url="https://www.youtube.com/watch?v=x",
                                   browsers=["firefox"], attempt_chain=True),
            classify=lambda t: "blocked")
        self.assertTrue(outcome["ok"])
        self.assertEqual([r["route"] for r in outcome["routes"]],
                         ["plain", "cookies_browser"],
                         "кэш забыт - зеркало не пробуется вне очереди")
        self.assertEqual(bypass.read_winning("youtube"), "cookies_browser",
                         "победитель записан заново")

    def test_winning_cache_moves_mirror_ahead(self):
        def attempt(o, u):
            if o.get("cookiesfrombrowser"):
                return {"id": "v1"}
            raise DownloadError(BLOCK_TEXT)

        bypass.remember_winning("youtube", "mirror")
        outcome = bypass.run_chain(
            attempt, self.make_ctx(url="https://www.youtube.com/watch?v=x",
                                   browsers=["firefox"]),
            classify=lambda t: "blocked")
        self.assertTrue(outcome["ok"])
        self.assertEqual([r["route"] for r in outcome["routes"]],
                         ["plain", "mirror", "cookies_browser"],
                         "выигравшая дорога идёт сразу после обычного запроса")

    def test_cache_expires(self):
        bypass.remember_winning("youtube", "mirror")
        with mock.patch.object(bypass, "CACHE_TTL", 0):
            self.assertIsNone(bypass.read_winning("youtube"),
                              "анти-бот меняется: кэш не вечный")

    def test_plain_not_cached(self):
        calls = {"n": 0}

        def attempt(opts, url):
            calls["n"] += 1
            return {"id": "v1"}

        bypass.run_chain(attempt, self.make_ctx(), classify=lambda t: "network")
        self.assertIsNone(bypass.read_winning("youtube"),
                          "обычный запрос запоминать нечего")


class TestSettings(ChainCase):
    def test_defaults(self):
        d = defaults()
        self.assertIs(d["bypass_enabled"], True, "цепочка включена по умолчанию")
        self.assertEqual(d["bypass_max_attempts"], 4)
        self.assertIs(d["bot_token_enabled"], True)
        self.assertEqual(d["bot_token"], "")

    def test_labels_are_neutral(self):
        """Подписки новых настроек - без имени взрослой площадки и без DPI."""
        for key in ("bypass_enabled", "bypass_max_attempts",
                    "bot_token_enabled", "bot_token"):
            f = field(key)
            text = f"{f['label']} {f['hint']}".lower()
            self.assertNotIn("pornhub", text)
            self.assertNotIn("zapret", text)
            self.assertNotIn("winws", text)
            self.assertEqual(f["section"], "Загрузка")

    def test_attempts_limit_rules(self):
        self.assertEqual(bypass.attempts_limit({}), 4)
        self.assertEqual(bypass.attempts_limit({"bypass_enabled": False}), 1)
        self.assertEqual(bypass.attempts_limit({"bypass_enabled": "нет"}), 1,
                         "строка «нет» не включает цепочку")
        self.assertEqual(bypass.attempts_limit({"bypass_max_attempts": 99}), 12,
                         "значение вне диапазона схемы - верх схемы")
        self.assertEqual(bypass.attempts_limit({"bypass_max_attempts": "кривое"}), 4,
                         "битое значение -> дефолт, не падение")


class TestQueueBotHint(GuiCase):
    """Очередь гасит подсказку по kind=bot (pattern из test_add_errors.py)."""

    def setUp(self):
        super().setUp()
        self.dest = self.dir / "downloads"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.api = self.make_api()
        storage = self.add_storage(self.api, self.dest)
        self.api.save_setting({"key": "default_storage_id",
                               "value": storage["id"]})
        self.messages: list = []
        self.api._logs = self.messages

    def _row(self):
        from app import repo
        vid, _ = repo.upsert_video(self.api.db.conn, {
            "platform": "youtube", "remote_id": "vidbot0001",
            "key": "youtube:vidbot0001", "title": "Бот-стена",
            "webpage_url": "https://youtu.be/vidbot0001",
            "raw_json": "{}", "origin": "yt-dlp"})
        repo.enqueue(self.api.db.conn, [vid])
        return repo.next_queued(self.api.db.conn), vid

    def test_kind_bot_sets_hint_and_keeps_reason(self):
        from app.queue import DownloadWorker
        row, vid = self._row()
        worker = DownloadWorker(self.api.db, self.api._current_settings,
                                log=self.messages.append)
        with mock.patch.object(downloader, "download",
                               return_value={"cancelled": False, "files": [],
                                             "info": {}, "hash": None,
                                             "kind": "bot",
                                             "error": "площадка требует "
                                                      "подтверждение, что вы не бот"}):
            worker._download_one(self.api.db.conn, row,
                                 self.api._current_settings())
        self.assertTrue(worker.state["bot_hint"],
                        "kind=bot - подсказка в состоянии очереди")
        self.assertEqual(worker.state["failed"], 1)
        snap = self.api.poll(0)
        self.assertEqual(snap["queue"][0]["last_error"],
                         "площадка требует подтверждение, что вы не бот")

    def test_retry_with_attempt_chain_flag(self):
        """«Повторить упавшие» с другой дорогой: флаг пробходит через Api и
        живёт один прогон (сеть здесь не трогаем - повтор без качалки)."""
        from app import repo
        vid, _ = repo.upsert_video(self.api.db.conn, {
            "platform": "youtube", "remote_id": "vidretry0001",
            "key": "youtube:vidretry0001", "title": "Упавшее",
            "raw_json": "{}", "origin": "yt-dlp"})
        repo.set_status(self.api.db.conn, [vid], "failed")

        # Очередь: флаг доходит и лится в журнале («другые дороги выкачки»).
        self.assertTrue(self.api.dl.retry_failed(attempt_chain=True)
                        ["attempt_chain"])
        self.assertTrue(any("другие дороги выкачки" in m for m in self.messages),
                        self.messages)
        self.assertFalse(self.api.dl.retry_failed()["attempt_chain"],
                         "обычный повтор - флаг сброшен после прогона")

        # Api-вход M13 (аддитивный queue_retry): флаг пробходит в очередь.
        fake = {"ok": True, "retried": 1, "attempt_chain": False}
        with mock.patch.object(self.api.dl, "retry_failed",
                               side_effect=lambda attempt_chain=False:
                               dict(fake, attempt_chain=attempt_chain)), \
            mock.patch.object(downloader, "download",
                              return_value={"cancelled": False, "files": [],
                                            "info": {}, "hash": None}):
            result = self.api.queue_retry({"attempt_chain": True})
        self.assertTrue(result.get("attempt_chain"))


if __name__ == "__main__":
    unittest.main()
