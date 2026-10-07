"""Пометки пользователя (теги/рейтинг/заметки) и проверка целостности."""

import json
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import repo, verify
from app.indexer import SIDECAR_SUFFIX, file_hash
from tests.test_gui import GuiCase


class TestNormalizeTags(unittest.TestCase):
    def test_trim_dedupe_and_limits(self):
        self.assertEqual(repo.normalize_tags("  рок , живой ,, РОК "),
                         "рок, живой")
        self.assertEqual(repo.normalize_tags(""), None)
        self.assertEqual(repo.normalize_tags(None), None)
        # Длинные и многочисленные теги не должны раздувать FTS.
        long_tags = ", ".join(f"тег{i}" for i in range(100))
        normalized = repo.normalize_tags(long_tags)
        self.assertLessEqual(len(normalized.split(", ")), 40)
        self.assertTrue(all(len(t) <= 48 for t in normalized.split(", ")))

    def test_newlines_are_separators(self):
        self.assertEqual(repo.normalize_tags("первый\nвторой"), "первый, второй")


class MarksCase(GuiCase):
    def setUp(self):
        super().setUp()
        self.api = self.make_api()
        self.conn = self.api.db.conn
        self.vid, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": "vid000000001",
            "key": "youtube:vid000000001", "title": "Клип",
            "raw_json": "{}", "origin": "yt-dlp"})
        (self.dir / "lib").mkdir()
        self.storage = self.add_storage(self.api, self.dir / "lib")
        # Куда качать - выбор человека: в тесте глобальное хранилище.
        self.api.save_setting({"key": "default_storage_id",
                               "value": self.storage["id"]})

    def row(self, vid=None):
        return dict(self.conn.execute(
            "SELECT * FROM videos WHERE id=?", (vid or self.vid,)).fetchone())


class TestSaveFields(MarksCase):
    def test_whitelist_blocks_status(self):
        # Попытка «случайно» поменять статус из окна игнорируется.
        result = self.api.save_fields({
            "ids": [self.vid],
            "fields": {"user_tags": "метка", "status": "downloaded",
                       "user_rating": 3}})
        self.assertTrue(result["ok"], result)
        row = self.row()
        self.assertEqual(row["user_tags"], "метка")
        self.assertEqual(row["user_rating"], 3)
        self.assertEqual(row["status"], "known", "статус нельзя тронуть извне")

    def test_rating_clamped_and_watchable(self):
        self.api.save_fields({"ids": [self.vid],
                              "fields": {"user_rating": 99}})
        self.assertEqual(self.row()["user_rating"], 5)
        self.api.save_fields({"ids": [self.vid], "fields": {"watched": True}})
        self.assertIsNotNone(self.row()["watched_at"])
        self.api.save_fields({"ids": [self.vid], "fields": {"watched": False}})
        self.assertIsNone(self.row()["watched_at"])

    def test_queue_order_survives_a_tag_edit(self):
        # updated_at не трогаем: правка тега не должна менять очередь.
        self.conn.execute("UPDATE videos SET updated_at='2020-01-01T00:00:00'")
        self.api.save_fields({"ids": [self.vid], "fields": {"user_tags": "x"}})
        self.assertEqual(self.row()["updated_at"], "2020-01-01T00:00:00")

    def test_tag_is_searchable_right_away(self):
        # Тег живёт в FTS5: триггер должен отработать на UPDATE.
        self.api.save_fields({"ids": [self.vid],
                              "fields": {"user_tags": "избранный автор"}})
        found = repo.list_videos(self.conn, query="избранный")
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["rows"][0]["id"], self.vid)

    def test_rating_filter(self):
        second, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": "vid000000002",
            "key": "youtube:vid000000002", "title": "Второй",
            "raw_json": "{}", "origin": "yt-dlp"})
        self.api.save_fields({"ids": [self.vid], "fields": {"user_rating": 5}})
        self.api.save_fields({"ids": [second], "fields": {"user_rating": 3}})

        self.assertEqual(repo.list_videos(self.conn, rating_min=5)["total"], 1)
        self.assertEqual(repo.list_videos(self.conn, rating_min=3)["total"], 2)
        self.assertEqual(repo.list_videos(self.conn)["total"], 2)


