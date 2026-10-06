"""Перенос между хранилищами: план, копия с проверкой, отмена, коллизии.

Операция уничтожает данные (оригинал удаляется), поэтому тесты проверяют
именно безопасность: при отказе на любом шаге источник цел, а строка в
базе не указывает на несуществующий файл.
"""

import json
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import migrate, repo
from app.indexer import SIDECAR_SUFFIX
from tests.test_gui import GuiCase


class MigrateCase(GuiCase):
    def setUp(self):
        super().setUp()
        self.api = self.make_api()
        self.conn = self.api.db.conn
        (self.dir / "A").mkdir()
        (self.dir / "B").mkdir()
        self.storage_a = self.add_storage(self.api, self.dir / "A")
        self.storage_b = self.add_storage(self.api, self.dir / "B")

    def make_library(self, payload: bytes = b"video-content") -> dict:
        """Видео + сайдкар в хранилище A (структура «канал/файл»)."""
        vid, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": "vid000000001",
            "key": "youtube:vid000000001", "title": "Клип",
            "raw_json": "{}", "origin": "yt-dlp"})
        folder = self.dir / "A" / "Автор"
        folder.mkdir(parents=True, exist_ok=True)
        media = folder / "Клип [vid000000001].mp4"
        media.write_bytes(payload)
        sidecar = folder / ("Клип [vid000000001]" + SIDECAR_SUFFIX)
        sidecar.write_text(json.dumps({"omnistash": 1, "path": str(media),
                                       "remote_id": "vid000000001",
                                       "info": {"id": "vid000000001"}},
                                      ensure_ascii=False), encoding="utf-8")
        repo.record_file(self.conn, vid, str(media), "video",
                         size=len(payload), mtime=1.0, digest="sha256:aa")
        repo.record_file(self.conn, vid, str(sidecar), "sidecar",
                         size=20, mtime=1.0)
        return {"vid": vid, "media": media, "sidecar": sidecar}


class TestPlan(MigrateCase):
    def test_plan_counts_and_targets(self):
        made = self.make_library()
        plan = migrate.plan_move(self.conn, [made["vid"]], self.storage_b)
        self.assertEqual(plan["count"], 2)          # видео + сайдкар
        self.assertEqual(plan["errors"] if "errors" in plan else [], [])
        self.assertGreater(plan["bytes"], 0)
        self.assertEqual(plan["target"]["id"], self.storage_b["id"])
        # Структура сохраняется: Автор/Клип ... под B, а не плоско.
        for item in plan["files"]:
            self.assertTrue(item["dst"].startswith(self.storage_b["path"]),
                            item["dst"])
            self.assertIn("Автор", item["rel"])

    def test_plan_detects_conflict_and_already(self):
        made = self.make_library()
        rel = "Автор" + "\\" + made["media"].name
        target_dir = self.dir / "B" / "Автор"
        target_dir.mkdir(parents=True, exist_ok=True)

        # Чужой файл того же имени и другого размера -> конфликт.
        (target_dir / made["media"].name).write_bytes(b"other-content-here")
        # Сайдкар уже лежит в цели -> «уже там» (содержимое то же).
        (target_dir / made["sidecar"].name).write_bytes(
            made["sidecar"].read_bytes())

        plan = migrate.plan_move(self.conn, [made["vid"]], self.storage_b)
        self.assertEqual(len(plan["conflicts"]), 1)
        self.assertEqual(plan["conflicts"][0]["target"].split("\\")[-1],
                         made["media"].name)
        self.assertEqual(len(plan["already"]), 1)
        self.assertEqual(plan["count"], 0, "конфликтный файл нельзя двигать")
        self.assertEqual(rel.split("\\")[0], "Автор")

    def test_plan_reports_missing_source(self):
        made = self.make_library()
        made["media"].unlink()
        plan = migrate.plan_move(self.conn, [made["vid"]], self.storage_b)
        self.assertEqual(len(plan["missing"]), 1)
        self.assertEqual(plan["count"], 1, "сайдкар всё ещё должен переехать")

    def test_plan_skips_files_already_in_target(self):
        made = self.make_library()
        # Всё уже в B: план пуст.
        self.conn.execute("UPDATE files SET storage_id=? WHERE video_id=?",
                          (self.storage_b["id"], made["vid"]))
        plan = migrate.plan_move(self.conn, [made["vid"]], self.storage_b)
        self.assertEqual(plan["count"], 0)
        self.assertEqual(plan["same_storage"], 2)

    def test_plan_rejects_empty_selection(self):
        plan = migrate.plan_move(self.conn, [], self.storage_b)
        self.assertIn("Не выбраны", plan["error"])


