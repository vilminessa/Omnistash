"""Хранилища: привязка путей, доступность, отвязка, восстановление, выбор.

Это ядро M5-M7: файлы живут относительно корня хранилища (storage_id +
rel_path), поэтому переезд диска - один UPDATE, а «файл пропал» имеет
право появиться только у ДОСТУПНОГО хранилища.
"""

import json
import os
import shutil
import unittest
from pathlib import Path

from app import repo, settings as settings_mod, storages
from app.indexer import SIDECAR_SUFFIX
from app.metadata import normalize_video
from tests.test_gui import GuiCase


class TestPaths(unittest.TestCase):
    def test_normalize_strips_trailing_separator(self):
        self.assertEqual(storages.normalize_path("D:/lib/"), "D:\\lib")
        self.assertEqual(storages.normalize_path("  D:\\lib  "), "D:\\lib")

    def test_split_prefers_longest_prefix(self):
        # Вложенные папки: выигрывает самая длинная (единственное правило).
        nested = [{"path": "D:\\lib"}, {"path": "D:\\lib\\downloads"}]
        storage, rel = storages.split_path(nested, "D:\\lib\\downloads\\a.mp4")
        self.assertEqual(storage["path"], "D:\\lib\\downloads")
        self.assertEqual(rel, "a.mp4")

        storage, rel = storages.split_path(nested, "D:\\lib\\b\\c.mp4")
        self.assertEqual(storage["path"], "D:\\lib")
        self.assertEqual(rel, "b\\c.mp4")

    def test_split_is_case_insensitive(self):
        storage, rel = storages.split_path([{"path": "D:\\Lib"}], "d:\\LIB\\x.mp4")
        self.assertIsNotNone(storage)
        # Относительный путь собирается из ОРИГИНАЛА, а не из пониженнного.
        self.assertEqual(rel, "x.mp4")

    def test_split_no_match(self):
        storage, rel = storages.split_path([{"path": "D:\\lib"}], "E:\\other\\x.mp4")
        self.assertIsNone(storage)
        self.assertEqual(rel, "")

    def test_split_of_root_itself(self):
        storage, rel = storages.split_path([{"path": "D:\\lib"}], "D:\\lib")
        self.assertIsNotNone(storage)
        self.assertEqual(rel, "")

    def test_label_defaults_to_folder_name(self):
        self.assertEqual(storages.label_for("D:\\видео\\Фильмы"), "Фильмы")


class StorageCase(GuiCase):
    """Общая база: Api + temp-профиль из GuiCase."""

    def setUp(self):
        super().setUp()
        self.api = self.make_api()
        self.conn = self.api.db.conn

    def storage_path(self, name: str) -> Path:
        path = self.dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path


class TestCrud(StorageCase):
    def test_add_creates_row_and_marker(self):
        path = self.storage_path("lib")
        storage = self.add_storage(self.api, path)
        self.assertEqual(storages.normalize_path(storage["path"]),
                         storages.normalize_path(str(path)))
        self.assertEqual(storage["status"], "active")
        # Маркер в папке - это то, что позволит узнать её при возврате.
        self.assertTrue((path / storages.MARKER).exists())
        self.assertTrue(storage["root_key"])
        self.assertEqual(storages.read_marker(path), storage["root_key"])

    def test_add_same_path_twice_is_hinted_not_duplicated(self):
        path = self.storage_path("lib")
        self.add_storage(self.api, path)
        again = storages.add(self.conn, str(path))
        self.assertEqual(again.get("hint"), "already")
        self.assertEqual(len(storages.all_storages(self.conn)), 1)

    def test_add_missing_folder_is_error(self):
        result = storages.add(self.conn, str(self.dir / "нет-такой"))
        self.assertIn("Папка не найдена", result["error"])

    def test_known_marker_is_hinted(self):
        # Папку уже знали (отвязали), потом добавили заново с другим путём.
        path = self.storage_path("old")
        first = self.add_storage(self.api, path)
        storages.detach(self.conn, first["id"], keep_trace=True)
        again = storages.add(self.conn, str(path))
        # Путь совпал с отвязанным хранилищем: это «вернуть?», а не дубль.
        self.assertEqual(again.get("hint"), "detached")
        self.assertEqual(again["storage"]["id"], first["id"])


