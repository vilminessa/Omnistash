"""Сверка: копии файлов, «возможный переезд», разбор и ручная привязка.

Главная аксиома M7: индекс не гадает. Если старое место проверить нельзя
(носитель отключён), файл получает собственную строку, а вопрос уходит
человеку - вместо молчаливого переезда, который оставил бы в базе путь
несуществующего файла.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from app import indexer, repo
from app.indexer import SIDECAR_SUFFIX, scan
from tests.test_gui import GuiCase


class DedupeCase(GuiCase):
    def setUp(self):
        super().setUp()
        self.api = self.make_api()
        self.conn = self.api.db.conn
        # Папки должны существовать ДО регистрации хранилища.
        (self.dir / "A").mkdir()
        (self.dir / "B").mkdir()
        self.storage_a = self.add_storage(self.api, self.dir / "A")
        self.storage_b = self.add_storage(self.api, self.dir / "B")

    def video(self, remote_id="vid000000001", title="Видео"):
        vid, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": remote_id,
            "key": f"youtube:{remote_id}", "title": title,
            "raw_json": "{}", "origin": "yt-dlp"})
        return vid

    def roots(self, *storages):
        return [dict(storage) for storage in storages]


class TestHashClassification(DedupeCase):
    def test_copy_gets_its_own_row(self):
        """Копия в другом хранилище не должна прятать оригинал."""
        vid = self.video(title="Клип")
        original = self.dir / "A" / "Клип [vid000000001].mp4"
        original.write_bytes(b"same-bytes-here")

        first = scan(self.roots(self.storage_a), self.api.db, compute_hash=True)
        self.assertEqual(first["bound_id"], 1, first)
        self.assertEqual(first["duplicates"], 0)

        # Сосед скопировал файл в своё хранилище (имя уже без ID).
        copy = self.dir / "B" / "Соседская копия.mp4"
        copy.write_bytes(b"same-bytes-here")

        second = scan(self.roots(self.storage_b), self.api.db, compute_hash=True)
        self.assertEqual(second["duplicates"], 1, second)
        self.assertEqual(second["possible_moves"], 0)
        self.assertEqual(second["rebound"], 0, "копию нельзя считать переездом")

        rows = [dict(r) for r in self.conn.execute(
            "SELECT path, storage_id, hash FROM files WHERE video_id=? "
            "AND kind='video' ORDER BY storage_id", (vid,))]
        self.assertEqual(len(rows), 2, rows)
        paths = {row["path"] for row in rows}
        self.assertIn(str(original), paths)
        self.assertIn(str(copy), paths)
        # Обе строки хранят один хеш - именно по нему их и найдут снова.
        self.assertEqual(len({row["hash"] for row in rows}), 1)
        self.assertIsNotNone(rows[0]["hash"], "хеш не записан: дедуп не найдёт пару")

    def test_unavailable_original_is_possible_move(self):
        """Носитель отключён: не гадаем, а спрашиваем."""
        vid = self.video(title="Клип")
        original = self.dir / "A" / "Клип [vid000000001].mp4"
        original.write_bytes(b"bytes-for-move")
        scan(self.roots(self.storage_a), self.api.db, compute_hash=True)

        # Диск отвалился: строка осталась, но проверить путь нельзя.
        self.conn.execute("UPDATE storages SET available=0 WHERE id=?",
                          (self.storage_a["id"],))
        moved = self.dir / "B" / "Переехало.mp4"
        moved.write_bytes(b"bytes-for-move")

        report = scan(self.roots(self.storage_b), self.api.db, compute_hash=True)
        self.assertEqual(report["possible_moves"], 1, report)
        self.assertEqual(report["duplicates"], 0)
        self.assertEqual(report["rebound"], 0, "нельзя переехать вслепую")

        detail = report["move_details"][0]
        self.assertEqual(detail["video_id"], vid)
        self.assertEqual(detail["path"], str(moved))
        self.assertEqual(detail["from"], self.storage_a["label"])
        # Обе записи живы: выбор за человеком.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE video_id=? AND kind='video'",
            (vid,)).fetchone()["n"], 2)

    def test_available_original_without_file_is_rebound(self):
        """Живое хранилище, а файла нет: это переезд, строка едет."""
        self.video(title="Клип")
        original = self.dir / "A" / "Клип [vid000000001].mp4"
        original.write_bytes(b"bytes-moved-away")
        scan(self.roots(self.storage_a), self.api.db, compute_hash=True)
        original.unlink()                      # файл ушёл, диск цел

        moved = self.dir / "B" / "Где-то теперь.mp4"
        moved.write_bytes(b"bytes-moved-away")
        report = scan(self.roots(self.storage_b), self.api.db, compute_hash=True)

        self.assertEqual(report["rebound"], 1, report)
        self.assertEqual(report["possible_moves"], 0)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT path FROM files WHERE kind='video'")]
        self.assertEqual(len(rows), 1, "строка должна была уехать, а не удвоиться")
        self.assertEqual(rows[0]["path"], str(moved))


class TestFindAndResolve(DedupeCase):
    def _pair(self):
        vid = self.video(title="Дважды")
        keep = self.dir / "A" / "основная.mp4"
        drop = self.dir / "B" / "копия.mp4"
        keep.write_bytes(b"main-content")
        drop.write_bytes(b"copy-content")
        repo.record_file(self.conn, vid, str(keep), "video", size=12, mtime=1.0,
                         digest="sha256:aa")
        repo.record_file(self.conn, vid, str(drop), "video", size=12, mtime=1.0,
                         digest="sha256:bb")
        # Сайдкар рядом с копией - он должен уехать вместе с ней.
        sidecar = drop.with_suffix(SIDECAR_SUFFIX)
        sidecar.write_text(json.dumps({"omnistash": 1, "remote_id": "vid000000001"}),
                           encoding="utf-8")
        repo.record_file(self.conn, vid, str(sidecar), "sidecar",
                         size=5, mtime=1.0)
        return vid, keep, drop, sidecar

    def test_find_duplicates_groups_files(self):
        vid, keep, drop, _sidecar = self._pair()
        groups = repo.find_duplicates(self.conn)
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["video_id"], vid)
        self.assertEqual(group["copies"], 2)
        self.assertEqual({f["path"] for f in group["files"]},
                         {str(keep), str(drop)})
        # Метка хранилища приходит вместе с файлом: понятно, где что лежит.
        self.assertTrue(all(f["storage"] for f in group["files"]), group["files"])

    def test_resolve_keeps_one_and_removes_sidecar_of_other(self):
        vid, keep, drop, sidecar = self._pair()
        result = repo.resolve_duplicates(self.conn, vid, self.conn.execute(
            "SELECT id FROM files WHERE path=?", (str(keep),)).fetchone()["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kept"], str(keep))
        self.assertTrue(keep.exists(), "оставленный файл удалился")
        self.assertFalse(drop.exists(), "копия осталась на диске")
        self.assertFalse(sidecar.exists(), "сайдкар сиротой не должен остаться")
        left = self.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE video_id=?", (vid,)).fetchone()["n"]
        self.assertEqual(left, 1, "строка сайдкара тоже должна уйти")
        status = self.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()["status"]
        self.assertEqual(status, "downloaded")

    def test_resolve_rejects_foreign_file(self):
        vid, _keep, _drop, _sidecar = self._pair()
        other = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": "vid000000002",
            "key": "youtube:vid000000002", "title": "Чужое",
            "raw_json": "{}", "origin": "yt-dlp"})[0]
        result = repo.resolve_duplicates(self.conn, vid, 999999)
        self.assertIn("оставить", result["error"])
        self.assertIsNotNone(other)

    def test_delete_failure_keeps_row(self):
        """Нельзя стереть строку, если файл стереть не удалось."""
        vid = self.video()
        path = self.dir / "A" / "занят.mp4"
        path.write_bytes(b"x")
        repo.record_file(self.conn, vid, str(path), "video", size=1, mtime=1.0)
        file_id = self.conn.execute(
            "SELECT id FROM files WHERE path=?", (str(path),)).fetchone()["id"]

        with mock.patch("app.repo.os.remove",
                        side_effect=OSError("занят другим процессом")):
            result = repo.delete_file_cascade(self.conn, file_id)
        self.assertIn("error", result)
        self.assertIsNotNone(self.conn.execute(
            "SELECT id FROM files WHERE id=?", (file_id,)).fetchone(),
            "строка пропала, хотя файл остался на диске")
        self.assertTrue(path.exists())


class TestRebind(GuiCase):
    def test_rebind_moves_file_to_known_video_and_drops_local_orphan(self):
        api = self.make_api()
        conn = api.db.conn
        target, _ = repo.upsert_video(conn, {
            "platform": "youtube", "remote_id": "vid000000001",
            "key": "youtube:vid000000001", "title": "Настоящее",
            "raw_json": "{}", "origin": "yt-dlp"})
        folder = self.dir / "lib"
        folder.mkdir()
        path = folder / "без имени.mp4"
        path.write_bytes(b"payload")
        local_id = repo.insert_local_video(conn, title="без имени",
                                           path=str(path), size=7, mtime=1.0,
                                           digest=None)
        file_id = conn.execute(
            "SELECT id FROM files WHERE path=?", (str(path),)).fetchone()["id"]

        result = repo.rebind_file(conn, file_id, target)
        self.assertTrue(result["ok"], result)
        # Файл теперь у настоящего видео, статус - скачано.
        row = conn.execute(
            "SELECT video_id FROM files WHERE id=?", (file_id,)).fetchone()
        self.assertEqual(row["video_id"], target)
        self.assertEqual(conn.execute(
            "SELECT status FROM videos WHERE id=?", (target,)).fetchone()["status"],
            "downloaded")
        # Локальное видео без файлов удалено: знать о нём больше нечего.
        self.assertIsNone(conn.execute(
            "SELECT id FROM videos WHERE id=?", (local_id,)).fetchone())

    def test_rebind_same_video_is_rejected(self):
        api = self.make_api()
        conn = api.db.conn
        folder = self.dir / "lib"
        folder.mkdir()
        path = folder / "a.mp4"
        path.write_bytes(b"x")
        vid = repo.insert_local_video(conn, title="a", path=str(path),
                                      size=1, mtime=1.0, digest=None)
        file_id = conn.execute(
            "SELECT id FROM files WHERE path=?", (str(path),)).fetchone()["id"]
        result = repo.rebind_file(conn, file_id, vid)
        self.assertIn("уже привязан", result["error"])


class TestEnsureHash(DedupeCase):
    def test_id_bound_file_gets_hash(self):
        """Без хеша в индексе поиск копий и переездов неработоспособен."""
        self.video(title="Клип")
        path = self.dir / "A" / "Клип [vid000000001].mp4"
        path.write_bytes(b"content-to-hash")
        report = scan(self.roots(self.storage_a), self.api.db, compute_hash=True)
        self.assertEqual(report["bound_id"], 1, report)
        row = self.conn.execute(
            "SELECT hash FROM files WHERE kind='video'").fetchone()
        self.assertTrue(row["hash"] and row["hash"].startswith("sha256:"),
                        "привязка по ID не записала хеш")

    def test_hash_skipped_when_disabled(self):
        self.video(title="Клип")
        path = self.dir / "A" / "Клип [vid000000001].mp4"
        path.write_bytes(b"content-to-hash")
        scan(self.roots(self.storage_a), self.api.db, compute_hash=False)
        row = self.conn.execute(
            "SELECT hash FROM files WHERE kind='video'").fetchone()
        self.assertIsNone(row["hash"])


if __name__ == "__main__":
    unittest.main()
