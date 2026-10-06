"""Скан библиотеки: опознание файлов, переезды, пропажи, отчёт."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import repo
from app.db import Database
from app.indexer import SIDECAR_SUFFIX, scan
from app.metadata import normalize_video


class ScanCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "library"
        self.root.mkdir(parents=True)
        self.db = Database(Path(self._tmp.name) / "library.db")
        self.conn = self.db.conn
        self.roots = [{"path": str(self.root), "recursive": True, "enabled": True}]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def write(self, name, payload=b"video-bytes"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def run_scan(self, **kwargs):
        kwargs.setdefault("compute_hash", True)
        return scan(self.roots, self.db, **kwargs)

    def rows(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT id, platform, remote_id, title, status FROM videos")]


class TestRecognize(ScanCase):
    def test_bound_by_id_in_filename(self):
        # Формат Synfronia не содержит ID, но если ID есть - он главный признак.
        repo.upsert_video(self.conn, normalize_video(
            {"id": "dQw4w9WgXcQ", "title": "Клип", "channel": "Автор"}))
        self.write("Клип [dQw4w9WgXcQ].mp4")
        report = self.run_scan()
        self.assertEqual(report["bound_id"], 1)
        self.assertEqual(report["added"], 0)
        row = self.rows()[0]
        self.assertEqual(row["remote_id"], "dQw4w9WgXcQ")
        self.assertEqual(row["status"], "downloaded")

    def test_bound_by_sidecar(self):
        self.write("Пост.mp4", b"payload-a")
        sidecar = {"omnistash": 1, "platform": "youtube", "remote_id": "abc000000001",
                   "info": {"id": "abc000000001", "title": "Название поста",
                            "channel": "Автор", "upload_date": "20250304"}}
        (self.root / ("Пост" + SIDECAR_SUFFIX)).write_text(
            json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
        report = self.run_scan()
        self.assertEqual(report["bound_sidecar"], 1)
        row = self.rows()[0]
        self.assertEqual(row["remote_id"], "abc000000001")
        self.assertEqual(row["title"], "Название поста")
        detail = repo.video_detail(self.conn, row["id"])
        kinds = {f["kind"] for f in detail["files"]}
        self.assertIn("video", kinds)
        self.assertIn("sidecar", kinds)

    def test_bound_by_title(self):
        repo.upsert_video(self.conn, normalize_video(
            {"id": "zzz000000001", "title": "Моё видео"}), full=True)
        self.write("Моё видео.mp4")
        report = self.run_scan()
        self.assertEqual(report["bound_title"], 1)
        self.assertEqual(self.rows()[0]["remote_id"], "zzz000000001")

    def test_unknown_file_becomes_local(self):
        self.write("Ни с чем не совпало.mp4")
        report = self.run_scan()
        self.assertEqual(report["added"], 1)
        row = self.rows()[0]
        self.assertEqual(row["platform"], "local")
        self.assertEqual(row["status"], "downloaded")

    def test_non_media_is_skipped(self):
        self.write("readme.txt", b"text")
        report = self.run_scan()
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(len(self.rows()), 0)


class TestRescan(ScanCase):
    def test_second_scan_changes_nothing(self):
        repo.upsert_video(self.conn, normalize_video(
            {"id": "dQw4w9WgXcQ", "title": "Клип"}))
        self.write("Клип [dQw4w9WgXcQ].mp4")
        self.run_scan()
        again = self.run_scan()
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(again["added"], 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE missing=0").fetchone()["n"], 1)

    def test_moved_file_is_rebound_not_duplicated(self):
        self.write("Без имени.mp4", b"unique-payload-1")
        self.run_scan()
        (self.root / "Без имени.mp4").rename(self.root / "Переименовано.mp4")
        report = self.run_scan()
        self.assertEqual(report["rebound"], 1)
        self.assertEqual(len(self.rows()), 1)          # дубля нет
        path = self.conn.execute("SELECT path FROM files").fetchone()["path"]
        self.assertTrue(path.endswith("Переименовано.mp4"))

    def test_deleted_file_marks_missing(self):
        path = self.write("Пропадёт.mp4", b"unique-payload-2")
        self.run_scan()
        path.unlink()
        report = self.run_scan()
        self.assertEqual(report["missing"], 1)
        self.assertEqual(self.rows()[0]["status"], "missing")
        # Запись не удаляется: индекс помнит, что файл был.
        self.assertEqual(len(self.rows()), 1)

    def test_returned_file_is_alive_again(self):
        path = self.write("Возвращается.mp4", b"unique-payload-3")
        self.run_scan()
        path.unlink()
        self.run_scan()
        path.write_bytes(b"unique-payload-3")
        self.run_scan()
        self.assertEqual(self.rows()[0]["status"], "downloaded")

    def test_disabled_and_missing_roots(self):
        self.roots = [
            {"path": str(self.root / "нет-такой"), "recursive": True, "enabled": True},
            {"path": str(self.root), "recursive": True, "enabled": False},
        ]
        report = self.run_scan()
        self.assertEqual(report["roots"][0]["state"], "unavailable")
        self.assertEqual(len(report["roots"]), 1)  # выключенный корень не считаем
        self.assertEqual(report["scanned"], 0)


class TestStop(ScanCase):
    def test_stop_event_halts_scan(self):
        import threading
        stop = threading.Event()
        stop.set()   # «нажали Отмена» до старта
        report = self.run_scan(stop=stop)
        self.assertTrue(report["stopped"])


if __name__ == "__main__":
    unittest.main()
