"""Хранилище доверия устройствам mesh (B0): статусы, решения, барьер.

Устройство = его Ed25519-ключ. Три статуса: TOFU (запомнено автоматически
при первом канале), подтверждено (человек сверил отпечаток) и отозвано
(манифесты и файлы ключа не принимаются - барьер проверяется в mesh.fetch).
Пин адресов (mesh_pins.json) остаётся транспортным кэшем и переносится
сюда один раз, идемпотентно.
"""

import tempfile
import unittest
from pathlib import Path

from app import repo
from app.db import Database

KEY_A = "a1" * 32          # 64 hex - валидный ключ устройства
KEY_B = "b2" * 32


class TrustCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "library.db")
        self.conn = self.db.conn
        # addCleanup идут в обратном порядке: сначала закрыть базу
        # (WAL-файлы держат дескриптор), потом снять временную папку.
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.db.close)

    def rows(self):
        return repo.truststore_list(self.conn)


class TestStatuses(TrustCase):
    def test_migration_created_table(self):
        tables = {row["name"] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("truststore", tables)

    def test_add_is_tofu_with_addr(self):
        device = repo.truststore_add(self.conn, KEY_A, addr="1.2.3.4:38574")
        self.assertEqual(device["status"], "tofu")
        self.assertIsNone(device["verified_at"])
        self.assertEqual(device["last_addr"], "1.2.3.4:38574")
        self.assertEqual(repo.truststore_get(self.conn, KEY_A)["pubkey"],
                         KEY_A)

    def test_repeat_contact_keeps_human_decisions(self):
        """Повторный TOFU-контакт не переписывает сверку и отзыв."""
        repo.truststore_verify(self.conn, KEY_A, label="Ноутбук")
        again = repo.truststore_add(self.conn, KEY_A, addr="5.6.7.8:1")
        self.assertEqual(again["status"], "verified")
        self.assertEqual(again["label"], "Ноутбук")
        self.assertEqual(again["last_addr"], "5.6.7.8:1",
                         "свежий адрес запоминается всегда")

    def test_verify_and_revoke_restore_cycle(self):
        repo.truststore_add(self.conn, KEY_A, addr="1.2.3.4:1")
        verified = repo.truststore_verify(self.conn, KEY_A)
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["verified_via"], "manual")
        self.assertTrue(verified["verified_at"])

        revoked = repo.truststore_revoke(self.conn, KEY_A)
        self.assertEqual(revoked["status"], "revoked")
        self.assertIn(KEY_A, repo.truststore_revoked(self.conn))

        # Снятие отзыва возвращает в TOFU: сверку сделает человек заново
        restored = repo.truststore_restore(self.conn, KEY_A)
        self.assertEqual(restored["status"], "tofu")
        self.assertIsNone(restored["verified_at"])
        self.assertNotIn(KEY_A, repo.truststore_revoked(self.conn))

    def test_verify_clears_revocation(self):
        repo.truststore_revoke(self.conn, KEY_A)
        device = repo.truststore_verify(self.conn, KEY_A)
        self.assertEqual(device["status"], "verified",
                         "явная сверка снимает и отзыв")

    def test_label_and_forget(self):
        repo.truststore_add(self.conn, KEY_A)
        repo.truststore_set_label(self.conn, KEY_A, "  Ферма  ")
        self.assertEqual(repo.truststore_get(self.conn, KEY_A)["label"],
                         "Ферма")
        self.assertTrue(repo.truststore_forget(self.conn, KEY_A))
        self.assertIsNone(repo.truststore_get(self.conn, KEY_A))
        self.assertFalse(repo.truststore_forget(self.conn, KEY_A))

    def test_list_sorts_revoked_last(self):
        repo.truststore_add(self.conn, KEY_A)
        repo.truststore_verify(self.conn, KEY_B, label="А-устройство")
        repo.truststore_revoke(self.conn, KEY_A)
        statuses = [row["status"] for row in self.rows()]
        self.assertEqual(statuses, ["verified", "revoked"])

    def test_bad_key_refused(self):
        for bad in ("", "zz" * 32, "ab12", "A1" * 31 + "!", None):
            with self.assertRaises(ValueError):
                repo.truststore_add(self.conn, bad)
            with self.assertRaises(ValueError):
                repo.truststore_revoke(self.conn, bad)


class TestImportPins(TrustCase):
    def test_import_creates_tofu_devices(self):
        pins = {"1.2.3.4:38574": KEY_A, "5.6.7.8:38574": KEY_B,
                "мусор": "не-ключ"}
        self.assertEqual(repo.truststore_import_pins(self.conn, pins), 2)
        devices = {row["pubkey"]: row for row in self.rows()}
        self.assertEqual(devices[KEY_A]["status"], "tofu")
        self.assertEqual(devices[KEY_A]["last_addr"], "1.2.3.4:38574")
        # Повторный импорт идемпотентен - дублей и сбросов нет
        self.assertEqual(repo.truststore_import_pins(self.conn, pins), 0)
        self.assertEqual(len(self.rows()), 2)

    def test_import_keeps_human_decisions(self):
        repo.truststore_verify(self.conn, KEY_A, label="Дом")
        repo.truststore_import_pins(self.conn, {"9.9.9.9:1": KEY_A})
        device = repo.truststore_get(self.conn, KEY_A)
        self.assertEqual(device["status"], "verified",
                         "импорт не должен понижать подтверждение")


class TestRevokedBarrier(TrustCase):
    def test_drop_revoked_filters_pairs(self):
        """Барьер в mesh.fetch: пары отозванного ключа отсекаются."""
        from app.mesh import fetch as mesh_fetch
        repo.truststore_revoke(self.conn, KEY_A)
        logs = []
        pairs = [("1.2.3.4:1", {"pubkey": KEY_A, "sha256": "x"}),
                 ("5.6.7.8:1", {"pubkey": KEY_B, "sha256": "y"})]
        kept = mesh_fetch._drop_revoked(self.conn, pairs, logs.append)
        self.assertEqual([addr for addr, _ in kept], ["5.6.7.8:1"])
        self.assertEqual(logs, ["Mesh: пропущено адресатов с отозванным "
                                "доверием: 1"])

    def test_drop_revoked_is_noop_without_bans(self):
        from app.mesh import fetch as mesh_fetch
        pairs = [("1.2.3.4:1", {"pubkey": KEY_A})]
        logs = []
        self.assertEqual(mesh_fetch._drop_revoked(self.conn, pairs,
                                                  logs.append), pairs)
        self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
