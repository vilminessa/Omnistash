"""Bencode: кодек протокола DHT (BEP3/BEP5).

Почему bencode, а не JSON: обнаружение участников обязано ходить через
ПУБЛИЧНЫЕ bootstrap-роутеры BitTorrent (router.bittorrent.com и
компания) - они понимают только bencode и стандартные RPC
(ping/find_node/get/put). Это и есть ответ на «DHT без своих серверов»:
своих серверов нет, но точка входа в сеть - общедоступный DNS.

Значение по ключу - наш bencoded-словарь {addr, pubkey, hash, size,
synced_at, ts, sig}: роутер хранит байты не разбирая (BEP5 допускает
произвольные значения до 1000 байт), а понимаем значение только мы.
"""

from __future__ import annotations

import typing


class BencodeError(ValueError):
    """Битый или враждебный bencode (защита от некорректных пиров)."""


def encode(value) -> bytes:
    """Значение -> bencode. Поддерживаются int, bytes, str, list, dict."""
    if isinstance(value, bool):
        # bool - подтип int, но в протоколе его быть не должно.
        raise BencodeError("bool не является bencode-значением")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        return b"%d:" % len(data) + data
    if isinstance(value, str):
        data = value.encode("utf-8")
        return b"%d:" % len(data) + data
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = []
        for key in value:
            raw_key = key if isinstance(key, bytes) else str(key).encode("utf-8")
            items.append((raw_key, value[key]))
        # Ключи - по возрастанию байтов: так требует спецификация.
        items.sort(key=lambda pair: pair[0])
        return (b"d" + b"".join(
            encode(raw_key) + encode(item) for raw_key, item in items) + b"e")
    raise BencodeError(f"неподдерживаемый тип: {type(value).__name__}")


def decode(data: bytes):
    """bencode -> значение (bytes для строк: DHT-строки - байты)."""
    if not isinstance(data, (bytes, bytearray)):
        raise BencodeError("ожидались байты")
    value, used = _decode(bytes(data), 0)
    if used != len(data):
        raise BencodeError(f"мусор после значения: {len(data) - used} байт")
    return value


def _decode(data: bytes, pos: int) -> tuple:
    if pos >= len(data):
        raise BencodeError("неожиданный конец")
    head = data[pos:pos + 1]
    if head == b"i":
        end = data.find(b"e", pos)
        if end < 0:
            raise BencodeError("нет конца integer")
        body = data[pos + 1:end]
        if not body or body == b"-":
            raise BencodeError("пустой integer")
        return int(body), end + 1
    if head == b"l":
        items = []
        pos += 1
        while pos < len(data) and data[pos:pos + 1] != b"e":
            item, pos = _decode(data, pos)
            items.append(item)
        if pos >= len(data):
            raise BencodeError("нет конца списка")
        return items, pos + 1
    if head == b"d":
        mapping = {}
        pos += 1
        while pos < len(data) and data[pos:pos + 1] != b"e":
            key, pos = _decode(data, pos)
            if not isinstance(key, bytes):
                raise BencodeError("ключ словаря не bytes")
            value, pos = _decode(data, pos)
            mapping[key] = value
        if pos >= len(data):
            raise BencodeError("нет конца словаря")
        return mapping, pos + 1
    if head.isdigit():
        end = data.find(b":", pos)
        if end < 0:
            raise BencodeError("нет разделителя длины")
        length_text = data[pos:end]
        if not length_text or (len(length_text) > 1
                               and length_text.startswith(b"0")):
            raise BencodeError("недопустимая длина строки")
        length = int(length_text)
        start = end + 1
        stop = start + length
        if stop > len(data):
            raise BencodeError("строка длиннее оставшихся данных")
        return data[start:stop], stop
    raise BencodeError(f"неизвестный тип: {head!r}")


def decode_dict(data: bytes) -> dict:
    """Только словари принимаем от пиров (запросы/ответы DHT)."""
    value = decode(data)
    if not isinstance(value, dict):
        raise BencodeError("ожидался словарь")
    return value
