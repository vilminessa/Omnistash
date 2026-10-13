"""Шифрование переноса чанков под ключ получателя (B2).

TLS (C1) уже шифрует канал и проверяет участника до первого байта.
Зачем шифровать и сам файл: прочитать байты по пути сможет только
обладатель ключа получателя - защита на случай, если канал после
аттестации скомпрометирован, и фундамент ретранслятора (B5): посредник
отдаёт байты, которых сам прочитать не может.

Протокол свой (только cryptography, сторонних зависимостей нет):

  * ключ X25519 получателя выводится из его Ed25519-идентичности тем же
    приёмом, что libsodium: скаляр = sha512(семя)[:32] со скалярной
    обрезкой, публичная часть - бирациональная трансляция точки
    u = (1+y)/(1-y); обе дороги сверяются тестами;
  * файл режется на чанки (CHUNK_SIZE), каждый шифруется СВОИМ ключом:
    ECDH(эфемерный ключ отправителя, ключ получателя) -> HKDF-SHA256
    (info = индекс чанка, хеш файла, размер чанка) -> ChaCha20-Poly1305
    с нулевым nonce (ключ каждый раз новый; индекс - в AAD, подмена
    порядка чанков не пройдёт);
  * конверт {v, alg, recipient, eph_pub, chunk, chunks, size, sha256}
    уходит заголовком (base64 JSON); sha256 - от РАСШИФРОВАННОГО файла,
    проверка хеша остаётся за mesh.fetch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import identity

ALG = "x25519-hkdf-sha256-chacha20poly1305"
HEADER = "X-Omnistash-Enc"
CHUNK_SIZE = 128 * 1024       # ровно под конверт: 128 КБ - 144 КБ шифра
TAG = 16                      # Poly1305-метка на чанк
_NONCE = b"\x00" * 12         # nonce не нужен: ключ уникален на чанк
_SALT = b"omnistash-chunks-v1"
_P = 2 ** 255 - 19


class CryptError(Exception):
    """Конверт или блок не прошли проверку: чужой ключ, подделанные байты."""


# ------------------------------------------------------------------ #
#  Ключи: Ed25519-идентичность -> X25519
# ------------------------------------------------------------------ #

def x25519_private_from_seed(seed: bytes) -> X25519PrivateKey:
    """X25519-приватный ключ из Ed25519-семени (приём libsodium)."""
    if len(seed) != 32:
        raise CryptError("семя Ed25519 - 32 байта")
    scalar = bytearray(hashlib.sha512(seed).digest()[:32])
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    return X25519PrivateKey.from_private_bytes(bytes(scalar))


def x25519_public_from_ed25519(pub_hex: str) -> bytes:
    """Публичный X25519 из публичного Ed25519 (бирациональная трансляция).

    u = (1 + y) / (1 - y) по модулю 2^255-19 (точка Ed25519 -> кривая
    Curve25519, как в crypto_sign_ed25519_pk_to_curve25519). Знак X
    кривой по этой карте не восстанавливается - X25519 его игнорирует.
    """
    try:
        raw = bytes.fromhex(str(pub_hex or "").strip())
    except ValueError as exc:
        raise CryptError("ключ не hex") from exc
    if len(raw) != 32:
        raise CryptError("Ed25519-pubkey - 32 байта")
    # координата y лежит little-endian, знак X - старший бит последнего
    # байта (X25519 его игнорирует по определению протокола)
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= _P:
        raise CryptError("точка вне поля")
    u = ((1 + y) * pow(1 - y, _P - 2, _P)) % _P
    if u == 0:
        raise CryptError("вырожденная точка")
    return u.to_bytes(32, "little")


def own_x25519_public() -> bytes:
    """Свой X25519-pub из Ed25519-идентичности (публичная дорога)."""
    _key, pub = identity.get_identity()
    return x25519_public_from_ed25519(pub.hex())


def own_x25519_private() -> X25519PrivateKey:
    """Свой X25519-приватный из семени Ed25519 (приватная дорога)."""
    priv, _pub = identity.get_identity()
    seed = priv.private_bytes(serialization.Encoding.Raw,
                              serialization.PrivateFormat.Raw,
                              serialization.NoEncryption())
    return x25519_private_from_seed(seed)


# ------------------------------------------------------------------ #
#  Чанки
# ------------------------------------------------------------------ #

def cipher_size(total: int) -> int:
    """Размер шифропотоки для файла size байт (для Content-Length)."""
    chunks = (int(total) + CHUNK_SIZE - 1) // CHUNK_SIZE
    return int(total) + chunks * TAG


def _per_chunk_key(shared: bytes, index: int, digest: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT,
                info=f"{index}|{digest}|{CHUNK_SIZE}".encode("utf-8")
                ).derive(shared)


def _seal(shared: bytes, index: int, digest: str, plain: bytes) -> bytes:
    key = _per_chunk_key(shared, index, digest)
    aad = struct.pack(">Q", index)
    return ChaCha20Poly1305(key).encrypt(_NONCE, plain, aad)


def _open(shared: bytes, index: int, digest: str, cipher: bytes) -> bytes:
    key = _per_chunk_key(shared, index, digest)
    aad = struct.pack(">Q", index)
    try:
        return ChaCha20Poly1305(key).decrypt(_NONCE, cipher, aad)
    except InvalidTag as exc:
        raise CryptError(f"чанк {index} не прошёл проверку "
                         "(метка) - байты подделаны или ключ не тот") from exc


def _shared_for_seal(recipient: X25519PublicKey,
                     eph: X25519PrivateKey) -> bytes:
    return eph.exchange(recipient)


def build_envelope(recipient_pub: bytes, digest: str, size: int) -> tuple:
    """(конверт, эфемерный ключ) - один на файл."""
    if len(recipient_pub) != 32:
        raise CryptError("X25519-ключ получателя - 32 байта")
    chunks = (int(size) + CHUNK_SIZE - 1) // CHUNK_SIZE
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw,
                                            serialization.PublicFormat.Raw)
    envelope = {"v": 1, "alg": ALG, "recipient": recipient_pub.hex(),
                "eph_pub": eph_pub.hex(), "chunk": CHUNK_SIZE,
                "chunks": chunks, "size": int(size), "sha256": digest}
    return envelope, eph


def seal_file(write: Callable[[bytes], object], source_path, envelope: dict,
              eph: X25519PrivateKey) -> int:
    """Дописать шифропотоку файла; конверт и ключ даёт build_envelope.

    Разделение нужно серверу: Content-Length и заголовок с конвертом
    уходят ДО первого байта тела, а блоки шифруются тем же эфемерным
    ключом, что объявлен (иначе получатель не расшифрует).
    Возвращает число отданных чанков.
    """
    recipient = X25519PublicKey.from_public_bytes(
        bytes.fromhex(str(envelope.get("recipient") or "")))
    shared = _shared_for_seal(recipient, eph)
    digest = str(envelope.get("sha256") or "")
    index = 0
    with open(source_path, "rb") as handle:
        while True:
            plain = handle.read(CHUNK_SIZE)
            if not plain:
                break
            write(_seal(shared, index, digest, plain))
            index += 1
    if index != envelope["chunks"]:
        raise CryptError("файл изменился во время отдачи")
    return index


def envelope_header(envelope: dict) -> str:
    return base64.b64encode(json.dumps(
        envelope, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).decode("ascii")


def parse_envelope(header: str | None) -> dict | None:
    """Конверт из заголовка; None - заголовка нет, CryptError - битый."""
    if not header:
        return None
    try:
        envelope = json.loads(base64.b64decode(str(header), validate=True))
    except Exception as exc:  # noqa: BLE001 - любая кривизна base64/JSON
        raise CryptError("битый конверт шифрования") from exc
    if not isinstance(envelope, dict) or envelope.get("v") != 1 \
            or envelope.get("alg") != ALG:
        raise CryptError("неизвестный конверт шифрования")
    for field in ("recipient", "eph_pub"):
        try:
            value = bytes.fromhex(str(envelope.get(field) or ""))
        except ValueError as exc:
            raise CryptError(f"поле {field} не hex") from exc
        if len(value) != 32:
            raise CryptError(f"поле {field} - не 32 байта")
    if not isinstance(envelope.get("chunks"), int) or \
            not isinstance(envelope.get("size"), int):
        raise CryptError("в конверте нет размеров")
    if envelope.get("chunk") != CHUNK_SIZE:
        raise CryptError("чужой размер чанка")
    return envelope


def open_stream(part_path, reader, envelope: dict, progress=None) -> int:
    """Расшифровать поток чанков (reader.read) в part_path.

    Каждый чанк проверяется меткой Poly1305 (подделка/подмена порядка
    не проходят); хеш расшифрованного файла считает mesh.fetch.
    Возвращает число записанных байт (должно сойтись с конвертом).
    """
    priv = own_x25519_private()
    if envelope["recipient"] != own_x25519_public().hex():
        raise CryptError("конверт адресован не нашему ключу")
    eph = X25519PublicKey.from_public_bytes(bytes.fromhex(envelope["eph_pub"]))
    shared = priv.exchange(eph)
    digest = str(envelope.get("sha256") or "")
    written = 0
    # последний чанк короче (plain + метка) - читаем ровно столько,
    # сколько осталось шифропотока
    left = envelope["size"] + envelope["chunks"] * TAG
    with open(part_path, "wb") as out:
        for index in range(envelope["chunks"]):
            want = min(CHUNK_SIZE + TAG, left)
            raw = _read_exactly(reader, want)
            left -= want
            plain = _open(shared, index, digest, raw)
            out.write(plain)
            written += len(plain)
            if progress:
                progress(written, envelope["size"])
    if written != envelope["size"]:
        raise CryptError(f"получено {written} байт, в конверте "
                         f"{envelope['size']}")
    return written


def _read_exactly(reader, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        piece = reader.read(count - len(data))
        if not piece:
            raise CryptError("поток оборвался на середине чанка")
        data.extend(piece)
    return bytes(data)
