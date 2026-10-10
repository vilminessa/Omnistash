"""Переупаковка: план по шаблону, переименование, конфликты, отмена.

Главная гарантия: строка в базе никогда не указывает на несуществующий
файл - либо всё переименовалось, либо видео откатилось обратно.
"""

import hashlib
import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import aggregate, repack, repo
from app.indexer import SIDECAR_SUFFIX
from tests.test_gui import GuiCase

TEMPLATE = "%(channel)s/%(upload_date)s - %(title)s [%(id)s].%(ext)s"


class RepackCase(GuiCase):
    def setUp(self):
        super().setUp()
        self.api = self.make_api()
        self.conn = self.api.db.conn
        self.root = self.dir / "lib"
        self.root.mkdir()
        self.storage = self.add_storage(self.api, self.root)
        self.api.save_setting({"key": "output_template", "value": TEMPLATE})

    def make_video(self, remote_id="vid000000001", title="Ролик",
                   date="20250101", channel="Автор", platform="youtube",
                   subdir="", with_sidecar=True, payload=None):
        """Видео с файлом (и сайдкаром) прямо в корне хранилища."""
        info = {"id": remote_id, "title": title, "upload_date": date,
                "ext": "mp4"}
        if channel:
            info["channel"] = channel
        data = {
            "platform": platform, "remote_id": remote_id,
            "key": f"{platform}:{remote_id}", "title": title,
            "uploaded_at": date, "raw_json": json.dumps(info, ensure_ascii=False),
            "origin": "yt-dlp",
        }
        if channel and platform == "youtube":
            # Каждому названию - свой id: иначе второй видео затёр бы
            # канал первого, и выбор по области сломался бы.
            digest = hashlib.sha1(channel.encode("utf-8")).hexdigest()[:22]
            data["channel"] = {"platform": "youtube", "remote_id": "UC" + digest,
                               "title": channel, "handle": None, "url": None}
        vid, _ = repo.upsert_video(self.conn, data)

        folder = self.root / subdir if subdir else self.root
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{title} [{remote_id}]"
        media = folder / f"{stem}.mp4"
        body = payload or ("payload-" + remote_id).encode()
        media.write_bytes(body)
        repo.record_file(self.conn, vid, str(media), "video",
                         size=len(body), mtime=1.0)
        if with_sidecar:
            sidecar = folder / (stem + SIDECAR_SUFFIX)
            sidecar.write_text(json.dumps(
                {"omnistash": 1, "path": str(media), "remote_id": remote_id},
                ensure_ascii=False), encoding="utf-8")
            repo.record_file(self.conn, vid, str(sidecar), "sidecar",
                             size=20, mtime=1.0)
        return vid

    def pool(self):
        return {"scope": {"type": "pool"}}


class TestPlan(RepackCase):
    def test_renames_by_template_with_subfolder(self):
        self.make_video(title="Ролик", date="20250101", channel="Автор")
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        self.assertNotIn("error", plan)
        self.assertEqual(plan["selected"], 1)
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["unchanged"], 0)
        expected = str(self.root / "Автор" /
                       "20250101 - Ролик [vid000000001].mp4")
        self.assertEqual(plan["rename"][0]["to"], expected)
        self.assertEqual(plan["rename"][0]["from"],
                         str(self.root / "Ролик [vid000000001].mp4"))

    def test_channel_comes_from_database_when_raw_json_silent(self):
        # raw_json бывает «молчит» о канале (плоские записи) - тогда берём
        # название из колонки, а не выдумываем NA.
        vid = self.make_video(channel="Автор")
        self.conn.execute(
            "UPDATE videos SET raw_json=? WHERE id=?",
            (json.dumps({"id": "vid000000001", "title": "Ролик",
                         "upload_date": "20250101", "ext": "mp4"},
                        ensure_ascii=False), vid))
        row = dict(self.conn.execute(
            "SELECT v.*, c.title AS channel FROM videos v "
            "LEFT JOIN channels c ON c.id=v.channel_id WHERE v.id=?",
            (vid,)).fetchone())
        self.assertNotIn("channel", json.loads(row["raw_json"]))
        info = repack._info_for(row, "mp4")
        self.assertEqual(info.get("channel"), "Автор")

    def test_local_video_is_skipped_with_reason(self):
        self.make_video(platform="local", remote_id="abcdef12345",
                        channel=None, title="Файл без метаданных")
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        self.assertEqual(plan["count"], 0)
        self.assertEqual(plan["no_meta_total"], 1)
        self.assertIn("локальный", plan["no_meta"][0]["reason"])

    def test_missing_field_is_reported_not_guessed(self):
        # Шаблон хочет upload_date, а его нет: NA в имени мы не пишем.
        self.make_video(date="", channel="Автор")
        self.conn.execute("UPDATE videos SET uploaded_at=NULL")
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        self.assertEqual(plan["count"], 0)
        self.assertIn("upload_date", plan["no_meta"][0]["reason"])

    def test_conflict_when_target_taken(self):
        self.make_video(title="Ролик", date="20250101", channel="Автор")
        target_dir = self.root / "Автор"
        target_dir.mkdir()
        (target_dir / "20250101 - Ролик [vid000000001].mp4").write_bytes(
            "чужое".encode())
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        self.assertEqual(plan["count"], 0)
        self.assertEqual(plan["conflict_total"], 1)

    def test_empty_and_fieldless_templates_rejected(self):
        self.make_video()
        self.assertIn("error", repack.plan_repack(self.conn, self.pool(), ""))
        self.assertIn("полей", repack.plan_repack(self.conn, self.pool(),
                                                  "просто текст").get("error", ""))

    def test_selection_by_channel_scope(self):
        first = self.make_video(remote_id="vid000000001", channel="Первый")
        self.make_video(remote_id="vid000000002", channel="Второй")
        channel_id = self.conn.execute(
            "SELECT id FROM channels WHERE title='Первый'").fetchone()["id"]
        plan = repack.plan_repack(self.conn,
                                  {"scope": {"type": "channel", "id": channel_id}},
                                  TEMPLATE)
        self.assertEqual(plan["selected"], 1, plan["selected"])
        self.assertEqual(plan["rename"][0]["video_id"], first)