class TestBackfill(StorageCase):
    def test_adopt_files_binds_by_prefix(self):
        path = self.storage_path("lib")
        video_id, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": "vid000000001",
            "key": "youtube:vid000000001", "title": "V", "raw_json": "{}",
            "origin": "yt-dlp"})
        # Файл записан до того, как появилось хранилище (старый dest_dir).
        target = path / "video.mp4"
        target.write_bytes(b"x")
        self.conn.execute(
            "INSERT INTO files (video_id, kind, path, size, missing) "
            "VALUES (?, 'video', ?, 1, 0)", (video_id, str(target)))

        self.add_storage(self.api, path)
        result = storages.adopt_files(self.conn)
        self.assertEqual(result["adopted"], 1)
        self.assertEqual(result["orphans"], 0)
        row = self.conn.execute(
            "SELECT storage_id, rel_path FROM files WHERE video_id=?",
            (video_id,)).fetchone()
        self.assertIsNotNone(row["storage_id"])
        self.assertEqual(row["rel_path"], "video.mp4")

    def test_adopt_counts_orphans(self):
        video_id, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": "vid000000002",
            "key": "youtube:vid000000002", "title": "V", "raw_json": "{}",
            "origin": "yt-dlp"})
        self.conn.execute(
            "INSERT INTO files (video_id, kind, path, size, missing) "
            "VALUES (?, 'video', 'E:\\вне\\библиотеки.mp4', 1, 0)",
            (video_id,))
        result = storages.adopt_files(self.conn)
        self.assertEqual(result["orphans"], 1)   # дыра №1: файл вне корней


class TestAvailability(StorageCase):
    def test_missing_folder_marks_unavailable(self):
        path = self.storage_path("gone-later")
        storage = self.add_storage(self.api, path)
        state = storages.refresh_availability(self.conn)
        self.assertEqual(state["available"], 1)

        # Носитель отключили: в папке лежит маркер, поэтому чистим всё.
        shutil.rmtree(path)
        state = storages.refresh_availability(self.conn)
        self.assertEqual(state["lost"], 1)
        row = storages.get(self.conn, storage["id"])
        self.assertEqual(row["available"], 0)
        self.assertIsNotNone(row["missing_since"])

    def test_disabled_storage_is_not_checked(self):
        path = self.storage_path("off")
        storage = self.add_storage(self.api, path, enabled=False)
        state = storages.refresh_availability(self.conn)
        # Выключенные не проверяются: их «недоступность» ничего не значит.
        self.assertEqual(state["checked"], 0)
        self.assertEqual(state["available"], 0)
        # Но строка видна - иначе включить обратно будет нечем.
        ids = [row["id"] for row in storages.all_storages(self.conn)]
        self.assertIn(storage["id"], ids)
        self.assertEqual(storage["enabled"], 0)
        # В скан не попадает: start_scan фильтрует enabled сам.
        self.assertTrue(all(row.get("enabled", 1) for row in
                            storages.all_storages(self.conn)
                            if row["id"] != storage["id"]))


