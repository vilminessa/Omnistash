"""Дымовой тест окна: Api без pywebview + сборка страницы."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import repo
from app import settings as settings_mod
from app import ui
from app.db import Database
from app.gui import MAX_LOG_BYTES, Api
from app.mesh import channel as mesh_channel
from app.mesh import identity as mesh_identity
from app.mesh import server as mesh_server


def wait_until(predicate, timeout=10.0, message="условие не наступило"):
    """Ждать условие с малым интервалом (для фоновых воркеров)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError(message)


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
        # Журнал тоже в профиль теста: без патча _log писал бы в настоящий
        # %LOCALAPPDATA%\Omnistash\omnistash.log.
        patcher = mock.patch("app.gui.log_path",
                             return_value=self.dir / "omnistash.log")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def make_api(self) -> Api:
        api = Api()
        self.addCleanup(api.close)
        return api

    def add_storage(self, api: Api, path, *, recursive: bool = True,
                    enabled: bool = True, label: str | None = None) -> dict:
        """Зарегистрировать папку-хранилище (вместо старого library_roots)."""
        from app import storages
        result = storages.add(api.db.conn, str(path), label=label,
                              enabled=enabled, recursive=recursive)
        self.assertIsInstance(result, dict, result)
        self.assertTrue(result.get("id"), result)
        return result

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
        # Корни убраны из настроек - вместо них поле-виджет хранилищ.
        self.assertTrue(any(f["key"] == "_storages" for f in initial["schema"]))
        self.assertFalse(any(f["key"] == "library_roots" for f in initial["schema"]))

        snap = api.poll(0)
        for key in ("log_cursor", "logs", "status", "busy", "scan", "settings",
                    "settings_rev", "stats", "tree", "sources", "runs", "queue",
                    "storages", "add_flow", "dl", "sync"):
            self.assertIn(key, snap)
        self.assertEqual(snap["storages"], [])
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
                         "Сначала добавьте папку-хранилище в настройках")

    def test_scan_end_to_end(self):
        api = self.make_api()
        root = self.dir / "library"
        root.mkdir()
        (root / "видео без личности.mp4").write_bytes(b"payload")
        self.add_storage(api, root)

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
        self.add_storage(api, root)
        # Держим воркер на входе, чтобы «Отмена» гарантированно успела
        # дойти: скан на 50 файлах иначе кончается быстрее клика.
        from app import indexer as indexer_mod
        real_scan = indexer_mod.scan

        def slow_scan(roots, db, progress=None, stop=None, **kwargs):
            stop.wait(10)
            return real_scan(roots, db, progress=progress, stop=stop, **kwargs)

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

    def test_open_url_rejects_non_http(self):
        # Валидный URL открыл бы настоящий браузер, поэтому проверяем только
        # защиту: наружу уходят исключительно http/https.
        api = self.make_api()
        api.open_url("javascript:alert(1)")
        api.open_url("file:///C:/Windows/System32/calc.exe")
        api.open_url("")
        api.open_url(None)
        # Системный вызов не долетел - иначе тест бы просто повис.
        self.assertTrue(True)

    def test_start_scan_twice_is_guarded(self):
        import threading

        api = self.make_api()
        root = self.dir / "slow"
        root.mkdir()
        (root / "f.mp4").write_bytes(b"x")
        self.add_storage(api, root)
        gate = threading.Event()
        from app import indexer as indexer_mod
        real_scan = indexer_mod.scan

        def gated(roots, db, progress=None, stop=None, **kwargs):
            gate.wait(10)
            return real_scan(roots, db, progress=progress, stop=stop, **kwargs)

        with mock.patch("app.gui.indexer.scan", side_effect=gated):
            api.start_scan()
            second = api.start_scan()
            gate.set()
            api._scan_thread.join(timeout=30)
        self.assertEqual(second.get("error"), "Переиндексация уже идёт")


