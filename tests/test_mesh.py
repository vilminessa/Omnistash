"""Mesh-M1: идентичность, подписанные манифесты, приём, стоп-лист.

Сети здесь нет: всё проверяется на локальных объектах. Структурная
сверка (ffprobe) подменяется - важна логика решений, а не сам ffprobe.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import db as db_mod
from app.mesh import feedback, identity, manifest, verify


class IdentityCase(unittest.TestCase):
    """Профиль изолируется: ключ не должен тащиться между тестами."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(
            identity, "profile_dir",
            lambda: Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        identity.reset_cache()
        self.addCleanup(identity.reset_cache)

    def key_file(self) -> Path:
        return Path(self._tmp.name) / identity.KEY_FILE


class TestIdentity(IdentityCase):
    def test_stable_within_profile_and_created_lazily(self):
        key1, pub1 = identity.get_identity()
        key2, pub2 = identity.get_identity()
        self.assertIs(key1, key2, "ключ должен кэшироваться в памяти")
        self.assertEqual(pub1, pub2)
        self.assertTrue(self.key_file().is_file())
        # Сброс кэша (новая сессия) - тот же ключ с диска.
        identity.reset_cache()
        _key3, pub3 = identity.get_identity()
        self.assertEqual(pub1, pub3, "ключ должен переживать перезапуск")

    def test_key_file_is_encrypted(self):
        identity.get_identity()
        blob = self.key_file().read_bytes()
        self.assertNotIn(b"BEGIN PRIVATE KEY", blob,
                         "приватный ключ не должен лежать открытым текстом")

    def test_node_id_is_20_bytes(self):
        self.assertEqual(len(identity.node_id()), 20)

    def test_sign_and_verify(self):
        message = b"omnistash-mesh-test"
        sig = identity.sign(message)
        _key, pub = identity.get_identity()
        self.assertTrue(identity.verify(pub, message, sig))
        self.assertFalse(identity.verify(pub, "другое сообщение".encode(),
                                         sig))
        # Чужая подпись и мусорный ключ.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import \
            Ed25519PrivateKey
        foreign = Ed25519PrivateKey.generate().public_key().public_bytes(
            encoding=encoding_raw(), format=format_raw())
        self.assertFalse(identity.verify(foreign, message, sig))
        self.assertFalse(identity.verify(pub, message, b"short"))


def encoding_raw():
    from cryptography.hazmat.primitives import serialization
    return serialization.Encoding.Raw


def format_raw():
    from cryptography.hazmat.primitives import serialization
    return serialization.PublicFormat.Raw


class TestManifest(IdentityCase):
    def _build(self, **kwargs):
        args = {"remote_id": "dQw4w9WgXcQ",
                "digest": "sha256:" + "ab" * 32,
                "size": 12345,
                "synced_at": "2026-10-11T09:00:00"}
        args.update(kwargs)
        return manifest.build(**args)

    def test_roundtrip(self):
        item = self._build()
        self.assertIsNone(manifest.verify(item), "своя подпись должна сходить")
        self.assertEqual(item["remote_id"], "dQw4w9WgXcQ")
        self.assertEqual(len(item["pubkey"]), 64)   # hex 32 байт

    def test_tampered_fields_rejected(self):
        item = self._build()
        for field in ("sha256", "size", "synced_at", "remote_id", "ts"):
            forged = dict(item)
            forged[field] = "подделка" if field != "size" else 999
            self.assertIsNotNone(
                manifest.verify(forged),
                f"подделка поля {field} должна ловиться")

    def test_invalid_keys_rejected(self):
        item = self._build()
        self.assertIsNotNone(manifest.verify({**item, "pubkey": "zz"}))
        self.assertIsNotNone(manifest.verify({**item, "sig": "deadbeef"}))
        self.assertIsNotNone(manifest.verify({**item, "pubkey": "ab" * 16}))
        missing = {k: v for k, v in item.items() if k != "ts"}
        self.assertIsNotNone(manifest.verify(missing), "нет ts - отказ")

    def test_claims_counts(self):
        items = [self._build(), self._build(digest="sha256:" + "cd" * 32),
                 self._build()]
        claims = manifest.claims(items)
        self.assertEqual(len(claims["sha256:" + "ab" * 32]), 2)
        self.assertEqual(len(claims["sha256:" + "cd" * 32]), 1)


