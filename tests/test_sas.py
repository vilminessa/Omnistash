"""SAS «12 слов» (1.1): словарь, обе дороги ECDH, живой обмен.

Ключевой инвариант: обе стороны считают ОДНИ И ТЕ ЖЕ слова из своего
ECDH-вывода общего секрета; MITM подставил бы свои ephemeral-ключи ->
слова разошлись бы. Интеграция гоняет реальный TLS-сервер участника и
сверяет слова клиента с серверными (из лога).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.mesh import identity, sas


class TestWordList(unittest.TestCase):
    def test_list_shape(self):
        self.assertEqual(len(sas.WORDS), sas.WORDS_COUNT)
        self.assertEqual(len(set(sas.WORDS)), sas.WORDS_COUNT,
                         "слова должны быть уникальны")
        for word in sas.WORDS[:50] + sas.WORDS[-50:]:
            self.assertGreaterEqual(len(word), 6, word)
            self.assertTrue(word.isalpha() and word.islower(), word)

    def test_words_deterministic(self):
        secret = b"\x01" * 32
        first = sas.words_from_secret(secret)
        second = sas.words_from_secret(secret)
        self.assertEqual(first, second)
        self.assertEqual(len(first), sas.SAS_LEN)
        for word in first:
            self.assertIn(word, sas.WORDS)


class TestEcdh(unittest.TestCase):
    def test_both_roads_agree(self):
        """Обе дороги общего секрета дают одни и те же слова."""
        a_priv, a_pub = sas.own_ephemeral()
        b_priv, b_pub = sas.own_ephemeral()
        shared_a = sas.shared_secret(a_priv, b_pub)
        shared_b = sas.shared_secret(b_priv, a_pub)
        self.assertEqual(shared_a, shared_b)
        self.assertEqual(sas.words_from_secret(shared_a),
                         sas.words_from_secret(shared_b))

    def test_impostor_words_differ(self):
        """Чужой ephemeral (MITM-посредник) даёт другие слова."""
        a_priv, _a_pub = sas.own_ephemeral()
        _b_priv, b_pub = sas.own_ephemeral()
        _mitv_priv, mitv_pub = sas.own_ephemeral()
        honest = sas.sas_words(a_priv, b_pub)
        spoofed = sas.sas_words(a_priv, mitv_pub)
        self.assertNotEqual(honest, spoofed,
                            "посредник обязан ломать сверку слов")


class TestBadKeys(unittest.TestCase):
    def test_shared_secret_validation(self):
        priv, _pub = sas.own_ephemeral()
        for bad in ("", "zz", "00" * 31, "00" * 33):
            with self.assertRaises(ValueError):
                sas.shared_secret(priv, bad)


class TestLiveExchange(unittest.TestCase):
    """Живой TLS-сервер участника: слова клиента == слова сервера."""

    def setUp(self):
        from app.mesh import channel as channel_mod
        from app.mesh import identity
        self._prof = tempfile.TemporaryDirectory()
        self.addCleanup(self._prof.cleanup)
        prof = Path(self._prof.name)
        for module in (identity, channel_mod):
            patcher = mock.patch.object(module, "profile_dir",
                                        lambda: prof)
            patcher.start()
            self.addCleanup(patcher.stop)
        identity.reset_cache()
        self.addCleanup(identity.reset_cache)
        channel_mod.reset_cache()
        self.addCleanup(channel_mod.reset_cache)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from app import db as db_mod
        from app.mesh import client as mesh_client
        from app.mesh import server as mesh_server
        self.client = mesh_client
        self.db = db_mod.Database(Path(self._tmp.name) / "lib.db")
        self.addCleanup(self.db.close)
        self.server_logs = []
        self.server = mesh_server.PeerServer(
            self.db, port=0, log=self.server_logs.append)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_words_agree_end_to_end(self):
        result = self.client.sas_begin(f"127.0.0.1:{self.server.port}")
        self.assertEqual(result["pubkey"], identity.get_identity()[1].hex())
        self.assertEqual(len(result["words"]), sas.SAS_LEN)
        server_words = [line.rsplit(": ", 1)[1] for line in self.server_logs
                        if "SAS: слова" in line]
        self.assertTrue(server_words, f"сервер не журналировал слова: "
                        f"{self.server_logs}")
        self.assertEqual(" ".join(result["words"]), server_words[-1],
                         "слова клиента и сервера должны совпадать")

    def test_dead_addr_is_honest_error(self):
        probe = __import__("socket").socket()
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
        probe.close()
        with self.assertRaises((RuntimeError, OSError)):
            self.client.sas_begin(f"127.0.0.1:{dead}", timeout=2.0)


if __name__ == "__main__":
    unittest.main()