class TestVerify(MarksCase):
    def _file(self, name, payload=b"payload", digest=True):
        path = self.dir / "lib" / name
        path.write_bytes(payload)
        repo.record_file(self.conn, self.vid, str(path), "video",
                         size=len(payload), mtime=1.0,
                         digest=file_hash(path) if digest else None)
        return path

    def test_broken_filled_and_missing_are_different(self):
        good = self._file("ok.mp4", b"good")
        broken = self.dir / "lib" / "broken.mp4"
        broken.write_bytes("другое содержимое".encode())
        repo.record_file(self.conn, self.vid, str(broken), "video",
                         size=3, mtime=1.0, digest="sha256:aa" * 16)
        nohash = self._file("nohash.mp4", b"nohash", digest=False)

        result = verify.verify(self.conn, {"scope": {"type": "pool"}})
        self.assertEqual(result["broken_total"], 1, result)
        self.assertEqual(result["broken"][0]["path"], str(broken))
        self.assertEqual(result["filled"], 1, "хеш должен дополняться")
        self.assertIsNotNone(self.conn.execute(
            "SELECT hash FROM files WHERE path=?", (str(nohash),)).fetchone()["hash"])
        # Все три файла у одного видео, все осмотрены: битой, с дописанным
        # хешом и без хеша.
        self.assertEqual(result["checked"], 3, result)
        self.assertTrue(good.exists())

    def test_missing_file_is_reported_not_broken(self):
        gone = self._file("gone.mp4", b"gone")
        gone.unlink()
        result = verify.verify(self.conn, {"scope": {"type": "pool"}})
        self.assertEqual(result["broken_total"], 0, result)
        self.assertEqual(result["missing_total"], 1, result)

    def test_stop_interrupts(self):
        for index in range(5):
            self._file(f"f{index}.mp4", b"content")
        stop = threading.Event()
        stop.set()
        result = verify.verify(self.conn, {"scope": {"type": "pool"}},
                               stop=stop)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["checked"], 0)

    def test_empty_selection_is_error(self):
        # Пустой выбор - не «всё подряд», а явный отказ.
        self.api._verify.update(broken=[], broken_total=0)
        self.assertIn("error", self.api.repair_broken())


class TestRepair(MarksCase):
    def test_repair_marks_failed_and_requeues(self):
        bad = self.dir / "lib" / "битое.mp4"
        bad.write_bytes("битые байты".encode())
        repo.record_file(self.conn, self.vid, str(bad), "video",
                         size=9, mtime=1.0, digest="sha256:00" * 32)
        result = verify.verify(self.conn, {"scope": {"type": "pool"}})
        self.assertEqual(result["broken_total"], 1)

        # Имитируем нажатие «Перекачать битые» в окне.
        with self.api._lock:
            self.api._verify.update(broken=result["broken"],
                                    broken_total=result["broken_total"])
        repaired = self.api.repair_broken()
        self.assertEqual(repaired["queued"], 1)

        row = self.row()
        self.assertEqual(row["status"], "queued")
        self.assertTrue(row["last_error"].startswith(repo.CHECKSUM_PREFIX),
                        row["last_error"])

    def test_repair_without_broken_is_error(self):
        self.assertIn("error", self.api.repair_broken())

    def test_corrupt_row_forces_overwrite_on_download(self):
        """Ключевой штрих: битый файл должен перекачаться, а не пропуститься."""
        bad = self.dir / "lib" / "битое.mp4"
        bad.write_bytes("битые байты".encode())
        repo.record_file(self.conn, self.vid, str(bad), "video",
                         size=9, mtime=1.0, digest="sha256:00" * 32)
        result = verify.verify(self.conn, {"scope": {"type": "pool"}})
        with self.api._lock:
            self.api._verify.update(broken=result["broken"],
                                    broken_total=result["broken_total"])
        # Патч ДО repair_broken: очередь стартует внутри repair и не должна
        # успеть уйти в реальную сеть.
        seen = {}

        def fake(video, settings, *, stop, on_progress=None, dest=None,
                 overwrite=False, **kwargs):
            seen["overwrite"] = overwrite
            target = Path(dest or ".")
            target.mkdir(parents=True, exist_ok=True)
            path = target / "перекачано.mp4"
            path.write_bytes("свежие байты".encode())
            return {"cancelled": False, "files": [(str(path), "video")],
                    "info": {}, "error": None,
                    "hash": file_hash(path)}

        with mock.patch("app.downloader.download", side_effect=fake):
            self.api.repair_broken()
            self.assertTrue(self.api.dl.wait(timeout=15), "воркер не отработал")

        self.assertTrue(seen.get("overwrite"),
                        "битый файл качнулся бы мимо, без перезаписи")
        self.assertEqual(self.row()["status"], "downloaded")
        self.assertIsNone(self.row()["last_error"], "причина должна сняться")


class TestThumb(MarksCase):
    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
           b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89")

    def test_missing_thumb_reports_reason(self):
        result = self.api.get_thumb({"id": self.vid})
        self.assertFalse(result["ok"])
        self.assertIn("не скачана", result["reason"])

    def test_local_thumb_becomes_data_uri(self):
        path = self.dir / "lib" / "обложка.png"
        path.write_bytes(self.PNG)
        repo.record_file(self.conn, self.vid, str(path), "thumbnail",
                         size=len(self.PNG), mtime=1.0)
        result = self.api.get_thumb({"id": self.vid})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["data"].startswith("data:image/png;base64,"))
        self.assertGreater(len(result["data"]), 40)

    def test_bad_id_is_rejected(self):
        self.assertFalse(self.api.get_thumb({"id": "не-число"})["ok"])
        self.assertFalse(self.api.get_thumb({})["ok"])


if __name__ == "__main__":
    unittest.main()
