"""Докачка ffmpeg: установка по согласию, атомарность, зеркало, отмена.

Сети здесь нет ни в одном тесте: шов _open подменяется фейковым ответом,
а пробный запуск (подделать запускаемый .exe в фикстуре нельзя) инжектится
параметром probe. Отдельно проверяется, что окно не ставит ffmpeg само:
только по нажатию и без двух параллельных закачек.
"""

import io
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app import downloader
from app import ffmpeg_installer as inst
from tests.test_gui import GuiCase


def fake_zip(files: dict) -> bytes:
    """Настоящий zip в памяти: ключи - вложенные пути архива (как в жизни)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


GOOD_ZIP = fake_zip({
    "ffmpeg-7.1-essentials_build/bin/ffmpeg.exe": b"MZ-fake-ffmpeg",
    "ffmpeg-7.1-essentials_build/bin/ffprobe.exe": b"MZ-fake-ffprobe",
    "ffmpeg-7.1-essentials_build/README.txt": b"ignore me",
})


class FakeResponse:
    """Ответ urlopen: байты из памяти (+ гибкие управление чтением)."""

    def __init__(self, payload: bytes, on_read=None):
        self._io = io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))}
        self._on_read = on_read

    def read(self, size):
        chunk = self._io.read(size)
        if self._on_read and chunk:
            self._on_read()
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class InstallerCase(unittest.TestCase):
    """Изолированный профиль, нулевые бэкоффы, журнал и счётчик обращений."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="omni_ff_")
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile = Path(self._dir) / "Omnistash"
        self.calls = []
        self.logs = []
        self.enterContext(mock.patch.object(inst, "RETRY_DELAY", 0))

    def install(self, payload=GOOD_ZIP, *, probe=None, stop=None, log=None):
        """Запустить установку с фейковой сетью.

        payload: bytes - один и тот же ответ; None - сети нет всегда;
        dict подстрока URL -> bytes|Exception - ответ по источникам.
        """
        probe = probe or (lambda exe: None)     # «проба прошла»
        if log is None:
            log = self.logs.append

        def fake_open(request, timeout=120):
            self.calls.append(request.full_url)
            if payload is None:
                raise OSError("нет сети")
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if key in request.full_url:
                        if isinstance(value, Exception):
                            raise value
                        return FakeResponse(value)
                raise OSError("источник не в фикстуре")
            if isinstance(payload, Exception):
                raise payload
            return FakeResponse(payload)

        with mock.patch.object(inst, "_open", side_effect=fake_open):
            return inst.install(log=log, stop=stop, probe=probe)


