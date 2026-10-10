"""Аккаунты Google: реестр копий кук, привязка к источникам, диагностика.

Ни один тест не ходит в сеть: окно входа - фикстура, куки - объекты
http.cookiejar. Проверяем и то, что секреты не утекают в журнал.
"""

import http.cookiejar
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import downloader
from app import google_auth
from app import repo
from app import settings as settings_mod
from tests.test_gui import GuiCase


def make_cookie(name, domain, value=None):
    return http.cookiejar.Cookie(
        0, name, value or ("secret-" + name), None, False,
        domain, True, domain.startswith("."), "/", True, False,
        int(time.time()) + 86400, False, None, None, {})


def session_cookies():
    return [make_cookie("SID", ".google.com"),
            make_cookie("HSID", ".google.com"),
            make_cookie("SAPISID", ".google.com"),
            make_cookie("VISITOR_INFO1_LIVE", ".youtube.com")]


def netscape_fixture():
    return google_auth.export_netscape(session_cookies())


class ProfileCase(unittest.TestCase):
    """Изолированный профиль: копии кук не трогают настоящий."""

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

    def test_create_account_stores_encrypted_copy_and_label(self):
        account = google_auth.create_account(session_cookies(), label="мой")
        path = google_auth.account_cookies_path(account["id"])
        raw = path.read_bytes()
        self.assertNotIn(b"secret-", raw, "на диске должен лежать шифротекст")
        back = google_auth.decrypted_cookies_text(account["id"])
        self.assertIn("SID", back)
        # Временный файл для качалки - честный текст и удаляемый.
        temp = google_auth.temporary_cookiefile(account["id"])
        self.assertIn("SID", temp.read_text(encoding="utf-8"))
        google_auth.release_temp(temp)
        self.assertFalse(temp.exists())
        # Реестр - только метки.
        registry = google_auth.load_registry()
        self.assertEqual(registry[0]["label"], "мой")
        self.assertNotIn("secret-", str(registry))

    def test_foreign_copy_is_not_silently_used(self):
        account = google_auth.create_account(session_cookies())
        google_auth.account_cookies_path(account["id"]).write_bytes("мусор".encode())
        with self.assertRaises(OSError):
            google_auth.temporary_cookiefile(account["id"])

    def test_google_cookies_required(self):
        with self.assertRaises(RuntimeError):
            google_auth.create_account([make_cookie("x", "example.org")])

    def test_remove_account_clears_everything(self):
        account = google_auth.create_account(session_cookies())
        removed = google_auth.remove_account(account["id"])
        self.assertTrue(removed["removed"])
        self.assertFalse(google_auth.has_account_cookies(account["id"]))
        self.assertEqual(google_auth.load_registry(), [])
        self.assertIsNone(google_auth.temporary_cookiefile(account["id"]))

    def test_netscape_garbage_rejected(self):
        with self.assertRaises(RuntimeError):
            google_auth.parse_netscape("просто текст")
        with self.assertRaises(RuntimeError):
            google_auth.parse_netscape("# Netscape HTTP Cookie File\n")

    def test_cookies_facts_hide_values(self):
        facts = google_auth.cookies_facts(session_cookies()
                                          + [make_cookie("x", "example.org")])
        self.assertEqual(facts["total"], 5)
        self.assertEqual(facts["google"], 4)
        self.assertIn("google.com", facts["domains"])
        self.assertNotIn("example.org", facts["domains"])
        self.assertNotIn("secret-", str(facts))


class TestMigrateLegacy(ProfileCase):
    def _settings(self, label="старый аккаунт", since="2026-01-02T03:04:05"):
        return {"google_account_label": label,
                "google_account_since": since}

    def test_legacy_file_becomes_first_account(self):
        legacy = self.profile / "google_cookies.bin"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(google_auth.encrypt(netscape_fixture().encode()))

        account_id = google_auth.migrate_legacy(self._settings())

        self.assertIsNotNone(account_id)
        self.assertFalse(legacy.exists(), "легаси-файл должен уйти")
        self.assertEqual(google_auth.load_registry()[0]["label"],
                         "старый аккаунт")
        self.assertEqual(google_auth.load_registry()[0]["since"],
                         "2026-01-02T03:04:05", "дата бережётся из старых "
                         "настроек")
        self.assertTrue(google_auth.has_account_cookies(account_id))

    def test_migration_is_idempotent(self):
        legacy = self.profile / "google_cookies.bin"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(google_auth.encrypt(netscape_fixture().encode()))
        self.assertIsNotNone(google_auth.migrate_legacy(self._settings()))
        self.assertIsNone(google_auth.migrate_legacy(self._settings()),
                          "повторять нечего")
        self.assertEqual(len(google_auth.load_registry()), 1)

    def test_broken_legacy_is_removed_not_fatal(self):
        legacy = self.profile / "google_cookies.bin"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes("не dpapi".encode())
        self.assertIsNone(google_auth.migrate_legacy(self._settings()))
        self.assertFalse(legacy.exists())


