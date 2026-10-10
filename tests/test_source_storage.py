"""«Куда качать» у источника: диалог -> playlists.storage_id -> очередь.

Фон не решает сам: выбор делает пользователь при добавлении, цепочка
resolve_target (источник -> канал -> глобальное) читает его при каждой
загрузке, а режим «Полная» и синк ставят строки в выбранную папку.
Колонка playlists.storage_id существовала с версии v2, но её никто не
писал - выбор молча подменялся глобальным хранилищем.
"""

import unittest
from pathlib import Path
from unittest import mock

from app import repo
from app import storages
from tests.test_add_flow import (PLAYLIST_URL, AddFlowCase, _FakeWorker,
                                 fixture_snapshot)
from tests.test_repo import RepoCase, entry, snapshot
from tests.test_sync import SyncCase


class StorageRepoCase(RepoCase):
    def make_storage(self, label):
        folder = Path(self._tmp.name) / label
        folder.mkdir(parents=True, exist_ok=True)
        result = storages.add(self.conn, str(folder), label=label)
        self.assertTrue(result.get("id"), result)
        return result["id"]

    def commit(self, snap, storage_id=None):
        return repo.commit_plan(self.conn, snap,
                                repo.plan_diff(self.conn, snap),
                                storage_id=storage_id)

    def playlist_storage(self):
        row = self.conn.execute("SELECT storage_id FROM playlists").fetchone()
        return row["storage_id"] if row else "<плейлиста нет>"


class TestCommitPlanStorage(StorageRepoCase):
    def test_choice_is_written(self):
        storage_id = self.make_storage("внешний")
        stats = self.commit(snapshot([entry("vid000000001", "Один", 1)]),
                            storage_id=storage_id)
        self.assertTrue(stats["playlist_id"])
        self.assertEqual(self.playlist_storage(), storage_id)

    def test_cancel_does_not_leave_choice(self):
        storage_id = self.make_storage("внешний")
        snap = snapshot([entry("vid000000001", "Один", 1)])

        def boom(name, state, current, total):
            if name == "links":
                raise RuntimeError("отмена")

        with self.assertRaises(RuntimeError):
            repo.commit_plan(self.conn, snap,
                             repo.plan_diff(self.conn, snap),
                             on_stage=boom, storage_id=storage_id)
        self.assertEqual(self.counts("playlists"), 0,
                         "отмена не должна оставить ни плейлиста, ни выбора")
        self.assertEqual(self.counts("videos"), 0)

    def test_reindex_without_choice_keeps_stored(self):
        storage_id = self.make_storage("внешний")
        snap = snapshot([entry("vid000000001", "Один", 1)])
        self.commit(snap, storage_id=storage_id)
        # Повторная индексация без явного выбора не должна стирать прежний:
        # None означает «не менял», а не «снять назначение».
        self.commit(snap)
        self.assertEqual(self.playlist_storage(), storage_id)


class TestSourcesCarryStorage(StorageRepoCase):
    def test_sources_include_storage_and_label(self):
        storage_id = self.make_storage("внешний")
        self.commit(snapshot([entry("vid000000001", "Один", 1)]),
                    storage_id=storage_id)

        source = repo.sources(self.conn)[0]
        self.assertEqual(source["storage_id"], storage_id)
        self.assertEqual(source["storage_label"], "внешний")

    def test_sources_without_choice_are_none(self):
        self.commit(snapshot([entry("vid000000001", "Один", 1)]))
        source = repo.sources(self.conn)[0]
        # Пусто - честное «по умолчанию»: resolve_target допьёт глобальное.
        self.assertIsNone(source["storage_id"])
        self.assertIsNone(source["storage_label"])


class TestResolveChain(StorageRepoCase):
    def test_playlist_choice_beats_default(self):
        default_id = self.make_storage("глобальный")
        chosen_id = self.make_storage("внешний")
        settings = {"default_storage_id": default_id}
        stats = self.commit(snapshot([entry("vid000000001", "Один", 1)]),
                            storage_id=chosen_id)
        video_id = self.conn.execute("SELECT id FROM videos").fetchone()["id"]

        # Цепочку зовут и по плейлисту, и по видео (так делает очередь).
        for kwargs in ({"playlist_id": stats["playlist_id"]},
                       {"video_id": video_id}):
            resolved = storages.resolve_target(self.conn, settings, **kwargs)
            self.assertEqual(resolved["id"], chosen_id,
                             f"выбор источника должен побеждать: {kwargs}")

    def test_no_choice_falls_to_default(self):
        default_id = self.make_storage("глобальный")
        self.commit(snapshot([entry("vid000000001", "Один", 1)]))
        stats_pid = self.conn.execute(
            "SELECT id FROM playlists").fetchone()["id"]
        resolved = storages.resolve_target(
            self.conn, {"default_storage_id": default_id},
            playlist_id=stats_pid)
        self.assertEqual(resolved["id"], default_id)


