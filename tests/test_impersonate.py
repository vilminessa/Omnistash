"""Имитация отпечатка браузера и куки из файла (cookies_file).

Обе настройки - секция «Загрузка»: impersonate (chrome/firefox/edge/off,
по умолчанию chrome) и cookies_file (путь к cookies.txt, рабочий путь
для браузеров с шифрованием куки). Здесь: цель yt-dlp (объект, не строка),
мягкая деградация без curl_cffi, приоритет cookies_file над браузерными
куками, подсказки в тексте ошибки и предупреждение очереди один раз.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import downloader
from tests.test_gui import GuiCase
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DownloadError

VIDEO = {"webpage_url": "https://example.com/v1", "id": "v1"}
HAVE_CFFI = downloader.curl_cffi_available()


class FakeYDL:
    """YoutubeDL-заглушка: успех с фейковым info, opts - в снимок."""

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
        if action == "err410":
            raise DownloadError(
                "ERROR: [PornHub] Unable to download webpage: "
                "HTTP Error 410: Gone (caused by HTTPError)")
        return {"id": "v1", "title": "Тест", "ext": "mp4",
                "requested_downloads": []}


class CacheCase(unittest.TestCase):
    """Кэши curl_cffi/целей живут между тестами - чистим тут и возвращаем."""

    def setUp(self):
        self._ok = downloader._CFFI_OK
        self._cache = dict(downloader._IMPERSONATE_CACHE)
        downloader._IMPERSONATE_CACHE.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        downloader._CFFI_OK = self._ok
        downloader._IMPERSONATE_CACHE.clear()
        downloader._IMPERSONATE_CACHE.update(self._cache)


class TestImpersonateTarget(CacheCase):
    def test_off_and_empty_are_none(self):
        self.assertIsNone(downloader.impersonate_target({"impersonate": "off"}))
        self.assertIsNone(downloader.impersonate_target({}))

    @unittest.skipUnless(HAVE_CFFI, "curl_cffi не установлена")
    def test_chrome_gives_target_object(self):
        target = downloader.impersonate_target({"impersonate": "chrome"})
        self.assertIsInstance(target, ImpersonateTarget,
                              "yt-dlp ждёт объект, строка падает ассертом")
        self.assertEqual(target.client, "chrome")

    @unittest.skipUnless(HAVE_CFFI, "curl_cffi не установлена")
    def test_unknown_client_is_none(self):
        # Нет ни у обработчика, ни (возможно) в парсере - везде None.
        self.assertIsNone(
            downloader.impersonate_target({"impersonate": "mosaic"}))

    @unittest.skipUnless(HAVE_CFFI, "curl_cffi не установлена")
    def test_degrades_without_curl_cffi(self):
        downloader._CFFI_OK = False
        self.assertIsNone(
            downloader.impersonate_target({"impersonate": "chrome"}),
            "без библиотеки - тихая деградация, не ошибка")

    @unittest.skipUnless(HAVE_CFFI, "curl_cffi не установлена")
    def test_result_is_cached(self):
        first = downloader.impersonate_target({"impersonate": "chrome"})
        second = downloader.impersonate_target({"impersonate": "chrome"})
        self.assertIs(first, second, "цель считается один раз за сессию")


class TestBuildOptsImpersonate(CacheCase):
    def setUp(self):
        super().setUp()
        self._dest_tmp = tempfile.TemporaryDirectory(prefix="impopts_")
        self.addCleanup(self._dest_tmp.cleanup)
        self.dest = Path(self._dest_tmp.name)

    def make_opts(self, settings):
        return downloader.build_opts(
            {**settings, "retries": 1}, self.dest, stop=threading.Event())

    def test_off_means_no_key(self):
        opts = self.make_opts({"impersonate": "off"})
        self.assertNotIn("impersonate", opts)

    @unittest.skipUnless(HAVE_CFFI, "curl_cffi не установлена")
    def test_chrome_by_default_when_available(self):
        # Дефолт схемы - chrome: при установленной библиотеке опция
        # должна попадать в выкачку без дополнительных действий юзера.
        opts = self.make_opts({"impersonate": "chrome"})
        self.assertIsInstance(opts.get("impersonate"), ImpersonateTarget)

    @unittest.skipUnless(HAVE_CFFI, "curl_cffi не установлена")
    def test_no_curl_cffi_means_no_key(self):
        downloader._CFFI_OK = False
        opts = self.make_opts({"impersonate": "chrome"})
        self.assertNotIn("impersonate", opts,
                         "без библиотеки опция не передаётся (иначе "
                         "yt-dlp упадёт при инициализации)")


class TestCookiesFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name)

    def run_download(self, settings):
        captured: list = []
        shared_plan = ["ok"]

        def factory(opts):
            captured.append(dict(opts))
            return FakeYDL(captured, shared_plan)

        with mock.patch.object(downloader.yt_dlp, "YoutubeDL",
                               side_effect=factory):
            result = downloader._download(
                dict(VIDEO),
                {"retries": 1, "dest_dir": str(self.dest), **settings},
                stop=threading.Event())
        return result, captured

    def test_cookies_file_beats_browser_mode(self):
        cookies_path = "C:\\tmp\\ph_cookies.txt"
        result, captured = self.run_download({
            "cookies_file": cookies_path, "browser_cookies": "firefox"})
        self.assertIsNone(result["error"], result)
        self.assertEqual(captured[0].get("cookiefile"), cookies_path,
                         "файл кук юзера приоритетнее браузерного режима")
        self.assertNotIn("cookiesfrombrowser", captured[0])

    def test_account_cookiefile_beats_settings_file(self):
        # Копия привязанного аккаунта важнее: её передаёт download().
        with mock.patch.object(downloader.yt_dlp, "YoutubeDL") as ydl:
            downloader._download(
                dict(VIDEO),
                {"retries": 1, "dest_dir": str(self.dest),
                 "cookies_file": "C:\\tmp\\ph_cookies.txt"},
                stop=threading.Event(), cookiefile=Path("C:\\acc\\c.txt"))
        opts = ydl.call_args[0][0]
        self.assertEqual(opts.get("cookiefile"), "C:\\acc\\c.txt")

    def test_empty_file_falls_back_to_browser(self):
        result, captured = self.run_download({
            "cookies_file": "", "browser_cookies": "firefox"})
        self.assertIsNone(result["error"], result)
        self.assertEqual(captured[0].get("cookiesfrombrowser"),
                         ("firefox",))


class TestHumanHints(unittest.TestCase):
    def test_410_points_to_cookies_file(self):
        message = downloader._human_error(DownloadError(
            "Unable to download webpage: HTTP Error 410: Gone"))
        self.assertIn("Куки из файла", message,
                      "подсказка - конкретное поле настроек, не диагноз")

    def test_403_points_to_impersonation(self):
        message = downloader._human_error(DownloadError(
            "ERROR: Unable to download webpage: HTTP Error 403: Forbidden"))
        self.assertIn("имитацию браузера", message)

    def test_generic_http_error_keeps_old_text(self):
        message = downloader._human_error(DownloadError(
            "ERROR: Unable to download webpage: HTTP Error 500: Server"))
        self.assertIn("площадка не отдала файл", message)


class TestWarnOnceImpersonateUnavailable(GuiCase):
    """Очередь жалуется на недоступную имитацию один раз за сессию."""

    def setUp(self):
        super().setUp()
        self.api = self.make_api()
        (self.dir / "lib").mkdir()
        self.storage = self.add_storage(self.api, self.dir / "lib")
        self.api.save_setting({"key": "default_storage_id",
                               "value": self.storage["id"]})
        self.api.save_setting({"key": "impersonate", "value": "chrome"})
        self.messages = []
        self.api._logs = self.messages

    def _enqueue(self, n):
        from app import repo
        for index in range(n):
            vid, _ = repo.upsert_video(self.api.db.conn, {
                "platform": "youtube", "remote_id": f"vid{index:07d}",
                "key": f"youtube:vid{index:07d}", "title": f"видео {index}",
                "raw_json": "{}", "origin": "yt-dlp"})
            repo.enqueue(self.api.db.conn, [vid])

    def test_warning_appears_once(self):
        from app import repo
        from app.queue import DownloadWorker

        worker = DownloadWorker(self.api.db, self.api._current_settings,
                                log=self.messages.append)
        self._enqueue(2)
        with mock.patch.object(downloader, "impersonate_target",
                               return_value=None), \
                mock.patch.object(
                    downloader, "download",
                    return_value={"cancelled": False, "error": None,
                                  "files": [], "hash": None}):
            for _ in range(2):
                row = repo.next_queued(self.api.db.conn)
                self.assertIsNotNone(row, "две строки должны стоять в очереди")
                worker._download_one(self.api.db.conn, row,
                                     self.api._current_settings())
        hits = [m for m in self.messages if "curl_cffi" in m]
        self.assertEqual(len(hits), 1, f"предупреждение должно быть одно: {hits}")
        self.assertIn("pip install", hits[0], "в тексте - как лечить")


if __name__ == "__main__":
    unittest.main()