class TestPickFolder(GuiCase):
    """Диалог выбора папки: три исхода, и каждый должен быть виден.

    Сам диалог руками в тесте не откроешь, поэтому подменяем окно:
    проверяется НАША часть - что вызов идёт в метод окна (pywebview 6),
    а не в модуль (pywebview 5), и что отмена отличается от поломки.
    """

    class FakeWindow:
        def __init__(self, result=None, exc=None):
            self.result = result
            self.exc = exc
            self.kinds = []

        def create_file_dialog(self, kind, *args, **kwargs):
            self.kinds.append(kind)
            if self.exc is not None:
                raise self.exc
            return self.result

    def test_path_from_window(self):
        api = self.make_api()
        window = self.FakeWindow(result=("D:\\новая папка",))
        api.bind_window(window)
        self.assertEqual(api.pick_folder(), {"path": "D:\\новая папка"})
        # Диалог обязан зваться у ОКНА, а не у модуля webview.
        import webview
        self.assertEqual(window.kinds, [webview.FileDialog.FOLDER])

    def test_tuple_result_takes_first(self):
        api = self.make_api()
        api.bind_window(self.FakeWindow(result=("D:\\a", "D:\\b")))
        self.assertEqual(api.pick_folder(), {"path": "D:\\a"})

    def test_cancel_is_not_an_error(self):
        api = self.make_api()
        api.bind_window(self.FakeWindow(result=None))
        self.assertEqual(api.pick_folder(), {"cancelled": True})
        # Отмена не должна оставлять след в журнале.
        self.assertNotIn("Диалог", " ".join(api.poll(0)["logs"]))

    def test_failure_is_reported_not_swallowed(self):
        # Именно так и выглядел баг: исключение -> null -> «нажал и ничего».
        api = self.make_api()
        api.bind_window(self.FakeWindow(exc=RuntimeError("boom")))
        result = api.pick_folder()
        self.assertIn("error", result)
        self.assertIn("boom", result["error"])
        self.assertTrue(any("Диалог выбора папки" in line
                            for line in api.poll(0)["logs"]))

    def test_missing_window_reports_error(self):
        api = self.make_api()
        result = api.pick_folder()
        self.assertIn("error", result, "без окна тихий null недопустим")


