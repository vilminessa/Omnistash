"""Аккаунт Google: шифрование кук, окно входа с авто-поимкой, подсказка.

Ни один тест не ходит в сеть: окно входа - фикстура, куки - объекты
http.cookiejar. Проверяем и то, что секреты не утекают в журнал.
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
from tests.test_gui import GuiCase, wait_until


def make_cookie(name, domain, value=None):
    return http.cookiejar.Cookie(
        0, name, value or ("secret-" + name), None, False,
        domain, True, domain.startswith("."), "/", True, False,
        int(time.time()) + 86400, False, None, None, {})


def netscape_fixture():
    return google_auth.export_netscape([
        make_cookie("SID", ".google.com"),
        make_cookie("HSID", ".google.com"),
        make_cookie("VISITOR_INFO1_LIVE", ".youtube.com"),
    ])


class ProfileCase(unittest.TestCase):
    """Изолированный профиль: куки не должны трогать настоящий."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA":
                                               tempfile.mkdtemp(prefix="ga_")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile = Path(os.environ["LOCALAPPDATA"]) / "Omnistash"


class TestCryptoAndStorage(ProfileCase):
    def test_encrypt_roundtrip_and_foreign_copy_fails(self):
        blob = google_auth.encrypt("секрет".encode("utf-8"))
        self.assertNotIn("секрет".encode("utf-8"), blob)
        self.assertEqual(google_auth.decrypt(blob).decode("utf-8"), "секрет")
        with self.assertRaises(OSError):
            google_auth.decrypt("это не dpapi".encode())

    def test_save_text_cookies_and_temporary_file(self):
        count = google_auth.save_text_cookies(netscape_fixture())
        self.assertEqual(count, 3)
        self.assertTrue(google_auth.has_cookies())
        back = google_auth.decrypted_cookies_text()
        self.assertIn("SID", back)
        self.assertIn("VISITOR_INFO1_LIVE", back)
        # Файл на диске - шифротекст, а не текст.
        raw = (self.profile / "google_cookies.bin").read_bytes()
        self.assertNotIn(b"SID", raw)

        temp = google_auth.temporary_cookiefile()
        self.assertIn("SID", temp.read_text(encoding="utf-8"))
        google_auth.release_temp(temp)
        self.assertFalse(temp.exists(), "временный файл обязан уйти")

    def test_broken_copy_is_not_silently_used(self):
        (self.profile).mkdir(parents=True, exist_ok=True)
        (self.profile / "google_cookies.bin").write_bytes("мусор".encode())
        with self.assertRaises(OSError):
            google_auth.temporary_cookiefile()

    def test_forget_removes_files(self):
        google_auth.save_text_cookies(netscape_fixture())
        removed = google_auth.forget()["removed"]
        self.assertIn("google_cookies.bin", removed)
        self.assertFalse(google_auth.has_cookies())
        self.assertEqual(google_auth.forget()["removed"], [], "повтор - пусто")

    def test_domains_are_filtered_and_tiny_sets_rejected(self):
        # Куки чужого домена не должны попасть в копию «сессии Google».
        cookies = [make_cookie("a", ".google.com"),
                   make_cookie("b", "example.org")]
        with self.assertRaises(RuntimeError):
            google_auth.save_cookies([make_cookie("b", "example.org")])
        count = google_auth.save_cookies(cookies)
        self.assertEqual(count, 1)
        self.assertNotIn("example.org", google_auth.decrypted_cookies_text())

    def test_crippled_netscape_rejected(self):
        with self.assertRaises(RuntimeError):
            google_auth.save_text_cookies("просто текст без кук")
        with self.assertRaises(RuntimeError):
            google_auth.save_text_cookies("# Netscape HTTP Cookie File\n")