class TestReview(IdentityCase):
    def _signed(self, digest):
        return manifest.build("dQw4w9WgXcQ", digest, 100,
                              synced_at="2026-10-11T09:00:00")

    def test_single_peer_is_accepted_with_level(self):
        digest = "sha256:" + "ab" * 32
        decision = verify.review(digest, manifests=[self._signed(digest)])
        self.assertTrue(decision["ok"])
        self.assertEqual(decision["consensus"], 1)
        self.assertIn("одиночный", decision["level"])
        self.assertEqual(decision["divergent"], {})

    def test_consensus_is_informational(self):
        digest = "sha256:" + "ab" * 32
        decision = verify.review(digest, manifests=[
            self._signed(digest), self._signed(digest),
            self._signed(digest)])
        self.assertTrue(decision["ok"])
        self.assertEqual(decision["consensus"], 3)
        self.assertIn("3 узлами", decision["level"])

    def test_own_digest_is_strongest(self):
        digest = "sha256:" + "ab" * 32
        other = "sha256:" + "cd" * 32
        # Эталон совпал - принимаем с уровнем эталона.
        decision = verify.review(digest, own_digest=digest,
                                 manifests=[self._signed(digest)])
        self.assertTrue(decision["ok"])
        self.assertEqual(decision["level"], "эталон моей базы")
        # Эталон разошёлся - отказ, хотя узел и утверждает своё.
        decision = verify.review(other, own_digest=digest,
                                 manifests=[self._signed(other)])
        self.assertFalse(decision["ok"])
        self.assertIn("эталоном", decision["reason"])

    def test_banned_hash_rejected(self):
        digest = "sha256:" + "ab" * 32
        decision = verify.review(digest, manifests=[self._signed(digest)],
                                 banned={digest})
        self.assertFalse(decision["ok"])
        self.assertIn("стоп-листе", decision["reason"])

    def test_divergent_hashes_reported_but_not_blocking(self):
        digest = "sha256:" + "ab" * 32
        other = "sha256:" + "cd" * 32
        decision = verify.review(digest, manifests=[
            self._signed(digest), self._signed(other)])
        self.assertTrue(decision["ok"], "правило пользователя: один узел достаточно")
        self.assertEqual(decision["divergent"], {other: 1},
                         "расхождение - информация для человека")

    def test_forged_manifests_do_not_count(self):
        digest = "sha256:" + "ab" * 32
        good = self._signed(digest)
        forged = dict(self._signed(digest))
        forged["pubkey"] = "11" * 32      # чужой ключ - подпись не сойдётся
        decision = verify.review(digest, manifests=[good, forged])
        self.assertEqual(decision["consensus"], 1,
                         "неподписанные заявления не участвуют")

    def test_offenses_are_informational_not_blocking(self):
        digest = "sha256:" + "ab" * 32
        decision = verify.review(digest, manifests=[self._signed(digest)],
                                 offenses=3)
        self.assertTrue(decision["ok"],
                        "репутация ключа не блокирует - блокирует стоп-лист")
        self.assertEqual(decision["offenses"], 3)
        # Без переданной информации - ноль, а не None (в JSON и UI).
        self.assertEqual(
            verify.review(digest, manifests=[self._signed(digest)])["offenses"],
            0)
        # В провенанс попадает - чтобы через месяц было видно в карточке.
        provenance = verify.provenance(decision, "cd" * 32)
        self.assertEqual(provenance["peer_offenses"], 3)