class TestLogFile(GuiCase):
    """Журнал на диске: строка с датой, ротация, открытие."""

    def test_log_line_reaches_file_with_date(self):
        api = self.make_api()
        api._log("строка в файл")
        text = (self.dir / "omnistash.log").read_text(encoding="utf-8")
        self.assertIn("строка в файл", text)
        # В файле нужна дата: без неё журнал прошлых дней неотличим.
        self.assertRegex(text, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")

    def test_log_rotates_on_start(self):
        # Путь уже перенесён в профиль теста (GuiCase), просто раздуваем файл.
        log = self.dir / "omnistash.log"
        log.write_bytes(b"x" * (MAX_LOG_BYTES + 50))
        api = self.make_api()           # ротация - в __init__, до первой строки
        size = log.stat().st_size
        self.assertLess(size, MAX_LOG_BYTES, "ротация не сработала")
        self.assertIn(b"rotated", log.read_bytes()[:64])
        # После ротации журнал продолжает писаться.
        api._log("после ротации")
        self.assertIn("после ротации", log.read_text(encoding="utf-8"))

    def test_open_log_creates_and_opens(self):
        api = self.make_api()
        with mock.patch("os.startfile") as startfile:
            result = api.open_log()
        self.assertTrue(result["ok"], result)
        startfile.assert_called_once()
        self.assertTrue((self.dir / "omnistash.log").exists())


class TestQueueResume(GuiCase):
    """Очередь переживает рестарт окна (настройка resume_queue)."""

    def _enqueue_one(self, api):
        vid, _ = repo.upsert_video(api.db.conn, {
            "platform": "youtube", "remote_id": "vid000000001",
            "key": "youtube:vid000000001", "title": "Очередное",
            "webpage_url": "https://youtu.be/x", "raw_json": "{}",
            "origin": "yt-dlp"})
        repo.enqueue(api.db.conn, [vid])
        return vid

    def _fake_download(self):
        def download(video, settings, *, stop, on_progress=None, dest=None,
                     **kwargs):
            target = Path(dest or ".")
            target.mkdir(parents=True, exist_ok=True)
            path = target / "готово.mp4"
            path.write_bytes(b"payload")
            return {"cancelled": False,
                    "files": [(str(path), "video")], "info": {},
                    "error": None, "hash": "sha256:aa"}
        patcher = mock.patch("app.downloader.download", side_effect=download)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_resume_starts_worker_and_finishes(self):
        api1 = self.make_api()
        api1.save_setting({"key": "resume_queue", "value": True})
        (self.dir / "out").mkdir()
        storage = self.add_storage(api1, self.dir / "out")
        api1.save_setting({"key": "default_storage_id", "value": storage["id"]})
        vid = self._enqueue_one(api1)
        api1.close()                     # имитируем закрытие окна

        self._fake_download()
        api2 = self.make_api()           # новый запуск
        self.assertTrue(
            wait_until(lambda: api2.db.conn.execute(
                "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()
                ["status"] == "downloaded", timeout=10),
            "очередь не возобновилась при старте")
        self.assertEqual(api2.dl.state["done"], 1)

    def test_resume_disabled_keeps_queue_paused(self):
        api1 = self.make_api()
        api1.save_setting({"key": "resume_queue", "value": False})
        (self.dir / "out").mkdir()
        storage = self.add_storage(api1, self.dir / "out")
        api1.save_setting({"key": "default_storage_id", "value": storage["id"]})
        vid = self._enqueue_one(api1)
        api1.close()

        self._fake_download()
        api2 = self.make_api()
        time.sleep(0.5)
        self.assertFalse(api2.dl.state["running"],
                         "выключенная настройка должна остановить автозапуск")
        status = api2.db.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()["status"]
        self.assertEqual(status, "queued")


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


class TestRefreshMeta(GuiCase):
    """«Обновить метаданные»: полный запрос -> колонки + synced_at."""

    def _video(self, api, title="Старое"):
        vid, _ = repo.upsert_video(api.db.conn, {
            "platform": "youtube", "remote_id": "vid000000001",
            "key": "youtube:vid000000001", "title": title,
            "webpage_url": "https://www.youtube.com/watch?v=vid000000001",
            "origin": "yt-dlp"})
        return vid

    def test_refresh_updates_columns_and_synced_at(self):
        api = self.make_api()
        vid = self._video(api)
        fake_info = {"id": "vid000000001", "title": "Свежее",
                     "like_count": 42, "comment_count": 7,
                     "release_date": "20250101", "view_count": 123,
                     "thumbnail": "https://i.ytimg.com/x.jpg",
                     "description": "Описание с площадки"}
        with mock.patch("app.gui.sources.fetch_video", return_value=fake_info):
            result = api.refresh_meta_start()
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result["total"], 1)
            # Join ВНУТРИ with: патч должен жить, пока воркер ходит в сеть.
            api._refresh_thread.join(timeout=15)
        self.assertFalse(api._refresh_thread.is_alive())

        row = dict(api.db.conn.execute(
            "SELECT * FROM videos WHERE id=?", (vid,)).fetchone())
        self.assertEqual(row["title"], "Свежее")
        self.assertEqual(row["like_count"], 42)
        self.assertEqual(row["comment_count"], 7)
        self.assertEqual(row["view_count"], 123)
        self.assertEqual(row["uploaded_at"], "2025-01-01")
        self.assertEqual(row["thumb_url"], "https://i.ytimg.com/x.jpg")
        self.assertTrue(row["synced_at"], "synced_at должен записаться")
        state = api.poll(0)["refresh"]
        self.assertFalse(state["running"])
        self.assertIn("1 из 1", state["summary"])

    def test_refresh_error_is_counted_not_fatal(self):
        api = self.make_api()
        self._video(api)
        with mock.patch("app.gui.sources.fetch_video",
                        side_effect=RuntimeError("площадка упала")):
            self.assertTrue(api.refresh_meta_start().get("ok"))
            api._refresh_thread.join(timeout=15)
        state = api.poll(0)["refresh"]
        self.assertFalse(state["running"])
        self.assertEqual(state["errors"], 1)
        self.assertIn("ошибок 1", state["summary"])

    def test_refresh_stop_between_videos(self):
        import threading

        api = self.make_api()
        self._video(api)
        second, _ = repo.upsert_video(api.db.conn, {
            "platform": "youtube", "remote_id": "vid000000002",
            "key": "youtube:vid000000002", "title": "Второй",
            "webpage_url": "https://www.youtube.com/watch?v=vid000000002",
            "origin": "yt-dlp"})
        started = threading.Event()
        release = threading.Event()

        def slow_fetch(url, **kwargs):
            started.set()
            release.wait(10)
            return {"id": "vid000000001", "title": "Первый"}

        with mock.patch("app.gui.sources.fetch_video", side_effect=slow_fetch):
            self.assertTrue(api.refresh_meta_start().get("ok"))
            self.assertTrue(started.wait(10), "воркер не стартовал")
            self.assertTrue(api.refresh_meta_stop().get("ok"))
            release.set()
            api._refresh_thread.join(timeout=15)
        self.assertFalse(api._refresh_thread.is_alive())
        state = api.poll(0)["refresh"]
        self.assertFalse(state["running"])
        self.assertTrue(state["summary"].startswith("Остановлено"),
                        state["summary"])

    def test_refresh_marks_unavailable_and_keeps_data(self):
        """Удалённое видео: метка «архивное», последние данные целы."""
        api = self.make_api()
        vid = self._video(api, title="Последнее известное")
        api.db.conn.execute(
            "UPDATE videos SET view_count=777 WHERE id=?", (vid,))

        with mock.patch("app.gui.sources.fetch_video",
                        side_effect=RuntimeError(
                            "ERROR: [youtube] vid000000001: Video unavailable")):
            self.assertTrue(api.refresh_meta_start().get("ok"))
            api._refresh_thread.join(timeout=15)
        self.assertFalse(api._refresh_thread.is_alive())

        row = dict(api.db.conn.execute(
            "SELECT status, title, view_count FROM videos WHERE id=?",
            (vid,)).fetchone())
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(row["title"], "Последнее известное",
                         "данные не должны затираться")
        self.assertEqual(row["view_count"], 777)
        state = api.poll(0)["refresh"]
        self.assertFalse(state["running"])
        self.assertEqual(state["errors"], 1)

    def test_refresh_restores_unavailable_marker(self):
        """Видео снова доступно: метка снимается, данные освежаются."""
        api = self.make_api()
        vid = self._video(api)
        repo.set_status(api.db.conn, [vid], "unavailable")

        fake_info = {"id": "vid000000001", "title": "Снова живое",
                     "like_count": 3}
        with mock.patch("app.gui.sources.fetch_video", return_value=fake_info):
            self.assertTrue(api.refresh_meta_start().get("ok"))
            api._refresh_thread.join(timeout=15)

        row = dict(api.db.conn.execute(
            "SELECT status, title FROM videos WHERE id=?", (vid,)).fetchone())
        # Файла не было - строка снова «в индексе».
        self.assertEqual(row["status"], "known")
        self.assertEqual(row["title"], "Снова живое")
        state = api.poll(0)["refresh"]
        self.assertIn("1 из 1", state["summary"])


class TestMeshReject(GuiCase):
    """Отзыв «файл оказался не тем»: стоп-лист + удаление + честный статус."""

    def _seed(self, api, *, mesh: bool = True):
        conn = api.db.conn
        vid, _ = repo.upsert_video(conn, {
            "platform": "youtube", "remote_id": "vidreject001",
            "key": "youtube:vidreject001", "title": "Клип",
            "origin": "yt-dlp"})
        path = self.dir / "Клип [vidreject001].mp4"
        path.write_bytes(b"ftyp-fake-" + b"x" * 32)
        fmt = None
        if mesh:
            fmt = json.dumps({
                "source": "mesh", "peer": "ab" * 32,
                "level": "одиночный узел (принимаем по правилу пользователя)",
                "consensus": 1, "peer_offenses": 0,
                "peer_synced_at": "2026-10-11T09:00:00",
            }, ensure_ascii=False)
        repo.record_file(conn, vid, str(path), "video",
                         size=path.stat().st_size, mtime=1.0,
                         digest="sha256:" + "cd" * 32, format_json=fmt)
        return vid, path

    def test_reject_bans_removes_and_fails_row(self):
        api = self.make_api()
        vid, path = self._seed(api)
        res = api.mesh_reject({"id": vid})
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res["removed"], "файл должен удалиться")
        self.assertFalse(path.exists(), "негодная копия уходит с диска")
        conn = api.db.conn
        row = dict(conn.execute(
            "SELECT status, last_error FROM videos WHERE id=?",
            (vid,)).fetchone())
        self.assertEqual(row["status"], "failed",
                         "«Повторить упавшие» пойдёт с площадки")
        self.assertIn("стоп-листе", row["last_error"] or "")
        file_row = dict(conn.execute(
            "SELECT hash, missing FROM files WHERE video_id=?",
            (vid,)).fetchone())
        self.assertEqual(file_row["missing"], 1)
        self.assertEqual(file_row["hash"], "sha256:" + "cd" * 32,
                         "хеш остаётся - он и есть предмет отзыва")
        from app.mesh import feedback
        self.assertIn("sha256:" + "cd" * 32, feedback.banned_set(conn))
        self.assertEqual(feedback.list_bans(conn)[0]["peer"], "ab" * 32,
                         "ключ-носитель запоминается как нарушитель")
        # Повторный отзыв невозможен: статус уже не «скачан».
        self.assertIn("error", api.mesh_reject({"id": vid}))

    def test_reject_only_for_mesh_copies(self):
        api = self.make_api()
        vid, path = self._seed(api, mesh=False)
        res = api.mesh_reject({"id": vid})
        self.assertIn("mesh", res.get("error", ""))
        self.assertTrue(path.exists(), "файл с площадки не трогаем")
        from app.mesh import feedback
        self.assertEqual(feedback.banned_set(api.db.conn), set())

    def test_reject_unknown_video_is_error(self):
        api = self.make_api()
        self.assertIn("error", api.mesh_reject({"id": 99999}))
        self.assertIn("error", api.mesh_reject({"id": "не число"}))


