"""Надёжность входа: чистка призраков, ручная поимка, куки браузера.

Браузерный режим - только метаданные настроек: копий не создаётся,
yt-dlp читает куки напрямую. Chrome/Edge v127+ шифруют куки (ABE) -
предупреждение проверяется отдельно.
"""

import http.cookiejar
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app import downloader
from app import google_auth
from app import repo
from app import sources
from tests.test_google_account import (FakeLoginWindow, ProfileCase,
                                       make_cookie, session_cookies)
from tests.test_gui import GuiCase


class TestRegistryPrune(ProfileCase):
    def test_ghost_without_cookies_is_dropped(self):
        account = google_auth.create_account(session_cookies(), label="живой")
        # Призрак: запись есть, копии нет (кто-то удалил файл руками).
        google_auth._save_registry(
            google_auth.load_registry() +
            [{"id": "призрак01", "label": "призрак", "since": "2026-01-01"}])

        pruned = google_auth.prune_registry()

        self.assertEqual(pruned["removed"], ["призрак01"])
        ids = [item["id"] for item in google_auth.load_registry()]
        self.assertEqual(ids, [account["id"]])

    def test_clean_registry_untouched(self):
        account = google_auth.create_account(session_cookies())
        self.assertEqual(google_auth.prune_registry(), {"removed": []})
        self.assertEqual(google_auth.load_registry()[0]["id"], account["id"])


class TestBrowserCookiesOption(unittest.TestCase):
    def test_option_mapping(self):
        self.assertIsNone(google_auth.browser_cookie_option({}))
        self.assertIsNone(google_auth.browser_cookie_option(
            {"browser_cookies": "none"}))
        self.assertEqual(
            google_auth.browser_cookie_option({"browser_cookies": "firefox"}),
            ("firefox",))

    def test_abe_browsers_warn(self):
        self.assertIn("App-Bound", google_auth.browser_warning(
            {"browser_cookies": "chrome"}))
        self.assertIn("App-Bound", google_auth.browser_warning(
            {"browser_cookies": "edge"}))
        self.assertEqual(
            google_auth.browser_warning({"browser_cookies": "firefox"}), "")