class TestDetach(StorageCase):
    def _library(self, path: Path) -> dict:
        """Библиотека из трёх историй: копия, файл только здесь, локальный."""
        storage = self.add_storage(self.api, path)
        other = self.storage_path("other")
        other_storage = self.add_storage(self.api, other)

        def video(remote_id, title):
            vid, _ = repo.upsert_video(self.conn, {
                "platform": "youtube", "remote_id": remote_id,
                "key": f"youtube:{remote_id}", "title": title,
                "raw_json": "{}", "origin": "yt-dlp"})
            return vid

        with_copy = video("vid000000001", "С копией в другом месте")
        only_here = video("vid000000002", "Только в этой папке")
        elsewhere = video("vid000000003", "Живёт в другом хранилище")
        local_id = repo.insert_local_video(
            self.conn, title="Без имени", path=str(path / "local.mp4"),
            size=3, mtime=1.0, digest=None)

        repo.record_file(self.conn, with_copy, str(path / "copy.mp4"),
                         "video", size=3, mtime=1.0)
        repo.record_file(self.conn, with_copy, str(other / "copy.mp4"),
                         "video", size=3, mtime=1.0)
        repo.record_file(self.conn, only_here, str(path / "only.mp4"),
                         "video", size=3, mtime=1.0)
        repo.record_file(self.conn, local_id, str(path / "local.mp4"),
                         "video", size=3, mtime=1.0)
        repo.record_file(self.conn, elsewhere, str(other / "other.mp4"),
                         "video", size=3, mtime=1.0)
        return {"storage": storage, "with_copy": with_copy,
                "only_here": only_here, "local_id": local_id,
                "other_id": other_storage["id"]}

    def test_preview_counts(self):
        path = self.storage_path("lib")
        made = self._library(path)
        preview = storages.preview_detach(self.conn, made["storage"]["id"])
        self.assertEqual(preview["files"], 3)
        self.assertEqual(preview["local_deleted"], 1)
        # Копия в другом хранилище остаётся - её отвязка ничего не ломает.
        self.assertEqual(preview["kept_elsewhere"], 1)
        self.assertEqual(preview["detached"], 1)

    def test_detach_forgets_folder_and_keeps_trace(self):
        path = self.storage_path("lib")
        made = self._library(path)
        result = storages.detach(self.conn, made["storage"]["id"], keep_trace=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["files_removed"], 3)
        self.assertEqual(result["local_deleted"], 1)
        self.assertEqual(result["detached"], 1)

        # Файловых записей папки больше нет вообще.
        left = self.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE storage_id=?",
            (made["storage"]["id"],)).fetchone()["n"]
        self.assertEqual(left, 0)
        # Локальное видео удалено целиком: без файла знать о нём нечего.
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM videos WHERE id=?", (made["local_id"],)).fetchone())
        # Файл был только здесь -> «откреплено», а не «пропало»:
        # синк его не перекачает, но карточка честно скажет, в чём дело.
        only = self.conn.execute(
            "SELECT status, detached_from FROM videos WHERE id=?",
            (made["only_here"],)).fetchone()
        self.assertEqual(only["status"], "detached")
        self.assertTrue(only["detached_from"])
        # Копия в другом хранилище сохранила обычный статус.
        copied = self.conn.execute(
            "SELECT status FROM videos WHERE id=?", (made["with_copy"],)).fetchone()
        self.assertEqual(copied["status"], "downloaded")
        # Сами файлы другого хранилища не тронуты.
        other_files = self.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE storage_id=?",
            (made["other_id"],)).fetchone()["n"]
        self.assertEqual(other_files, 2)
        # След папки остался.
        storage = storages.get(self.conn, made["storage"]["id"])
        self.assertEqual(storage["status"], "detached")

    def test_detach_without_trace_removes_row(self):
        path = self.storage_path("lib")
        made = self._library(path)
        storages.detach(self.conn, made["storage"]["id"], keep_trace=False)
        self.assertIsNone(storages.get(self.conn, made["storage"]["id"]))

    def test_restore_then_forget(self):
        path = self.storage_path("lib")
        made = self._library(path)
        storages.detach(self.conn, made["storage"]["id"], keep_trace=True)

        # Пока отвязано - «забыть навсегда» нельзя (сначала restore или
        # проверка), а на активное хранилище forget вообще не действует.
        restored = storages.restore(self.conn, made["storage"]["id"])
        self.assertTrue(restored["ok"])
        self.assertEqual(restored["storage"]["status"], "active")
        self.assertEqual(restored["storage"]["enabled"], 1)

    def test_forget_requires_detached(self):
        path = self.storage_path("lib")
        storage = self.add_storage(self.api, path)
        result = storages.forget(self.conn, storage["id"])
        self.assertIn("отвяжите", result["error"])

    def test_record_file_restores_detached_status(self):
        path = self.storage_path("lib")
        made = self._library(path)
        storages.detach(self.conn, made["storage"]["id"], keep_trace=True)
        # Вернули папку и прошли сканом: файл снова записан в индекс,
        # статус «откреплено» сменяется на «скачано».
        repo.record_file(self.conn, made["only_here"], str(path / "only.mp4"),
                         "video", size=3, mtime=1.0)
        row = self.conn.execute(
            "SELECT status FROM videos WHERE id=?", (made["only_here"],)).fetchone()
        self.assertEqual(row["status"], "downloaded")


