"""SAS (1.1): канон a3fa9c7 (векторы) + обёртки API + живой обмен.

Векторы - контракт стабильности: словарь/списки/нарезка битов менять
нельзя, иначе пары разных версий перестанут сходиться. Живой обмен
гоняет реальный TLS-сервер участника и сверяет рендер клиента с серверным
журналом для выбранного формата.
"""

import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.mesh import identity, sas

# Общий вход для всех векторов: shared = 00..1f, идентичности aa/cc.
SHARED = bytes(range(32))
ID_A = "aa" * 32
ID_B = "cc" * 32
DIGEST_HEX = ("413b19380b64ce320f9df8cf0c88c8de"
              "899c6b09c55eb5d78be4022b49b3bf7d")
WORDS_VEC = ["посудомойка", "люлька", "минута", "заря", "катер",
             "заготовка", "карп", "рецепт", "призёр", "ковёр",
             "инвентарь", "чечевица"]
EMOJI_VEC = "🐱🐻🎁🐵🌽🍎🧩🎯"
ICONS_VEC = [51, 35, 8, 15, 39, 31, 35, 15]
DIGITS_VEC = "25480802"


class TestCanon(unittest.TestCase):
    def test_validate_canon(self):
        """Канон: словарь 1024 без дублей, эмодзи 64, сетки иконок."""
        sas.validate()
        from app.mesh.sas_words import SAS_WORDS
        self.assertEqual(len(SAS_WORDS), 1024)

    def test_sas_bytes_is_symmetric_and_bound(self):
        """Порядок идентичностей не важен; пара и версия - важны."""
        digest = sas.sas_bytes(SHARED, ID_A, ID_B)
        self.assertEqual(len(digest), 32)
        self.assertEqual(digest, sas.sas_bytes(SHARED, ID_B, ID_A))
        self.assertNotEqual(digest, sas.sas_bytes(b"\x01" * 32, ID_A, ID_B),
                            "другой ECDH-секрет - другой SAS (ловит MITM)")
        self.assertNotEqual(digest, sas.sas_bytes(SHARED, ID_A, "bb" * 32),
                            "другая пара устройств - другой SAS")

    def test_methods_exposed(self):
        self.assertEqual(sas.METHODS, ("words", "emoji", "icons", "digits"))
        for method in sas.METHODS:
            self.assertIn(method, sas.METHOD_LABELS)


class TestVector(unittest.TestCase):
    """Фиксированный вектор: вход -> ровно эти значения."""

    def setUp(self):
        self.digest = sas.sas_bytes(SHARED, ID_A, ID_B)

    def test_digest(self):
        self.assertEqual(self.digest.hex(), DIGEST_HEX)

    def test_words_vector(self):
        got = sas.words(self.digest)
        self.assertEqual(got, WORDS_VEC)
        self.assertEqual(len(got), 12, "ровно 12 слов")

    def test_emoji_vector(self):
        self.assertEqual("".join(sas.emoji(self.digest)), EMOJI_VEC)

    def test_icons_vector_and_svg(self):
        indexes = sas.icons(self.digest)
        self.assertEqual(indexes, ICONS_VEC)
        for index in indexes:
            svg = sas.svg_icon(index)
            self.assertIn("<svg", svg)
            self.assertIn("viewBox", svg)

    def test_digits_vector(self):
        self.assertEqual(sas.digits(self.digest), DIGITS_VEC)

    def test_render_dispatch(self):
        for method, expected in (("words", WORDS_VEC),
                                 ("emoji", list(EMOJI_VEC)),
                                 ("digits", DIGITS_VEC)):
            self.assertEqual(sas.render(method, self.digest), expected)
        with self.assertRaises(ValueError):
            sas.render("png", self.digest)


class TestEcdhWrappers(unittest.TestCase):
    def test_both_roads_agree(self):
        a_priv, a_pub = sas.own_ephemeral()
        b_priv, b_pub = sas.own_ephemeral()
        self.assertEqual(sas.shared_secret(a_priv, b_pub),
                         sas.shared_secret(b_priv, a_pub))

    def test_impostor_digest_differ(self):
        """Чужой ephemeral (MITM) даёт другой дайджест - слова разойдутся."""
        a_priv, a_pub = sas.own_ephemeral()
        _b_priv, b_pub = sas.own_ephemeral()
        _m_priv, m_pub = sas.own_ephemeral()
        honest = sas.digest_for(a_priv, b_pub, ID_A, ID_B)
        spoofed = sas.digest_for(a_priv, m_pub, ID_A, ID_B)
        self.assertNotEqual(honest, spoofed)

    def test_bad_keys(self):
        priv, _pub = sas.own_ephemeral()
        for bad in ("", "zz", "00" * 31, "00" * 33):
            with self.assertRaises(ValueError):
                sas.shared_secret(priv, bad)


class TestLiveExchange(unittest.TestCase):
    """Живой TLS-сервер участника: рендер клиента == рендер сервера."""

    def setUp(self):
        from app.mesh import channel as channel_mod
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

    def _server_render(self, method: str) -> str:
        lines = [line for line in self.server_logs
                 if line.startswith(f"SAS[{method}]")]
        self.assertTrue(lines, f"сервер не журналировал {method}: "
                        f"{self.server_logs}")
        return lines[-1].rsplit(": ", 1)[1]

    def test_words_end_to_end(self):
        result = self.client.sas_begin(f"127.0.0.1:{self.server.port}",
                                       method="words")
        self.assertEqual(result["pubkey"], identity.get_identity()[1].hex())
        self.assertEqual(len(result["shown"]), 12)
        self.assertEqual(" ".join(result["shown"]),
                         self._server_render("words"),
                         "слова клиента и сервера должны совпадать")

    def test_digits_end_to_end(self):
        result = self.client.sas_begin(f"127.0.0.1:{self.server.port}",
                                       method="digits")
        self.assertEqual(result["shown"], self._server_render("digits"))

    def test_unknown_method_refused(self):
        with self.assertRaises(RuntimeError):
            self.client.sas_begin(f"127.0.0.1:{self.server.port}",
                                  method="png")

    def test_dead_addr_is_honest_error(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
        probe.close()
        with self.assertRaises((RuntimeError, OSError)):
            self.client.sas_begin(f"127.0.0.1:{dead}", timeout=2.0)


if __name__ == "__main__":
    unittest.main()