class TestDownloaderUsesAccount(ProfileCase):
    """Качалка получает cookiefile привязанного аккаунта и чистит его."""

    def setUp(self):
        super().setUp()
        self.created = {}
        created = self.created

        class FakeYDL:
            def __init__(self, opts):
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

    def _download(self, account_id):
        import threading as _t
        dest = tempfile.mkdtemp(prefix="dl_")
        downloader.download(
            {"remote_id": "abcdefghijk",
             "webpage_url": "https://example.invalid/v"},
            {"dest_dir": dest}, stop=_t.Event(), dest=dest,
            account_id=account_id)

    def test_cookiefile_present_and_removed_after(self):
        account = google_auth.create_account(session_cookies())
        self._download(account["id"])
        cookiefile = self.created["opts"].get("cookiefile")
        self.assertTrue(cookiefile, "куки аккаунта должны попасть в опции")
        self.assertFalse(Path(cookiefile).exists(),
                         "копия обязана уйти после загрузки")

    def test_unknown_account_leaves_opts_clean(self):
        self._download("нет-такого")
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
                 overwrite=False, account_id=None, **kwargs):
            return {"cancelled": False, "files": [], "info": {},
                    "error": "площадка требует вход в аккаунт", "hash": None}

        with mock.patch.object(downloader, "find_ffmpeg", return_value=None), \
                mock.patch.object(downloader, "download", side_effect=fake):
            row = repo.next_queued(api.db.conn)
            api.dl._download_one(api.db.conn, row, api._current_settings())

        self.assertTrue(api.dl.state.get("bot_hint"),
                        "ошибка входа должна подсказать про аккаунт")


class FakeLoginWindow:
    """Фикстура окна входа: сессия появляется не мгновенно.

    warmup - сколько первых опросов вернут «до входа» куки (без
    маркеров): имитирует реальный вход, иначе воркер успевает закрыть
    окно раньше, чем тест успеет его потрогать.
    """

    def __init__(self, cookies, email="человек@example.com", warmup=0):
        self._cookies = cookies
        self._email = email
        self._warmup = warmup
        self.destroyed = False
        self.polls = 0

    def get_cookies(self):
        if self.destroyed:
            raise RuntimeError("окно закрыто")
        self.polls += 1
        if self.polls <= self._warmup:
            return [make_cookie("NID", ".google.com")]
        return list(self._cookies)

    def evaluate_js(self, script):
        return self._email

    def destroy(self):
        self.destroyed = True


class TestAccountApi(GuiCase):
    def setUp(self):
        super().setUp()
        # Профиль изолируем сами: копии кук не должны трогать настоящий
        # %LOCALAPPDATA% (GuiCase изолирует только БД/настройки).
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA":
                                               tempfile.mkdtemp(prefix="ga_")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = self.make_api()

    def test_poll_reports_accounts_without_secrets(self):
        account = self.api.poll(0)["account"]
        for key in ("accounts", "labels", "logging_in", "note", "visible"):
            self.assertIn(key, account)
        google_auth.create_account(session_cookies(), label="мой")
        account = self.api.poll(0)["account"]
        self.assertEqual(account["accounts"][0]["label"], "мой")
        blob = str(account) + "\n".join(self.api._logs)
        self.assertNotIn("secret-", blob)

    def test_login_creates_account_and_closes_window(self):
        window = FakeLoginWindow(session_cookies())
        with mock.patch("webview.create_window", return_value=window):
            self.assertTrue(self.api.account_login_start()["ok"])
        # Ждём полного завершения воркера: окно закрывается раньше, чем
        # создаётся запись реестра.
        self.api._login_thread.join(10)
        self.assertIsNone(self.api._login_window, "окно должно закрыться само")
        self.assertTrue(window.destroyed)
        registry = google_auth.load_registry()
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry[0]["label"], "человек@example.com")
        self.assertTrue(google_auth.has_account_cookies(registry[0]["id"]))
        self.assertIn("Аккаунт добавлен", self.api.poll(0)["account"]["note"])

    def test_login_without_session_reports_diagnosis(self):
        # Визитёрская кука - не сессия: аккаунт не создаётся, но в статусе
        # остаётся диагностика «что видела поимка».
        window = FakeLoginWindow([make_cookie("NID", ".google.com")])
        with mock.patch("webview.create_window", return_value=window), \
                mock.patch.object(type(self.api), "LOGIN_TIMEOUT", 0.3):
            self.assertTrue(self.api.account_login_start()["ok"])
            self.api._login_thread.join(10)
        self.assertEqual(google_auth.load_registry(), [])
        account = self.api.poll(0)["account"]
        self.assertIn("куки не получены", account["note"])
        self.assertIn("увидено кук", account["note"],
                      "диагностика должна попасть в статус")
        facts = account["visible"]
        self.assertEqual(facts["total"], 1)
        self.assertEqual(facts["markers"], 0)

    def test_visible_reports_what_window_sees(self):
        # warmup держит окно «до входа» - воркер спит между опросами.
        window = FakeLoginWindow(session_cookies(), warmup=10)
        with mock.patch("webview.create_window", return_value=window):
            self.assertTrue(self.api.account_login_start()["ok"])
        result = self.api.account_visible()
        self.assertNotIn("error", result, result)
        self.assertEqual(result["total"], 1, "пока до входа - визитёрская")
        self.assertEqual(result["markers"], 0)
        self.assertNotIn("secret-", str(result))
        # Закрыли окно - диагностика говорит, что окна нет.
        self.api.account_login_stop()
        self.api._login_thread.join(10)
        self.assertIn("error", self.api.account_visible())

    def test_import_dialog_creates_account(self):
        fixture = Path(tempfile.mkdtemp(prefix="ga_")) / "cookies.txt"
        fixture.write_text(netscape_fixture(), encoding="utf-8")
        fake_win = mock.Mock()
        fake_win.create_file_dialog.return_value = [str(fixture)]
        with mock.patch("webview.windows", [fake_win]):
            result = self.api.account_import()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(google_auth.load_registry()), 1)

    def test_import_garbage_rejected(self):
        fixture = Path(tempfile.mkdtemp(prefix="ga_")) / "bad.txt"
        fixture.write_text("просто текст", encoding="utf-8")
        fake_win = mock.Mock()
        fake_win.create_file_dialog.return_value = [str(fixture)]
        with mock.patch("webview.windows", [fake_win]):
            result = self.api.account_import()
        self.assertIn("error", result)
        self.assertEqual(google_auth.load_registry(), [])

    def test_forget_removes_account(self):
        account = google_auth.create_account(session_cookies())
        result = self.api.account_forget({"id": account["id"]})
        self.assertTrue(result["ok"])
        self.assertFalse(google_auth.has_account_cookies(account["id"]))
        self.assertEqual(google_auth.load_registry(), [])
        self.assertIn("error", self.api.account_forget({}))