class TestProbe(unittest.TestCase):
    """Структурная сверка: подменяем ffprobe (его логика - не предмет)."""

    def _run(self, info_or_error, expected_duration=None):
        done = mock.Mock(returncode=0, stdout=json.dumps(info_or_error),
                         stderr="")
        with mock.patch.object(verify, "_ffprobe_exe", return_value="ffprobe.exe"), \
                mock.patch.object(verify.subprocess, "run", return_value=done):
            return verify.probe("video.mp4", expected_duration)

    def test_duration_match(self):
        result = self._run({"format": {"duration": "213.0"},
                            "streams": [{"codec_type": "video"}]},
                           expected_duration=213)
        self.assertTrue(result["checked"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["duration"], 213.0)

    def test_duration_mismatch_rejected(self):
        result = self._run({"format": {"duration": "99.0"},
                            "streams": [{"codec_type": "video"}]},
                           expected_duration=213)
        self.assertFalse(result["ok"])
        self.assertTrue(any("длительность" in e for e in result["errors"]))

    def test_no_video_stream_rejected(self):
        result = self._run({"format": {"duration": "10"}, "streams": []},
                           expected_duration=10)
        self.assertFalse(result["ok"])
        self.assertTrue(any("видео-потока" in e for e in result["errors"]))

    def test_unreadable_file_rejected(self):
        done = mock.Mock(returncode=1, stdout="", stderr="moov atom not found")
        with mock.patch.object(verify, "_ffprobe_exe", return_value="ffprobe.exe"), \
                mock.patch.object(verify.subprocess, "run", return_value=done):
            result = verify.probe("video.mp4")
        self.assertFalse(result["ok"])
        self.assertTrue(any("не читается" in e for e in result["errors"]))

    def test_no_ffprobe_is_honest_skip(self):
        with mock.patch.object(verify, "_ffprobe_exe", return_value=None):
            result = verify.probe("video.mp4", expected_duration=10)
        self.assertFalse(result["checked"])
        self.assertIsNone(result["ok"], "без ffprobe - не проверялось, не «ок»")

    def test_provenance_records_everything(self):
        decision = {"level": "одиночный узел (принимаем по правилу "
                             "пользователя)", "consensus": 1,
                    "claims": {"sha256:aa": 1}}
        record = verify.provenance(decision, peer_pubkey="ab" * 32,
                                   peer_synced_at="2026-10-11T09:00:00",
                                   structure={"checked": True, "errors": [],
                                              "duration": 213.0})
        self.assertEqual(record["source"], "mesh")
        self.assertEqual(record["peer"], "ab" * 32)
        self.assertEqual(record["peer_synced_at"], "2026-10-11T09:00:00")
        self.assertEqual(record["consensus"], 1)
        self.assertTrue(record["structure_checked"])
        self.assertEqual(record["received_duration"], 213.0)
        self.assertTrue(record["received_at"])


class TestFeedbackAndMigration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = db_mod.Database(Path(self._tmp.name) / "library.db")
        self.conn = self.db.conn
        self.addCleanup(self.db.close)

    def test_v5_created(self):
        tables = {row["name"] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("mesh_bans", tables)
        version = int(self.conn.execute(
            "PRAGMA user_version").fetchone()[0])
        self.assertEqual(version, 6)   # v6 добавила truststore (B0)

    def test_ban_unban_cycle(self):
        digest = "sha256:" + "ef" * 32
        self.assertTrue(feedback.ban(self.conn, digest, peer="cd" * 32,
                                     note="содержит лишнее"))
        self.assertFalse(feedback.ban(self.conn, digest),
                         "повторный бан - не ошибка и не дубль")
        self.assertIn(digest, feedback.banned_set(self.conn))
        rows = feedback.list_bans(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["peer"], "cd" * 32)

        self.assertTrue(feedback.unban(self.conn, digest))
        self.assertEqual(feedback.banned_set(self.conn), set())
        self.assertFalse(feedback.unban(self.conn, digest))

    def test_normalizes_digest(self):
        bare = "ef" * 32
        feedback.ban(self.conn, bare)
        self.assertIn("sha256:" + bare, feedback.banned_set(self.conn))

    def test_empty_digest_rejected(self):
        with self.assertRaises(ValueError):
            feedback.ban(self.conn, "")

    def test_peer_offenses_counts_by_key(self):
        peer = "ab" * 32
        self.assertEqual(feedback.peer_offenses(self.conn, peer), 0)
        feedback.ban(self.conn, "sha256:" + "01" * 32, peer=peer, note="1")
        feedback.ban(self.conn, "sha256:" + "02" * 32, peer=peer, note="2")
        feedback.ban(self.conn, "sha256:" + "03" * 32, peer="ee" * 32, note="3")
        self.assertEqual(feedback.peer_offenses(self.conn, peer), 2,
                         "считаются нарушения КЛЮЧА, а не все баны подряд")
        self.assertEqual(feedback.peer_offenses(self.conn, "ff" * 32), 0)
        self.assertEqual(feedback.peer_offenses(self.conn, ""), 0)


if __name__ == "__main__":
    unittest.main()
