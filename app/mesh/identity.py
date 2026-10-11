"""Идентичность узла mesh: ключ Ed25519, приватная часть под DPAPI.

Роль ключа в сети:
  * подпись манифестов («у меня файл с хешом H») - заявление нельзя
    подделать по пути, адрес привязан к ключу;
  * node_id = sha1(pubkey) -20 байт, валидный идентификатор узла для DHT.

Приватный ключ шифруется тем же DPAPI-швом, что куки аккаунтов
(google_auth.encrypt): файл в профиле бесполезен без учётки Windows.
Потеря ключа = новая идентичность в сети (старые заявления остаются
у других узлов, но мы их больше не подтверждаем) - это осознанно.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

from .. import google_auth
from ..paths import profile_dir

KEY_FILE = "mesh_key.bin"

_lock = threading.Lock()
_identity: tuple[Ed25519PrivateKey, bytes] | None = None   # (priv, pub_raw)


def _key_path() -> Path:
    return profile_dir() / KEY_FILE


def _public_raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _load(pem: bytes) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(pem, password=None)


def get_identity() -> tuple[Ed25519PrivateKey, bytes]:
    """(приватный ключ, сырой публичный 32 байта); создаёт при первом
    обращении. Кэшируется в памяти - DPAPI не дёргаем на каждый манифест.
    """
    global _identity
    with _lock:
        if _identity is not None:
            return _identity
        path = _key_path()
        key = None
        if path.is_file():
            try:
                key = _load(google_auth.decrypt(path.read_bytes()))
            except Exception:  # noqa: BLE001 - битый/чужой файл: пересоздаём
                key = None
                try:
                    path.unlink()
                except OSError:
                    pass
        if key is None:
            key = Ed25519PrivateKey.generate()
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption())
            try:
                path.write_bytes(google_auth.encrypt(pem))
            except OSError:
                # Профиль недоступен: работаем в памяти (сессионная
                # идентичность) - лучше, чем не работать.
                pass
        _identity = (key, _public_raw(key))
        return _identity


def node_id() -> bytes:
    """20-байтный id узла = sha1(pubkey): валиден для DHT-протокола."""
    _pub = get_identity()[1]
    return hashlib.sha1(_pub).digest()


def sign(message: bytes) -> bytes:
    key = get_identity()[0]
    return key.sign(message)


def verify(public_raw: bytes, message: bytes, signature: bytes) -> bool:
    """Проверка подписи ЧУЖОГО ключа (ключ из манифеста/записи DHT)."""
    try:
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, message)
        return True
    except Exception:  # noqa: BLE001 - неверная длина/подделанная подпись
        return False


def reset_cache() -> None:
    """Сброс кэша (тесты подменяют профиль - идентичность не должна
    тащиться между тестами)."""
    global _identity
    with _lock:
        _identity = None
