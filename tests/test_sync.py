"""Синхронизация источников: diff против уже известного, режимы, отмена.

Сеть подменяется фикстурой на всё время теста (не «на момент вызова»):
воркер синхронизации живёт в своём потоке и вполне может дойти до
fetch_snapshot уже после того, как «узкий» with-патч закрылся. Качалка
заглушена всегда - даже случайный старт не уйдёт в сеть.
"""

import unittest
from unittest import mock

from app import sources as sources_mod
from tests.test_add_flow import _FakeWorker, fixture_snapshot
from tests.test_gui import GuiCase

PLAYLIST_URL = "https://youtube.com/playlist?list=PLfixtur0000000000000000000001"


class SyncCase(GuiCase):
    def setUp(self):
        super().setUp()
        fake = _FakeWorker()
        patcher = mock.patch("app.gui.DownloadWorker",
                             side_effect=lambda *a, **k: fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.worker = fake

        # Поведение «площадки» задаётся атрибутами теста.
        self.fetch_result = None
        self.fetch_error = None
        self.fetch_delay = 0.0

        def fake_fetch(url, settings=None, on_progress=None, stop=None,
                       account_id=None):
            if self.fetch_delay:
                # Блокируемся до «Стоп» - так тест гарантированно успевает
                # нажать кнопку раньше площадки.
                if stop is not None and stop.wait(self.fetch_delay):
                    raise sources_mod.Aborted()
            if self.fetch_error is not None:
                raise self.fetch_error
            snapshot = self.fetch_result
            if snapshot is None:
                raise AssertionError("тест не выставил fetch_result")
            if on_progress:
                total = len(snapshot.get("entries") or [])
                on_progress(0, total)
                on_progress(total, total)
            return snapshot

        patcher = mock.patch("app.gui.sources.fetch_snapshot",
                             side_effect=fake_fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.api = self.make_api()

    # ------------------------------------------------------------------ #

    def add_source(self, snapshot, mode="manual"):
        """Занести источник так, как это делает диалог добавления."""
        self.fetch_result = snapshot
        self.api.add_start({"url": PLAYLIST_URL, "mode": mode})
        self.wait_phase(self.api, "confirm")
        self.api.add_confirm()
        self.wait_phase(self.api, "done")
        self.api.add_close()

    def sync(self, snapshot, mode=None):
        """Синкнуть источник новым снапшотом и дождаться результата."""
        self.fetch_result = snapshot
        if mode is not None:
            self.api.db.conn.execute(
                "UPDATE playlists SET sync_mode=? WHERE remote_id=?",
                (mode, snapshot["playlist"]["remote_id"]))
        started = self.api.sync_start()
        self.assertTrue(started.get("ok"), started)
        return self.wait_sync(self.api)


class TestSyncDiff(SyncCase):
    def test_new_entries_are_counted_and_written(self):
        self.add_source(fixture_snapshot(count=2))
        self.assertEqual(self.api.poll(0)["stats"]["total"], 2)

        result = self.sync(fixture_snapshot(count=3))   # появилось третье

        row = result["results"][0]
        self.assertEqual(row["new"], 1)
        self.assertEqual(row["known"], 2)
        self.assertEqual(row["removed"], 0)
        self.assertIsNone(row["error"])
        self.assertEqual(result["new_total"], 1)
        self.assertEqual(self.api.poll(0)["stats"]["total"], 3)

    def test_removed_entry_keeps_video_and_file_state(self):
        snap = fixture_snapshot(count=3)
        self.add_source(snap)
        first = self.api.db.conn.execute(
            "SELECT id FROM videos ORDER BY id LIMIT 1").fetchone()
        self.api.db.conn.execute(
            "UPDATE videos SET status='downloaded' WHERE id=?", (first["id"],))

        gone = snap["entries"][0]["remote_id"]
        smaller = fixture_snapshot(count=3)
        smaller["entries"] = [e for e in smaller["entries"]
                              if e["remote_id"] != gone]
        result = self.sync(smaller)

        row = result["results"][0]
        self.assertEqual(row["removed"], 1)
        self.assertEqual(row["new"], 0)
        # Исчезнуть из плейлиста - не значит удалиться у меня.
        still = self.api.db.conn.execute(
            "SELECT status FROM videos WHERE id=?", (first["id"],)).fetchone()
        self.assertEqual(still["status"], "downloaded")
        self.assertEqual(self.api.poll(0)["stats"]["total"], 3)
        removed = self.api.db.conn.execute(
            "SELECT COUNT(*) n FROM playlist_items WHERE removed_at IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(removed, 1)

    def test_sync_is_idempotent(self):
        snap = fixture_snapshot(count=3)
        self.add_source(snap)
        first = self.sync(snap)
        self.assertEqual(first["results"][0]["new"], 0)
        second = self.sync(snap)
        self.assertEqual(second["results"][0]["new"], 0)
        self.assertEqual(second["results"][0]["known"], 3)
        self.assertEqual(self.api.poll(0)["stats"]["total"], 3)


class TestSyncModes(SyncCase):
    def test_full_mode_queues_new_entries(self):
        self.add_source(fixture_snapshot(count=2))   # manual: их не качали
        result = self.sync(fixture_snapshot(count=4), mode="full")

        row = result["results"][0]
        self.assertEqual(row["new"], 2)
        self.assertEqual(row["known"], 2)
        # «Полная» догоняет ВСЁ ожидающее в источнике, а не только новое:
        # переключение режима означает «источник должен быть скачан целиком».
        self.assertEqual(row["queued"], 4,
                         "полный режим не поставил в очередь всё ожидающее")
        self.assertEqual(result["queued"], 4)
        self.assertEqual(self.api.poll(0)["stats"]["queued"], 4)
        # «Полная» обязана сама запустить качалку - иначе она ничего
        # не делает ни в окне, ни в --sync под планировщик.
        self.assertGreaterEqual(self.worker.started, 1,
                                "после синка качалка не запущена")

    def test_partial_mode_only_reports(self):
        self.add_source(fixture_snapshot(count=2))
        result = self.sync(fixture_snapshot(count=4), mode="partial")
        row = result["results"][0]
        self.assertEqual(row["new"], 2)
        self.assertEqual(row["queued"], 0, "частичный режим не должен качать сам")
        self.assertEqual(result["new_total"], 2)
        # Сам синк ничего не запускает - только кнопка «поставить в очередь».
        self.assertEqual(self.worker.started, 0)

        # Но кнопка после синка есть: «поставить новые в очередь».
        queued = self.api.sync_queue_new()
        self.assertEqual(queued["queued"], 2)
        self.assertGreaterEqual(self.worker.started, 1)
        self.assertEqual(self.api.poll(0)["stats"]["queued"], 2)

    def test_queue_new_says_when_nothing_to_do(self):
        snap = fixture_snapshot(count=2)
        self.add_source(snap)
        self.sync(snap)                    # всё уже известно
        result = self.api.sync_queue_new()
        self.assertEqual(result["error"], "Новых для загрузки нет")


class TestSyncGuards(SyncCase):
    def test_without_sources_is_an_error(self):
        result = self.api.sync_start()
        self.assertEqual(result["error"],
                         "Нет источников для синхронизации")

    def test_second_start_is_guarded(self):
        self.add_source(fixture_snapshot(count=2))
        self.fetch_delay = 10              # площадка «висит» до нажатия Стоп
        self.assertTrue(self.api.sync_start().get("ok"))
        again = self.api.sync_start()
        self.assertEqual(again["error"], "Синхронизация уже идёт")
        self.assertTrue(self.api.sync_stop()["ok"])
        self.wait_sync(self.api)

    def test_stop_between_fetches_writes_nothing(self):
        snap = fixture_snapshot(count=2)
        self.add_source(snap)
        before = self.api.poll(0)["stats"]["total"]

        self.fetch_delay = 10              # ждём «Стоп», а не площадку
        self.fetch_result = fixture_snapshot(count=5)
        self.assertTrue(self.api.sync_start().get("ok"))
        self.assertTrue(self.api.sync_stop()["ok"])
        result = self.wait_sync(self.api)

        self.assertFalse(result["running"])
        # Отмена на снапшоте: ни новых строк, ни изменений.
        self.assertEqual(result["results"], [])
        self.assertEqual(self.api.poll(0)["stats"]["total"], before)

    def test_error_in_one_source_does_not_kill_the_run(self):
        self.add_source(fixture_snapshot(count=2))
        self.fetch_error = sources_mod.FetchError("Площадка не ответила: 403")

        self.assertTrue(self.api.sync_start().get("ok"))
        result = self.wait_sync(self.api)

        row = result["results"][0]
        self.assertIn("403", row["error"])
        self.assertEqual(row["new"], 0)
        self.assertIsNone(result["error"])   # сам воркер не упал
        # База цела: ничего не записано мимо плана.
        self.assertEqual(self.api.poll(0)["stats"]["total"], 2)


if __name__ == "__main__":
    unittest.main()
