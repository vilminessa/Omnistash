"""Узел mesh-DHT: обнаружение участников без своих серверов (BEP5).

Вход в сеть - публичные bootstrap-роутеры BitTorrent (не наши
серверы, ничего не хостим: BEP5 = совместимость bencode и 1000 байт)
+ LAN-multicast приветствие для машин в одной локальной сети.

Ключ: sha1("omnistash:" + remote_id). Значение: {addr, pubkey, hash,
size, synced_at, ts, sig} с подписью Ed25519 (bencode-кодирование).
Проверка - на месте: parse_value.

Известное ограничение: addr объявляется локальным (проброс за NAT -
стадия M4); maintenance спасает: ping мёртвых узлов и продление своих значений.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import threading
import time

from . import bencode, identity

NODE_ID_LEN = 20
K = 8
ALPHA = 3
LOOKUP_ROUNDS = 8
BOOTSTRAP = (
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
)
MULTICAST_GROUP = "239.255.77.77"
MULTICAST_PORT = 38575
HELLO_INTERVAL = 60.0
MAX_VALUES_PER_KEY = 20
VALUE_TTL = 24 * 3600
REANNOUNCE = 20 * 60
MAINTENANCE = 60.0
QUERY_TIMEOUT = 4.0
MAX_QUERY_RETRIES = 2


def node_id() -> bytes:
    return identity.node_id()


def key_for(remote_id: str) -> bytes:
    """Ключ по адресу remote_id? (20 байт, как infohash в BEP5)."""
    return hashlib.sha1(
        ("omnistash:" + str(remote_id)).encode("utf-8")).digest()


# Хранение «своих» значений: ключ -> (значение, срок переобъявления).
# Чужие значения живут отдельно (store) и отдаются по запросу get.
_CURRENT: list = [None]


def set_current(node) -> None:
    _CURRENT[0] = node


def current():
    return _CURRENT[0]


def xor_distance(a: bytes, b: bytes) -> int:
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


def local_ip() -> str:
    """Локальный адрес машины: connect к внешнему адресу без отправки
    пакетов - у UDP это просто выбор исходящего интерфейса."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 53))
        return probe.getsockname()[0] or "127.0.0.1"
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


PEER_LIMIT = 300          # предел контактов: лишние не читаем и не пишем
PEER_FILE = "dht_peers.json"


def load_peers(path: str) -> list:
    """Загрузка живых контактов из профиля: продолжаем сеть, а не
    начинаем каждый запуск с нуля."""
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    out = []
    for item in (data.get("peers") or [])[:PEER_LIMIT]:
        try:
            node = bytes.fromhex(item["node"])
            host = str(item["host"])
            port = int(item["port"])
        except (KeyError, ValueError, TypeError):
            continue
        if len(node) == NODE_ID_LEN and host and 0 < port < 65536:
            out.append(Contact(node, host, port))
    return out


def save_peers(path: str, contacts) -> None:
    """Атомарная запись списка (tmp -> replace); сбой - не беда:
    профиль без списка просто начнёт сеть заново."""
    payload = {"peers": [
        {"node": c.node.hex(), "host": c.host, "port": c.port}
        for c in contacts[:PEER_LIMIT]]}
    try:
        tmp = path + ".tmp"
        open(tmp, "w", encoding="utf-8").write(
            json.dumps(payload, ensure_ascii=False))
        os.replace(tmp, path)
    except OSError:
        pass


def compact_node(node: bytes, host: str, port: int) -> bytes:
    return node + socket.inet_aton(host) + port.to_bytes(2, "big")


def parse_compact_nodes(blob: bytes) -> list:
    out = []
    for start in range(0, len(blob) - 25, 26):
        chunk = blob[start:start + 26]
        try:
            host = socket.inet_ntoa(chunk[20:24])
        except OSError:
            continue
        port = int.from_bytes(chunk[24:26], "big")
        if port:
            out.append((chunk[:20], host, port))
    return out


