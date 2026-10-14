"""SAS «12 слов»: сверка устройства при заключении дружбы (этап 1.1).

Идея (как Signal/Matrix SAS): стороны, уже соединённые по аттестованному
TLS-каналу (verify_peer до первого байта), обмениваются ЭФЕМЕРНЫМИ
X25519-ключами, считают общий секрет и показывают человеку 12 слов.
Сверка голосом/лично; слова совпали -> устройство подтверждено
(truststore verified_via='sas').

Почему MITM не проходит:
  * канал аттестован SPKI до первого байта - подменить участника без его
    приватного ключа нельзя (verify_peer откажет);
  * если бы посредник relay-ил сам обмен, он подставил бы СВОИ ephemeral-
    ключи по бокам - общие секреты разошлись бы, слова не совпали.

Словарь: 2048 произносимых слов ГЕНЕРИРУЕТСЯ детерминированно из фикс.
таблиц согласных/гласных (слог cv, слово cv-cv-cv) - stdlib, без пакетов
и без многокилобайтных констант в коде. 12 слов = 12 индексов по 11 бит
= 132 бита общего секрета (перебор 2^132 нереален).
"""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)

CONSONANTS = "bdghjklmnprstvz"
VOWELS = "aeiouy"
WORDS_COUNT = 2048             # 11 бит на индекс
SAS_LEN = 12                   # слов в сверке


def _make_words() -> list[str]:
    """2048 произносимых слов cv-cv-cv в фиксированном порядке."""
    syllables = [c + v for c in CONSONANTS for v in VOWELS]
    out: list[str] = []
    for first in syllables:
        for second in syllables:
            for third in syllables:
                out.append(first + second + third)
                if len(out) >= WORDS_COUNT:
                    return out
    return out


WORDS = _make_words()


def words_from_secret(secret: bytes) -> list[str]:
    """12 слов из общего секрета: sha256, 11 бит на слово."""
    digest = hashlib.sha256(secret).digest()
    bits = int.from_bytes(digest, "big")
    out = []
    for _index in range(SAS_LEN):
        out.append(WORDS[bits & (WORDS_COUNT - 1)])
        bits >>= 11
    return out


def own_ephemeral() -> tuple[X25519PrivateKey, str]:
    """Свежая ephemeral-пара: (ключ, hex публичного). Не сохраняется."""
    eph = X25519PrivateKey.generate()
    pub = eph.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    return eph, pub.hex()


def shared_secret(own_priv: X25519PrivateKey, peer_pub_hex: str) -> bytes:
    """Общий секрет ECDH(свой ephemeral, чужой ephemeral)."""
    try:
        raw = bytes.fromhex(str(peer_pub_hex or "").strip())
    except ValueError as exc:
        raise ValueError("чужой ephemeral-ключ не hex") from exc
    if len(raw) != 32:
        raise ValueError("чужой ephemeral-ключ - 32 байта")
    return own_priv.exchange(X25519PublicKey.from_public_bytes(raw))


def sas_words(own_priv: X25519PrivateKey, peer_pub_hex: str) -> list[str]:
    """Слова сверки для пары ephemeral-ключей."""
    return words_from_secret(shared_secret(own_priv, peer_pub_hex))