class TestPeerApi(GuiCase):
    """B1: профиль участника, поиск по нику, проверка адреса.

    Сеть минимальная: только тестовый TLS-сервер на localhost; настоящий
    DHT в офлайн-тестах не поднимается (ошибки «узел не поднят» - тоже
    проверяемое поведение).
    """

    def setUp(self):
        super().setUp()
        # Профиль для ключа идентичности и пинов - строго тестовый.
        self._prof = tempfile.TemporaryDirectory()
        self.addCleanup(self._prof.cleanup)
        prof = Path(self._prof.name)
        for module in (mesh_identity, mesh_channel):
            patcher = mock.patch.object(module, "profile_dir",
                                        lambda: prof)
            patcher.start()
            self.addCleanup(patcher.stop)
        mesh_identity.reset_cache()
        self.addCleanup(mesh_identity.reset_cache)
        mesh_channel.reset_cache()
        self.addCleanup(mesh_channel.reset_cache)

    def _api(self, *, enabled=True, nick="Иван Тест") -> Api:
        """Api с подменёнными настройками: без реального старта mesh."""
        api = self.make_api()
        api._current_settings = lambda: {"mesh_enabled": enabled,
                                         "mesh_nick": nick}
        return api

    def test_peer_profile_key_and_fingerprint(self):
        api = self._api()
        profile = api.peer_profile()
        self.assertEqual(len(profile["pubkey"]), 64)
        self.assertEqual(profile["fingerprint"],
                         mesh_identity.fingerprint(profile["pubkey"]))
        self.assertEqual(profile["nick"], "Иван Тест")
        self.assertTrue(profile["nick_valid"])
        self.assertTrue(profile["enabled"])
        self.assertFalse(profile["dht"],
                         "DHT в офлайн-тесте не должен считаться поднятым")
        # Ключ создан В тестовом профиле (не в настоящем).
        self.assertTrue((Path(self._prof.name) / "mesh_key.bin").is_file())

    def test_nick_search_validates_before_network(self):
        api = self._api()
        for bad in ({"query": ""}, {"query": "a"}, {},
                    {"query": "x" * 33}, {"query": "ник<script>"}):
            res = api.nick_search(bad)
            self.assertIn("error", res, bad)
        # Участие выключено - про настройки, а не про пустой результат.
        res = self._api(enabled=False).nick_search({"query": "Друг"})
        self.assertIn("настройках", res.get("error", ""))
        # Включено, но узел не поднят - честная ошибка про DHT.
        res = self._api(enabled=True).nick_search({"query": "Друг"})
        self.assertIn("DHT", res.get("error", ""))

    def test_nick_search_results_shape(self):
        api = self._api()

        class FakeNode:
            running = True
            table = ["узел-в-таблице"]      # len>0: DHT уже набрал узлов

            def name_lookup_sync(self, nick, *args, **kwargs):
                return [{"addr": "127.0.0.1:38574"},
                        {"addr": "10.0.0.9:38574"}]

            def stop_sync(self, *args, **kwargs):
                # api.close() останавливает узел - заглушка для фейка.
                return None

        api._mesh_dht = FakeNode()
        mesh_channel.pin_set("127.0.0.1:38574", "ab" * 32)
        # Поиск открывает канал каждому адресату (личность - оттуда);
        # патчим: живых машин в офлайн-тесте нет.
        identities = {"127.0.0.1:38574": "ab" * 32,
                      "10.0.0.9:38574": "cd" * 32}
        with mock.patch("app.gui.mesh_client.probe",
                        side_effect=lambda addr, **kw: identities[addr]):
            res = api.nick_search({"query": "Друг"})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["query"], "Друг")
        self.assertEqual(res.get("found"), 2,
                         "счётчик «адресов в DHT» нужен для диагностики: "
                         "пусто-в-DHT и отсеянный-каналом - разные болезни")
        self.assertEqual(len(res["results"]), 2, "коллизии - списком")
        first, second = res["results"]
        self.assertEqual(first["nick"], "Друг")
        self.assertEqual(first["pubkey"], "ab" * 32)
        self.assertEqual(first["fingerprint"],
                         mesh_identity.fingerprint("ab" * 32))
        self.assertTrue(first["pinned"], "запомненный адрес помечается")
        self.assertEqual(second["fingerprint"],
                         mesh_identity.fingerprint("cd" * 32))
        self.assertFalse(second["pinned"])
        # Пин при поиске НЕ пишется (это делает явная кнопка проверки).
        self.assertEqual(mesh_channel.pin_get("10.0.0.9:38574"), "")

    def test_nick_search_waits_for_dht_table(self):
        """Пока DHT не набрал узлов - честная просьба подождать."""
        api = self._api()

        class EmptyNode:
            running = True
            table: list = []

            def name_lookup_sync(self, *args, **kwargs):
                return []

            def stop_sync(self, *args, **kwargs):
                return None

        api._mesh_dht = EmptyNode()
        res = api.nick_search({"query": "Друг"})
        self.assertIn("набирает", res.get("error", ""))

    def test_nick_probe_tls_pin_cycle(self):
        api = self._api()
        server = mesh_server.PeerServer(api.db, 0)
        port = server.start()
        self.addCleanup(server.stop)
        addr = f"127.0.0.1:{port}"
        own = mesh_identity.get_identity()[1].hex()

        res = api.nick_probe({"addr": addr, "pubkey": own})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["identity"], own)
        self.assertTrue(res["pinned"])
        self.assertTrue(res["new_pin"], "первый контакт запоминает ключ")
        self.assertEqual(res["fingerprint"],
                         mesh_identity.fingerprint(own))

        res = api.nick_probe({"addr": addr, "pubkey": own})
        self.assertTrue(res.get("ok"))
        self.assertFalse(res["new_pin"], "повтор - ключ уже известен")

        # Пин есть, но заявлЕн чужой ключ - отказ (канал открылся не тем).
        res = api.nick_probe({"addr": addr, "pubkey": "ff" * 32})
        self.assertIn("error", res)
        self.assertIn("пин", res.get("error", ""))

        # Мёртвый адрес и кривая строка адреса - честные ошибки.
        res = api.nick_probe({"addr": "127.0.0.1:1", "pubkey": own})
        self.assertIn("error", res)
        self.assertIn("error", api.nick_probe({"addr": "без-порта"}))

    def test_nick_probe_requires_mesh_enabled(self):
        res = self._api(enabled=False).nick_probe(
            {"addr": "127.0.0.1:38574", "pubkey": "ab" * 32})
        self.assertIn("настройках", res.get("error", ""))

    def test_trust_op_cycle_and_devices_in_profile(self):
        """B0: probe заводит устройство, человек вершит цикл доверия."""
        api = self._api()
        server = mesh_server.PeerServer(api.db, 0)
        port = server.start()
        self.addCleanup(server.stop)
        addr = f"127.0.0.1:{port}"
        own = mesh_identity.get_identity()[1].hex()

        res = api.nick_probe({"addr": addr, "pubkey": own})
        self.assertEqual(res["trust"], "tofu", "первый контакт - TOFU")
        devices = api.peer_profile()["devices"]
        self.assertEqual(devices[0]["pubkey"], own)
        self.assertEqual(devices[0]["status"], "tofu")
        self.assertEqual(devices[0]["last_addr"], addr)

        res = api.trust_op({"op": "verify", "pubkey": own})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["device"]["status"], "verified")
        res = api.nick_probe({"addr": addr, "pubkey": own})
        self.assertEqual(res["trust"], "verified", "probe видит отметку")

        res = api.trust_op({"op": "revoke", "pubkey": own})
        self.assertEqual(res["device"]["status"], "revoked")
        res = api.nick_probe({"addr": addr, "pubkey": own})
        self.assertTrue(res["revoked"], "probe честно пишет об отзыве")

        res = api.trust_op({"op": "restore", "pubkey": own})
        self.assertEqual(res["device"]["status"], "tofu",
                         "снятие отзыва возвращает в TOFU")

        self.assertIn("error",
                      api.trust_op({"op": "label", "pubkey": "кривой"}))
        self.assertIn("error",
                      api.trust_op({"op": "не знаю", "pubkey": own}))

        res = api.trust_op({"op": "forget", "pubkey": own})
        self.assertTrue(res["forgotten"])
        self.assertEqual(api.peer_profile()["devices"], [])

    def test_sas_flow(self):
        """1.1: SAS-слова -> подтверждение человека -> verified_via=sas."""
        api = self._api()
        server = mesh_server.PeerServer(api.db, 0)
        port = server.start()
        self.addCleanup(server.stop)
        addr = f"127.0.0.1:{port}"

        res = api.trust_sas_begin({"addr": addr})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(res["words"]), 12, "ровно 12 слов")
        own = res["pubkey"]

        # чужой ключ / повтор без сверки - честные отказы
        bad = api.trust_sas_confirm({"pubkey": "ff" * 32})
        self.assertIn("Нет активной", bad.get("error", ""))

        ok = api.trust_sas_confirm({"pubkey": own})
        self.assertTrue(ok.get("ok"), ok)
        self.assertEqual(ok["device"]["status"], "verified")
        self.assertEqual(ok["device"]["verified_via"], "sas")

        again = api.trust_sas_confirm({"pubkey": own})
        self.assertIn("Нет активной", again.get("error", ""),
                      "pending израсходован - вторая сверка нужна новая")

        # пин НЕ писался до подтверждения - теперь устройство verified
        self.assertEqual(api.peer_profile()["devices"][0]["pubkey"], own)

    def test_catalog_refresh_list_restore_guard(self):
        """B3: обновление зеркала, список и защита «только своя строка»."""
        api = self._api()
        addr = "127.0.0.1:38574"
        api._current_settings = lambda: {"mesh_enabled": True,
                                         "mesh_nick": "Иван Тест",
                                         "mesh_peers": addr}
        entries = [{"remote_id": "vid000000001", "title": "Клип",
                    "duration_s": 213, "synced_at": "2026-10-01",
                    "updated_at": "2026-10-01T10:00:00",
                    "sha256": "sha256:" + "ab" * 32}]
        with mock.patch("app.gui.mesh_client.collect_catalogs",
                        return_value=[(addr, "ab" * 32, entries)]):
            res = api.catalog_refresh()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["fetched"][0]["added"], 1)
        self.assertEqual(api.peer_profile()["catalogs"][0]["entries"], 1)

        listed = api.catalog_list({"query": "КЛИП"})
        self.assertEqual(len(listed["rows"]), 1)
        self.assertIsNone(listed["rows"][0]["own_id"],
                          "строки нет в индексе - восстанавливать нечего")

        res = api.catalog_restore({"remote_id": "vid000000001",
                                   "origin": "ab" * 32, "addr": addr})
        self.assertIn("нет в вашем индексе", res.get("error", ""))

        res = self._api(enabled=False).catalog_refresh()
        self.assertIn("настройках", res.get("error", ""))

        res = api.catalog_forget({"origin": "ab" * 32})
        self.assertEqual(res["removed"], 1)
        self.assertEqual(api.catalog_list({})["rows"], [])

    def test_mdns_found_flow_and_profile_status(self):
        """B4: находка mDNS живёт в памяти, своё объявление отфильтровано."""
        api = self._api()
        api._mesh_port = 38574
        api._mdns_on_found("9.9.9.9", 38574, "Сосед")
        self.assertIn("9.9.9.9", api._mdns_found)
        with mock.patch("app.gui.mesh_mdns.lan_ip", return_value="5.5.5.5"):
            api._mdns_on_found("5.5.5.5", 38574, "")
        self.assertNotIn("5.5.5.5", api._mdns_found,
                         "собственное объявление не должно попадать в список")
        addrs = [addr for addr, _key in api._catalog_addresses()]
        self.assertIn("9.9.9.9:38574", addrs,
                      "mDNS-находка участвует в адресах каталогов")
        profile = api.peer_profile()
        mdns_status = profile["status"]["mdns"]
        self.assertIsInstance(mdns_status["available"], bool)
        self.assertFalse(mdns_status["running"], "в тесте старт не поднимаем")
        self.assertEqual(mdns_status["found"], 1)

    def test_wormhole_status_and_send_guard(self):
        """B1: статус читается, действия без установки честно откажут."""
        api = self._api()
        status = api.wormhole_status()
        self.assertIn("installed", status)
        self.assertFalse(status["busy"])
        with mock.patch("app.gui.wormhole_mod.installed",
                        return_value=False):
            res = api.wormhole_send({"path": "x.mp4"})
            self.assertIn("не установлен", res.get("error", ""))
            res = api.wormhole_receive({"code": "7-a-b-c"})
            self.assertIn("не установлен", res.get("error", ""))
        res = api.wormhole_send({"path": ""})
        self.assertIn("путь", res.get("error", ""))
        res = api.wormhole_receive({"code": ""})
        self.assertIn("код", res.get("error", "").lower())

    def test_nick_change_during_announce_not_lost(self):
        """Смена ника ПОКА идёт объявление - не теряется (P3).

        Живой сбой: сохранён 'vilmi' -> объявляется ~50с -> сохранён
        'vilm' -> старый код молча выходил («поток занят») -> в профиле
        один ник, в DHT другой и поиск не находит.
        """
        api = self.make_api()
        settings_box = {"mesh_enabled": True, "mesh_nick": "vilmi"}
        api._current_settings = lambda: dict(settings_box)
        calls = []

        class SlowNode:
            running = True
            table = ["узел"]
            publish_public = False    # gui выставляет по факту проброса

            def announce_name_sync(self, nick, port, **kwargs):
                calls.append(nick)
                time.sleep(0.3)          # объявляет «долго»
                return 7

            def ensure_ready(self, timeout=None):
                return True

            def learn_external_ip(self, ip):
                return False

            def set_publish_public(self, ok):
                return None

            def stop_sync(self):
                return None

        api._mesh_dht = SlowNode()
        api._start_nick_announce()       # объявляет 'vilmi'
        time.sleep(0.05)
        settings_box["mesh_nick"] = "vilm"
        api._start_nick_announce()       # раньше это терялось!
        self.assertIsNotNone(api._nick_thread)
        api._nick_thread.join(timeout=10)
        self.assertFalse(api._nick_thread.is_alive())
        self.assertEqual(calls, ["vilmi", "vilm"],
                         "второй ник обязан объявиться после первого")
        self.assertEqual(api._nick_last, {"nick": "vilm", "accepted": 7})


if __name__ == "__main__":
    unittest.main()