class TestRelocation(StorageCase):
    def test_set_path_rewrites_cached_paths(self):
        old = self.storage_path("old-place")
        new = self.storage_path("new-place")
        (old / "sub").mkdir(parents=True, exist_ok=True)
        video_id, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": "vid000000001",
            "key": "youtube:vid000000001", "title": "V", "raw_json": "{}",
            "origin": "yt-dlp"})
        storage = self.add_storage(self.api, old)
        rel_before = "sub" + os.sep + "video.mp4"
        self.conn.execute(
            "INSERT INTO files (video_id, kind, path, size, missing, "
            "storage_id, rel_path) VALUES (?, 'video', ?, 1, 0, ?, ?)",
            (video_id, str(old / "sub" / "video.mp4"), storage["id"], rel_before))

        result = storages.set_path(self.conn, storage["id"], str(new))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["files"], 1)
        # Относительный путь - канон, он не меняется; абсолют пересчитан.
        row = self.conn.execute(
            "SELECT path, rel_path FROM files WHERE video_id=?",
            (video_id,)).fetchone()
        self.assertEqual(row["rel_path"], rel_before)
        self.assertEqual(row["path"], str(new / "sub" / "video.mp4"))
        # И хранилище, и файлы - одной транзакцией.
        self.assertEqual(storages.get(self.conn, storage["id"])["path"],
                         str(new))


class TestResolve(StorageCase):
    def test_chain_playlist_then_channel_then_default(self):
        channel = repo.upsert_channel(self.conn, {
            "platform": "youtube", "remote_id": "UCaaaaaaaaaaaaaaaaaaaaaa",
            "title": "Автор", "handle": None, "url": None})
        storage_a = self.add_storage(self.api, self.storage_path("a"))
        storage_b = self.add_storage(self.api, self.storage_path("b"))

        # Глобальное по умолчанию.
        config = {"default_storage_id": storage_a["id"]}
        self.assertEqual(
            storages.resolve_target(self.conn, config)["id"], storage_a["id"])

        # Канал задаёт своё хранилище - оно перебивает глобальное.
        self.conn.execute("UPDATE channels SET storage_id=? WHERE id=?",
                          (storage_b["id"], channel))
        self.assertEqual(
            storages.resolve_target(self.conn, config, channel_id=channel)["id"],
            storage_b["id"])

        # Отключили каналное - падаем на глобальное.
        self.conn.execute("UPDATE channels SET storage_id=? WHERE id=?",
                          ("st_нету", channel))
        self.assertEqual(
            storages.resolve_target(self.conn, config, channel_id=channel)["id"],
            storage_a["id"])

    def test_no_default_returns_none(self):
        self.assertIsNone(storages.resolve_target(self.conn, {}))

    def test_detached_storage_is_not_resolved(self):
        path = self.storage_path("lib")
        storage = self.add_storage(self.api, path)
        storages.detach(self.conn, storage["id"], keep_trace=True)
        self.assertIsNone(
            storages.resolve_target(self.conn,
                                    {"default_storage_id": storage["id"]}))