class TestInstall(InstallerCase):
    def test_places_both_exes_and_cleans_staging(self):
        path = self.install()
        self.assertEqual(path, self.profile / "bin" / "ffmpeg.exe")
        self.assertEqual((self.profile / "bin" / "ffprobe.exe").read_bytes(),
                         b"MZ-fake-ffprobe")
        # staging и zip убраны: в bin лежат ровно два exe
        leftovers = sorted(p.name for p in (self.profile / "bin").iterdir())
        self.assertEqual(leftovers, ["ffmpeg.exe", "ffprobe.exe"])

    def test_source_is_gyan(self):
        self.install()
        self.assertTrue(any("gyan.dev" in url for url in self.calls))

    def test_idempotent_without_network(self):
        target = self.profile / "bin" / "ffmpeg.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes("уже стоит".encode())
        with mock.patch.object(
                inst, "_open",
                side_effect=AssertionError("сеть не должна открываться")):
            path = inst.install(probe=lambda exe: None)
        self.assertEqual(path, target)
        self.assertEqual(target.read_bytes(), "уже стоит".encode())

    def test_second_source_when_first_is_down(self):
        payload = {           # gyan.dev всегда падает - уходим в зеркало
            "gyan.dev": OSError("503 на CDN"),
            "BtbN": GOOD_ZIP,
        }
        path = self.install(payload=payload)
        self.assertTrue(path.is_file())
        self.assertEqual(len([u for u in self.calls if "gyan.dev" in u]),
                         inst.ATTEMPTS, "бэкофф отмотан на основном")
        self.assertEqual(len([u for u in self.calls if "BtbN" in u]), 1,
                         "зеркало с первой попытки")
        self.assertTrue(any("gyan.dev" in line for line in self.logs))

    def test_broken_zip_leaves_no_broken_exe(self):
        with self.assertRaises(RuntimeError):
            self.install(payload="это не zip".encode())
        bin_dir = self.profile / "bin"
        self.assertFalse((bin_dir / "ffmpeg.exe").exists(),
                         "битый exe не должен появиться")
        self.assertEqual(list(bin_dir.iterdir()) if bin_dir.exists() else [],
                         [], "staging убран и в неудаче")

    def test_probe_failure_does_not_install(self):
        # Проба не прошла -> подмены не было: старая версия (или пустота)
        # остаются как есть, битого exe не появляется.
        def boom(exe):
            raise RuntimeError("не запустился")

        with self.assertRaises(RuntimeError):
            self.install(probe=boom)
        self.assertFalse((self.profile / "bin" / "ffmpeg.exe").exists())
        self.assertTrue(all("установлен" not in line for line in self.logs))

    def test_zip_without_ffmpeg_is_an_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.install(payload=fake_zip({"build/README.txt":
                                           "нет exe".encode()}))
        self.assertIn("нет ffmpeg.exe", str(ctx.exception))

    def test_cancel_before_start(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(inst.InstallCancelled):
            self.install(stop=stop)
        self.assertEqual(self.calls, [], "сеть не открывается после отмены")
        self.assertFalse((self.profile / "bin" / "ffmpeg.exe").exists())

    def test_cancel_mid_download(self):
        stop = threading.Event()
        cancelling = FakeResponse(GOOD_ZIP, on_read=stop.set)  # «Стоп» на 1-м чанке
        with mock.patch.object(inst, "_open", return_value=cancelling):
            with self.assertRaises(inst.InstallCancelled):
                inst.install(stop=stop, probe=lambda exe: None)
        self.assertFalse((self.profile / "bin" / "ffmpeg.exe").exists())

    def test_progress_phase_order(self):
        seen = []
        with mock.patch.object(inst, "_open",
                               return_value=FakeResponse(GOOD_ZIP)):
            inst.install(progress=lambda phase, pct: seen.append(phase),
                         probe=lambda exe: None)
        # download повторяется на чанках и попытках - порядок фаз читаем
        # по первым вхождениям
        self.assertEqual(list(dict.fromkeys(seen)),
                         ["download", "extract", "verify", "done"])

    def test_install_dir_is_profile_bin(self):
        self.assertEqual(inst.install_dir(), self.profile / "bin")
        self.assertIsNone(inst.installed_ffmpeg())
        inst.install_dir().mkdir(parents=True)
        (inst.install_dir() / "ffmpeg.exe").write_bytes(b"x")
        self.assertEqual(inst.installed_ffmpeg(),
                         inst.install_dir() / "ffmpeg.exe")


class TestFindFfmpeg(unittest.TestCase):
    """find_ffmpeg: своя папка в профиле, чужая копия Synfronia - последняя."""

    @staticmethod
    def _isolated(tmp):
        # base_dir подменяем в downloader (там он импортирован по имени),
        # иначе вернёт настоящий <корень>/bin и тест решит, что ffmpeg стоит.
        return (mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}),
                mock.patch.object(downloader, "base_dir",
                                  return_value=Path(tmp) / "нет-такой"),
                mock.patch.object(downloader.shutil, "which",
                                  return_value=None))

    def test_profile_dir_is_searched(self):
        tmp = tempfile.mkdtemp(prefix="omni_find_")
        target = Path(tmp) / "Omnistash" / "bin" / "ffmpeg.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"fake")
        env, base, which = self._isolated(tmp)
        with env, base, which:
            self.assertEqual(downloader.find_ffmpeg(), str(target))

    def test_no_ffmpeg_returns_none(self):
        tmp = tempfile.mkdtemp(prefix="omni_find_")
        env, base, which = self._isolated(tmp)
        with env, base, which:
            self.assertIsNone(downloader.find_ffmpeg())


