"""Миграция v1 -> v2: хранилища и перестройка таблицы videos.

Главная опасность этого шага - DROP TABLE videos при включённых внешних
ключах: SQLite делает неявный DELETE FROM, а files/playlist_items висят на
CASCADE - индекс мог бы исчезнуть целиком. Тесты ниже проверяют именно это,
плюс идемпотентность повторного прогона и работоспособность FTS после
перестройки.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import db as db_mod


class MigrationCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = sqlite3.connect(Path(self._tmp.name) / "old.db")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        db_mod._migrate_v1(self.conn)          # база «старой» версии
        self._seed()

    def tearDown(self):
        self.conn.close()

    def _seed(self):
        conn = self.conn
        conn.execute(
            "INSERT INTO channels (platform, remote_id, title, created_at, "
            "updated_at) VALUES ('youtube','UCseed','Автор','t','t')")
        conn.execute(
            """INSERT INTO videos (key, platform, remote_id, channel_id, title,
                                   status, first_seen_at, updated_at)
               VALUES ('youtube:vid000000001','youtube','vid000000001',1,
                       'Докачанное видео','downloaded','t','t')""")
        conn.execute(
            "INSERT INTO playlists (platform, remote_id, title, created_at, "
            "updated_at) VALUES ('youtube','PLseed','Плейлист','t','t')")
        conn.execute(
            "INSERT INTO playlist_items (playlist_id, video_id, position, "
            "added_at) VALUES (1,1,1,'t')")
        # Файл лежит под корнем, которого в настройках ещё нет: бэкфилл
        # storage_id делает bootstrap, здесь важна сама строка.
        conn.execute(
            "INSERT INTO files (video_id, kind, path, size) "
            "VALUES (1,'video','D:\\\\lib\\\\video.mp4',1000)")
        conn.commit()

    def migrate(self):
        db_mod._migrate_v2(self.conn)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


class TestSchema(MigrationCase):
    def test_storages_and_columns_appear(self):
        self.migrate()
        self.assertTrue(db_mod._table_exists(self.conn, "storages"))
        self.assertIn("storage_id", db_mod._columns(self.conn, "files"))
        self.assertIn("rel_path", db_mod._columns(self.conn, "files"))
        self.assertIn("detached_from", db_mod._columns(self.conn, "videos"))
        self.assertIn("target_storage_id", db_mod._columns(self.conn, "videos"))
        self.assertIn("last_error", db_mod._columns(self.conn, "videos"))
        self.assertIn("storage_id", db_mod._columns(self.conn, "channels"))
        self.assertIn("storage_id", db_mod._columns(self.conn, "playlists"))

    def test_data_survives_rebuild(self):
        self.migrate()
        video = self.conn.execute("SELECT * FROM videos").fetchone()
        self.assertEqual(video["title"], "Докачанное видео")
        self.assertEqual(video["status"], "downloaded")
        self.assertEqual(video["key"], "youtube:vid000000001")
        # Связи живы: главный страх - CASCADE при DROP TABLE.
        self.assertEqual(self.count("files"), 1, "files уничтожены при DROP")
        self.assertEqual(self.count("playlist_items"), 1)
        self.assertEqual(self.count("channels"), 1)
        self.assertEqual(self.count("playlists"), 1)

    def test_detached_status_accepted_after_rebuild(self):
        self.migrate()
        # Параметры вместо литералов: обратные слэши в SQL-строке - ловушка.
        self.conn.execute(
            "UPDATE videos SET status='detached', detached_from=? WHERE id=1",
            ("D:\\lib",))
        row = self.conn.execute(
            "SELECT status, detached_from FROM videos WHERE id=1").fetchone()
        self.assertEqual(row["status"], "detached")
        self.assertEqual(row["detached_from"], "D:\\lib")

    def test_unknown_status_still_rejected(self):
        self.migrate()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE videos SET status='прочерк' WHERE id=1")

    def test_fts_works_after_rebuild(self):
        self.migrate()
        # Старая строка должна найтись в индексе (он пережил перестройку)...
        found = self.conn.execute(
            "SELECT rowid FROM videos_fts WHERE videos_fts MATCH 'докачанное'"
        ).fetchall()
        self.assertTrue(found, "FTS потерял существующие строки")
        # ...а новые триггеры должны работать: без них правка не попадёт в индекс.
        self.conn.execute(
            "INSERT INTO videos (key, platform, remote_id, title, status, "
            "first_seen_at, updated_at) VALUES ('youtube:vid000000002',"
            "'youtube','vid000000002','Свежая строка','known','t','t')")
        self.conn.execute(
            "UPDATE videos SET title='Совсем другое' WHERE remote_id='vid000000002'")
        fresh = self.conn.execute(
            "SELECT rowid FROM videos_fts WHERE videos_fts MATCH 'совсем'"
        ).fetchall()
        self.assertTrue(fresh, "триггер FTS после перестройки не работает")
        # И удаление из videos не должно оставлять призрака в FTS.
        self.conn.execute("DELETE FROM videos WHERE remote_id='vid000000002'")
        ghost = self.conn.execute(
            "SELECT rowid FROM videos_fts WHERE videos_fts MATCH 'совсем'"
        ).fetchall()
        self.assertFalse(ghost, "FTS не удалил строку")

    def test_indexes_recreated(self):
        self.migrate()
        names = {row["name"] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for index in ("videos_channel_idx", "videos_status_idx",
                      "videos_title_idx", "videos_date_idx"):
            self.assertIn(index, names)


class TestIdempotency(MigrationCase):
    def test_second_run_is_safe(self):
        self.migrate()
        db_mod._migrate_v2(self.conn)      # обрыв + повтор
        self.assertEqual(self.count("videos"), 1)
        self.assertEqual(self.count("files"), 1)

    def test_partial_rebuild_is_picked_up(self):
        """Обрыв между DROP videos и RENAME videos_v2 -> повтор доделывает."""
        # Воспроизводим ровно то промежуточное состояние: копия готова,
        # оригинала уже нет (FK выключены - иначе каскад бы всё снёс).
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("DROP TABLE IF EXISTS videos_v2")
        self.conn.executescript(db_mod._VIDEOS_V2)
        marks = ", ".join(db_mod._VIDEOS_V1_COLUMNS)
        self.conn.execute(
            f"INSERT INTO videos_v2 ({marks}) SELECT {marks} FROM videos")
        self.conn.execute("DROP TABLE videos")
        self.conn.execute("PRAGMA foreign_keys=ON")

        db_mod._migrate_v2(self.conn)
        self.assertTrue(db_mod._table_exists(self.conn, "videos"))
        self.assertFalse(db_mod._table_exists(self.conn, "videos_v2"))
        self.assertEqual(self.count("videos"), 1)
        self.assertEqual(self.conn.execute(
            "SELECT title FROM videos").fetchone()["title"],
            "Докачанное видео")
        # Файлы пережили и это испытание (были при DROP с выключенными FK).
        self.assertEqual(self.count("files"), 1)

    def test_add_column_twice_is_safe(self):
        # _add_column должна молча пропускать существующую колонку.
        self.migrate()
        db_mod._add_column(self.conn, "files", "storage_id TEXT")
        self.assertIn("storage_id", db_mod._columns(self.conn, "files"))


if __name__ == "__main__":
    unittest.main()