def make_value(addr: str, digest20: bytes, size: int, synced_at: str) -> bytes:
    """bencode-словарь с подписью Ed25519 (для значений файлов)."""
    pub = identity.get_identity()[1]
    payload = {
        "addr": str(addr).encode("utf-8"),
        "pubkey": pub,
        "hash": digest20,
        "size": int(size),
        "synced_at": str(synced_at or "").encode("utf-8"),
        "ts": int(time.time()),
    }
    sig = identity.sign(bencode.encode(payload))
    return bencode.encode(dict(payload, sig=sig))


def parse_value(data: bytes) -> dict | None:
    """Разбор значения; None - нет ключа или битая подпись."""
    try:
        raw = bencode.decode_dict(data)
    except bencode.BencodeError:
        return None
    required = (b"addr", b"pubkey", b"hash", b"size", b"synced_at", b"ts",
                b"sig")
    if any(key not in raw for key in required):
        return None
    try:
        payload = {key: raw[key] for key in required if key != b"sig"}
        if not identity.verify(raw[b"pubkey"], bencode.encode(payload),
                               raw[b"sig"]):
            return None
        return {
            "addr": raw[b"addr"].decode("utf-8"),
            "pubkey": raw[b"pubkey"].hex(),
            "hash": raw[b"hash"],
            "size": int(raw[b"size"]),
            "synced_at": raw[b"synced_at"].decode("utf-8"),
            "ts": int(raw[b"ts"]),
            "raw": data,
        }
    except Exception:  # noqa: BLE001 - чужой формат не наш
        return None


# ---- каталог имён: ник -> адрес (B1) ---------------------------------
# Запись НЕ содержит списка видео - только ник, адрес и ключ. Ключ
# имени живёт в отдельном пространстве (свой префикс): DHT ищет по
# точному ключу, частичных запросов протокол не умеет - UI об этом
# честно пишет. Коллизии (один ник у разных ключей) отдаются списком:
# истина в отпечатке ключа, а не в нике.

NICK_MIN = 2
NICK_MAX = 32
NICK_EXTRA = " -_"


def clean_nick(text) -> str:
    """Ник к каноничному виду: края обрезать, пробелы схлопнуть."""
    return " ".join(str(text or "").split())


def nick_ok(nick) -> bool:
    """Ник пригоден для каталога: длина и символы (буквы/цифры юникода)."""
    nick = clean_nick(nick)
    if not (NICK_MIN <= len(nick) <= NICK_MAX):
        return False
    return all(ch.isalnum() or ch in NICK_EXTRA for ch in nick)


def name_key(nick: str) -> bytes:
    """Ключ каталога имён: регистр не важен (Nick == nick)."""
    clean = clean_nick(nick).casefold()
    return hashlib.sha1(("omnistash:name:" + clean).encode("utf-8")).digest()


def make_name_value(nick: str, addr: str) -> bytes:
    """Подписанная запись «ник -> адрес» (для каталога имён)."""
    pub = identity.get_identity()[1]
    payload = {
        "nick": clean_nick(nick).encode("utf-8"),
        "addr": str(addr).encode("utf-8"),
        "pubkey": pub,
        "ts": int(time.time()),
    }
    sig = identity.sign(bencode.encode(payload))
    return bencode.encode(dict(payload, sig=sig))


def parse_name_value(data: bytes) -> dict | None:
    """Разбор записи имени; None - битая/неподписанная/непригодная."""
    try:
        raw = bencode.decode_dict(data)
    except bencode.BencodeError:
        return None
    required = (b"nick", b"addr", b"pubkey", b"ts", b"sig")
    if any(key not in raw for key in required):
        return None
    try:
        payload = {key: raw[key] for key in required if key != b"sig"}
        if not identity.verify(raw[b"pubkey"], bencode.encode(payload),
                               raw[b"sig"]):
            return None
        nick = raw[b"nick"].decode("utf-8")
        if not nick_ok(nick):
            return None          # мусорный/пустой ник в каталог не идёт
        return {"nick": clean_nick(nick),
                "addr": raw[b"addr"].decode("utf-8"),
                "pubkey": raw[b"pubkey"].hex(),
                "ts": int(raw[b"ts"]),
                "raw": data}
    except Exception:  # noqa: BLE001 - чужой формат не наш
        return None


