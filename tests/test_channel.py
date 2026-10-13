"""C1: TLS-канал - аттестация SPKI, TOFU-пины, отказ до первого байта.

Сети нет: материала и проверки живут на локальных объектах. Сквозной
путь по TLS (сервер -> клиент -> MITM-сценарии) покрыт в
test_mesh_net; здесь - механика channel.py.
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey)
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from app.mesh import channel, identity


def _der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(Encoding.DER)


def _spki(cert: x509.Certificate) -> bytes:
    return cert.public_key().public_bytes(
        Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _cert_with(claim_pub: bytes, signer_key) -> bytes:
    """Сертификат: claim заявляет claim_pub, но SPKI подписан signer_key.

    Нормальный пир: claim_pub == signer публичный. MITM: заявляет чужой
    ключ, подписывает своим - аттестация не должна сойтись.
    """
    channel_key = Ed25519PrivateKey.generate()
    spki = channel_key.public_key().public_bytes(
        Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    blob = claim_pub + signer_key.sign(spki)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         "omnistash-mesh")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(channel_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.UnrecognizedExtension(channel.ATTEST_OID, blob),
                       critical=False)
        .sign(channel_key, None))
    return _der(cert)


def _cert_without_attestation() -> bytes:
    channel_key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         "omnistash-mesh")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(channel_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(channel_key, None))
    return _der(cert)


class ChannelCase(unittest.TestCase):
    """Профиль (идентичность + пины) изолируется между тестами.

    Патчим profile_dir в ОБОИХ модулях: и identity, и channel берут
    имя из paths по-своему - патч только одного оставил бы запись
    пинов в настоящем профиле.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        for module in (identity, channel):
            patcher = mock.patch.object(module, "profile_dir",
                                        lambda: self.dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        identity.reset_cache()
        self.addCleanup(identity.reset_cache)
        channel.reset_cache()
        self.addCleanup(channel.reset_cache)


class TestMaterial(ChannelCase):
    def test_attestation_verifies_against_identity(self):
        cert_pem, _key_pem = channel.server_material()
        cert = x509.load_pem_x509_certificate(cert_pem)
        own_pub = identity.get_identity()[1]
        got = channel.verify_peer(_der(cert), expected=own_pub.hex())
        self.assertEqual(got, own_pub.hex())
        blob = cert.extensions.get_extension_for_oid(
            channel.ATTEST_OID).value.value
        self.assertEqual(len(blob), channel.ATTEST_LEN,
                         "96 байт: identity(32) || подпись(64)")

    def test_channel_key_is_ephemeral(self):
        _cert1, key1 = channel.server_material()
        _cert2, key2 = channel.server_material()
        self.assertNotEqual(key1, key2,
                            "канальный ключ живёт только в памяти процесса")

    def test_server_context_leaves_no_pem_on_disk(self):
        identity.get_identity()          # ключ идентичности - до снимка
        before = {p.name for p in self.dir.iterdir()}
        context = channel.server_context()
        self.assertIsNotNone(context)
        after = {p.name for p in self.dir.iterdir()}
        self.assertEqual(before, after,
                         "PEM пишется только на мгновение и удаляется")


class TestVerifyPeer(ChannelCase):
    def test_wrong_expected_is_rejected(self):
        cert_pem, _key = channel.server_material()
        cert = x509.load_pem_x509_certificate(cert_pem)
        with self.assertRaises(channel.ChannelError) as caught:
            channel.verify_peer(_der(cert), expected="ab" * 32)
        self.assertIn("пин", str(caught.exception))

    def test_foreign_attestation_is_rejected(self):
        """Заявляет чужой ключ, подписывает своим - не проходит."""
        own_pub = identity.get_identity()[1]
        foreign = Ed25519PrivateKey.generate()
        foreign_pub = foreign.public_key().public_bytes(
            Encoding.Raw, serialization.PublicFormat.Raw)
        # Вариант «подпись чужим ключом под нашим claim».
        with self.assertRaises(channel.ChannelError):
            channel.verify_peer(_cert_with(own_pub, foreign))
        # Вариант «всё своё, но не наш identity» - при пине отклоняется.
        pin_set_own = own_pub.hex()
        channel.pin_set("1.2.3.4:5", pin_set_own)
        with self.assertRaises(channel.ChannelError):
            channel.verify_peer(_cert_with(foreign_pub, foreign),
                                addr="1.2.3.4:5")

    def test_missing_attestation_is_rejected(self):
        with self.assertRaises(channel.ChannelError) as caught:
            channel.verify_peer(_cert_without_attestation())
        self.assertIn("аттестац", str(caught.exception))

    def test_garbage_der_is_channel_error(self):
        for junk in (b"", b"not a certificate", b"\x30\x03\x01\x02\x01",
                     "мусор".encode("utf-8")):
            with self.assertRaises(channel.ChannelError):
                channel.verify_peer(junk)

    def test_tofu_first_contact_pins_then_protects(self):
        own_pub = identity.get_identity()[1].hex()
        cert_pem, _key = channel.server_material()
        der = _der(x509.load_pem_x509_certificate(cert_pem))
        addr = "10.0.0.5:38574"
        self.assertEqual(channel.pin_get(addr), "")
        self.assertEqual(channel.verify_peer(der, addr=addr), own_pub)
        self.assertEqual(channel.pin_get(addr), own_pub,
                         "первый контакт запомнил ключ")
        # Тот же ключ - проходит.
        self.assertEqual(channel.verify_peer(der, addr=addr), own_pub)
        # Другой (самосогласованный!) ключ на том же адресе - отказ.
        foreign = Ed25519PrivateKey.generate()
        foreign_pub = foreign.public_key().public_bytes(
            Encoding.Raw, serialization.PublicFormat.Raw)
        with self.assertRaises(channel.ChannelError) as caught:
            channel.verify_peer(_cert_with(foreign_pub, foreign), addr=addr)
        self.assertIn("изменился", str(caught.exception))
        self.assertIn("mesh_pins.json", str(caught.exception),
                      "подсказка должна говорить, что делать")
        self.assertEqual(channel.pin_get(addr), own_pub,
                         "запомненный ключ не затирается")


class TestPinStore(ChannelCase):
    def test_pins_survive_cache_reset(self):
        channel.pin_set("9.9.9.9:1", "cd" * 32)
        channel.reset_cache()
        self.assertEqual(channel.pin_get("9.9.9.9:1"), "cd" * 32,
                         "пин должен жить в файле профиля")
        data = json.loads((self.dir / channel.PIN_FILE)
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["9.9.9.9:1"], "cd" * 32)

    def test_broken_file_recovers_to_empty(self):
        (self.dir / channel.PIN_FILE).write_text("{сломано",
                                                 encoding="utf-8")
        channel.reset_cache()
        self.assertEqual(channel.pins(), {})
        channel.pin_set("1.1.1.1:1", "ee" * 32)
        self.assertEqual(channel.pin_get("1.1.1.1:1"), "ee" * 32)

    def test_pin_del_and_del_key(self):
        """Удаление пинов: точечно и «забыть устройство целиком»."""
        channel.pin_set("2.2.2.2:1", "aa" * 32)
        channel.pin_set("2.2.2.2:2", "aa" * 32)   # тот же узел, другой адрес
        channel.pin_set("3.3.3.3:1", "bb" * 32)

        self.assertFalse(channel.pin_del("2.2.2.2:9"), "нет пина - нет работы")
        self.assertTrue(channel.pin_del("2.2.2.2:1"))
        self.assertEqual(channel.pin_get("2.2.2.2:1"), "")
        self.assertEqual(channel.pin_get("2.2.2.2:2"), "aa" * 32,
                         "второй адрес того же узла не тронут")

        # Кнопка «Забыть устройство» снимает ВСЕ его адреса, чужие - нет
        self.assertEqual(channel.pin_del_key("aa" * 32), 1)
        self.assertEqual(channel.pin_get("2.2.2.2:2"), "")
        self.assertEqual(channel.pin_get("3.3.3.3:1"), "bb" * 32)
        self.assertEqual(channel.pin_del_key("aa" * 32), 0, "повтор - пусто")
        channel.reset_cache()                    # удаление живёт в файле
        self.assertEqual(channel.pins(), {"3.3.3.3:1": "bb" * 32})


if __name__ == "__main__":
    unittest.main()