class TestFfmpegApi(GuiCase):
    """Окно: только по кнопке, без двух закачек, отмена доводит до конца."""

    def setUp(self):
        super().setUp()
        self.api = self.make_api()

    def test_poll_reports_ffmpeg_state(self):
        state = self.api.poll()["ffmpeg"]
        for key in ("found", "path", "running", "phase", "pct", "error",
                    "degraded"):
            self.assertIn(key, state)

    def test_start_short_circuits_when_ffmpeg_found(self):
        with mock.patch.object(downloader, "find_ffmpeg",
                               return_value="C:/ffmpeg/bin/ffmpeg.exe"):
            result = self.api.ffmpeg_start()
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("already"))
        self.assertFalse(self.api.poll()["ffmpeg"]["running"])
        self.assertIsNone(self.api._ffmpeg_thread)

    def test_double_start_is_refused(self):
        started = threading.Event()
        release = threading.Event()

        def fake_install(*, progress, log, stop):
            started.set()
            release.wait(10)
            return Path("x") / "ffmpeg.exe"

        with mock.patch.object(downloader, "find_ffmpeg", return_value=None), \
                mock.patch.object(inst, "install", side_effect=fake_install):
            self.assertTrue(self.api.ffmpeg_start()["ok"])
            self.assertTrue(started.wait(5), "воркер не стартовал")
            again = self.api.ffmpeg_start()
            self.assertIn("error", again, "второй запуск должен упасть")
            self.assertTrue(self.api.poll()["ffmpeg"]["running"])
            release.set()
            self.api._ffmpeg_thread.join(10)
            state = self.api.poll()["ffmpeg"]
        self.assertFalse(state["running"])
        self.assertEqual(state["phase"], "done")
        self.assertIsNone(state["error"])

    def test_stop_cancels_install(self):
        started = threading.Event()

        def fake_install(*, progress, log, stop):
            started.set()
            stop.wait(10)
            raise inst.InstallCancelled("остановлено пользователем")

        with mock.patch.object(downloader, "find_ffmpeg", return_value=None), \
                mock.patch.object(inst, "install", side_effect=fake_install):
            self.assertTrue(self.api.ffmpeg_start()["ok"])
            self.assertTrue(started.wait(5), "воркер не стартовал")
            self.assertTrue(self.api.ffmpeg_stop()["ok"])
            self.api._ffmpeg_thread.join(10)
            state = self.api.poll()["ffmpeg"]
        self.assertFalse(state["running"])
        self.assertEqual(state["phase"], "cancelled")
        self.assertIsNone(state["error"])

    def test_failure_is_visible_in_state_and_log(self):
        def fake_install(*, progress, log, stop):
            raise RuntimeError("не удалось скачать ffmpeg ни с одного "
                               "источника: нет сети")

        with mock.patch.object(downloader, "find_ffmpeg", return_value=None), \
                mock.patch.object(inst, "install", side_effect=fake_install):
            self.api.ffmpeg_start()
            self.api._ffmpeg_thread.join(10)
            state = self.api.poll()["ffmpeg"]
        self.assertEqual(state["phase"], "error")
        self.assertIn("нет сети", state["error"])
        self.assertTrue(any("ffmpeg не установлен" in line
                            for line in self.api._logs))


class TestWarnOnceWithoutFfmpeg(GuiCase):
    """Очередь жалуется на деградацию один раз за сессию, а не на каждую строку."""

    def setUp(self):
        super().setUp()
        self.api = self.make_api()
        (self.dir / "lib").mkdir()
        self.storage = self.add_storage(self.api, self.dir / "lib")
        self.api.save_setting({"key": "default_storage_id",
                               "value": self.storage["id"]})
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
        # download фейкаем: в тестах нет ни сети, ни файлов; очередь честно
        # дождёт «файл не найден», нам важны строки про ffmpeg.
        with mock.patch.object(downloader, "find_ffmpeg", return_value=None), \
                mock.patch.object(
                    downloader, "download",
                    return_value={"cancelled": False, "error": None,
                                  "files": [], "hash": None}):
            self.assertFalse(worker.ffmpeg_warned)
            for _ in range(2):
                row = repo.next_queued(self.api.db.conn)
                self.assertIsNotNone(row, "две строки должны стоять в очереди")
                worker._download_one(self.api.db.conn, row,
                                     self.api._current_settings())
        self.assertTrue(worker.ffmpeg_warned)
        hits = [m for m in self.messages if "ffmpeg не найден" in m]
        self.assertEqual(len(hits), 1, f"предупреждение должно быть одно: {hits}")
        self.assertIn("настройках", hits[0], "в тексте - куда идти за установкой")


class TestVersionSingleSource(unittest.TestCase):
    """version.py - переэкспорт: два номера версии не должны разойтись."""

    def test_version_py_reexports_app(self):
        import app
        import version
        self.assertIs(version.__version__, app.__version__)
        self.assertRegex(app.__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
