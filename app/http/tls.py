"""TLS-материал для клиента-браузера: эфемерный ECDSA P-256 с SAN.

Отдельный модуль от `app/mesh/channel.py` - не дублирование, а разная роль:

* mesh-канал остаётся Ed25519: собеседник - другой Omnistash, привязка идёт
  аттестацией SPKI (`ATTEST_OID`), hostname не проверяется;
* здесь собеседник - браузер: он верифицирует сертификат по SAN и отказывает
  по типу ключа ДО решения человека о доверии. Замер на этой машине
  (in-app Chromium, loopback, TLS 1.3): сертификат Ed25519 даёт
  `ERR_SSL_VERSION_OR_CIPHER_MISMATCH` (отказ рукопожатия), ECDSA P-256 и RSA -
  `ERR_CERT_AUTHORITY_INVALID` (это решение человека: предупреждение браузера
  про самоподписанный сертификат, docs/SERVER-CLIENT.md §5).

Ключ эфемерный: `ssl.load_cert_chain` принимает только пути, поэтому PEM
пишется во временный файл в профиле на мгновение и удаляется сразу - на диске
не оседает, перезапуск даёт новую пару (пин привязан к отпечатку SPKI, §5).
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import ssl
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurve
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509 import DNSName, IPAddress
from cryptography.x509.oid import NameOID

from ..paths import profile_dir

VALID_DAYS = 30              # срок самоподписанного сертификата слушателя
COMMON_NAME = "omnistash-serve"
CURVE: EllipticCurve = ec.SECP256R1()   # P-256: принимается TLS-стеком браузера


class TlsError(Exception):
    """Материал TLS не построен: этот сертификат браузер не примет."""


def san_names(hosts) -> list:
    """SAN сертификата: IP - отдельным типом, имена - DNSName.

    Адрес из цифр как DNS-имя браузер не принимает: страница по 127.0.0.1
    выглядела бы «сертификат не для этого адреса», даже когда ключ верный.
    """
    names: list = []
    for host in (hosts or ()):
        text = str(host).strip()
        if not text:
            continue
        try:
            names.append(IPAddress(ipaddress.ip_address(text)))
        except ValueError:
            names.append(DNSName(text))
    return names


def material(san_hosts=()) -> tuple[bytes, bytes]:
    """(cert_pem, key_pem) эфемерного сертификата HTTPS-слушателя.

    ECDSA P-256 + SHA-256 - подпись, которую принимает TLS-стек браузера.
    SAN обязателен: современные браузеры Common Name не читают.
    """
    key = ec.generate_private_key(CURVE)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS)))
    names = san_names(san_hosts)
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName(names),
                                       critical=False)
    try:
        cert = builder.sign(key, hashes.SHA256())
    except Exception as exc:  # noqa: BLE001 - подпись ключа не прошла
        raise TlsError(f"сертификат не подписан: {type(exc).__name__}: {exc}") from exc
    cert_pem = cert.public_bytes(Encoding.PEM)
    key_pem = key.private_bytes(Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return cert_pem, key_pem


def context_from(cert_pem: bytes, key_pem: bytes) -> ssl.SSLContext:
    """Контекст TLS-сервера из готового материала (TLS 1.3 минимум).

    Материал пишется на диск только на мгновение и удаляется в finally: ключ
    живёт в памяти OpenSSL. Один и тот же сертификат служит и для контекста, и
    для отпечатка в журнале - иначе показанный человеку отпечаток не совпал бы
    с тем ключом, который реально отдаёт слушатель.
    """
    base = profile_dir()
    directory = str(base) if base.is_dir() else tempfile.gettempdir()
    cert_fd, cert_path = tempfile.mkstemp(suffix=".pem", dir=directory)
    key_fd, key_path = tempfile.mkstemp(suffix=".pem", dir=directory)
    try:
        with os.fdopen(cert_fd, "wb") as handle:
            handle.write(cert_pem)
        with os.fdopen(key_fd, "wb") as handle:
            handle.write(key_pem)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        try:
            context.load_cert_chain(cert_path, key_path)
        except (OSError, ssl.SSLError) as exc:
            raise TlsError(f"материал TLS не загружен: {exc}") from exc
        return context
    finally:
        for path in (cert_path, key_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def server_context(san_hosts=()) -> ssl.SSLContext:
    """Контекст TLS-сервера для клиента: материал строится один раз."""
    return context_from(*material(san_hosts))


def fingerprint(cert_pem: bytes) -> str:
    """Отпечаток SPKI сертификата (SHA-256, hex) - пин для клиента (§5).

    Хеш по SubjectPublicKeyInfo, а не по всему сертификату: клиент снимает
    именно SPKI и сравнивает его с тем, что запомнил (TOFU).
    """
    cert = x509.load_pem_x509_certificate(cert_pem)
    spki = cert.public_key().public_bytes(
        Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()