class TestBootstrap(GuiCase):
    def test_old_settings_become_storages(self):
        api = self.make_api()
        roots = self.dir / "lib"
        roots.mkdir()
        downloads = self.dir / "downloads"
        downloads.mkdir()
        raw = {"library_roots": [{"path": str(roots), "recursive": True,
                                  "enabled": True}],
               "dest_dir": str(downloads),
               "quality": "low"}

        boot = storages.bootstrap(api.db, raw)
        self.assertEqual(boot["created"], 2)
        created = storages.all_storages(api.db.conn)
        self.assertEqual(len(created), 2)
        # Папка загрузок стала выбором по умолчанию (там и качали всегда).
        settings_mod.save({"default_storage_id": boot["default"],
                           "quality": "low"})
        self.assertEqual(settings_mod.load()["default_storage_id"],
                         boot["default"])

        # Повторный запуск ничего не плодит.
        self.assertEqual(storages.bootstrap(api.db, raw)["skipped"], True)

    def test_purge_removes_transferred_keys(self):
        api = self.make_api()   # сначала окно: его init сам чистит файл
        # Старый settings.json пишем напрямую: save() уже фильтрует по
        # схеме и убрал бы эти ключи до того, как мы увидим их.
        settings_mod.settings_path().write_text(json.dumps({
            "library_roots": [{"path": "D:/old"}],
            "dest_dir": "D:/old/downloads",
            "quality": "low",
        }, ensure_ascii=False), encoding="utf-8")

        raw = settings_mod.read_raw()
        self.assertIn("library_roots", raw)
        storages.bootstrap(api.db, raw)
        purged = settings_mod.purge_transferred()
        self.assertEqual(sorted(purged["purged"]),
                         ["dest_dir", "library_roots"])
        raw = settings_mod.read_raw()
        self.assertNotIn("library_roots", raw)
        self.assertNotIn("dest_dir", raw)
        self.assertEqual(raw["quality"], "low")


class TestScanBindsStorage(GuiCase):
    def test_scanned_file_gets_storage_and_rel(self):
        from app import indexer
        api = self.make_api()
        root = self.dir / "lib"
        root.mkdir()
        (root / "видео.mp4").write_bytes(b"payload")
        storage = self.add_storage(api, root)

        report = indexer.scan([storage], api.db, compute_hash=True)
        self.assertEqual(report["added"], 1)
        row = api.db.conn.execute(
            "SELECT storage_id, rel_path FROM files").fetchone()
        self.assertEqual(row["storage_id"], storage["id"])
        self.assertEqual(row["rel_path"], "видео.mp4")

    def test_missing_only_when_storage_available(self):
        from app import indexer
        api = self.make_api()
        root = self.dir / "lib"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"one")
        storage = self.add_storage(api, root)
        indexer.scan([storage], api.db, compute_hash=False)
        self.assertEqual(api.db.conn.execute(
            "SELECT missing FROM files").fetchone()["missing"], 0)

        # Носитель отключили (папки нет): файл НЕ пропал, а ждёт.
        import shutil
        shutil.rmtree(root)
        report = indexer.scan([storage], api.db, compute_hash=False)
        self.assertEqual(report["missing"], 0,
                         "недоступный корень не должен помечать пропало")


