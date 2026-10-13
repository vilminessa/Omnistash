"""TLS-канал участника: шифрование всегда, привязка - аттестацией SPKI.

Решение спайка C1 (замер на этой машине: +1.6 мс на запрос против
plaintext - пренебрежимо против файлов в МБ/ГБ): TLS поверх обычного
порта, никакого своего протокола.

Роли:
  * секретность и форвардная секретность - от TLS 1.3 (эфемерный
    ECDHE на каждом соединении; статические ключи для этого не нужны -
    отсюда отказ от «компаньона X25519» из первоначального плана);
  * ПРИВЯЗКА к участнику - от аттестации: сертификат самоподписан
    ЭФЕМЕРНЫМ канальным ключом (живёт только в памяти процесса, на
    диск не пишется), а расширение ATTEST_OID несёт 96 байт:
        identity_pub(32) || Sign(identity, SPKI канального ключа)(64)
  * клиент снимает сертификат и верифицирует аттестацию ДО первого
    байта запроса (см. client._connect): MITM без приватного ключа
    участника запроса не увидит - сервер фиксирует ноль обращений.

Откуда клиент берёт ожидаемый ключ (пин):
  * запись DHT (addr -> pubkey: подписана тем же ключом);
  * кэш знакомств mesh_pins.json в профиле (TOFU: первый контакт
    запоминает ключ, дальше несовпадение = отказ) - формат SSH-style,
    файл можно править/удалять руками;
  * позже - таблица друзей (этап B2), там ключ проверен «12 словами».
Без пина первый контакт - TOFU: ключ запоминается и работает как
пин со следующего раза. Чужой ключ при наличии пина = отказ.
"""

from __future__ import annotations

import datetime
import json
import ssl
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from ..paths import profile_dir

# Подentreprise-OID проекта: значение - аттестация SPKI (см. модуль).
ATTEST_OID = x509.ObjectIdentifier("1.3.6.1.4.1.55535.1.1")
IDENT_LEN = 32                 # сырой Ed25519-pubkey
ATTEST_LEN = IDENT_LEN + 64    # pubkey || подпись по SPKI
PIN_FILE = "mesh_pins.json"
HANDSHAKE_TIMEOUT = 10.0       # медленный/чужой клиент не висит вечно
VALID_DAYS = 365 * 30          # канальный ключ эфемерный - срок для людей


class ChannelError(Exception):
    """Канал не прошёл проверку: чужой ключ, пин не совпал, битый серт."""


# ------------------------------------------------------------------ #
#  Сервер: эфемерный сертификат с аттестацией
# ------------------------------------------------------------------ #

