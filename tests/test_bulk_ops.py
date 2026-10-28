"""M17: массовые операции над выбранной группой и над папкой-коллекцией.

Офлайн, как остальные тесты окна: профиль (настройки, база, журнал, факты
согласий) во временной папке, сеть подменяется фикстурой
downloader.download. Проверяем честную сводку «сделано / пропущено с
причиной», гейт 18+ и то, что удаление строк из индекса НЕ трогает файлы
на диске.
"""

import unittest
from pathlib import Path
from unittest import mock

from app import repo
from app.db import Database
from tests.test_gui import GuiCase


def add_video(api, remote_id, *, platform="youtube", title="Клип"):
    """Строка videos со статусом known (как после добавления источника)."""
    vid, _created = repo.upsert_video(api.db.conn, {
        "platform": platform, "remote_id": remote_id,
        "key": f"{platform}:{remote_id}", "title": title,
        "raw_json": "{}", "origin": "yt-dlp"})
    return vid


def record_file(conn, video_id, path):
    """Файл на диске -> строка files (missing=0), статус -> downloaded."""
    stat = Path(path).stat()
    repo.record_file(conn, video_id, str(path), "video",
                     size=stat.st_size, mtime=stat.st_mtime)


def status_of(conn, video_id):
    return conn.execute("SELECT status FROM videos WHERE id=?",
                        (video_id,)).fetchone()["status"]


def row_of(conn, video_id):
    row = conn.execute("SELECT * FROM videos WHERE id=?",
                       (video_id,)).fetchone()
    return dict(row) if row else None