class TestApply(RepackCase):
    def test_renames_file_sidecar_and_rows(self):
        vid = self.make_video(title="Ролик", date="20250101", channel="Автор")
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        result = repack.apply_repack(self.conn, plan)

        self.assertEqual(result["renamed"], 1, result)
        self.assertEqual(result["errors"], [])
        new = self.root / "Автор" / "20250101 - Ролик [vid000000001].mp4"
        new_sidecar = self.root / "Автор" / ("20250101 - Ролик [vid000000001]"
                                             + SIDECAR_SUFFIX)
        self.assertTrue(new.exists(), "видео не переименовалось")
        self.assertTrue(new_sidecar.exists(), "сайдкар не поехал с видео")
        self.assertFalse((self.root / "Ролик [vid000000001].mp4").exists())

        row = self.conn.execute(
            "SELECT path, rel_path, storage_id FROM files WHERE video_id=? "
            "AND kind='video'", (vid,)).fetchone()
        self.assertEqual(row["path"], str(new))
        self.assertEqual(row["storage_id"], self.storage["id"])
        self.assertEqual(row["rel_path"],
                         "Автор" + "\\" + "20250101 - Ролик [vid000000001].mp4")

        # Сайдкар ведёт себя правдиво.
        data = json.loads(new_sidecar.read_text(encoding="utf-8"))
        self.assertEqual(data["path"], str(new))
        # Корень хранилища не тронут (иначе папка-хранилище удалилась бы).
        self.assertTrue(self.root.exists())

    def test_empty_old_subfolders_are_removed(self):
        self.make_video(title="Ролик", date="20250101", channel="Автор",
                        subdir="старая-папка")
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        repack.apply_repack(self.conn, plan)
        self.assertFalse((self.root / "старая-папка").exists(),
                         "освободившаяся папка должна уйти")
        self.assertTrue(self.root.exists(), "корень хранилища обязан остаться")

    def test_second_run_changes_nothing(self):
        self.make_video(title="Ролик", date="20250101", channel="Автор")
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        repack.apply_repack(self.conn, plan)

        again = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        self.assertEqual(again["count"], 0)
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(again["rename"], [])

    def test_cancel_moves_nothing(self):
        self.make_video(title="Ролик", date="20250101", channel="Автор")
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(repack.RepackCancelled):
            repack.apply_repack(self.conn, plan, stop=stop)
        # Ни файл, ни строка не тронуты.
        self.assertTrue((self.root / "Ролик [vid000000001].mp4").exists())
        row = self.conn.execute(
            "SELECT path FROM files WHERE kind='video'").fetchone()
        self.assertEqual(row["path"],
                         str(self.root / "Ролик [vid000000001].mp4"))
        self.assertTrue(Path(row["path"]).exists())

    def test_failed_rename_rolls_back_that_video(self):
        self.make_video(title="Ролик", date="20250101", channel="Автор")
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        # Отказ на втором шаге (сайдкар): видео уже переименовано, и именно
        # его обязан вернуть откат. Отказываем только в точке сбоя,
        # откат должен пройти.
        real_move = repack._move
        calls = []

        def flaky_move(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise OSError("диск отвалился")
            real_move(src, dst)

        with mock.patch("app.repack._move", side_effect=flaky_move):
            result = repack.apply_repack(self.conn, plan)

        self.assertEqual(result["renamed"], 0, result)
        self.assertEqual(len(result["errors"]), 1, result["errors"])
        # Откат: файл вернулся, строка указывает на существующий путь.
        old = self.root / "Ролик [vid000000001].mp4"
        self.assertTrue(old.exists(), "файл не вернулся после отказа")
        sidecar = self.root / ("Ролик [vid000000001]" + SIDECAR_SUFFIX)
        self.assertTrue(sidecar.exists(), "сайдкар не должен был сдвинуться")
        row = self.conn.execute(
            "SELECT path FROM files WHERE kind='video'").fetchone()
        self.assertEqual(row["path"], str(old))


class TestApiRepack(RepackCase):
    def test_preview_and_start_through_api(self):
        self.make_video(title="Ролик", date="20250101", channel="Автор")
        preview = self.api.repack_preview({"scope": {"type": "pool"}})
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(preview["count"], 1)
        self.assertEqual(preview["rename_total"], 1)
        self.assertIn("%(channel)s", preview["template"])

        started = self.api.repack_start({"scope": {"type": "pool"}})
        self.assertTrue(started.get("ok"), started)
        thread = self.api._repack_thread
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())

        state = self.api.poll(0)["repack"]
        self.assertFalse(state["running"])
        self.assertIn("Переименовано 1", state["summary"])
        self.assertTrue((self.root / "Автор" /
                         "20250101 - Ролик [vid000000001].mp4").exists())

    def test_start_when_nothing_to_do(self):
        self.make_video(title="Ролик", date="20250101", channel="Автор")
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        repack.apply_repack(self.conn, plan)
        result = self.api.repack_start({"scope": {"type": "pool"}})
        self.assertIn("Нечего переупаковывать", result["error"])

    def test_preview_of_empty_selection(self):
        result = self.api.repack_preview({"scope": {"type": "pool"}})
        self.assertIn("error", result)


