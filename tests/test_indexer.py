"""Скан библиотеки: опознание файлов, переезды, пропажи, отчёт."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import aggregate, repo
from app.db import Database
from app.indexer import SIDECAR_SUFFIX, file_hash, scan
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
        # Файлов теперь два: видео и дописанный самоусилением сайдкар.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE missing=0 AND kind='video'"
        ).fetchone()["n"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE kind='sidecar'"
        ).fetchone()["n"], 1)
        self.assertGreaterEqual(again["sidecars"], 0)

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


class TestSelfHeal(ScanCase):
    """Самоусиление: опознанный файл без post.json получает его.

    В этом стоит вся идея повторной переинициализации: чем больше
    сайдкаров в папке, тем дешевле её восстановить после отвязки.
    """

    def test_scan_writes_missing_sidecar(self):
        repo.upsert_video(self.conn, normalize_video(
            {"id": "dQw4w9WgXcQ", "title": "Клип", "channel": "Автор",
             "upload_date": "20091025", "duration": 213}), full=True)
        self.write("Клип [dQw4w9WgXcQ].mp4", b"content")

        report = self.run_scan()
        self.assertEqual(report["bound_id"], 1)
        self.assertEqual(report["sidecars"], 1)

        sidecar = self.root / ("Клип [dQw4w9WgXcQ]" + SIDECAR_SUFFIX)
        self.assertTrue(sidecar.exists(), "сайдкар не дописан")
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(data["remote_id"], "dQw4w9WgXcQ")
        self.assertEqual(data["info"]["title"], "Клип")
        self.assertTrue(data["hash"].startswith("sha256:"))
        # И он же теперь основной источник при опознании: строка files есть.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE kind='sidecar'"
        ).fetchone()["n"], 1)

        # Повторный скан ничего не переписывает: видео уже известно по
        # пути (fast-path), а сам сайдкар - не медиа, он просто пропущен.
        again = self.run_scan()
        self.assertEqual(again["sidecars"], 0)
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(again["added"], 0)

    def test_local_video_gets_no_sidecar(self):
        # У локального файла нет площадочной личности: сайдкар с его
        # «remote_id» read_sidecar не поймёт, поэтому не пишем.
        self.write("Ни с чем не совпало.mp4", b"content")
        report = self.run_scan()
        self.assertEqual(report["added"], 1)
        self.assertEqual(report["sidecars"], 0)

    def test_sidecar_can_be_switched_off(self):
        repo.upsert_video(self.conn, normalize_video(
            {"id": "dQw4w9WgXcQ", "title": "Клип"}), full=True)
        self.write("Клип [dQw4w9WgXcQ].mp4", b"content")
        report = scan([{"path": str(self.root), "recursive": True,
                        "enabled": True}], self.db,
                      compute_hash=True, keep_sidecar=False)
        self.assertEqual(report["sidecars"], 0)
        self.assertFalse((self.root / ("Клип [dQw4w9WgXcQ]" +
                                       SIDECAR_SUFFIX)).exists())


class TestAggregateBinding(ScanCase):
    """Опознание по агрегату папки (.omnistash.json, режим «всё в видео»).

    Файл без пофайлового сайдкара, но с записью в общем json папки -
    полная правда о нём: скан понимает оба формата независимо от
    настройки, поэтому переключение режима библиотеку не ломает.
    """

    def _record(self, vid, title, digest="sha256:aa"):
        return {"omnistash": 1, "platform": "youtube", "remote_id": vid,
                "path": f"Пост [{vid}].mp4", "hash": digest,
                "info": {"id": vid, "title": title, "channel": "Автор",
                         "upload_date": "20250304"}}

    def test_bound_by_aggregate_record(self):
        self.write("Пост [abc000000001].mp4", b"payload-a")
        aggregate.write_record(self.root, "abc000000001",
                               self._record("abc000000001",
                                            "Название из агрегата"))
        report = self.run_scan()
        self.assertEqual(report["bound_sidecar"], 1)
        row = self.rows()[0]
        self.assertEqual(row["remote_id"], "abc000000001")
        self.assertEqual(row["title"], "Название из агрегата")
        # Агрегат - не файл видео: строка ровно одна, на сам mp4.
        detail = repo.video_detail(self.conn, row["id"])
        kinds = {f["kind"] for f in detail["files"]}
        self.assertEqual(kinds, {"video"})

    def test_perfile_sidecar_wins_over_aggregate(self):
        # Оба формата легальны одновременно (библиотека на разных
        # режимах): пофайловый сайдкар - старший по приоритету.
        self.write("Пост [abc000000001].mp4", b"payload-a")
        (self.root / ("Пост [abc000000001]" + SIDECAR_SUFFIX)).write_text(
            json.dumps({"omnistash": 1, "remote_id": "abc000000001",
                        "info": {"id": "abc000000001",
                                 "title": "Из сайдкара"}}), encoding="utf-8")
        aggregate.write_record(self.root, "abc000000001",
                               self._record("abc000000001", "Из агрегата"))
        report = self.run_scan()
        self.assertEqual(report["bound_sidecar"], 1)
        self.assertEqual(self.rows()[0]["title"], "Из сайдкара")

    def test_record_for_other_video_is_not_used(self):
        # ID в имени не совпал ни с одной записью - шаги идут дальше,
        # и файл честно становится локальным, а не получает чужие данные.
        self.write("Ни с чем не совпало [zzz999999999].mp4")
        aggregate.write_record(self.root, "aaaaaaaaaaa",
                               self._record("aaaaaaaaaaa", "Чужой"))
        report = self.run_scan()
        self.assertEqual(report["added"], 1)
        self.assertEqual(report["bound_sidecar"], 0)
        self.assertEqual(self.rows()[0]["platform"], "local")


class TestSelfHealEmbed(ScanCase):
    """Самоусиление в режиме «всё в видео»: агрегат вместо post.json."""

    def _video(self):
        repo.upsert_video(self.conn, normalize_video(
            {"id": "dQw4w9WgXcQ", "title": "Клип", "channel": "Автор",
             "upload_date": "20091025", "duration": 213}), full=True)

    def test_scan_writes_aggregate_not_sidecar(self):
        self._video()
        self.write("Клип [dQw4w9WgXcQ].mp4", b"content")
        report = self.run_scan(sidecar_mode="embed")
        self.assertEqual(report["bound_id"], 1)
        self.assertEqual(report["sidecars"], 1)
        self.assertFalse(
            (self.root / ("Клип [dQw4w9WgXcQ]" + SIDECAR_SUFFIX)).exists(),
            "в режиме embed пофайловый сайдкар не появляется")
        record = aggregate.find(self.root, "dQw4w9WgXcQ")
        self.assertIsNotNone(record, "запись агрегата не дописана")
        self.assertEqual(record["info"]["title"], "Клип")
        self.assertEqual(record["path"], "Клип [dQw4w9WgXcQ].mp4")
        self.assertTrue(record["hash"].startswith("sha256:"))

        # Повторный скан: запись уже есть - ничего не переписывается.
        again = self.run_scan(sidecar_mode="embed")
        self.assertEqual(again["sidecars"], 0)
        self.assertEqual(again["unchanged"], 1)

    def test_wrong_hash_in_record_is_corrected(self):
        # Запись с неверным хешем (ручная правка/последствия перезаписи):
        # когда скан считает настоящий хеш, запись освежается.
        self._video()
        self.write("Клип.mp4", b"content")          # без ID в имени -> шаг 4
        aggregate.write_record(self.root, "dQw4w9WgXcQ", {
            "omnistash": 1, "platform": "youtube", "remote_id": "dQw4w9WgXcQ",
            "path": "Клип.mp4", "hash": "sha256:wrong",
            "info": {"id": "dQw4w9WgXcQ", "title": "Клип"}})
        report = self.run_scan(sidecar_mode="embed")
        self.assertEqual(report["bound_title"], 1)
        self.assertEqual(report["sidecars"], 1)
        record = aggregate.find(self.root, "dQw4w9WgXcQ")
        self.assertEqual(record["hash"],
                         file_hash(self.root / "Клип.mp4"),
                         "хеш в записи должен стать настоящим")

    def test_files_mode_leaves_no_aggregate(self):
        self._video()
        self.write("Клип [dQw4w9WgXcQ].mp4", b"content")
        self.run_scan()                            # режим files по умолчанию
        self.assertFalse((self.root / aggregate.FILENAME).exists())
        self.assertTrue((self.root / ("Клип [dQw4w9WgXcQ]"
                                      + SIDECAR_SUFFIX)).exists())


if __name__ == "__main__":
    unittest.main()
