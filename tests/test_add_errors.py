"""Причины отказов площадки при добавлении (F1/F2/F3).

kind - единственная правда об отказе: «видео недоступно на площадке» и
«площадка недоступна из нашей сети» - разные случаи с разными подсказками,
и путать их нельзя (повтор с VPN против удалённого видео ничего не даст).
"""

import threading
import unittest
from unittest import mock

from app import google_auth
from app import sources
from app.sources import Aborted, FetchError, classify_failure, failure_message
from app.sources import stop_requested
from tests.test_gui import GuiCase, wait_until


class FakeYDL:
    """Подмена YoutubeDL: бросает переданный отказ, запоминает opts.

    on_attempt вызывается перед отказом - этим подменяется отмена человеком
    в середине прогона дорог (см. TestHonestAbort).
    """

    created = []
    raises = None
    on_attempt = None

    def __init__(self, opts, **kwargs):
        FakeYDL.created.append(dict(opts))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if FakeYDL.on_attempt:
            FakeYDL.on_attempt()
        if FakeYDL.raises:
            raise FakeYDL.raises
        return {"id": "vid00000000", "title": "Клип", "duration": 100}


class TestClassify(unittest.TestCase):
    def test_video_unavailable_wins_over_network(self):
        """Площадка сама сказала «недоступно» - это не наша блокировка."""
        self.assertEqual(
            classify_failure("ERROR: [youtube] M4IalLlIbNE: Offline."),
            "unavailable")
        self.assertEqual(classify_failure("ERROR: [vk] This video has been "
                                          "deleted and is no longer available"),
                         "unavailable")
        self.assertEqual(classify_failure("ERROR: [youtube] x: Video unavailable"),
                         "unavailable")
        # текст с ANSI-кодами разборный (консольный шум не мешает)
        self.assertEqual(
            classify_failure("\x1b[0;31mERROR:\x1b[0m [PornHub] k: Offline."),
            "unavailable")

    def test_network_block(self):
        self.assertEqual(classify_failure("ERROR: Connection aborted."), "blocked")
        self.assertEqual(classify_failure("Unable to download webpage: "
                                          "HTTP Error 403: Forbidden"), "blocked")
        self.assertEqual(classify_failure("Unable to download webpage: "
                                          "HTTP Error 410: Gone"), "blocked")
        self.assertEqual(classify_failure("timed out"), "blocked")

    def test_bot_check(self):
        self.assertEqual(classify_failure("Please sign in to confirm you're "
                                          "not a bot"), "bot")

    def test_other_is_network(self):
        self.assertEqual(classify_failure("something odd"), "network")

    def test_messages_are_explainable(self):
        unavailable = failure_message("unavailable", "Offline")
        self.assertIn("недоступно на площадке", unavailable)
        self.assertIn("не поможет", unavailable)
        self.assertIn("участников", unavailable)     # подсказка про mesh
        blocked = failure_message("blocked", "403")
        self.assertIn("недоступна из вашей сети", blocked)
        self.assertIn("обход", blocked)
        self.assertIn("mesh", blocked)
        bot = failure_message("bot", "bot")
        self.assertIn("аккаунт", bot)