class AddStorageCase(AddFlowCase):
    """Сквозной флоу добавления с выбором; качалка заглушена."""

    def setUp(self):
        super().setUp()
        fake = _FakeWorker()
        patcher = mock.patch("app.gui.DownloadWorker",
                             side_effect=lambda *a, **k: fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.worker = fake

    def make_storage(self, api, label):
        folder = Path(self.dir) / label
        folder.mkdir(parents=True, exist_ok=True)
        result = storages.add(api.db.conn, str(folder), label=label)
        self.assertTrue(result.get("id"), result)
        return result["id"]


class TestAddFlowStorage(AddStorageCase):
    def test_choice_reaches_playlist_and_queue(self):
        api = self.api()
        storage_id = self.make_storage(api, "внешний")
        self.patch_fetch(fixture_snapshot())

        self.assertTrue(api.add_start({"url": PLAYLIST_URL, "mode": "full",
                                       "storage_id": storage_id})["ok"])
        self.wait_phase(api, "confirm")
        self.assertTrue(api.add_confirm({"mode": "full",
                                         "storage_id": storage_id})["ok"])
        flow = self.wait_phase(api, "done")
        result = flow["result"]
        self.assertEqual(result["storage_id"], storage_id)
        self.assertEqual(result["storage_label"], "внешний")

        row = api.db.conn.execute("SELECT storage_id FROM playlists").fetchone()
        self.assertEqual(row["storage_id"], storage_id)
        # Режим «Полная»: строки в очереди получили прямую цель.
        targets = [r["target_storage_id"] for r in api.db.conn.execute(
            "SELECT target_storage_id FROM videos WHERE status='queued'")]
        self.assertTrue(targets, "«Полная» должна была поставить строки в очередь")
        self.assertEqual(targets, [storage_id] * len(targets))
        self.assertGreaterEqual(self.worker.started, 1)

    def test_without_choice_queued_rows_have_no_direct_target(self):
        # Честный прежний путь: цель не записана, resolve_target допьёт
        # глобальное хранилище на старте загрузки.
        api = self.api()
        self.patch_fetch(fixture_snapshot())
        api.add_start({"url": PLAYLIST_URL, "mode": "full"})
        self.wait_phase(api, "confirm")
        api.add_confirm({"mode": "full"})
        result = self.wait_phase(api, "done")["result"]
        self.assertIsNone(result["storage_id"])
        targets = [r["target_storage_id"] for r in api.db.conn.execute(
            "SELECT target_storage_id FROM videos WHERE status='queued'")]
        self.assertTrue(targets)
        self.assertEqual(set(targets), {None})

    def test_unknown_storage_is_rejected_early(self):
        api = self.api()
        self.patch_fetch(fixture_snapshot())
        result = api.add_start({"url": PLAYLIST_URL,
                                "storage_id": "st_нет-такого"})
        self.assertIn("error", result)
        self.assertIn("не найдено", result["error"])
        self.assertEqual(api.poll(0)["add_flow"]["phase"], "idle")


class TestSyncUsesSourceChoice(SyncCase):
    def make_storage(self, label):
        folder = Path(self.dir) / label
        folder.mkdir(parents=True, exist_ok=True)
        result = storages.add(self.api.db.conn, str(folder), label=label)
        return result["id"]

    def test_full_sync_targets_source_storage(self):
        storage_id = self.make_storage("внешний")
        self.fetch_result = fixture_snapshot(count=2)
        self.api.add_start({"url": PLAYLIST_URL, "mode": "manual",
                            "storage_id": storage_id})
        self.wait_phase(self.api, "confirm")
        self.api.add_confirm({"storage_id": storage_id})
        self.wait_phase(self.api, "done")
        self.api.add_close()

        result = self.sync(fixture_snapshot(count=3), mode="full")

        row = result["results"][0]
        self.assertIsNone(row["error"])
        self.assertGreaterEqual(row["queued"], 1,
                                "«Полная» должна сама ставить новые в очередь")
        targets = [r["target_storage_id"] for r in self.api.db.conn.execute(
            "SELECT target_storage_id FROM videos WHERE status='queued'")]
        self.assertTrue(targets)
        self.assertEqual(targets, [storage_id] * len(targets),
                         "синк обязан качать в папку источника, а не глобальную")


if __name__ == "__main__":
    unittest.main()
