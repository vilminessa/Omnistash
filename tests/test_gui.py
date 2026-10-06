"""Дымовой тест окна: Api без pywebview + сборка страницы."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import settings as settings_mod
from app import ui
from app.db import Database
from app.gui import Api


class GuiCase(unittest.TestCase):
    """Профиль (настройки и БД) переносим во временную папку."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        # Профиль (настройки и база) переносим во временную папку.
        patcher = mock.patch("app.settings.settings_path",
                             return_value=self.dir / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("app.db.db_path", return_value=self.dir / "library.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def make_api(self) -> Api:
        api = Api()
        self.addCleanup(api.close)
        return api

    def wait_phase(self, api, phase, timeout=15.0):
        """Дождаться фазы добавления; неожиданная ошибка - провал с текстом."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            flow = api.poll(0)["add_flow"]
            if flow["phase"] == phase:
                return flow
            if phase != "error" and flow["phase"] == "error":
                self.fail(f"ошибка вместо «{phase}»: {flow.get('error')}")
            time.sleep(0.02)
        self.fail(f"фаза {phase} не наступила, сейчас "
                  f"{api.poll(0)['add_flow']['phase']}")

    def wait_sync(self, api, timeout=20.0):
        """Дождаться конца синхронизации: total>0 ставится при старте."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            sync = api.poll(0)["sync"]
            if sync.get("total") and not sync.get("running"):
                return sync
            time.sleep(0.02)
        self.fail(f"синхронизация не завершилась: {api.poll(0)['sync']}")


class TestApi(GuiCase):
    def test_initial_and_poll_shape(self):
        api = self.make_api()
        initial = api.get_initial()
        self.assertIn("version", initial)
        self.assertIn("schema", initial)
        self.assertTrue(any(f["key"] == "library_roots" for f in initial["schema"]))

        snap = api.poll(0)
        for key in ("log_cursor", "logs", "status", "busy", "scan", "settings",
                    "settings_rev", "stats", "tree", "sources", "runs", "queue"):
            self.assertIn(key, snap)
        # Журнал отдаётся дельтой: повторный опрос с тем же курсором пуст.
        cursor = snap["log_cursor"]
        self.assertEqual(api.poll(cursor)["logs"], [])

    def test_save_setting_roundtrip(self):
        api = self.make_api()
        before = api.get_initial()["settings_rev"]
        result = api.save_setting({"key": "quality", "value": "low"})
        self.assertEqual(result["settings"]["quality"], "low")
        self.assertGreater(result["settings_rev"], before)
        # Неизвестный ключ не роняет окно и ничего не меняет.
        result = api.save_setting({"key": "левый", "value": 1})
        self.assertNotIn("левый", result.get("settings", {}))

    def test_scan_without_roots_reports_error(self):
        api = self.make_api()
        self.assertEqual(api.start_scan()["error"],
                         "Сначала добавьте папки библиотеки в настройках")

    def test_scan_end_to_end(self):
        api = self.make_api()
        root = self.dir / "library"
        root.mkdir()
        (root / "видео без личности.mp4").write_bytes(b"payload")

        api.save_setting({"key": "library_roots",
                          "value": [{"path": str(root), "recursive": True,
                                     "enabled": True}]})
        started = api.start_scan()
        self.assertTrue(started.get("ok"), started)

        thread = api._scan_thread
        thread.join(timeout=60)
        self.assertFalse(thread.is_alive(), "скан не завершился")

        snap = api.poll(0)
        self.assertFalse(snap["scan"]["running"])
        self.assertIn("Готово", snap["scan"]["summary"])
        self.assertEqual(snap["stats"]["total"], 1)
        self.assertEqual(snap["stats"]["downloaded"], 1)
        # Запуск попал в журнал, дерево отражает импортированный файл.
        self.assertTrue(any(r["kind"] == "scan" for r in snap["runs"]))

    def test_scan_is_cancellable(self):
        import threading

        api = self.make_api()
        root = self.dir / "many"
        root.mkdir()
        for i in range(50):
            (root / f"file-{i:03d}.mp4").write_bytes(b"x")

        api.save_setting({"key": "library_roots",
                          "value": [{"path": str(root), "recursive": True,
                                     "enabled": True}]})
        # Держим воркер на входе, чтобы «Отмена» гарантированно успела
        # дойти: скан на 50 файлах иначе кончается быстрее клика.
        from app import indexer as indexer_mod
        real_scan = indexer_mod.scan

        def slow_scan(roots, db, progress=None, stop=None, compute_hash=True):
            stop.wait(10)
            return real_scan(roots, db, progress=progress, stop=stop,
                             compute_hash=compute_hash)

        with mock.patch("app.gui.indexer.scan", side_effect=slow_scan):
            api.start_scan()
            time.sleep(0.05)
            stopped = api.stop_scan()
            api._scan_thread.join(timeout=30)

        self.assertTrue(stopped["ok"], stopped)
        self.assertFalse(api._scan_thread.is_alive())
        snap = api.poll(0)
        self.assertFalse(snap["scan"]["running"])
        self.assertTrue(snap["scan"]["summary"].startswith("Остановлено"),
                        snap["scan"]["summary"])

    def test_start_scan_twice_is_guarded(self):
        import threading

        api = self.make_api()
        root = self.dir / "slow"
        root.mkdir()
        (root / "f.mp4").write_bytes(b"x")
        api.save_setting({"key": "library_roots",
                          "value": [{"path": str(root), "recursive": True,
                                     "enabled": True}]})
        gate = threading.Event()
        from app import indexer as indexer_mod
        real_scan = indexer_mod.scan

        def gated(roots, db, progress=None, stop=None, compute_hash=True):
            gate.wait(10)
            return real_scan(roots, db, progress=progress, stop=stop,
                             compute_hash=compute_hash)

        with mock.patch("app.gui.indexer.scan", side_effect=gated):
            api.start_scan()
            second = api.start_scan()
            gate.set()
            api._scan_thread.join(timeout=30)
        self.assertEqual(second.get("error"), "Переиндексация уже идёт")


class TestPage(GuiCase):
    def test_build_page_inlines_assets(self):
        page = ui.build_page()
        self.assertNotIn("__CSS__", page)
        self.assertNotIn("__JS__", page)
        self.assertIn("Omnistash", page)
        self.assertIn("grid-body", page)
        self.assertIn("renderGrid", page)

    def test_page_has_no_external_requests(self):
        # Страница вшивается в окно строкой: внешних скриптов, шрифтов и
        # картинок не бывает. Примеры ссылок в placeholder - это текст.
        page = ui.build_page()
        self.assertNotIn("<script src", page)
        self.assertNotIn("<link ", page)
        self.assertNotIn("@import", page)
        self.assertNotIn("url(http", page)
        self.assertNotIn('src="http', page)
        self.assertNotIn('href="http', page)
        self.assertNotIn("fetch(", page)


if __name__ == "__main__":
    unittest.main()