class TestFetchVideoErrors(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(sources.yt_dlp, "YoutubeDL", FakeYDL)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        FakeYDL.created = []
        FakeYDL.raises = None
        FakeYDL.on_attempt = None

    def test_unavailable_video_reports_unavailable_kind(self):
        FakeYDL.raises = sources.yt_dlp.utils.DownloadError(
            "ERROR: [youtube] M4IalLlIbNE: Offline.")
        with self.assertRaises(FetchError) as ctx:
            sources.fetch_video("https://www.youtube.com/watch?v=M4IalLlIbNE")
        self.assertEqual(ctx.exception.kind, "unavailable")
        self.assertIn("недоступно на площадке", str(ctx.exception))

    def test_blocked_network_reports_blocked_kind(self):
        FakeYDL.raises = sources.yt_dlp.utils.DownloadError(
            "ERROR: [adult site] k: Unable to download webpage: HTTP Error 410: Gone")
        with self.assertRaises(FetchError) as ctx:
            sources.fetch_video("https://example.invalid/view_video.php?viewkey=k")
        self.assertEqual(ctx.exception.kind, "blocked")
        self.assertIn("недоступна из вашей сети", str(ctx.exception))

    def test_account_cookies_reach_single_video_request(self):
        """F2: куки привязанного аккаунта применяются к одиночному видео."""
        cookie = mock.patch.object(google_auth, "temporary_cookiefile",
                                   return_value="cookies.tmp")
        release = mock.patch.object(google_auth, "release_temp")
        with cookie, release:
            snapshot = sources.fetch_snapshot(
                "https://www.youtube.com/watch?v=vid00000000",
                settings={}, account_id="acc-1")
        self.assertEqual(snapshot["total"], 1)
        self.assertEqual(FakeYDL.created[0]["cookiefile"], "cookies.tmp")
        self.assertTrue(FakeYDL.created[0]["skip_download"],
                        "каталог и индексация не качают файлы")

    def test_browser_cookies_when_no_account(self):
        browser = mock.patch.object(google_auth, "browser_cookie_option",
                                    return_value=("chrome", None))
        with browser:
            sources.fetch_video("https://www.youtube.com/watch?v=vid00000000")
        self.assertEqual(FakeYDL.created[0]["cookiesfrombrowser"], ("chrome", None))


class TestHonestAbort(unittest.TestCase):
    """Отмена человеком не должна выглядеть как отказ площадки.

    Дорога могла успеть упасть на 403/«не бот» ровно в момент нажатия
    «Отмена». Если это ветвить по тексту отказа, окно показало бы
    «подтвердите, что вы не бот» + подсказку про обход вместо честной
    отмены - и человек стал бы искать причину, которой нет.
    """

    def setUp(self):
        self._patch = mock.patch.object(sources.yt_dlp, "YoutubeDL", FakeYDL)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        FakeYDL.created = []
        FakeYDL.raises = None
        FakeYDL.on_attempt = None

    def test_stop_during_chain_aborts_single_video(self):
        stop = threading.Event()
        FakeYDL.raises = sources.yt_dlp.utils.DownloadError(
            "ERROR: Please sign in to confirm you're not a bot")
        FakeYDL.on_attempt = stop.set
        with self.assertRaises(Aborted):
            sources.fetch_video("https://www.youtube.com/watch?v=vid00000000",
                                settings={}, stop=stop)

    def test_stop_during_chain_aborts_snapshot(self):
        stop = threading.Event()
        FakeYDL.raises = sources.yt_dlp.utils.DownloadError(
            "Unable to download webpage: HTTP Error 410: Gone")
        FakeYDL.on_attempt = stop.set
        with self.assertRaises(Aborted):
            sources.fetch_snapshot("https://www.youtube.com/watch?v=vid00000000",
                                   settings={}, stop=stop)

    def test_stop_before_request_aborts(self):
        """Отмена до запроса: пустой прогон, но тоже отмена, не отказ площадки."""
        stop = threading.Event()
        stop.set()
        FakeYDL.raises = sources.yt_dlp.utils.DownloadError(
            "ERROR: [youtube] k: Offline.")
        with self.assertRaises(Aborted):
            sources.fetch_video("https://www.youtube.com/watch?v=vid00000000",
                                settings={}, stop=stop)

    def test_non_platform_exception_with_stop_aborts(self):
        """Не-сетевой исключение в середине прогона - тоже честная отмена."""
        stop = threading.Event()
        FakeYDL.raises = RuntimeError("площадка вернула мусор")
        FakeYDL.on_attempt = stop.set
        with self.assertRaises(Aborted):
            sources.fetch_video("https://www.youtube.com/watch?v=vid00000000",
                                settings={}, stop=stop)

    def test_without_stop_keeps_the_honest_failure(self):
        """Без отмены причина остаётся: подсказки про «не бот» нужны человеку."""
        FakeYDL.raises = sources.yt_dlp.utils.DownloadError(
            "ERROR: Please sign in to confirm you're not a bot")
        with self.assertRaises(FetchError) as ctx:
            sources.fetch_video("https://www.youtube.com/watch?v=vid00000000")
        self.assertEqual(ctx.exception.kind, "bot")
        self.assertIn("аккаунт", str(ctx.exception))

    def test_stop_flag_checked_without_breaking_caller(self):
        """stop мог быть None или объектом без is_set - проверка не валит воркер."""
        self.assertFalse(stop_requested(None))
        self.assertFalse(stop_requested(object()))
        self.assertFalse(stop_requested(threading.Event()))
        gone = threading.Event()
        gone.set()
        self.assertTrue(stop_requested(gone))


class TestUnsupportedLink(unittest.TestCase):
    def test_bad_link_kind(self):
        with self.assertRaises(FetchError) as ctx:
            sources.fetch_snapshot("https://example.org/page")
        self.assertEqual(ctx.exception.kind, "unsupported")


class ApiFailureCase(GuiCase):
    """Api: причина отказа доходит до интерфейса (add_flow.error_kind)."""

    PLAYLIST_URL = ("https://youtube.com/playlist"
                    "?list=PLfixtur0000000000000000000001")

    def wait_error(self, api, timeout=12.0):
        wait_until(lambda: api._add.get("phase") == "error", timeout,
                   "фаза error не наступила")
        return dict(api._add)

    def test_blocked_error_reaches_ui_with_kind(self):
        api = self.make_api()
        boom = FetchError(failure_message("blocked", "403"), kind="blocked")
        with mock.patch.object(sources, "fetch_snapshot", side_effect=boom):
            api.add_start({"url": self.PLAYLIST_URL, "mode": "partial"})
            flow = self.wait_error(api)
        self.assertEqual(flow["error_kind"], "blocked")
        self.assertIn("недоступна из вашей сети", flow["error"])
        api.close()

    def test_unavailable_error_reaches_ui_with_kind(self):
        api = self.make_api()
        boom = FetchError(failure_message("unavailable", "Offline"),
                          kind="unavailable")
        with mock.patch.object(sources, "fetch_snapshot", side_effect=boom):
            api.add_start({"url": "https://www.youtube.com/watch?v=vid00000000",
                           "mode": "partial"})
            flow = self.wait_error(api)
        self.assertEqual(flow["error_kind"], "unavailable")
        self.assertIn("недоступно на площадке", flow["error"])
        api.close()


if __name__ == "__main__":
    unittest.main()