class TestSchema(unittest.TestCase):
    def test_account_fields(self):
        from app import settings_schema as schema
        widget = schema.field("_google_account")
        self.assertEqual(widget["type"], "account")
        self.assertTrue(widget.get("transient"))
        # Служебные метки - скрытые, но валидные ключи (set_value их пишет).
        for key in ("google_account_label", "google_account_since"):
            self.assertIsNotNone(schema.field(key))
            self.assertTrue(schema.field(key).get("hidden"))
        self.assertNotIn("use_google_cookies", schema.defaults(),
                         "глобальной галки больше нет - только привязки")


class TestSourceBinding(GuiCase):
    """Сквозная привязка: аккаунт -> плейлист -> синк и качалка."""

    def setUp(self):
        super().setUp()
        # Профиль изолируем сами (реестр аккаунтов - файлы, не БД).
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA":
                                               tempfile.mkdtemp(prefix="ga_")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = self.make_api()
        self.account = google_auth.create_account(session_cookies(),
                                                  label="привязанный")

    def _snapshot(self):
        channel = {"platform": "youtube", "remote_id": "UCfixtur000000000000000000000001",
                   "title": "Автор", "handle": None, "url": None,
                   "fallback": False}
        entries = [{"position": i, "remote_id": f"vid{i:09d}",
                    "title": f"Видео {i}", "duration_s": 60, "uploaded_at": None,
                    "view_count": None, "unavailable": False,
                    "webpage_url": f"https://youtu.be/vid{i:09d}",
                    "channel": channel} for i in (1, 2)]
        return {"playlist": {"platform": "youtube",
                             "remote_id": "PLfixtur0000000000000000000001",
                             "title": "Плейлист", "description": None,
                             "kind": "remote", "url": "u", "item_count": 2,
                             "raw_json": "{}", "channel": channel},
                "entries": entries, "total": 2, "url": "u"}

    def test_commit_plan_writes_account(self):
        snap = self._snapshot()
        stats = repo.commit_plan(self.api.db.conn, snap,
                                 repo.plan_diff(self.api.db.conn, snap),
                                 account_id=self.account["id"])
        row = self.api.db.conn.execute(
            "SELECT account_id FROM playlists WHERE id=?",
            (stats["playlist_id"],)).fetchone()
        self.assertEqual(row["account_id"], self.account["id"])

    def test_video_account_follows_source(self):
        snap = self._snapshot()
        stats = repo.commit_plan(self.api.db.conn, snap,
                                 repo.plan_diff(self.api.db.conn, snap),
                                 account_id=self.account["id"])
        self.assertTrue(stats["playlist_id"])
        vid = self.api.db.conn.execute(
            "SELECT id FROM videos").fetchone()["id"]
        self.assertEqual(repo.video_account_id(self.api.db.conn, vid),
                         self.account["id"])

    def test_sources_carry_account_id(self):
        snap = self._snapshot()
        repo.commit_plan(self.api.db.conn, snap,
                         repo.plan_diff(self.api.db.conn, snap),
                         account_id=self.account["id"])
        sources = self.api.poll(0)["account"]  # реестр отдельно
        source = repo.sources(self.api.db.conn)[0]
        self.assertEqual(source["account_id"], self.account["id"])
        # Подпись подмешивается gui-хелпером для списка источников.
        labeled = self.api.poll(0)
        self.assertEqual(labeled["account"]["accounts"][0]["id"],
                         self.account["id"])


if __name__ == "__main__":
    unittest.main()