def server_material() -> tuple[bytes, bytes]:
    """(cert_pem, key_pem) самоподписанного канального сертификата.

    Ключ генерируется ЗДЕСЬ и нигде не сохраняется: перезапуск - новая
    пара, пины привязаны к identity, а не к каналу, поэтому ротация
    канального ключа никому не мешает.
    """
    from . import identity

    _key, ident_pub = identity.get_identity()
    channel_key = Ed25519PrivateKey.generate()
    spki = channel_key.public_key().public_bytes(
        Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    attestation = ident_pub + identity.sign(spki)
    if len(attestation) != ATTEST_LEN:       # защита от «обрезанной» подписи
        raise ChannelError("не удалось подписать канальный ключ")
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         "omnistash-mesh")])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(channel_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.UnrecognizedExtension(ATTEST_OID, attestation),
                       critical=False))
    cert = builder.sign(channel_key, None)   # Ed25519: hash не нужен
    cert_pem = cert.public_bytes(Encoding.PEM)
    key_pem = channel_key.private_bytes(
        Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    return cert_pem, key_pem


def server_context() -> ssl.SSLContext:
    """Контекст TLS-сервера. Материал в файл только на мгновение.

    ssl.load_cert_chain принимает лишь пути - пишем PEM во временный
    файл рядом и удаляем сразу после загрузки (ключ остаётся в памяти
    OpenSSL; на диске не оседает).
    """
    import os
    import tempfile

    cert_pem, key_pem = server_material()
    base = profile_dir()
    directory = str(base) if base.is_dir() else tempfile.gettempdir()
    cert_fd, cert_path = tempfile.mkstemp(suffix=".pem", dir=str(directory))
    key_fd, key_path = tempfile.mkstemp(suffix=".pem", dir=str(directory))
    try:
        with os.fdopen(cert_fd, "wb") as handle:
            handle.write(cert_pem)
        with os.fdopen(key_fd, "wb") as handle:
            handle.write(key_pem)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        return context
    finally:
        for path in (cert_path, key_path):
            try:
                os.unlink(path)
            except OSError:
                pass


# ------------------------------------------------------------------ #
#  Клиент: проверка ДО первого байта
# ------------------------------------------------------------------ #

def client_context() -> ssl.SSLContext:
    """Контекст клиента: сертификатов не доверяем, проверяем СВОИМИ руками."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def verify_peer(cert_der: bytes, expected: str | None = None,
                addr: str | None = None) -> str:
    """Аттестация сертификата -> hex identity-ключа пира.

    Порядок проверок:
      1) аттестация сходится claimed-ключом (подпись по SPKI);
      2) пин из DHT/друзей (expected) совпал с claimed;
      3) TOFU-кэш addr: первый контакт - запомнить, дальше несовпадение
         с запомненным = отказ (подмена адреса не пройдёт).
    Возвращает identity pubkey hex; ChannelError - любой отказ.
    """
    try:
        cert = x509.load_der_x509_certificate(cert_der)
        extension = cert.extensions.get_extension_for_oid(ATTEST_OID).value
        blob = bytes(getattr(extension, "value", b""))
        if len(blob) != ATTEST_LEN:
            raise ChannelError(f"сертификат без аттестации "
                               f"(длина {len(blob)} вместо {ATTEST_LEN})")
        claimed_raw, attestation = blob[:IDENT_LEN], blob[IDENT_LEN:]
        spki = cert.public_key().public_bytes(
            Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        Ed25519PublicKey.from_public_bytes(claimed_raw).verify(
            attestation, spki)
    except ChannelError:
        raise
    except Exception as exc:  # noqa: BLE001 - битый DER/подпись/поле
        raise ChannelError(f"аттестация канала не прошла: {exc}") from exc

    claimed = claimed_raw.hex()
    if expected and claimed != expected:
        raise ChannelError("пин не совпал: канал открылся не тем ключом "
                           f"(ожидали {expected[:16]}…, пришёл "
                           f"{claimed[:16]}…)")
    if addr:
        known = pin_get(addr)
        if known and known != claimed:
            raise ChannelError(f"ключ участника {addr} изменился "
                               "(запомнен другой) - возможна подмена; "
                               "если участник сменил ключ - удалите строку "
                               f"из {PIN_FILE}")
        if not known:
            pin_set(addr, claimed)
    return claimed


# ------------------------------------------------------------------ #
#  TOFU-кэш адрес -> ключ (mesh_pins.json в профиле)
# ------------------------------------------------------------------ #

_pins_lock = threading.Lock()
_pins: dict | None = None


def _pins_path() -> Path:
    return profile_dir() / PIN_FILE


def _load() -> dict:
    global _pins
    if _pins is not None:
        return _pins
    try:
        data = json.loads(_pins_path().read_text(encoding="utf-8"))
        _pins = data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        _pins = {}
    return _pins


def pin_get(addr: str) -> str:
    with _pins_lock:
        return str(_load().get(addr) or "")


def pin_set(addr: str, pubkey_hex: str) -> None:
    with _pins_lock:
        pins = _load()
        if pins.get(addr) == pubkey_hex:
            return
        pins[addr] = pubkey_hex
        try:
            _pins_path().write_text(
                json.dumps(pins, indent=1, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass                # профиль недоступен - пин живёт в памяти


def pin_del(addr: str) -> bool:
    """Забыть ключ адреса; True - строка была. (Забыть устройство целиком
    - это pin_del_key: один узел может быть найден по разным адресам.)"""
    with _pins_lock:
        pins = _load()
        if addr not in pins:
            return False
        del pins[addr]
        try:
            _pins_path().write_text(
                json.dumps(pins, indent=1, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass
        return True


def pin_del_key(pubkey_hex: str) -> int:
    """Снять пины ВСЕХ адресов, ведущих на ключ; возвращает число.

    Нужно кнопке «Забыть устройство» в хранилище доверия: без этого пин
    в файле высевал бы устройство заново при каждом открытии диалога.
    """
    key = str(pubkey_hex or "").strip().lower()
    with _pins_lock:
        pins = _load()
        doomed = [addr for addr, pinned in pins.items() if pinned == key]
        for addr in doomed:
            del pins[addr]
        if doomed:
            try:
                _pins_path().write_text(
                    json.dumps(pins, indent=1, ensure_ascii=False),
                    encoding="utf-8")
            except OSError:
                pass
    return len(doomed)


def pins() -> dict:
    """Снимок кэша (для диагностики/тестов)."""
    with _pins_lock:
        return dict(_load())


def reset_cache() -> None:
    """Сброс кэша пинов (тесты меняют профиль)."""
    global _pins
    with _pins_lock:
        _pins = None