class BulkCase(GuiCase):
    def setUp(self):
        super().setUp()
        # Факты согласий в профиле теста: без патча гейт 18+ читал бы
        # настоящее «мне есть 18» пользователя и не срабатывал бы.
        patcher = mock.patch("app.legal.legal_path",
                             return_value=self.dir / "legal.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = self.make_api()
        self.conn = self.api.db.conn
        self.dest = self.dir / "out"
        self.dest.mkdir()
        self.storage = self.add_storage(self.api, self.dest,
                                        label="Тестовая папка")

    def logs(self) -> list[str]:
        return self.api.poll(0)["logs"]

    def fake_download(self):
        """Очередь в офлайн-прогоне не должна трогать настоящую площадку."""
        def download(video, settings, *, stop, on_progress=None, dest=None,
                     **kwargs):
            target = Path(dest or self.dest)
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"{video['remote_id']}.mp4"
            path.write_bytes(b"payload")
            return {"cancelled": False, "files": [(str(path), "video")],
                    "info": {}, "error": None, "hash": "sha256:" + "ab" * 32}

        patcher = mock.patch("app.downloader.download", side_effect=download)
        patcher.start()
        self.addCleanup(patcher.stop)

    def file_on_disk(self, name):
        path = self.dest / name
        path.write_bytes(b"payload")
        return path


class TestBulkDownload(BulkCase):
    def test_group_goes_to_queue_and_present_files_are_skipped(self):
        """Одно действие на всю группу; файл на месте - пропуск, не повтор."""
        plain = add_video(self.api, "a1")
        present = add_video(self.api, "a2")
        archived = add_video(self.api, "a3")
        record_file(self.conn, present, self.file_on_disk("a2.mp4"))
        repo.set_status(self.conn, [archived], "unavailable")
        self.fake_download()

        result = self.api.bulk_download({
            "ids": [plain, present, archived, 99999],
            "storage_id": self.storage["id"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["queued"], 1, "в очередь только одна строка")
        self.assertEqual(result["skipped"], 3)
        self.assertEqual(result["reasons"],
                         {"уже скачано": 1, "удалено на площадке": 1,
                          "нет строки в индексе": 1},
                         "причины пропуска должны быть видны человеку")
        self.assertEqual(status_of(self.conn, plain), "queued")
        self.assertEqual(row_of(self.conn, plain)["target_storage_id"],
                         self.storage["id"], "цель записывается при постановке")
        self.assertEqual(status_of(self.conn, present), "downloaded")

        # Очередь стартовала сама (человек явно попросил) и скачала ровно одну.
        self.assertTrue(self.api.dl.wait(timeout=15), "очередь не завершилась")
        state = self.api.dl.state
        self.assertEqual(state["attempted"], 1, state)
        self.assertEqual(state["done"], 1, state)
        self.assertEqual(status_of(self.conn, plain), "downloaded")
        self.assertTrue((self.dest / "a1.mp4").exists())
        self.assertIn("Массовая выкачка", "\n".join(self.logs()))

    def test_lost_file_and_failed_rows_go_back_to_queue(self):
        """«Файл пропал» и «ошибка» - не «уже скачано»: их надо перекачать."""
        missing = add_video(self.api, "b1")
        failed = add_video(self.api, "b2")
        repo.set_status(self.conn, [missing], "missing")
        repo.set_status(self.conn, [failed], "failed")
        self.fake_download()

        result = self.api.bulk_download({"ids": [missing, failed]})
        self.assertEqual(result["queued"], 2, result)
        self.assertEqual(result["skipped"], 0)
        self.assertIsNone(result["storage_id"],
                          "без цели цель строки остаётся прежней")
        self.assertEqual(status_of(self.conn, missing), "queued")

    def test_duplicates_are_counted_once(self):
        vid = add_video(self.api, "c1")
        result = self.api.bulk_download({"ids": [vid, vid, vid]})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["queued"], 1)

    def test_unknown_storage_is_rejected_before_queueing(self):
        vid = add_video(self.api, "d1")
        result = self.api.bulk_download({"ids": [vid],
                                         "storage_id": "левый-id"})
        self.assertIn("Хранилище", result["error"], result)
        self.assertEqual(status_of(self.conn, vid), "known",
                         "отказ не должен менять статус")

    def test_detached_storage_is_rejected(self):
        from app import storages
        vid = add_video(self.api, "e1")
        gone_dir = self.dir / "нет-носителя"
        gone_dir.mkdir()
        detached = self.add_storage(self.api, gone_dir, label="Отвязанная")
        storages.detach(self.conn, detached["id"], keep_trace=True)
        result = self.api.bulk_download({"ids": [vid],
                                         "storage_id": detached["id"]})
        self.assertIn("отвязано", result["error"], result)
        self.assertEqual(status_of(self.conn, vid), "known")

    def test_empty_selection_reports_honestly(self):
        self.assertEqual(self.api.bulk_download({"ids": []})["error"],
                         "Ничего не выбрано")
        self.assertEqual(self.api.bulk_download({})["error"],
                         "Ничего не выбрано")


class TestBulkLimit(BulkCase):
    def test_group_larger_than_limit_is_rejected(self):
        vid = add_video(self.api, "f1")
        ids = [vid] + list(range(1000, 1000 + self.api.BULK_LIMIT))
        result = self.api.bulk_download({"ids": ids})
        self.assertIn("слишком большая", result["error"], result)
        self.assertIn(str(self.api.BULK_LIMIT), result["error"])
        self.assertEqual(status_of(self.conn, vid), "known",
                         "лимит должен отсекать операцию до записи")

    def test_limit_counts_distinct_rows(self):
        vids = [add_video(self.api, f"g{i}") for i in range(self.api.BULK_LIMIT)]
        result = self.api.bulk_download({"ids": vids + vids})
        self.assertEqual(result["total"], self.api.BULK_LIMIT)
        self.assertEqual(result["queued"], self.api.BULK_LIMIT)


class TestBulkTags(BulkCase):
    def test_add_and_remove_several_tags_at_once(self):
        first = add_video(self.api, "h1")
        second = add_video(self.api, "h2")
        repo.save_fields(self.conn, [first], {"user_tags": "рок, живой"})
        repo.save_fields(self.conn, [second], {"user_tags": "Живой"})

        result = self.api.bulk_tags({
            "ids": [first, second],
            "add": ["избранное", "4К"],
            "remove": ["рок"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(row_of(self.conn, first)["user_tags"],
                         "живой, избранное, 4К")
        # merge, а не замена: чужие метки группы остаются, дубль по регистру схлопнут
        self.assertEqual(row_of(self.conn, second)["user_tags"],
                         "Живой, избранное, 4К")

    def test_tags_as_comma_string_is_accepted(self):
        vid = add_video(self.api, "i1")
        result = self.api.bulk_tags({"ids": [vid],
                                     "add": " живое , избранное ",
                                     "remove": ""})
        self.assertEqual(result["updated"], 1)
        self.assertEqual(row_of(self.conn, vid)["user_tags"],
                         "живое, избранное")

    def test_no_change_reported_as_unchanged(self):
        vid = add_video(self.api, "j1")
        repo.save_fields(self.conn, [vid], {"user_tags": "живое"})
        result = self.api.bulk_tags({"ids": [vid], "add": ["Живое"]})
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["unchanged"], 1)

    def test_remove_all_tags_clears_field(self):
        vid = add_video(self.api, "k1")
        repo.save_fields(self.conn, [vid], {"user_tags": "a, b"})
        self.api.bulk_tags({"ids": [vid], "remove": ["a", "b"]})
        self.assertIsNone(row_of(self.conn, vid)["user_tags"])

    def test_missing_rows_are_counted_as_gone(self):
        vid = add_video(self.api, "l1")
        result = self.api.bulk_tags({"ids": [vid, 99999], "add": ["метка"]})
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["gone"], 1)

    def test_empty_tag_request_is_rejected(self):
        vid = add_video(self.api, "m1")
        self.assertIn("теги", self.api.bulk_tags({"ids": [vid]})["error"])
        self.assertIn("теги", self.api.bulk_tags(
            {"ids": [vid], "add": "  ", "remove": ""})["error"])

    def test_platform_tags_are_not_touched(self):
        """Локальные метки и теги площадки - разные сущности (PH2)."""
        vid = add_video(self.api, "n1", platform="pornhub")
        repo.set_platform_tags(self.conn, vid, "pornhub", ["Genre"])
        self.api.bulk_tags({"ids": [vid], "add": ["метка"]})
        tags = [dict(r) for r in self.conn.execute(
            "SELECT t.name FROM video_tags vt JOIN tags t ON t.id=vt.tag_id "
            "WHERE vt.video_id=?", (vid,))]
        self.assertEqual([t["name"] for t in tags], ["Genre"])


class TestBulkRating(BulkCase):
    def test_rating_applies_to_group_and_can_be_reset(self):
        vids = [add_video(self.api, f"o{i}") for i in range(3)]
        result = self.api.bulk_rating({"ids": vids, "rating": 4})
        self.assertEqual(result["updated"], 3)
        self.assertEqual({row_of(self.conn, v)["user_rating"] for v in vids},
                         {4})
        self.api.bulk_rating({"ids": vids, "rating": 0})
        self.assertEqual({row_of(self.conn, v)["user_rating"] for v in vids},
                         {0})

    def test_rating_out_of_range_and_non_number_are_rejected(self):
        vid = add_video(self.api, "p1")
        repo.save_fields(self.conn, [vid], {"user_rating": 3})
        for bad in (9, -1, None, "abc"):
            result = self.api.bulk_rating({"ids": [vid], "rating": bad})
            self.assertIn("Рейтинг", result["error"], result)
        self.assertEqual(row_of(self.conn, vid)["user_rating"], 3)

    def test_rating_leaves_queue_order_alone(self):
        """updated_at не меняется: пометка не переставляет очередь."""
        vid = add_video(self.api, "q1")
        self.conn.execute(
            "UPDATE videos SET updated_at='2020-01-01T00:00:00' WHERE id=?",
            (vid,))
        self.api.bulk_rating({"ids": [vid], "rating": 5})
        self.assertEqual(row_of(self.conn, vid)["updated_at"],
                         "2020-01-01T00:00:00")


class TestBulkCollection(BulkCase):
    def test_add_group_then_remove_it(self):
        vids = [add_video(self.api, f"r{i}") for i in range(2)]
        cid = repo.collection_create(self.conn, "Избранное")

        added = self.api.bulk_collection({"ids": vids, "collection_id": cid,
                                          "action": "add"})
        self.assertTrue(added["ok"], added)
        self.assertEqual(added["added"], 2)
        self.assertEqual(repo.collection_ids(self.conn, cid), vids)

        again = self.api.bulk_collection({"ids": vids, "collection_id": cid,
                                          "action": "add"})
        self.assertEqual(again["added"], 0, "дубли позиций не плодятся")
        self.assertEqual(again["unchanged"], 2)

        removed = self.api.bulk_collection({"ids": vids, "collection_id": cid,
                                            "action": "remove"})
        self.assertEqual(removed["removed"], 2)
        self.assertEqual(repo.collection_ids(self.conn, cid), [])
        self.assertEqual(repo.stats(self.conn)["total"], 2,
                         "строки videos остаются - ушли только связи")

    def test_unknown_collection_and_bad_action_are_rejected(self):
        vid = add_video(self.api, "s1")
        cid = repo.collection_create(self.conn, "Папка")
        self.assertEqual(self.api.bulk_collection(
            {"ids": [vid], "collection_id": 999, "action": "add"})["error"],
            "Коллекция не найдена")
        self.assertEqual(self.api.bulk_collection(
            {"ids": [vid], "collection_id": cid, "action": "shove"})["error"],
            "Нужно действие: add или remove")
        self.assertEqual(self.api.bulk_collection(
            {"ids": [vid], "action": "add"})["error"], "Не указана коллекция")

    def test_collection_scope_works_without_selection(self):
        """«Скачать всё» и «очистить папку»: тот же вход, но без ids."""
        cid = repo.collection_create(self.conn, "На потом")
        vids = [add_video(self.api, f"t{i}") for i in range(2)]
        repo.collection_add(self.conn, cid, vids)
        self.fake_download()

        downloaded = self.api.bulk_download({"collection_id": cid,
                                             "storage_id": self.storage["id"]})
        self.assertEqual(downloaded["queued"], 2, downloaded)
        self.assertTrue(self.api.dl.wait(timeout=15))
        self.assertEqual({status_of(self.conn, v) for v in vids},
                         {"downloaded"})

        purge = self.api.bulk_collection({"collection_id": cid,
                                          "action": "remove"})
        self.assertEqual(purge["removed"], 2, purge)
        self.assertEqual(repo.collection_ids(self.conn, cid), [])
        self.assertEqual(repo.stats(self.conn)["total"], 2,
                         "папка очищена, строки videos целы")

    def test_empty_collection_reports_nothing_selected(self):
        cid = repo.collection_create(self.conn, "Пустая")
        self.assertEqual(self.api.bulk_download({"collection_id": cid})["error"],
                         "Ничего не выбрано")


class TestBulkRemoveRows(BulkCase):
    def test_needs_explicit_confirmation(self):
        vid = add_video(self.api, "u1")
        result = self.api.bulk_remove_rows({"ids": [vid]})
        self.assertIn("подтверждение", result["error"], result)
        self.assertIsNotNone(row_of(self.conn, vid),
                             "без подтверждения строка обязана остаться")

    def test_rows_gone_files_on_disk_kept(self):
        first = add_video(self.api, "v1")
        second = add_video(self.api, "v2")
        keep = self.file_on_disk("v1.mp4")
        other = self.file_on_disk("v2.mp4")
        record_file(self.conn, first, keep)
        record_file(self.conn, second, other)
        cid = repo.collection_create(self.conn, "Папка")
        repo.collection_add(self.conn, cid, [first, second])

        result = self.api.bulk_remove_rows({"ids": [first, second, 99999],
                                            "confirm": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["gone"], 1)
        self.assertTrue(result["files_kept"])

        self.assertIsNone(row_of(self.conn, first))
        self.assertEqual(repo.stats(self.conn)["total"], 0)
        # Связи ушли каскадом по FK - это записи об индексе, не файлы.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM files").fetchone()["n"], 0)
        self.assertEqual(repo.collection_ids(self.conn, cid), [])
        # Файлы на диске целы: скан найдёт их заново.
        self.assertTrue(keep.exists())
        self.assertTrue(other.exists())
        self.assertIn("файлы на диске не тронуты", "\n".join(self.logs()))

    def test_queue_rows_are_removed_too(self):
        vid = add_video(self.api, "w1")
        repo.enqueue(self.conn, [vid], storage_id=self.storage["id"])
        result = self.api.bulk_remove_rows({"ids": [vid], "confirm": True})
        self.assertEqual(result["removed"], 1)
        self.assertEqual(repo.stats(self.conn)["queued"], 0)
        self.assertEqual(repo.queue_rows(self.conn), [])


class TestAdultGate(BulkCase):
    """Гейт 18+ над группой, содержащей строки взрослой площадки (P1/PH5)."""

    def test_download_needs_confirmation_then_passes(self):
        adult = add_video(self.api, "x1", platform="pornhub")
        plain = add_video(self.api, "x2")

        refused = self.api.bulk_download({"ids": [adult, plain],
                                          "storage_id": self.storage["id"]})
        self.assertTrue(refused.get("adult_confirm"), refused)
        self.assertIn("18", refused["error"])
        self.assertEqual(status_of(self.conn, adult), "known")
        self.assertEqual(status_of(self.conn, plain), "known")

        self.api.legal_accept({"adult": True})
        self.fake_download()
        result = self.api.bulk_download({"ids": [adult, plain],
                                         "storage_id": self.storage["id"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["queued"], 2)

    def test_other_bulk_endpoints_are_gated(self):
        adult = add_video(self.api, "y1", platform="pornhub")
        cid = repo.collection_create(self.conn, "Папка")

        self.assertTrue(self.api.bulk_tags(
            {"ids": [adult], "add": ["метка"]}).get("adult_confirm"))
        self.assertTrue(self.api.bulk_rating(
            {"ids": [adult], "rating": 3}).get("adult_confirm"))
        self.assertTrue(self.api.bulk_collection(
            {"ids": [adult], "collection_id": cid,
             "action": "add"}).get("adult_confirm"))
        self.assertTrue(self.api.bulk_remove_rows(
            {"ids": [adult], "confirm": True}).get("adult_confirm"))

        # Ничего не записано: ни метки, ни связи, ни удаления строки.
        self.assertIsNone(row_of(self.conn, adult)["user_tags"])
        self.assertIsNone(row_of(self.conn, adult)["user_rating"])
        self.assertEqual(repo.collection_ids(self.conn, cid), [])
        self.assertIsNotNone(row_of(self.conn, adult))

    def test_group_without_adult_rows_needs_no_confirmation(self):
        vids = [add_video(self.api, f"z{i}") for i in range(2)]
        result = self.api.bulk_rating({"ids": vids, "rating": 2})
        self.assertTrue(result.get("ok"), result)
        self.assertFalse(result.get("adult_confirm"))

    def test_collection_scope_adult_rows_are_gated(self):
        cid = repo.collection_create(self.conn, "Взрослая папка")
        adult = add_video(self.api, "zz1", platform="pornhub")
        repo.collection_add(self.conn, cid, [adult])
        refused = self.api.bulk_download({"collection_id": cid})
        self.assertTrue(refused.get("adult_confirm"), refused)


class TestRepoBulk(unittest.TestCase):
    """Репозиторийные функции массовых операций: без окна и без сети."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        database = Database(Path(self.dir.name) / "bulk.db")
        self.addCleanup(database.close)
        self.conn = database.conn

    def video(self, remote_id, *, platform="youtube", status="known"):
        vid, _ = repo.upsert_video(self.conn, {
            "platform": platform, "remote_id": remote_id,
            "key": f"{platform}:{remote_id}", "title": remote_id})
        if status != "known":
            repo.set_status(self.conn, [vid], status)
        return vid

    def test_bulk_enqueue_reasons_are_exact(self):
        path = Path(self.dir.name) / "clip.mp4"
        path.write_bytes(b"payload")
        downloaded = self.video("a")
        record_file(self.conn, downloaded, path)
        queued = self.video("b", status="queued")
        downloading = self.video("c", status="downloading")
        archived = self.video("d", status="unavailable")
        failed = self.video("e", status="failed")
        gone = self.video("f")
        repo.bulk_delete_rows(self.conn, [gone])

        summary = repo.bulk_enqueue(
            self.conn, [downloaded, queued, downloading, archived, failed, gone])
        self.assertEqual(summary["queued"], 2, summary)   # queued + failed
        self.assertEqual(summary["reasons"],
                         {"уже скачано": 1, "качается сейчас": 1,
                          "удалено на площадке": 1, "нет строки в индексе": 1})

    def test_bulk_tags_merges_and_caps(self):
        vid = self.video("g")
        repo.save_fields(self.conn, [vid], {"user_tags": "один"})
        repo.bulk_tags(self.conn, [vid], add=[f"тег{i}" for i in range(60)])
        tags = row_of(self.conn, vid)["user_tags"].split(", ")
        self.assertLessEqual(len(tags), 40, "FTS не должен раздуваться")
        self.assertEqual(tags[0], "один", "merge, а не замена")

    def test_bulk_collection_requires_existing_folder(self):
        vid = self.video("h")
        with self.assertRaises(ValueError):
            repo.bulk_collection(self.conn, [vid], 404, "add")
        cid = repo.collection_create(self.conn, "Папка")
        summary = repo.bulk_collection(self.conn, [vid], cid, "add")
        self.assertEqual(summary["added"], 1)
        self.assertEqual(repo.bulk_collection(self.conn, [vid], cid, "add")["added"], 0)
        self.assertEqual(repo.bulk_collection(self.conn, [vid], cid, "remove")["removed"], 1)

    def test_count_adult_only_counts_adult_rows(self):
        adult = self.video("i", platform="pornhub")
        plain = self.video("j")
        self.assertEqual(repo.count_adult(self.conn, [adult, plain]), 1)
        self.assertEqual(repo.count_adult(self.conn, [plain]), 0)

    def test_collection_ids_keep_order_of_adding(self):
        cid = repo.collection_create(self.conn, "Порядок")
        first = self.video("k1")
        second = self.video("k2")
        repo.collection_add(self.conn, cid, [second])
        repo.collection_add(self.conn, cid, [first])
        self.assertEqual(repo.collection_ids(self.conn, cid), [second, first])


if __name__ == "__main__":
    unittest.main()
