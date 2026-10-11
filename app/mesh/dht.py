"""Узел mesh-DHT: обнаружение участников без своих серверов (BEP5).

Вход в сеть - публичные bootstrap-роутеры BitTorrent (не наши серверы,
ничего не хостим: BEP5 = совместимость bencode) + LAN-multicast
приветствие для машин в одной локальной сети.

Доставка АДРЕСОВ (каталог имён и файлов) - через ЯДРО протокола:
get_peers / announce_peer (то, что умеет каждый торрент-клиент).
Значения произвольными ключами (расширение dht_store) в живой сети
НЕ работают: там get с «target», immutable-ключ = sha1(самого значения),
поиск по произвольному нику невозможен - проверено пробами на реальной
сети. Наш ключ (имя/видео) = info_hash в get_peers; хранится компактный
адрес (ip+port) держателя, а подпись/хеш/дата приходят С ТЕРМИНАЛА уже
по TLS-каналу (манифест) - это и честнее, и совместимо со всем DHT.

Записи принимаются только с КОМПЛИАНТНЫМ node id (BEP42: id выводится
из внешнего IP через crc32c - сеть молча отбрасывает «чужие» анонсы,
как показали пробы). Внешний IP выучиваем из поля ip в ответах узлов
(стандартный механизм BEP42); LAN-адреса от проверки освобождены.
Объявление своё же: узел обязан отдавать и локальные значения.
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
    ("dht.libtorrent.org", 25401),   # живой (проверено пробами)
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
)
MULTICAST_GROUP = "239.255.77.77"
MULTICAST_PORT = 38575
HELLO_INTERVAL = 60.0
REANNOUNCE = 20 * 60
MAINTENANCE = 60.0
QUERY_TIMEOUT = 4.0
MAX_QUERY_RETRIES = 2

# Живость адресов в get_peers: объявление идёт каждые REANNOUNCE,
# запас - полтора интервала; просроченные записи выкидываются на лету.
PEER_TTL = 45 * 60
PEERS_PER_KEY = 10
PEER_KEYS_LIMIT = 4096
REBOOTSTRAP_INTERVAL = 300.0     # не чаще раза в 5 минут


def node_id() -> bytes:
    return identity.node_id()


def key_for(remote_id: str) -> bytes:
    """Ключ по адресу remote_id? (20 байт, как infohash в BEP5)."""
    return hashlib.sha1(
        ("omnistash:" + str(remote_id)).encode("utf-8")).digest()


# ---- BEP42: node id от внешнего IP (иначе сеть не принимает анонсы) ---

_CRC32C_TABLE: list = []


def _crc32c(data: bytes) -> int:
    """CRC32C (Castagnoli) на stdlib-таблице: данные 4 байта, вызов редкий."""
    global _CRC32C_TABLE
    if not _CRC32C_TABLE:
        table = []
        for index in range(256):
            crc = index
            for _ in range(8):
                crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
            table.append(crc)
        _CRC32C_TABLE = table
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def bep42_id(ip: str, rand_byte: int | None = None) -> bytes:
    """Валидный node id для внешнего IP (BEP42).

    Алгоритм из спецификации: маскированный ip + r (3 бита) -> crc32c ->
    первые 21 бит в префикс id, последний байт = r<<5|… (полный rand).
    Против векторов из спеки (5 штук) покрыто тестами; младшие 3 бита
    третьего байта и хвост - случайные, как в спецификации.
    """
    rand_byte = os.urandom(1)[0] if rand_byte is None else rand_byte
    octets = [int(part) for part in str(ip).split(".")[:4]]
    if len(octets) != 4:
        raise ValueError(f"не IPv4: {ip!r}")
    mask = (0x03, 0x0F, 0x3F, 0xFF)
    masked = [octet & m for octet, m in zip(octets, mask)]
    masked[0] |= (rand_byte & 0x07) << 5
    crc = _crc32c(bytes(masked))
    node = bytearray(20)
    node[0] = (crc >> 24) & 0xFF
    node[1] = (crc >> 16) & 0xFF
    node[2] = ((crc >> 8) & 0xF8) | (os.urandom(1)[0] & 0x07)
    node[3:19] = os.urandom(16)
    node[19] = rand_byte & 0xFF
    return bytes(node)


def bep42_ok(ip: str, node: bytes) -> bool:
    """Совпадает ли id с префиксом BEP42 для этого IP (21 бит)."""
    if len(node) != NODE_ID_LEN:
        return False
    try:
        expected = bep42_id(ip, node[19])
    except ValueError:
        return False
    return (node[0] == expected[0] and node[1] == expected[1]
            and (node[2] & 0xF8) == (expected[2] & 0xF8))


def is_private_ip(ip: str) -> bool:
    """LAN/loopback-адреса от проверки BEP42 освобождены (по спеке)."""
    try:
        first, second = (int(part) for part in str(ip).split(".")[:2])
    except (ValueError, TypeError):
        return True          # не разобрано - считаем локальным (не вредим)
    return (first == 10 or first == 127
            or (first == 172 and 16 <= second <= 31)
            or (first == 192 and second == 168)
            or (first == 169 and second == 254))


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


# ---- каталог имён: ник -> адрес (B1) ---------------------------------
# Ключ имени живёт в отдельном пространстве (свой префикс): DHT ищет по
# точному ключу, частичных запросов протокол не умеет - UI об этом
# честно пишет. «Запись имени» - это адрес держателя в get_peers/
# announce_peer (ядро BEP5); личность (ключ, отпечаток) приходит с
# терминала по TLS: подписывать адрес бессмысленно - его выдаёт сеть,
# а подпись подтверждала бы только само себя. Коллизии (один ник у
# разных ключей) отдаются списком адресов: истину устанавливает
# отпечаток ключа, а не написание ника.

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
            fresh = time.time() - oldest.last_seen < 900
            closer = (xor_distance(self._own, contact.node)
                      < xor_distance(self._own, oldest.node))
            # Переполненный бакет: свежих не вытесняем, НО ближающийся
            # к себе узел берём всегда - иначе поиск застревает (замечено
            # трассировкой: переполненные бакеты отбрасывали узлы, ведущие
            # к ключу, и расстояние не убывало).
            if fresh and not closer:
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
                 persist: str | None = None,
                 own: bytes | None = None):
        # own - осознанно в конструкторе: подмена после создания оставит
        # в RoutingTable устаревший id (самопроверка перестанет ловить
        # собственные пакеты). В проде id меняется только через
        # learn_external_ip - там таблица пересобирается.
        self.own = own if own is not None else node_id()
        self.table = RoutingTable(self.own)
        self._log = log or (lambda message: None)
        self._port = int(port)
        self._multicast = multicast
        self._persist = persist
        self._transport = None
        self._mc_transport = None    # приём multicast (фикс. MULTICAST_PORT)
        self.hello_rx = 0            # сколько LAN-приветствий получили
        self._pending = {}
        self._peers = {}      # info_hash -> [(ip, port, ts)] - наши пиры
        self._mine = {}       # key -> (tcp-порт, срок переобъявления)
        self._seq = 0
        self._tasks = []
        self._loop = None
        self._thread = None
        self._started = threading.Event()
        self._stopping = threading.Event()
        self._failure = None
        self._lock = threading.Lock()   # own/table/_peers из разных потоков
        self._external_ip: str | None = None
        self._ip_learned = threading.Event()
        self._last_bootstrap = 0.0

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
        if self._multicast:
            # ПРИЁМ multicast на фиксированном MULTICAST_PORT (38575):
            # пакет доставляется сокету, привязанному к ПУНКТУ НАЗНАЧЕНИЯ,
            # а все участники шлют приветствия именно на 38575 (свой
            # mesh-порт у всех может быть разным!). Раньше мы слушали
            # только свой mesh-порт - чужие приветствия не доходили ВООБЩЕ
            # (ловушка пряталась за skip'ом теста hello) - обнаружение
            # соседей по LAN было мертво.
            try:
                mc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                mc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                mc.bind(("", MULTICAST_PORT))
                for iface in (local_ip(), "0.0.0.0"):
                    try:
                        membership = socket.inet_aton(MULTICAST_GROUP) + \
                            socket.inet_aton(iface)
                        mc.setsockopt(socket.IPPROTO_IP,
                                      socket.IP_ADD_MEMBERSHIP, membership)
                    except OSError:
                        continue
                transport, _ = await self._loop.create_datagram_endpoint(
                    lambda: _Protocol(self), sock=mc)
                self._mc_transport = transport
            except OSError as exc:
                self._log(f"DHT: multicast-приём недоступен "
                          f"(порт {MULTICAST_PORT}): {exc}")
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
        mc_transport = self._mc_transport
        self._mc_transport = None
        if mc_transport is not None:
            mc_transport.close()
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

    def learn_external_ip(self, ip: str) -> bool:
        """Выучить внешний IP (поле ip в ответах, BEP42) и уточнить id.

        Принимаем ПЕРВЫЙ выученный адрес (дальше не метаемся - ответы
        подделываются UDP-спуфом). Если id не комплиантен для этого IP -
        пересчитываем (BEP42): сеть молча отбрасывает анонсы «чужих» id,
        что и было причиной неработающего поиска через интернет.
        Приватный адрес тоже считается выученным (LAN от проверки
        освобождена) - ensure_ready не должен висеть в локальной сети.
        """
        ip = str(ip or "").strip()
        try:
            octets = [int(part) for part in ip.split(".")[:4]]
            if len(octets) != 4 or any(not 0 <= o <= 255 for o in octets):
                return False
        except (ValueError, TypeError):
            return False
        with self._lock:
            if self._external_ip is not None:
                return False          # первый выученный - главный
            self._external_ip = ip
            self._ip_learned.set()
            if is_private_ip(ip) or bep42_ok(ip, self.own):
                return False
            new_id = bep42_id(ip)
            contacts = self.table.all()
            self.own = new_id
            self.table = RoutingTable(new_id)
            for contact in contacts:
                self.table.add(contact)
        self._log(f"DHT: id уточнён по внешнему IP {ip} (BEP42)")
        return True

    def ensure_ready(self, timeout: float = 15.0) -> bool:
        """Дождаться выученного внешнего IP (перед анонсом); True = выучен."""
        return self._ip_learned.wait(timeout)

    @property
    def external_ip(self) -> str | None:
        with self._lock:
            return self._external_ip

    def lookup_sync(self, remote_id: str, want: int = 8,
                    timeout: float = 60.0) -> list:
        """Адреса держателей файла: [{"addr": "ip:port"}].

        DHT отдаёт только адреса (ядро BEP5); подпись, хеш и дата синка
        приходят с терминала по TLS-каналу в манифесте. Локальные
        значения берутся СИНХРОННО - сеть не должна на них влиять.
        """
        if not self.running:
            return []
        key = key_for(remote_id)
        pairs = list(self._peers_for(key))
        for entry in (self._call(self.find_peers(key, want,
                                                 budget=timeout - 10),
                                 timeout) or []):
            if entry not in pairs:
                pairs.append(entry)
        return [{"addr": f"{ip}:{port}"} for ip, port in pairs[:want]]

    def announce_sync(self, remote_id: str, tcp_port: int,
                      timeout: float = 90.0) -> int:
        """Объявить адрес файла (announce_peer); число принявших узлов."""
        if not self.running:
            return 0
        result = self._call(self._announce(key_for(remote_id),
                                           int(tcp_port)), timeout)
        return int(result or 0)

    def name_lookup_sync(self, nick: str, want: int = 8,
                         timeout: float = 60.0) -> list:
        """Адреса по нику: [{"addr": "ip:port"}] (B1).

        Коллизии (один ник у разных ключей) отдаются честно списком -
        пользователь выбирает по отпечатку (его даёт TLS-канал), а не
        по написанию ника.
        """
        if not self.running:
            return []
        key = name_key(nick)
        pairs = list(self._peers_for(key))
        for entry in (self._call(self.find_peers(key, want,
                                                 budget=timeout - 10),
                                 timeout) or []):
            if entry not in pairs:
                pairs.append(entry)
        return [{"addr": f"{ip}:{port}"} for ip, port in pairs[:want]]

    def announce_name_sync(self, nick: str, tcp_port: int,
                           timeout: float = 90.0) -> int:
        """Объявить «мой ник» в каталоге; число принявших узлов.

        Запись попадает в _mine - maintenance переобъявит её сам, как
        и файлы. Смена ника: новое имя объявляется сразу, старое
        устареет по TTL (PEER_TTL) - удаления в DHT нет.
        """
        if not self.running or not nick_ok(nick):
            return 0
        result = self._call(self._announce(name_key(nick), int(tcp_port)),
                            timeout)
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
                # Ответ на НАШ запрос: поле ip (BEP42) = наш адрес, как его
                # видит сеть. Учим только здесь - спуф-пакет без совпадения
                # txid id не подменит.
                ip_field = message.get(b"ip")
                if isinstance(ip_field, bytes) and len(ip_field) >= 4:
                    self.learn_external_ip(
                        ".".join(str(b) for b in ip_field[:4]))
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
            # контакт выше уже добавлен (id + адрес источника = его
            # mesh-порт); просто считаем, что соседи живы в LAN.
            self.hello_rx += 1
            return
        reply = {b"id": self.own}
        if query == b"ping":
            pass
        elif query in (b"find_node", b"get_peers"):
            # get_peers - ядро BEP5: и узлов, и токен, и наши пиры.
            target = args.get(b"target") or args.get(b"info_hash")
            if not isinstance(target, bytes) or len(target) != NODE_ID_LEN:
                target = self.own
            reply[b"nodes"] = b"".join(
                compact_node(c.node, c.host, c.port)
                for c in self.table.closest(target, k=K)
                if _looks_ipv4(c.host))
            if query == b"get_peers":
                reply[b"token"] = self._token(target)
                values = [ip.encode("ascii") + port.to_bytes(2, "big")
                          for ip, port in self._peers_for(target)]
                if values:
                    reply[b"values"] = values
        elif query == b"announce_peer":
            info_hash = args.get(b"info_hash")
            token = args.get(b"token")
            if (not isinstance(info_hash, bytes)
                    or len(info_hash) != NODE_ID_LEN
                    or not isinstance(token, bytes)
                    or token != self._token(info_hash)):
                self._send({b"t": txid, b"y": b"e",
                            b"e": [203, "invalid token"]}, addr)
                return
            implied = bool(args.get(b"implied_port"))
            port = addr[1] if implied else int(args.get(b"port") or 0)
            # BEP42: анонс чужого (публичного) IP принимаем только с
            # комплиантным id; LAN освобождена. Ответ сервисный в любом
            # случае (спека: запросы обслуживаются всегда), но хранение
            # разрешено только разрешённым - как это делает вся сеть.
            if 0 < port < 65536 and self._may_store(addr[0], peer):
                self._store_peer(info_hash, addr[0], port)
        else:
            self._send({b"t": txid, b"y": b"e",
                        b"e": [204, "Method Unknown"]}, addr)
            return
        self._send({b"t": txid, b"y": b"r", b"r": reply}, addr)

    def _token(self, key: bytes) -> bytes:
        return hashlib.sha1(self.own + (key or b"")).digest()[:8]

    @staticmethod
    def _may_store(sender_ip: str, node_id_hex) -> bool:
        """BEP42: чужой IP обязан иметь комплиантный id; LAN - исключение.

        node_id_hex - bytes id отправителя (20) или None (неизвестен -
        не храним для публичных адресов).
        """
        if is_private_ip(sender_ip):
            return True
        if not isinstance(node_id_hex, bytes) or len(node_id_hex) != NODE_ID_LEN:
            return False
        return bep42_ok(sender_ip, node_id_hex)

    def _peers_for(self, lookup_key: bytes) -> list:
        """Живые пиры ключа [(ip, port)] - и отдаём, и сами ищем тут же."""
        now = time.time()
        with self._lock:
            alive = [(ip, port, ts) for ip, port, ts
                     in self._peers.get(lookup_key, [])
                     if now - ts < PEER_TTL]
            alive = alive[:PEERS_PER_KEY]
            self._peers[lookup_key] = alive
        return [(ip, port) for ip, port, _ts in alive]

    def _store_peer(self, key: bytes, ip: str, port: int) -> bool:
        """Положить адрес под ключ (свой анонс и чужой - одна структура:
        локальное значение обязано отдаваться при прямом запросе)."""
        if not ip or not (0 < int(port) < 65536):
            return False
        now = time.time()
        with self._lock:
            entries = [(a, p, ts) for a, p, ts in self._peers.get(key, [])
                       if now - ts < PEER_TTL
                       and not (a == ip and p == int(port))]
            entries.append((ip, int(port), now))
            if key not in self._peers and len(self._peers) >= PEER_KEYS_LIMIT:
                oldest = min(self._peers,
                             key=lambda k: self._peers[k][0][2]
                             if self._peers[k] else 0)
                self._peers.pop(oldest, None)
            self._peers[key] = entries[-PEERS_PER_KEY:]
        return True

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

    @staticmethod
    def _parse_values(raw_values) -> list:
        """Ответ values -> [(ip, port)]: компакт 6 байт и текст ip:port."""
        if isinstance(raw_values, (bytes, bytearray)):
            raw_values = [raw_values]
        out = []
        for item in (raw_values or []):
            if not isinstance(item, bytes):
                continue
            if len(item) == 6:                      # compact v4: ip + port
                ip = ".".join(str(b) for b in item[:4])
                port = int.from_bytes(item[4:6], "big")
            elif b":" in item:                      # текст «ip:port»
                host, _sep, raw_port = item.partition(b":")
                try:
                    ip, port = host.decode("ascii"), int(raw_port)
                except (UnicodeDecodeError, ValueError):
                    continue
            else:
                continue
            if port and "." in ip:
                out.append((ip, port))
        return out

    async def _iterative(self, key: bytes, deadline: float, want: int):
        """Итеративный обход к ключу: короткий список ВНЕ таблицы.

        Жизненно важный урок трассировки: если опираться только на
        k-бакеты, переполненные бакеты отбрасывают ближающихся узлов из
        ответов - поиск стоит на месте (расстояние не убывает). Поэтому
        узлы из ответов складываются в короткий список (как в Kademlia
        по RFC-шному паттерну), а таблица пополняется как получится.
        Возвращает (адреса [(ip, port)], ближайшие контакты).
        """
        entries: dict = {}
        for contact in self.table.closest(key, k=K * 2):
            entries[contact.node] = contact
        queried = set()
        found = list(self._peers_for(key))       # и свои значения
        seen = set(found)
        for _ in range(LOOKUP_ROUNDS):
            if time.time() >= deadline:
                break
            frontier = sorted(entries.values(),
                              key=lambda c: xor_distance(c.node, key))
            frontier = [c for c in frontier
                        if c.node not in queried][:ALPHA]
            if not frontier or len(found) >= want:
                break
            results = await asyncio.gather(
                *(self._query(c.host, c.port, "get_peers",
                              {b"info_hash": key}) for c in frontier))
            moved = False
            for reply, contact in zip(results, frontier):
                queried.add(contact.node)
                if reply is None:
                    continue
                contact.last_seen = time.time()
                self.table.add(contact)
                moved = True
                for entry in self._parse_values(reply.get(b"values")):
                    if entry not in seen:
                        seen.add(entry)
                        found.append(entry)
                for node, host, port in parse_compact_nodes(
                        reply.get(b"nodes") or b""):
                    if node == self.own or not _looks_ipv4(host):
                        continue
                    entries.setdefault(node, Contact(node, host, port))
            if not moved:
                break
        closest = sorted(entries.values(),
                          key=lambda c: xor_distance(c.node, key))[:K]
        return found[:want], closest

    async def find_peers(self, key: bytes, want: int = 8,
                         budget: float = 25.0) -> list:
        """Адреса держателей ключа (свои + сеть); budget - сетевая часть.

        Локальный ответ идёт отдельно в sync-обёртках: он не должен
        зависеть от здоровья сети (замечено вживую - _call по таймауту
        возвращал None и уносил найденное).
        """
        peers, _closest = await self._iterative(
            key, time.time() + max(1.0, float(budget)), want)
        return peers

    async def find_nodes(self, target: bytes,
                         timeout: float = 25.0) -> list:
        """Ближайшие к target контакты (тот же обход, что и поиск пиры)."""
        _peers, closest = await self._iterative(
            target, time.time() + timeout, 10 ** 9)
        return closest

    async def _announce(self, key: bytes, tcp_port: int) -> int:
        """Положить СВОЙ адрес (announce_peer) под ключ; число принявших.

        Локальная запись обязательна: узел обязан отдавать и свои
        значения (прямой запрос соседа и поиск на той же машине), иначе
        объявленное «невидимо» для всех, кто спрашивает нас самих.
        Кладём ОБА адреса: LAN (соседям по сети достижим) и внешний
        (интернету) - раньше вносился только внешний, и LAN-пиры,
        спрашивая напрямую, получали мёртвый публичный адрес.
        """
        port = int(tcp_port)
        self._mine[key] = (port, time.time() + REANNOUNCE)
        for ip in self._announce_ips():
            self._store_peer(key, ip, port)
        targets = await self.find_nodes(key)
        accepted = 0
        for contact in targets:
            reply = await self._query(contact.host, contact.port,
                                      "get_peers", {b"info_hash": key})
            if reply is None:
                continue
            token = reply.get(b"token")
            if not isinstance(token, bytes) or not token:
                continue
            ok = await self._query(contact.host, contact.port,
                                   "announce_peer",
                                   {b"info_hash": key, b"token": token,
                                    b"port": port, b"implied_port": 0})
            if ok is not None:
                accepted += 1
                contact.last_seen = time.time()
        return accepted

    def _announce_ips(self) -> list:
        """Адреса под собой: локальный (LAN) + внешний (интернет), без дублей.

        LAN-соседям достижим только локальный адрес, интернету - только
        внешний; кто спрашивает напрямую, должен получить достижимый для
        СЕБЯ вариант (оба храним - выбор за спрашивающим, проба отсеет).
        """
        with self._lock:
            external = self._external_ip
        local = local_ip()
        ips = [local]
        if external and external != local and not is_private_ip(external):
            ips.append(external)
        return ips

    def add_contacts_sync(self, addrs: list, timeout: float = 20.0) -> int:
        """Ручные адреса (mesh_peers) как постоянные контакты таблицы.

        Страховка, когда multicast/DHT не помогает (заблокированный hello,
        экзотическая сеть): ping берёт настоящий id узла, таблица узнаёт
        соседа - прямые запросы и поиск по нику начинают работать.
        """
        if not self.running:
            return 0
        result = self._call(self._add_contacts(addrs), timeout)
        return int(result or 0)

    async def _add_contacts(self, addrs: list) -> int:
        added = 0
        for entry in (addrs or []):
            host, sep, port = str(entry).rpartition(":")
            if not sep or not host:
                continue
            try:
                port = int(port)
            except ValueError:
                continue
            if not (0 < port < 65536):
                continue
            reply = await self._query(host, port, "ping", {})
            node = (reply or {}).get(b"id")
            if isinstance(node, bytes) and len(node) == NODE_ID_LEN \
                    and node != self.own:
                self.table.add(Contact(node, host, port))
                added += 1
        return added

    # -- фоновые циклы -------------------------------------------------------

    async def _bootstrap_once(self) -> None:
        """Первый выход в сеть; сбой - не беда (работаем LAN-only)."""
        self._last_bootstrap = time.time()
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
                for key, (port, due) in list(self._mine.items()):
                    if now >= due:
                        await self._announce(key, port)
                stale = sorted(self.table.all(),
                               key=lambda c: c.last_seen)[:3]
                for contact in stale:
                    if not await self.ping(contact):
                        self.table.remove(contact.node)
                # Сеть дала мало узлов (старт/провал bootstrap) - пробуем
                # снова: однократного выхода при старте мало, сеть
                # поднимается не всегда вместе с программой.
                if (len(self.table) < 12
                        and now - self._last_bootstrap > REBOOTSTRAP_INTERVAL):
                    await self._bootstrap_once()
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