class Contact:
    """Контакт узла из таблицы маршрутизации."""

    __slots__ = ("node", "host", "port", "last_seen")

    def __init__(self, node: bytes, host: str, port: int):
        self.node = node
        self.host = host
        self.port = port
        self.last_seen = time.time()

    @property
    def addr(self):
        return (self.host, self.port)

    def __repr__(self) -> str:
        return "Contact(%s %s:%d)" % (self.node.hex()[:8], self.host, self.port)


class RoutingTable:
    """k-бакеты Kademlia: узлы по XOR-расстоянию до своих.

    Упрощение против строгого учебника: бакеты не делим
    (в BEP5 дробление опционально, нас интересует содержание,
    а не формальности протокола).
    """

    def __init__(self, own: bytes):
        self._own = own
        self._buckets = [[] for _ in range(NODE_ID_LEN * 8)]

    def _index(self, node: bytes) -> int:
        return max(xor_distance(self._own, node).bit_length() - 1, 0)

    def add(self, contact: Contact) -> bool:
        if contact.node == self._own:
            return False
        bucket = self._buckets[self._index(contact.node)]
        for pos, existing in enumerate(bucket):
            if existing.node == contact.node:
                bucket[pos] = contact
                return True
        if len(bucket) >= K:
            oldest = min(bucket, key=lambda c: c.last_seen)
            if time.time() - oldest.last_seen < 900:
                return False
            bucket.remove(oldest)
        bucket.append(contact)
        return True

    def closest(self, target: bytes, k: int = K) -> list:
        flat = [contact for bucket in self._buckets for contact in bucket]
        flat.sort(key=lambda c: xor_distance(c.node, target))
        return flat[:k]

    def all(self) -> list:
        return [contact for bucket in self._buckets for contact in bucket]

    def remove(self, node: bytes) -> None:
        bucket = self._buckets[self._index(node)]
        for contact in list(bucket):
            if contact.node == node:
                bucket.remove(contact)

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._buckets)



class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, node):
        self._node = node

    def datagram_received(self, data: bytes, addr) -> None:
        self._node._receive(data, addr)