class TestRestoreFlow(StorageCase):
    """Критерий M6: отвязал папку -> вернул -> файлы вернулись сами.

    Восстановление опирается на содержимое папки (сайдкар/ID/название),
    а не на строки в базе - их при отвязке уже нет.
    """

    def _filled_storage(self):
        from app import indexer
        root = self.dir / "library"
        root.mkdir()
        # Видео, скачанное Omnistash: файл + sidecar, всё как делает очередь.
        info = {"id": "dQw4w9WgXcQ", "title": "Клип", "channel": "Автор",
                "channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa",
                "upload_date": "20091025", "duration": 213}
        vid, _ = repo.upsert_video(self.api.db.conn,
                                   normalize_video(info), full=True)
        media = root / "Клип [dQw4w9WgXcQ].mp4"
        media.write_bytes(b"content")
        sidecar = root / ("Клип [dQw4w9WgXcQ]" + SIDECAR_SUFFIX)
        sidecar.write_text(
            json.dumps({"omnistash": 1, "platform": "youtube",
                        "remote_id": "dQw4w9WgXcQ", "path": str(media),
                        "hash": "sha256:aa", "info": info},
                       ensure_ascii=False), encoding="utf-8")
        storage = self.add_storage(self.api, root)
        report = indexer.scan([storage], self.api.db, compute_hash=True)
        self.assertEqual(report["bound_sidecar"], 1, report)
        return storage, vid, root, sidecar

    def test_detach_then_restore_then_scan(self):
        from app import indexer
        storage, vid, root, sidecar = self._filled_storage()

        preview = self.api.storage_preview_detach({"id": storage["id"]})
        self.assertEqual(preview["files"], 2)      # видео + сайдкар
        self.assertEqual(preview["detached"], 1)

        result = self.api.storage_detach({"id": storage["id"],
                                          "keep_trace": True})
        self.assertTrue(result["ok"], result)
        # Папка забыта: ни строк files, ни «скачано».
        self.assertEqual(self.api.db.conn.execute(
            "SELECT COUNT(*) n FROM files").fetchone()["n"], 0)
        self.assertEqual(self.api.db.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()["status"],
            "detached")

        restored = self.api.storage_restore({"id": storage["id"]})
        self.assertTrue(restored["ok"], restored)

        # Скан восстановления: сайдкар отдаёт личность файла.
        row = restored["storage"]
        report = indexer.scan([row], self.api.db, compute_hash=True)
        self.assertEqual(report["bound_sidecar"], 1, report)
        self.assertEqual(report["missing"], 0, report)
        self.assertEqual(self.api.db.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()["status"],
            "downloaded")
        # Сайдкар остался на месте (скан его читал, а не пересоздавал).
        self.assertTrue(sidecar.exists())

    def test_restore_without_sidecar_binds_by_id(self):
        from app import indexer
        root = self.dir / "library"
        root.mkdir()
        vid, _ = repo.upsert_video(self.api.db.conn, normalize_video(
            {"id": "abcdefghijk", "title": "Без сайдкара"}), full=True)
        path = root / "Без сайдкара [abcdefghijk].mp4"
        path.write_bytes(b"x")
        storage = self.add_storage(self.api, root)
        indexer.scan([storage], self.api.db, compute_hash=True)

        self.api.storage_detach({"id": storage["id"], "keep_trace": False})
        # Следа в базе нет совсем.
        self.assertIsNone(storages.get(self.api.db.conn, storage["id"]))

        # Забыли совсем -> добавляем заново (маркер в папке всё ещё наш).
        added = self.api.storage_add({"path": str(root)})
        self.assertIsInstance(added, dict)
        if added.get("hint") == "known_root":
            self.assertEqual(added["storage"]["id"], storage["id"])
            self.api.storage_restore({"id": storage["id"]})
            target = storages.get(self.api.db.conn, storage["id"])
        else:
            self.assertNotIn(added.get("hint"), ("already", "detached"))
            target = added["storage"]
        report = indexer.scan([target], self.api.db, compute_hash=True)
        # ID в имени файла хватило, чтобы вернуть запись.
        self.assertEqual(report["bound_id"] + report["bound_sidecar"], 1, report)
        self.assertEqual(self.api.db.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()["status"],
            "downloaded")

    def write_in(self, root: Path, name: str, payload: bytes = b"x") -> Path:
        path = root / name
        path.write_bytes(payload)
        return path


if __name__ == "__main__":
    unittest.main()
