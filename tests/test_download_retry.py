"""Фоллбэки выкачки по фактическим симптомам живой пары (3).

1) имя файла не прошло Windows (query-мусор title/id, длина) ->
   повтор с ascii-именами (restrictfilenames);
2) площадка ответила 410/403, а браузерные куки выключены ->
   повтор с куками рабочего браузера (firefox; ABE-битые мимо).
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import downloader
from app import google_auth
from yt_dlp.utils import DownloadError

VIDEO = {"webpage_url": "https://example.com/v1", "id": "v1"}


class FakeYDL:
    """YoutubeDL-заглушка: очередь действий -> успех с фейковым info."""

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
        action = self._plan.pop(0) if self._plan else "ok"
        if action == "errname":
            raise DownloadError(
                "unable to open for writing: [Errno 22] Invalid argument: "
                "'x?srcIp=1&sig=abc'")
        if action == "err410":
            raise DownloadError(
                "ERROR: [PornHub] Unable to download webpage: "
                "HTTP Error 410: Gone (caused by HTTPError)")
        return {"id": "v1", "title": "Тест", "ext": "mp4",
                "requested_downloads": []}


class RetryCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name)

    def run_plan(self, plan, settings=None):
        captured: list = []
        shared_plan = list(plan)

        def factory(opts):
            captured.append(dict(opts))
            return FakeYDL(captured, shared_plan)

        settings = {"retries": 1, "dest_dir": str(self.dest),
                    **(settings or {})}
        with mock.patch.object(downloader.yt_dlp, "YoutubeDL",
                               side_effect=factory):
            result = downloader._download(
                dict(VIDEO), settings, stop=threading.Event())
        return result, captured


class TestNameFallback(RetryCase):
    def test_invalid_name_retries_with_restrict(self):
        result, captured = self.run_plan(["errname", "ok"])
        self.assertIsNone(result["error"], result)
        self.assertEqual(len(captured), 2, "ровно один повтор")
        self.assertNotIn("restrictfilenames", captured[0])
        self.assertTrue(captured[1].get("restrictfilenames"),
                        "повтор с ascii-именами")

    def test_no_infinite_retry_on_name_error(self):
        result, captured = self.run_plan(["errname", "errname"])
        self.assertIsNotNone(result["error"])
        self.assertEqual(len(captured), 2, "повтор ровно один раз")


class TestCookieFallback(RetryCase):
    def test_410_retries_with_browser_cookies(self):
        result, captured = self.run_plan(["err410", "ok"])
        self.assertIsNone(result["error"], result)
        self.assertEqual(len(captured), 2)
        self.assertNotIn("cookiesfrombrowser", captured[0])
        self.assertEqual(captured[1].get("cookiesfrombrowser"),
                         ("firefox",), "первый рабочий браузер")

    def test_410_with_chosen_browser_is_honest_error(self):
        # Куки выбранного браузера уже подключены с самого начала - повтор
        # с ними же бессмыслен, ошибка возвращается сразу.
        result, captured = self.run_plan(
            ["err410"], settings={"browser_cookies": "firefox"})
        self.assertIsNotNone(result["error"])
        self.assertEqual(len(captured), 1, "бесмысленных повторов нет")

    def test_410_without_browsers_is_honest_error(self):
        with mock.patch.object(google_auth, "BROWSER_CHOICES", ()):
            result, captured = self.run_plan(["err410"])
        self.assertIsNotNone(result["error"])
        self.assertEqual(len(captured), 1, "нечем пробовать - без повторов")
        self.assertIn("410", result["error"],
                      "подсказка по факту ошибки сохраняется")

    def test_unrelated_error_not_retried(self):
        def factory(opts):
            raise downloader.yt_dlp.utils.DownloadError("ERROR: не то")
        with mock.patch.object(downloader.yt_dlp, "YoutubeDL",
                               side_effect=factory):
            result = downloader._download(
                dict(VIDEO),
                {"retries": 1, "dest_dir": str(self.dest)},
                stop=threading.Event())
        self.assertIsNotNone(result["error"])


class TestCookieBrowsers(unittest.TestCase):
    def test_default_order_and_broken_filtered(self):
        browsers = downloader._cookie_browsers({})
        self.assertIn("firefox", browsers)
        for broken in google_auth.BROKEN_BROWSERS:
            if broken != "firefox":
                self.assertNotIn(broken, browsers)

    def test_chosen_browser_first(self):
        browsers = downloader._cookie_browsers(
            {"browser_cookies": "firefox"})
        self.assertEqual(browsers, ["firefox"])

    def test_none_setting_gives_probe_order(self):
        browsers = downloader._cookie_browsers({"browser_cookies": "none"})
        self.assertIsInstance(browsers, list)


if __name__ == "__main__":
    unittest.main()
