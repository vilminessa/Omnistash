"""Очередь загрузки: статусы, отмена, повторы, запись файлов в индекс.

Сеть подменяется фикстурой (downloader.download), которая пишет настоящий
файл во временную папку: важно проверить конвейер «файл появился ->
строки files -> downloaded», а не сам YouTube.
"""

import pathlib
import time
from unittest import mock

from app import repo
from app.queue import DownloadWorker
from tests.test_gui import GuiCase


def make_video(api, remote_id="vid000000001", title="Видео"):
    """Строка videos со статусом queued - так её оставляет добавление."""
    vid, _created = repo.upsert_video(api.db.conn, {
        "platform": "youtube", "remote_id": remote_id, "key": f"youtube:{remote_id}",
        "title": title, "webpage_url": f"https://youtu.be/{remote_id}",
        "raw_json": "{}", "origin": "yt-dlp",
    })
    repo.enqueue(api.db.conn, [vid])
    return vid


def wait_until(predicate, timeout=10.0, message="условие не наступило"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError(message)


class QueueCase(GuiCase):
    def setUp(self):
        super().setUp()
        self.dest = self.dir / "downloads"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.api = self.make_api()
        self.api.save_setting({"key": "dest_dir", "value": str(self.dest)})

    def fake_download(self, behaviour=None):
        """Подмена downloader.download: пишет файл и сайдкар в dest_dir."""

        def download(video, settings, *, stop, on_progress=None):
            if behaviour:
                return behaviour(video, settings, stop=stop, on_progress=on_progress)
            target = pathlib.Path(settings.get("dest_dir") or ".")
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"{video['remote_id']}.mp4"
            path.write_bytes(b"payload-" + video["remote_id"].encode())
            sidecar = path.with_suffix(".post.json")
            sidecar.write_text('{"omnistash": 1}', encoding="utf-8")
            if on_progress:
                on_progress({"stage": "файл", "percent": 100, "downloaded": 8,
                             "total": 8, "speed": 1024.0, "eta": 0})
            return {"cancelled": False,
                    "files": [(str(path), "video"), (str(sidecar), "sidecar")],
                    "info": {"id": video["remote_id"]}, "error": None,
                    "hash": "sha256:" + "ab" * 32}

        patcher = mock.patch("app.downloader.download", side_effect=download)
        patcher.start()
        self.addCleanup(patcher.stop)
        return download


class TestWorker(QueueCase):
    def test_downloads_and_records_files(self):
        vid = make_video(self.api, "vid000000001", "Первое")
        self.fake_download()
        result = self.api.queue_start()
        self.assertTrue(result["ok"], result)

        worker = self.api.dl
        self.assertTrue(worker.wait(timeout=15), "воркер не завершился")

        status = self.api.db.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()["status"]
        self.assertEqual(status, "downloaded")
        files = [dict(r) for r in self.api.db.conn.execute(
            "SELECT kind, path, size, hash FROM files WHERE video_id=?", (vid,))]
        kinds = sorted(f["kind"] for f in files)
        self.assertEqual(kinds, ["sidecar", "video"])
        self.assertTrue(all(f["size"] for f in files), "размер файла не записан")
        self.assertTrue(all(str(self.dest) in f["path"] for f in files))

        state = worker.state
        self.assertFalse(state["running"])
        self.assertEqual(state["done"], 1)
        self.assertEqual(state["failed"], 0)
        self.assertEqual(self.api.poll(0)["stats"]["downloaded"], 1)

    def test_failure_marks_failed_and_counts(self):
        vid = make_video(self.api, "vid000000002", "Плохое")

        def broken(video, settings, *, stop, on_progress=None):
            return {"cancelled": False, "files": [], "info": {},
                    "error": "площадка не отдала файл", "hash": None}

        self.fake_download(broken)
        self.api.queue_start()
        self.api.dl.wait(timeout=15)

        status = self.api.db.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()["status"]
        self.assertEqual(status, "failed")
        state = self.api.dl.state
        self.assertEqual(state["failed"], 1)
        self.assertEqual(state["done"], 0)

        # Повтор уводит упавшее обратно в очередь.
        retry = self.api.queue_retry()
        self.assertEqual(retry["retried"], 1)
        self.api.dl.stop()
        self.api.dl.wait(timeout=15)

    def test_stop_returns_item_to_queue(self):
        make_video(self.api, "vid000000003", "Долгое")
        started = {"hit": False}

        def slow(video, settings, *, stop, on_progress=None):
            started["hit"] = True
            stop.wait(10)          # качаем, пока не нажмут Стоп
            return {"cancelled": stop.is_set(), "files": [], "info": {},
                    "error": None, "hash": None}

        self.fake_download(slow)
        self.api.queue_start()
        wait_until(lambda: started["hit"], message="загрузка не началась")

        self.assertTrue(self.api.queue_stop()["ok"])
        self.assertTrue(self.api.dl.wait(timeout=15), "воркер не остановился")

        row = self.api.db.conn.execute("SELECT status FROM videos").fetchone()
        # Остановка - не ошибка: возвращаемся в очередь для докачки.
        self.assertEqual(row["status"], "queued")
        self.assertEqual(self.api.dl.state["failed"], 0)

    def test_start_with_empty_queue_says_so(self):
        result = self.api.queue_start()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Очередь пуста")


class TestEnqueueApi(QueueCase):
    def test_enqueue_starts_worker_and_downloads(self):
        vid = make_video(self.api, "vid000000004", "Из пикера")
        self.fake_download()
        result = self.api.enqueue({"ids": [vid]})
        self.assertEqual(result["queued"], 1)

        # enqueue запускает качалку сам - это смысл режима «Полная».
        self.assertTrue(self.api.dl.wait(timeout=15), "воркер не отработал")
        status = self.api.db.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()["status"]
        self.assertEqual(status, "downloaded")

    def test_enqueue_skips_already_downloaded(self):
        vid = make_video(self.api, "vid000000005", "Готовое")
        self.api.db.conn.execute(
            "UPDATE videos SET status='downloaded' WHERE id=?", (vid,))
        self.assertEqual(self.api.enqueue({"ids": [vid]})["queued"], 0)


class TestWorkerState(QueueCase):
    def test_state_is_a_copy(self):
        worker = DownloadWorker(self.api.db, self.api._current_settings)
        snap = worker.state
        snap["done"] = 99
        self.assertEqual(worker.state["done"], 0, "state отдаёт копию, не внутренность")
