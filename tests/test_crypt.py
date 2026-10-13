"""B2: шифрование чанков под ключ получателя.

Ключевой инвариант - обе дороги вывода X25519 из Ed25519-идентичности
(приватная: скаляр из семени; публичная: трансляция точки) сходятся:
иначе отправитель зашифровал бы ключом, которого у получателя нет.
"""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey)

from app.mesh import crypt, identity


class CryptCase(unittest.TestCase):
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
        self.root = Path(self._tmp.name)

    def seal(self, source: Path, recipient: bytes,
             digest: str = "sha256:" + "ab" * 32):
        """(конверт, шифробайты) для файла."""
        envelope, eph = crypt.build_envelope(recipient, digest,
                                             os.path.getsize(source))
        cipher = bytearray()
        crypt.seal_file(cipher.extend, str(source), envelope, eph)
        return envelope, bytes(cipher)


class TestKeys(CryptCase):
    def test_ed25519_to_x25519_two_roads_agree(self):
        """Приватная дорога (скаляр) и публичная (точка) = один ключ."""
        priv = crypt.own_x25519_private()
        derived = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.assertEqual(len(derived), 32)
        self.assertEqual(crypt.own_x25519_public(), derived,
                         "обе дороги Ed25519->X25519 обязаны сходиться")

    def test_bad_keys_refused(self):
        for bad in ("", "zz", "00" * 31, "00" * 33, "ff" * 32):
            with self.assertRaises(crypt.CryptError):
                crypt.x25519_public_from_ed25519(bad)
        with self.assertRaises(crypt.CryptError):
            crypt.x25519_private_from_seed(b"\x01" * 31)


class TestSealOpen(CryptCase):
    def test_roundtrip_three_chunks(self):
        size = crypt.CHUNK_SIZE * 2 + 5000          # три чанка
        src = self.root / "src.bin"
        src.write_bytes(bytes(i % 251 for i in range(size)))
        recipient = crypt.own_x25519_public()

        envelope, cipher = self.seal(src, recipient)
        self.assertEqual(envelope["chunks"], 3)
        self.assertEqual(len(cipher), crypt.cipher_size(size))

        # конверт едет заголовком и читается обратно
        parsed = crypt.parse_envelope(crypt.envelope_header(envelope))
        self.assertEqual(parsed["recipient"], recipient.hex())

        part = self.root / "out.bin"
        seen = []
        written = crypt.open_stream(
            str(part), io.BytesIO(cipher), parsed,
            progress=lambda done, _total: seen.append(done))
        self.assertEqual(written, size)
        self.assertEqual(part.read_bytes(), src.read_bytes())
        self.assertEqual(seen[-1], size, "прогресс должен дойти до конца")

    def test_single_byte_file(self):
        src = self.root / "tiny.bin"
        src.write_bytes(b"\x00")
        envelope, cipher = self.seal(src, crypt.own_x25519_public())
        self.assertEqual(envelope["chunks"], 1)
        part = self.root / "tiny-out.bin"
        crypt.open_stream(str(part), io.BytesIO(cipher), envelope)
        self.assertEqual(part.read_bytes(), b"\x00")

    def test_tampered_chunk_refused(self):
        src = self.root / "src.bin"
        src.write_bytes(os.urandom(crypt.CHUNK_SIZE + 100))
        envelope, cipher = self.seal(src, crypt.own_x25519_public())
        broken = bytearray(cipher)
        broken[10] ^= 0x01                          # подмена байта
        with self.assertRaises(crypt.CryptError) as caught:
            crypt.open_stream(str(self.root / "x.bin"), io.BytesIO(bytes(broken)),
                              envelope)
        self.assertIn("метка", str(caught.exception))

    def test_swapped_chunks_refused(self):
        """Поменять два шифроблока местами нельзя: индекс в AAD."""
        src = self.root / "src.bin"
        src.write_bytes(os.urandom(crypt.CHUNK_SIZE * 2))
        envelope, cipher = self.seal(src, crypt.own_x25519_public())
        size = crypt.CHUNK_SIZE + crypt.TAG
        swapped = cipher[size:] + cipher[:size]
        with self.assertRaises(crypt.CryptError):
            crypt.open_stream(str(self.root / "x.bin"),
                              io.BytesIO(swapped), envelope)

    def test_truncated_stream_refused(self):
        src = self.root / "src.bin"
        src.write_bytes(os.urandom(crypt.CHUNK_SIZE + 10))
        envelope, cipher = self.seal(src, crypt.own_x25519_public())
        with self.assertRaises(crypt.CryptError) as caught:
            crypt.open_stream(str(self.root / "x.bin"),
                              io.BytesIO(cipher[:-5]), envelope)
        self.assertIn("оборвался", str(caught.exception))

    def test_foreign_recipient_refused(self):
        src = self.root / "src.bin"
        src.write_bytes(os.urandom(100))
        stranger = X25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        envelope, cipher = self.seal(src, stranger)
        with self.assertRaises(crypt.CryptError) as caught:
            crypt.open_stream(str(self.root / "x.bin"), io.BytesIO(cipher),
                              envelope)
        self.assertIn("не нашему", str(caught.exception))

    def test_envelope_garbage(self):
        self.assertIsNone(crypt.parse_envelope(None))
        for bad in ("не base64!!", "", "!!!!"):
            if bad == "":
                continue                     # пустой заголовок = None
            with self.assertRaises(crypt.CryptError):
                crypt.parse_envelope(bad)
        envelope, _eph = crypt.build_envelope(crypt.own_x25519_public(),
                                              "sha256:" + "ab" * 32, 10)
        wrong = dict(envelope, alg="rsa")
        with self.assertRaises(crypt.CryptError):
            crypt.parse_envelope(crypt.envelope_header(wrong))

    def test_cipher_size_edges(self):
        self.assertEqual(crypt.cipher_size(0), 0)
        self.assertEqual(crypt.cipher_size(1), 1 + crypt.TAG)
        self.assertEqual(crypt.cipher_size(crypt.CHUNK_SIZE),
                         crypt.CHUNK_SIZE + crypt.TAG)
        self.assertEqual(crypt.cipher_size(crypt.CHUNK_SIZE + 1),
                         crypt.CHUNK_SIZE + 1 + 2 * crypt.TAG)


if __name__ == "__main__":
    unittest.main()