class TestBrowserOptionInTransports(ProfileCase):
    """Куки браузера попадают в опции качалки и синка (без аккаунта)."""

    def test_downloader_sets_cookiesfrombrowser(self):
        created = {}

        class FakeYDL:
            def __init__(self, opts):
                created["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                return {"ext": "mp4", "filepath": "/tmp/x.mp4"}

        dest = tempfile.mkdtemp(prefix="dl_")
        with mock.patch.object(downloader.yt_dlp, "YoutubeDL", FakeYDL):
            downloader.download(
                {"remote_id": "abcdefghijk",
                 "webpage_url": "https://example.invalid/v"},
                {"dest_dir": dest, "browser_cookies": "firefox"},
                stop=threading.Event(), dest=dest, account_id=None)
        self.assertEqual(created["opts"].get("cookiesfrombrowser"),
                         ("firefox",))

    def test_account_copy_beats_browser_mode(self):
        created = {}

        class FakeYDL:
            def __init__(self, opts):
                created["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                return {"ext": "mp4", "filepath": "/tmp/x.mp4"}

        account = google_auth.create_account(session_cookies())
        dest = tempfile.mkdtemp(prefix="dl_")
        with mock.patch.object(downloader.yt_dlp, "YoutubeDL", FakeYDL):
            downloader.download(
                {"remote_id": "abcdefghijk",
                 "webpage_url": "https://example.invalid/v"},
                {"dest_dir": dest, "browser_cookies": "firefox"},
                stop=threading.Event(), dest=dest, account_id=account["id"])
        self.assertIn("cookiefile", created["opts"],
                      "копия привязанного аккаунта важнее")
        self.assertNotIn("cookiesfrombrowser", created["opts"])

    def test_extract_uses_browser_mode(self):
        captured = {}

        class FakeYDL:
            def __init__(self, opts):
                captured["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                return {"_type": "playlist", "id": "PLx", "entries": []}

        with mock.patch.object(sources.yt_dlp, "YoutubeDL", FakeYDL):
            sources._extract("https://youtube.com/playlist?list=PLx", "1-10",
                             {"browser_cookies": "firefox"})
        self.assertEqual(captured["opts"].get("cookiesfrombrowser"),
                         ("firefox",))


class TestSimpleCookieFormat(ProfileCase):
    """pywebview отдаёт и Cookie, и list[SimpleCookie] - принимаем оба."""

    @staticmethod
    def _simple_cookie(*names):
        from http.cookies import SimpleCookie
        sample = SimpleCookie()
        for name in names:
            sample[name] = f"value-{name}"
            sample[name]["path"] = "/"
        return sample

    def test_morsel_becomes_cookie_with_domain(self):
        cookies = google_auth.as_cookie_list(
            self._simple_cookie("SID", "HSID"), default_domain="youtube.com")
        self.assertEqual(len(cookies), 2)
        for cookie in cookies:
            self.assertIsInstance(cookie, http.cookiejar.Cookie)
            self.assertEqual(cookie.domain, "youtube.com")
            self.assertTrue(google_auth.is_google_cookie(cookie))
        # Маркеры сессии опознаются - воркер поимки не упадёт.
        self.assertTrue(any(c.name == "SID" for c in cookies))
        facts = google_auth.cookies_facts(cookies)
        self.assertEqual(facts["google"], 2)

    def test_explicit_domain_in_morsel_wins(self):
        sample = self._simple_cookie("SID")
        sample["SID"]["domain"] = ".google.com"
        cookies = google_auth.as_cookie_list(sample,
                                             default_domain="youtube.com")
        self.assertEqual(cookies[0].domain, ".google.com")

    def test_garbage_and_passthrough(self):
        self.assertEqual(google_auth.as_cookie_list(None), [])
        self.assertEqual(google_auth.as_cookie_list("строка"), [])

        cookie = http.cookiejar.Cookie(
            0, "SID", "v", None, False, ".google.com", True, True, "/",
            True, False, int(time.time()) + 86400, False, None, None, {})
        self.assertEqual(google_auth.as_cookie_list([cookie]), [cookie],
                         "готовые Cookie проходят без изменений")


class TestManualCapture(GuiCase):
    """«Я вошёл - забрать куки»: разовая поимка по кнопке."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA":
                                               tempfile.mkdtemp(prefix="ga_")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = self.make_api()

    def test_capture_without_window_refused(self):
        result = self.api.account_capture_now()
        self.assertIn("error", result)
        self.assertIn("не открыто", result["error"])

    def test_capture_with_session_creates_account(self):
        window = FakeLoginWindow(session_cookies(), warmup=10)
        with mock.patch("webview.create_window", return_value=window):
            self.assertTrue(self.api.account_login_start()["ok"])
        # «Пользователь вошёл»: прогрев заканчивается, куки сессии появились.
        window._warmup = 0
        result = self.api.account_capture_now()
        self.assertTrue(result.get("ok"), result)
        registry = google_auth.load_registry()
        self.assertEqual(len(registry), 1)
        # Окно остаётся открытым (пользователь ещё может доработать).
        self.assertFalse(window.destroyed)
        self.api.account_login_stop()
        self.api._login_thread.join(10)

    def test_capture_without_session_explains(self):
        window = FakeLoginWindow([make_cookie("NID", ".google.com")],
                                 warmup=10)
        with mock.patch("webview.create_window", return_value=window):
            self.assertTrue(self.api.account_login_start()["ok"])
        result = self.api.account_capture_now()
        self.assertIn("error", result)
        self.assertIn("аккаунтских 0", result["error"],
                      "ошибка должна объяснить, что видела поимка")
        self.assertEqual(google_auth.load_registry(), [])
        self.api.account_login_stop()
        self.api._login_thread.join(10)

    def test_poll_carries_browser_warning(self):
        self.api.save_setting({"key": "browser_cookies", "value": "edge"})
        account = self.api.poll(0)["account"]
        self.assertIn("App-Bound", account["browser_warning"])


class TestSchema(unittest.TestCase):
    def test_browser_choice_present(self):
        from app import settings_schema as schema
        spec = schema.field("browser_cookies")
        self.assertEqual(spec["type"], "choice")
        values = [value for value, _label in spec["choices"]]
        self.assertEqual(values, ["none", "firefox", "edge", "chrome",
                                  "brave"])
        self.assertIn("browser_cookies", schema.defaults())


if __name__ == "__main__":
    unittest.main()