class DHTNode:
    """UDP-узел mesh-DHT: RPC, таблица маршрутизации, хранилище значений.

    Всё в фоновом потоке со своим event loop; наружу - тонкие
    синхронные обёртки (start_sync / lookup_sync / announce_sync /
    stop_sync), чтобы остальные модули не знали про asyncio.
    """

    def __init__(self, log=None, port: int = 0,
                 multicast: bool = True,
                 persist: str | None = None):
        self.own = node_id()
        self.table = RoutingTable(self.own)
        self._log = log or (lambda message: None)
        self._port = int(port)
        self._multicast = multicast
        self._persist = persist
        self._transport = None
        self._pending = {}
        self._store = {}      # key -> [(ts, raw)]
        self._mine = {}       # key -> (value, срок переобъявления)
        self._seq = 0
        self._tasks = []
        self._loop = None
        self._thread = None
        self._started = threading.Event()
        self._stopping = threading.Event()
        self._failure = None

    @property
    def running(self) -> bool:
        return self._transport is not None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> int:
        if self.running:
            return self._port
        self._loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self._port))
        if self._multicast:
            try:
                membership = socket.inet_aton(MULTICAST_GROUP) + \
                    socket.inet_aton(local_ip())
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                membership)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            except OSError:
                pass
        self._port = sock.getsockname()[1]
        transport, _ = await self._loop.create_datagram_endpoint(
            lambda: _Protocol(self), sock=sock)
        self._transport = transport
        if self._persist:
            for contact in load_peers(self._persist):
                self.table.add(contact)
        self._tasks = [
            asyncio.ensure_future(self._bootstrap_once()),
            asyncio.ensure_future(self._maintenance_loop()),
            asyncio.ensure_future(self._hello_loop()),
        ]
        return self._port

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        transport = self._transport
        self._transport = None
        if transport is not None:
            transport.close()
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()

    # -- синхронные обёртки --------------------------------------------------

    def start_sync(self) -> int:
        if self._thread is not None and self._thread.is_alive():
            return self._port
        self._started.clear()
        self._stopping.clear()
        self._failure = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="omnistash-dht")
        self._thread.start()
        self._started.wait(timeout=15)
        if self._failure:
            raise RuntimeError(self._failure)
        return self._port

    def stop_sync(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _run(self) -> None:
        async def main():
            try:
                await self.start()
            except Exception as exc:
                self._failure = str(exc)
                self._log("DHT: не поднялся: %s" % exc)
                return
            finally:
                self._started.set()
            while not self._stopping.is_set():
                await asyncio.sleep(0.2)
            await self.stop()

        try:
            asyncio.run(main())
        except Exception as exc:
            self._failure = self._failure or str(exc)
            self._log("DHT: фон умер: %s" % exc)
            self._started.set()

    def _call(self, coro, timeout: float):
        if self._loop is None or self._failure:
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except Exception:
            return None

    def lookup_sync(self, remote_id: str, want: int = 4,
                    timeout: float = 30.0) -> list:
        """Поиск значений: только подписанные parse_value (без мусора)."""
        if not self.running:
            return []
        values = self._call(self.get_values(key_for(remote_id), want),
                            timeout)
        out = []
        for raw in (values or []):
            parsed = parse_value(raw)
            if parsed is not None:
                out.append(parsed)
        return out

    def announce_sync(self, remote_id: str, digest_hex: str, size: int,
                      synced_at: str, addr: str,
                      timeout: float = 60.0) -> int:
        """Объявить значение по ключу от remote_id; число принявших."""
        if not self.running:
            return 0
        clean = str(digest_hex).replace("sha256:", "")
        digest20 = bytes.fromhex(clean[:40])
        value = make_value(addr, digest20, size, synced_at)
        result = self._call(self.announce(key_for(remote_id), value),
                            timeout)
        return int(result or 0)

    def name_lookup_sync(self, nick: str, want: int = 8,
                         timeout: float = 30.0) -> list:
        """Кандидаты по нику: все ПОДПИСАННЫЕ записи имени (B1).

        Коллизии (один ник у разных ключей) отдаются честно списком -
        пользователь выбирает по отпечатку, а не по написанию ника.
        """
        if not self.running:
            return []
        values = self._call(self.get_values(name_key(nick), want), timeout)
        out = []
        for raw in (values or []):
            parsed = parse_name_value(raw)
            if parsed is not None:
                out.append(parsed)
        return out

    def announce_name_sync(self, nick: str, addr: str,
                           timeout: float = 60.0) -> int:
        """Объявить «ник -> адрес» в каталоге; число принявших узлов.

        Запись попадает в _mine - maintenance переобъявит её сам, как
        и файлы. Смена ника: новое имя объявляется сразу, старое
        устареет по TTL (VALUE_TTL) - удаления в DHT нет.
        """
        if not self.running or not nick_ok(nick):
            return 0
        value = make_name_value(nick, addr)
        result = self._call(self.announce(name_key(nick), value), timeout)
        return int(result or 0)

    # -- приём/отправка -------------------------------------------------------

    def _send(self, payload: dict, addr) -> None:
        if self._transport is None:
            return
        try:
            self._transport.sendto(bencode.encode(payload), addr)
        except (OSError, ValueError):
            pass

    def _txid(self) -> bytes:
        self._seq = (self._seq + 1) % 65536
        return self._seq.to_bytes(2, "big") + os.urandom(1)

    def _receive(self, data: bytes, addr) -> None:
        try:
            message = bencode.decode_dict(data)
        except bencode.BencodeError:
            return
        kind = message.get(b"y")
        txid = message.get(b"t")
        if kind in (b"r", b"e"):
            future = self._pending.get(txid) if isinstance(txid, bytes) else None
            if future is not None and not future.done():
                future.set_result(message)
            return
        if kind == b"q":
            self._handle_query(message, addr)

    def _handle_query(self, message: dict, addr) -> None:
        query = message.get(b"q")
        args = message.get(b"a") or {}
        peer = args.get(b"id")
        txid = message.get(b"t")
        if not isinstance(txid, bytes):
            txid = b"\x00\x00"
        if isinstance(peer, bytes) and len(peer) == NODE_ID_LEN:
            # Любой запрос - уже контакт: узнали узла (и hello тоже).
            self.table.add(Contact(peer, addr[0], addr[1]))
        if query == b"omnistash-hello":
            return
        reply = {b"id": self.own}
        if query == b"ping":
            pass
        elif query == b"find_node":
            target = args.get(b"target")
            if not isinstance(target, bytes):
                target = self.own
            reply[b"nodes"] = b"".join(
                compact_node(c.node, c.host, c.port)
                for c in self.table.closest(target, k=K)
                if _looks_ipv4(c.host))
        elif query == b"get":
            key = args.get(b"key")
            reply[b"token"] = (self._token(key)
                               if isinstance(key, bytes) else b"")
            values = self._values_for(key) if isinstance(key, bytes) else []
            if values:
                reply[b"values"] = values
        elif query == b"put":
            self._accept_put(args)
        else:
            self._send({b"t": txid, b"y": b"e",
                        b"e": [204, "Method Unknown"]}, addr)
            return
        self._send({b"t": txid, b"y": b"r", b"r": reply}, addr)

    def _token(self, key: bytes) -> bytes:
        return hashlib.sha1(self.own + (key or b"")).digest()[:8]

    def _values_for(self, lookup_key: bytes) -> list:
        now = time.time()
        alive = [(ts, raw) for ts, raw in self._store.get(lookup_key, [])
                 if now - ts < VALUE_TTL]
        self._store[lookup_key] = alive
        return [raw for _ts, raw in alive][:MAX_VALUES_PER_KEY]

    def _accept_put(self, args: dict) -> None:
        key = args.get(b"key")
        value = args.get(b"v")
        if not isinstance(key, bytes) or len(key) != NODE_ID_LEN:
            return
        if not isinstance(value, bytes) or not value or len(value) > 1000:
            return
        entries = self._store.setdefault(key, [])
        if len(entries) >= MAX_VALUES_PER_KEY:
            entries.pop(0)
        entries.append((time.time(), value))
        if len(self._store) > 4096:
            oldest_key = min(self._store,
                             key=lambda k: self._store[k][0][0]
                             if self._store[k] else 0)
            self._store.pop(oldest_key, None)

    # -- RPC ----------------------------------------------------------------

    async def _query(self, host: str, port: int, method: str,
                     args: dict):
        for attempt in range(MAX_QUERY_RETRIES):
            txid = self._txid()
            future = self._loop.create_future()
            self._pending[txid] = future
            payload = {b"t": txid, b"y": b"q", b"q": method.encode(),
                       b"a": {b"id": self.own, **args}}
            self._send(payload, (host, port))
            try:
                reply = await asyncio.wait_for(future, timeout=QUERY_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._pending.pop(txid, None)
                if attempt + 1 >= MAX_QUERY_RETRIES:
                    return None
                continue
            self._pending.pop(txid, None)
            if reply.get(b"y") == b"e":
                return None
            body = reply.get(b"r")
            return body if isinstance(body, dict) else {}
        return None

    def _learn(self, reply) -> None:
        if not reply:
            return
        blob = reply.get(b"nodes")
        if isinstance(blob, bytes):
            for node, host, port in parse_compact_nodes(blob):
                if node != self.own and _looks_ipv4(host):
                    self.table.add(Contact(node, host, port))

    async def ping(self, contact: Contact) -> bool:
        reply = await self._query(contact.host, contact.port, "ping", {})
        if reply is None:
            return False
        contact.last_seen = time.time()
        self.table.add(contact)
        return True

    def ping_sync(self, contact: Contact, timeout: float = 10.0) -> bool:
        """Синхронный ping - проверить живость узла из обычного потока."""
        return bool(self._call(self.ping(contact), timeout))

    # -- итеративный lookup --------------------------------------------------

    async def find_nodes(self, target: bytes,
                         rounds: int = LOOKUP_ROUNDS) -> list:
        known = {c.node: c for c in self.table.all()}
        queried = set()
        for _ in range(rounds):
            candidates = sorted(known.values(),
                                key=lambda c: xor_distance(c.node, target))
            frontier = [c for c in candidates if c.node not in queried][:ALPHA]
            if not frontier:
                break
            results = await asyncio.gather(
                *(self._query(c.host, c.port, "find_node",
                              {b"target": target}) for c in frontier))
            moved = False
            for reply, contact in zip(results, frontier):
                queried.add(contact.node)
                if reply is None:
                    continue
                contact.last_seen = time.time()
                self.table.add(contact)
                self._learn(reply)
                moved = True
            if not moved:
                break
        known = {c.node: c for c in self.table.all()}
        return sorted(known.values(),
                      key=lambda c: xor_distance(c.node, target))[:K]

    async def get_values(self, key: bytes, want: int = 4) -> list:
        """Итеративный FIND_VALUE: свои хранилища + соседи по XOR."""
        # Начинаем с того, что уже знаем: близкие узлы спрашиваем первыми,
        # за несколько кругов сходимся к владельцам ключа.
        found = list(self._values_for(key))
        seen = set(found)
        queried = set()
        for _ in range(LOOKUP_ROUNDS):
            candidates = sorted(self.table.all(),
                                key=lambda c: xor_distance(c.node, key))
            frontier = [c for c in candidates if c.node not in queried][:ALPHA]
            if not frontier or len(found) >= want:
                break
            results = await asyncio.gather(
                *(self._query(c.host, c.port, "get", {b"key": key})
                  for c in frontier))
            moved = False
            for reply, contact in zip(results, frontier):
                queried.add(contact.node)
                if reply is None:
                    continue
                contact.last_seen = time.time()
                self.table.add(contact)
                moved = True
                values = reply.get(b"values") or []
                if isinstance(values, bytes):
                    values = [values]
                for raw in values:
                    if isinstance(raw, bytes) and raw not in seen:
                        seen.add(raw)
                        found.append(raw)
            if not moved:
                break
        return found[:want]

    async def announce(self, key: bytes, value: bytes) -> int:
        """Положить значение к соседям; возвращает число принявших."""
        self._mine[key] = (value, time.time() + REANNOUNCE)
        targets = await self.find_nodes(key)
        accepted = 0
        for contact in targets:
            reply = await self._query(contact.host, contact.port, "get",
                                      {b"key": key})
            if reply is None:
                continue
            token = reply.get(b"token")
            if not isinstance(token, bytes) or not token:
                continue
            ok = await self._query(contact.host, contact.port, "put",
                                   {b"key": key, b"token": token, b"v": value})
            if ok is not None:
                accepted += 1
                contact.last_seen = time.time()
        return accepted

    # -- фоновые циклы -------------------------------------------------------

    async def _bootstrap_once(self) -> None:
        """Первый выход в сеть; сбой - не беда (работаем LAN-only)."""
        alive = 0
        for host, port in BOOTSTRAP:
            try:
                infos = await self._loop.getaddrinfo(
                    host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
            except OSError:
                continue
            for _family, _type, _proto, _canon, sockaddr in infos:
                reply = await self._query(sockaddr[0], sockaddr[1],
                                          "find_node",
                                          {b"target": self.own})
                if reply is not None:
                    alive += 1
                    self._learn(reply)
                    break
        if alive:
            await self.find_nodes(self.own)
            self._log("DHT: поднялся: ответили %d, известно узлов %d"
                      % (alive, len(self.table)))

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(MAINTENANCE)
            try:
                now = time.time()
                for key, (value, due) in list(self._mine.items()):
                    if now >= due:
                        await self.announce(key, value)
                stale = sorted(self.table.all(),
                               key=lambda c: c.last_seen)[:3]
                for contact in stale:
                    if not await self.ping(contact):
                        self.table.remove(contact.node)
                if self._persist:
                    save_peers(self._persist, self.table.all())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("DHT: maintenance: %s" % exc)

    async def _hello_loop(self) -> None:
        """LAN-обнаружение: шлём приветствие, чтобы узлы одной сети
        узнали друг друга (свой же пакет вернётся - ему отвечать не надо)."""
        if not self._multicast:
            return
        group = (MULTICAST_GROUP, MULTICAST_PORT)
        while True:
            self._send({b"t": self._txid(), b"y": b"q",
                        b"q": b"omnistash-hello",
                        b"a": {b"id": self.own}}, group)
            await asyncio.sleep(HELLO_INTERVAL)


def _looks_ipv4(host: str) -> bool:
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False