class TestAggregateFollows(RepackCase):
    """Агрегат папки (.omnistash.json): запись живёт вместе с видео.

    Запись хранит только имя файла, поэтому переименование в той же
    папке правит одно поле, а смена папки - переносит запись целиком.
    """

    def _record(self, remote_id, name):
        return {"omnistash": 1, "platform": "youtube",
                "remote_id": remote_id, "path": name,
                "hash": "sha256:aa",
                "info": {"id": remote_id, "title": "Ролик"}}

    def test_record_moves_to_new_folder(self):
        self.make_video(title="Ролик", date="20250101", channel="Автор",
                        with_sidecar=False)
        aggregate.write_record(self.root, "vid000000001",
                               self._record("vid000000001",
                                            "Ролик [vid000000001].mp4"))
        # Сосед по папке, который никуда не едет.
        aggregate.write_record(self.root, "vid000000002",
                               self._record("vid000000002", "Сосед.mp4"))

        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        result = repack.apply_repack(self.conn, plan)
        self.assertEqual(result["renamed"], 1, result)
        self.assertEqual(result["errors"], [])

        new_folder = self.root / "Автор"
        record = aggregate.find(new_folder, "vid000000001")
        self.assertIsNotNone(record, "запись не переехала в новую папку")
        self.assertEqual(record["path"],
                         "20250101 - Ролик [vid000000001].mp4")
        self.assertIsNone(aggregate.find(self.root, "vid000000001"))
        self.assertIsNotNone(
            aggregate.find(self.root, "vid000000002"),
            "чужая запись обязана остаться в исходной папке")

    def test_record_path_updated_on_rename_in_place(self):
        self.make_video(title="Ролик", date="20250101", channel=None,
                        with_sidecar=False)
        aggregate.write_record(self.root, "vid000000001",
                               self._record("vid000000001",
                                            "Ролик [vid000000001].mp4"))
        template = "%(title)s v2 [%(id)s].%(ext)s"
        plan = repack.plan_repack(self.conn, self.pool(), template)
        result = repack.apply_repack(self.conn, plan)
        self.assertEqual(result["renamed"], 1, result)

        record = aggregate.find(self.root, "vid000000001")
        self.assertEqual(record["path"], "Ролик v2 [vid000000001].mp4")
        self.assertTrue((self.root / "Ролик v2 [vid000000001].mp4").exists())

    def test_without_record_is_noop(self):
        # Файловый режим (записи нет): переупаковка работает как раньше.
        self.make_video(title="Ролик", date="20250101", channel="Автор")
        plan = repack.plan_repack(self.conn, self.pool(), TEMPLATE)
        result = repack.apply_repack(self.conn, plan)
        self.assertEqual(result["renamed"], 1, result)
        self.assertEqual(result["errors"], [])
        self.assertFalse((self.root / aggregate.FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