class TestMove(MigrateCase):
    def test_move_updates_rows_and_removes_source(self):
        made = self.make_library(b"unique-bytes-here")
        plan = migrate.plan_move(self.conn, [made["vid"]], self.storage_b)
        result = migrate.move_files(self.conn, plan, self.storage_b)

        self.assertEqual(result["done"], 2, result)
        self.assertEqual(result["errors"], [])
        # Источника нет, цель цела, строка указывает на новый путь.
        self.assertFalse(made["media"].exists())
        self.assertFalse(made["sidecar"].exists())
        moved = self.dir / "B" / "Автор" / made["media"].name
        self.assertTrue(moved.exists())
        self.assertEqual(moved.read_bytes(), b"unique-bytes-here")

        row = self.conn.execute(
            "SELECT path, storage_id, rel_path, hash FROM files WHERE video_id=? "
            "AND kind='video'", (made["vid"],)).fetchone()
        self.assertEqual(row["path"], str(moved))
        self.assertEqual(row["storage_id"], self.storage_b["id"])
        self.assertEqual(row["rel_path"],
                         "Автор" + "\\" + made["media"].name)
        # Хеш сверяется с тем, что реально в цели: устаревшее значение из
        # БД (в тесте - «sha256:aa») заменяется настоящим.
        self.assertEqual(row["hash"], migrate.file_hash(moved))

    def test_move_rewrites_sidecar_path(self):
        made = self.make_library()
        plan = migrate.plan_move(self.conn, [made["vid"]], self.storage_b)
        migrate.move_files(self.conn, plan, self.storage_b)

        moved_sidecar = self.dir / "B" / "Автор" / made["sidecar"].name
        data = json.loads(moved_sidecar.read_text(encoding="utf-8"))
        expected = str(self.dir / "B" / "Автор" / made["media"].name)
        self.assertEqual(data["path"], expected,
                         "в сайдкарe остался старый путь")

    def test_cancel_keeps_source_and_target_consistent(self):
        made = self.make_library()
        # Второй файл: чтобы отмена случилась между файлами.
        vid2, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": "vid000000002",
            "key": "youtube:vid000000002", "title": "Второй",
            "raw_json": "{}", "origin": "yt-dlp"})
        other = self.dir / "A" / "второй.mp4"
        other.write_bytes(b"second-bytes")
        repo.record_file(self.conn, vid2, str(other), "video",
                         size=12, mtime=1.0, digest="sha256:bb")

        plan = migrate.plan_move(self.conn, [made["vid"], vid2],
                                 self.storage_b)
        # порядок: файл из первого видео (видео, сайдкар), затем второй
        stop = threading.Event()
        seen = []

        def progress(done_bytes, total_bytes, files_done, total_files, path):
            seen.append(files_done)
            if files_done >= 1:
                stop.set()          # «Стоп» после первого перенесённого файла

        with self.assertRaises(migrate.MigrateCancelled):
            migrate.move_files(self.conn, plan, self.storage_b,
                               stop=stop, progress=progress)

        # Что-то уже в цели и удалено из источника, остальное цело.
        self.assertTrue((self.dir / "B").exists())
        remaining = [p.name for p in (self.dir / "A").rglob("*") if p.is_file()]
        self.assertTrue(remaining, "после отмены источник не должен опустеть")
        # Строка каждой сдвинутой записи указывает на существующий файл.
        for row in self.conn.execute(
                "SELECT path FROM files WHERE storage_id=?",
                (self.storage_b["id"],)):
            self.assertTrue(Path(row["path"]).exists(), row["path"])

    def test_checksum_mismatch_leaves_source_alone(self):
        made = self.make_library(b"payload-for-checksum")
        plan = migrate.plan_move(self.conn, [made["vid"]], self.storage_b)
        real_hash = migrate.file_hash
        target_prefix = str(self.storage_b["path"])

        def lying_hash(path):
            digest = real_hash(path)
            if str(path).startswith(target_prefix):
                return "sha256:" + "00" * 32      # ложим сверку цели
            return digest

        with mock.patch("app.migrate.file_hash", side_effect=lying_hash):
            result = migrate.move_files(self.conn, plan, self.storage_b)

        self.assertEqual(result["done"], 0, result)
        self.assertTrue(any("контрольная сумма" in e for e in result["errors"]),
                        result["errors"])
        self.assertTrue(made["media"].exists(), "источник удалили при провале")
        row = self.conn.execute(
            "SELECT path, storage_id FROM files WHERE video_id=? AND kind='video'",
            (made["vid"],)).fetchone()
        self.assertEqual(row["path"], str(made["media"]),
                         "строка не должна переключиться на несуществующий файл")
        self.assertEqual(row["storage_id"], self.storage_a["id"])
        # Недокопированная цель подчищена.
        self.assertFalse(
            (self.dir / "B" / "Автор" / made["media"].name).exists())

    def test_progress_reports_bytes(self):
        made = self.make_library(b"x" * 5000)
        plan = migrate.plan_move(self.conn, [made["vid"]], self.storage_b)
        seen = []

        def progress(done_bytes, total_bytes, files_done, total_files, path):
            seen.append((done_bytes, total_bytes, files_done, total_files))

        migrate.move_files(self.conn, plan, self.storage_b, progress=progress)
        self.assertTrue(seen, "прогресс не отдавался")
        self.assertEqual(seen[-1][1], plan["bytes"])
        self.assertEqual(seen[-1][3], plan["count"])
        self.assertEqual(seen[-1][0], plan["bytes"], "байты не сошлись")


class TestApiMigrate(MigrateCase):
    def test_preview_and_start_and_stop(self):
        made = self.make_library()
        preview = self.api.migrate_preview({
            "video_ids": [made["vid"]],
            "target_storage_id": self.storage_b["id"]})
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(preview["count"], 2)

        started = self.api.migrate_start({
            "video_ids": [made["vid"]],
            "target_storage_id": self.storage_b["id"]})
        self.assertTrue(started.get("ok"), started)
        thread = self.api._migrate_thread
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())

        state = self.api.poll(0)["migrate"]
        self.assertFalse(state["running"])
        self.assertEqual(state["done"], 2)
        self.assertIn("Перенесено 2", state["summary"])

    def test_start_without_target_is_error(self):
        made = self.make_library()
        result = self.api.migrate_start({"video_ids": [made["vid"]]})
        self.assertIn("не найдено", result["error"])

    def test_start_nothing_to_move(self):
        made = self.make_library()
        self.conn.execute("UPDATE files SET storage_id=? WHERE video_id=?",
                          (self.storage_b["id"], made["vid"]))
        result = self.api.migrate_start({
            "video_ids": [made["vid"]],
            "target_storage_id": self.storage_b["id"]})
        self.assertIn("Нечего переносить", result["error"])


if __name__ == "__main__":
    unittest.main()