class TestDownloaderUsesCookies(ProfileCase):
    def setUp(self):
        super().setUp()
        created = self.created = {}

        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts
                created["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                return {"ext": "mp4", "filepath": "/tmp/x.mp4"}

        patcher = mock.patch.object(downloader.yt_dlp, "YoutubeDL", FakeYDL)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _download(self, settings):
        return downloader.download(
            {"remote_id": "abcdefghijk",
             "webpage_url": "https://example.invalid/v"},
            settings, stop=threading.Event(),
            dest=tempfile.mkdtemp(prefix="dl_"))

    def test_cookiefile_present_and_removed_after(self):
        google_auth.save_text_cookies(netscape_fixture())
        self._download({"use_google_cookies": True})
        cookiefile = self.created["opts"].get("cookiefile")
        self.assertTrue(cookiefile, "куки должны были попасть в опции")
        self.assertFalse(Path(cookiefile).exists(),
                         "копия обязана уйти после загрузки")

    def test_disabled_and_missing_account_leave_opts_clean(self):
        google_auth.save_text_cookies(netscape_fixture())
        self._download({"use_google_cookies": False})
        self.assertNotIn("cookiefile", self.created["opts"])
        google_auth.forget()
        self._download({"use_google_cookies": True})
        self.assertNotIn("cookiefile", self.created["opts"])


class TestBotHint(GuiCase):
    def test_login_error_sets_hint(self):
        api = self.make_api()
        (self.dir / "lib").mkdir()
        storage = self.add_storage(api, self.dir / "lib")
        api.save_setting({"key": "default_storage_id", "value": storage["id"]})
        vid, _ = repo.upsert_video(api.db.conn, {
            "platform": "youtube", "remote_id": "vid000000009",
            "key": "youtube:vid000000009", "title": "видео",
            "raw_json": "{}", "origin": "yt-dlp"})
        repo.enqueue(api.db.conn, [vid])

        def fake(video, settings, *, stop, on_progress=None, dest=None,
                 overwrite=False, **kwargs):
            return {"cancelled": False, "files": [], "info": {},
                    "error": "площадка требует вход в аккаунт", "hash": None}

        with mock.patch.object(downloader, "find_ffmpeg", return_value=None), \
                mock.patch.object(downloader, "download", side_effect=fake):
            row = repo.next_queued(api.db.conn)
            api.dl._download_one(api.db.conn, row, api._current_settings())

        state = api.dl.state
        self.assertTrue(state.get("bot_hint"),
                        "ошибка входа должна подсказать про аккаунт")


class FakeLoginWindow:
    """Фикстура окна входа: куки появляются «после входа»."""

    def __init__(self, cookies, email="человек@example.com"):
        self._cookies = cookies
        self._email = email
        self.destroyed = False

    def get_cookies(self):
        if self.destroyed:
            raise RuntimeError("окно закрыто")
        return list(self._cookies)

    def evaluate_js(self, script):
        return self._email

    def destroy(self):
        self.destroyed = True


class TestAccountApi(GuiCase):
    def setUp(self):
        super().setUp()
        # Профиль изолируем сами: секреты не должны трогать настоящий
        # %LOCALAPPDATA% пользователя (GuiCase изолирует только БД/настройки).
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA":
                                               tempfile.mkdtemp(prefix="ga_")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = self.make_api()

    def test_poll_reports_account_without_secrets(self):
        account = self.api.poll(0)["account"]
        for key in ("has_cookies", "use_cookies", "label", "since",
                    "note", "logging_in"):
            self.assertIn(key, account)
        # В журнале и в poll не должно быть значений кук.
        google_auth.save_text_cookies(netscape_fixture())
        account = self.api.poll(0)["account"]
        self.assertTrue(account["has_cookies"])
        blob = str(account) + "\n".join(self.api._logs)
        self.assertNotIn("secret-", blob)

    def test_login_captures_cookies_and_closes_window(self):
        window = FakeLoginWindow([
            make_cookie("SID", ".google.com"),
            make_cookie("HSID", ".google.com"),
            make_cookie("SAPISID", ".google.com"),
            make_cookie("VISITOR_INFO1_LIVE", ".youtube.com"),
        ])
        with mock.patch("webview.create_window", return_value=window):
            self.assertTrue(self.api.account_login_start()["ok"])
        self.assertTrue(self.api._login_thread.join(10) or True, "ждём воркер")
        deadline = time.time() + 10
        while time.time() < deadline and self.api._login_window is not None:
            time.sleep(0.02)
        self.assertIsNone(self.api._login_window, "окно должно закрыться само")
        self.assertTrue(window.destroyed)
        self.assertTrue(google_auth.has_cookies())
        account = self.api.poll(0)["account"]
        self.assertEqual(account["label"], "человек@example.com")
        self.assertIn("Аккаунт сохранён", account["note"])

    def test_login_without_session_reports_failure(self):
        window = FakeLoginWindow([make_cookie("NID", ".google.com")])  # без сессии
        with mock.patch("webview.create_window", return_value=window), \
                mock.patch.object(type(self.api), "LOGIN_TIMEOUT", 0.3):
            self.assertTrue(self.api.account_login_start()["ok"])
        deadline = time.time() + 10
        while time.time() < deadline and self.api._login_window is not None:
            time.sleep(0.02)
        self.assertIsNone(self.api._login_window)
        self.assertFalse(google_auth.has_cookies(),
                         "визитёрские куки не должны считаться сессией")
        self.assertIn("куки не получены", self.api.poll(0)["account"]["note"])

    def test_import_file_dialog(self):
        fixture = Path(tempfile.mkdtemp(prefix="ga_")) / "cookies.txt"
        fixture.write_text(netscape_fixture(), encoding="utf-8")
        # Диалог живёт на ОКНЕ (как pick_folder) - мокаем список окон.
        fake_win = mock.Mock()
        fake_win.create_file_dialog.return_value = [str(fixture)]
        with mock.patch("webview.windows", [fake_win]):
            result = self.api.account_import()
        self.assertEqual(result.get("count"), 3)
        self.assertTrue(google_auth.has_cookies())

    def test_import_garbage_rejected(self):
        fixture = Path(tempfile.mkdtemp(prefix="ga_")) / "bad.txt"
        fixture.write_text("просто текст", encoding="utf-8")
        fake_win = mock.Mock()
        fake_win.create_file_dialog.return_value = [str(fixture)]
        with mock.patch("webview.windows", [fake_win]):
            result = self.api.account_import()
        self.assertIn("error", result)
        self.assertFalse(google_auth.has_cookies())

    def test_forget_clears_everything(self):
        google_auth.save_text_cookies(netscape_fixture())
        self.api.save_setting({"key": "google_account_label",
                               "value": "кто-то"})
        result = self.api.account_forget()
        self.assertTrue(result["ok"])
        self.assertFalse(google_auth.has_cookies())
        account = self.api.poll(0)["account"]
        self.assertFalse(account["has_cookies"])
        self.assertEqual(account["label"], "")


class TestSchema(unittest.TestCase):
    def test_account_fields(self):
        from app import settings_schema as schema
        widget = schema.field("_google_account")
        self.assertEqual(widget["type"], "account")
        self.assertTrue(widget.get("transient"))
        toggle = schema.field("use_google_cookies")
        self.assertEqual(toggle["default"], True)
        self.assertIn("use_google_cookies", schema.defaults())


if __name__ == "__main__":
    unittest.main()
