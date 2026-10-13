"""B3: подписанные каталоги участников и LWW-зеркало индекса.

Каталог проверяет подпись ЧУЖОГО ключа (identity в тестовом профиле);
зеркало живёт по правилу «версия строки сравнивается только внутри
одного узла» - шкалы времени разных ПК не сравниваются.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import repo
from app.db import Database
from app.mesh import catalog, identity

KEY_A = "a1" * 32
KEY_B = "b2" * 32


def entry(remote_id="vid1", **overrides):
    item = {"remote_id": remote_id, "title": "Клип", "duration_s": 100,
            "synced_at": "2026-10-01T09:00:00",
            "updated_at": "2026-10-01T10:00:00",
            "sha256": "sha256:" + "ab" * 32}
    item.update(overrides)
    return item


class CatalogCase(unittest.TestCase):
    def setUp(self):
        self._prof = tempfile.TemporaryDirectory()
        self.addCleanup(self._prof.cleanup)
        patcher = mock.patch.object(identity, "profile_dir",
                                    lambda: Path(self._prof.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        identity.reset_cache()
        self.addCleanup(identity.reset_cache)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "library.db")
        self.addCleanup(self.db.close)
        self.conn = self.db.conn

    def seed_own_video(self, remote_id="vid1", platform="youtube"):
        self.conn.execute(
            "INSERT INTO videos (key, platform, remote_id, title, status, "
            "first_seen_at, updated_at) VALUES (?,?,?,?, 'known','t','t')",
            (f"{platform}:{remote_id}", platform, remote_id, "Мой клип"))


class TestBuildVerify(CatalogCase):
    def test_roundtrip(self):
        snapshot = catalog.build([entry(), entry("vid2", title="Второй")])
        self.assertEqual(snapshot["pubkey"],
                         identity.get_identity()[1].hex())
        entries, error = catalog.verify(snapshot)
        self.assertIsNone(error, error)
        self.assertEqual([e["remote_id"] for e in entries], ["vid1", "vid2"],
                         "записи канонизируются по remote_id")

    def test_tampered_entry_refused(self):
        snapshot = catalog.build([entry()])
        snapshot["entries"][0]["title"] = "Подделка"
        entries, error = catalog.verify(snapshot)
        self.assertIsNone(entries)
        self.assertIn("подпись", error)

    def test_shape_refusals(self):
        for bad in (None, [], "строка", {}, {"entries": []},
                    {"entries": [], "pubkey": "zz", "sig": "00"},
                    {"entries": [], "pubkey": "ab" * 32, "sig": "00"}):
            entries, error = catalog.verify(bad)
            self.assertIsNone(entries, bad)
            self.assertTrue(error)

    def test_duplicate_remote_id_refused(self):
        # дубль внутри ПОДПИСАННОГО снапшота (build его не снимает -
        # защита от кривого участника)
        snapshot = catalog.build([entry("vid1"), entry("vid1")])
        entries, error = catalog.verify(snapshot)
        self.assertIsNone(entries)
        self.assertIn("повторяющийся", error)


class TestApplyMirror(CatalogCase):
    def test_lww_by_own_version(self):
        stats = repo.catalog_apply(self.conn, KEY_A, [entry()])
        self.assertEqual(stats["added"], 1)

        stats = repo.catalog_apply(
            self.conn, KEY_A,
            [entry(title="Новое имя", updated_at="2026-10-02T10:00:00")])
        self.assertEqual((stats["added"], stats["updated"]), (0, 1))

        # более старая версия ТОГО ЖЕ узла не качает зеркало назад
        stats = repo.catalog_apply(
            self.conn, KEY_A,
            [entry(title="Старое имя", updated_at="2026-10-01T09:00:00")])
        self.assertEqual(stats["unchanged"], 1)
        row = self.conn.execute("SELECT title FROM mesh_catalog "
                                "WHERE origin=? AND remote_id='vid1'",
                                (KEY_A,)).fetchone()
        self.assertEqual(row["title"], "Новое имя")

    def test_snapshot_is_authoritative(self):
        repo.catalog_apply(self.conn, KEY_A,
                           [entry("vid1"), entry("vid2")])
        stats = repo.catalog_apply(self.conn, KEY_A, [entry("vid2")])
        self.assertEqual(stats["removed"], 1)
        rows = repo.catalog_list(self.conn)
        self.assertEqual([r["remote_id"] for r in rows], ["vid2"])

    def test_origins_are_independent(self):
        repo.catalog_apply(self.conn, KEY_A, [entry(title="У А")])
        repo.catalog_apply(self.conn, KEY_B, [entry(title="У Б")])
        rows = {r["origin"]: r for r in repo.catalog_list(self.conn)}
        self.assertEqual(rows[KEY_A]["title"], "У А")
        self.assertEqual(rows[KEY_B]["title"], "У Б")

    def test_list_search_own_and_origins(self):
        self.seed_own_video()
        repo.catalog_apply(self.conn, KEY_A, [entry()],
                           addr="1.2.3.4:38574")
        rows = repo.catalog_list(self.conn, query="КЛИП")
        self.assertEqual(len(rows), 1, "поиск без регистра (кириллица)")
        self.assertEqual(rows[0]["own"], 1, "строка есть и в моём индексе")
        self.assertEqual(repo.catalog_list(self.conn, query="нет-такого"), [])

        origins = repo.catalog_origins(self.conn)
        self.assertEqual(origins[0]["origin"], KEY_A)
        self.assertEqual(origins[0]["entries"], 1)

    def test_own_lookup_and_clear(self):
        self.seed_own_video("vid77")
        self.assertIsNotNone(repo.catalog_own(self.conn, "vid77"))
        self.assertIsNone(repo.catalog_own(self.conn, "vid99"))
        repo.catalog_apply(self.conn, KEY_A, [entry()])
        self.assertEqual(repo.catalog_clear(self.conn, KEY_A), 1)
        self.assertEqual(repo.catalog_list(self.conn), [])
        with self.assertRaises(ValueError):
            repo.catalog_clear(self.conn, "кривой")


if __name__ == "__main__":
    unittest.main()
