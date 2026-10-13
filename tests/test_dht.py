"""Mesh-M3: bencode-кодек, k-бакеты, BEP42, адреса в get_peers, узел DHT.

Сеть локальная: узлы на 127.0.0.1 (системный порт), bootstrap в тестах
выключен (иначе узлы ходили бы на публичные роутеры). LAN-multicast
проверяется с малым интервалом и честно skip, если среда его не даёт.

Доставка адресов - get_peers/announce_peer (ядро BEP5): значения
произвольными ключами в живой сети не работают (проверено пробами на
реальной сети), поэтому DHT хранит только ip:port держателя, а всё
подписанное приходит с терминала по TLS.
"""

import os
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.mesh import bencode, dht, identity


class IdentityCase(unittest.TestCase):
    def setUp(self):
        self._prof = tempfile.TemporaryDirectory()
        self.addCleanup(self._prof.cleanup)
        patcher = mock.patch.object(identity, "profile_dir",
                                    lambda: Path(self._prof.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        identity.reset_cache()
        self.addCleanup(identity.reset_cache)


class TestBencode(unittest.TestCase):
    def test_roundtrip(self):
        # Декодер возвращает строки как bytes (DHT-стиль): сравниваем так.
        value = {b"a": 42, b"l": [b"x", 1, {b"n": "в".encode("utf-8")}],
                 b"s": "привет".encode("utf-8")}
        self.assertEqual(bencode.decode(bencode.encode(value)), value)

    def test_keys_sorted(self):
        # Порядок ключей фиксирован байтами - иначе подпись не сошлась бы.
        first = bencode.encode({b"b": 1, b"a": 2})
        second = bencode.encode({b"a": 2, b"b": 1})
        self.assertEqual(first, second)

    def test_garbage_rejected(self):
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"d1:ai1e1:bi1eeEXTRA")
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"i42")              # нет конца
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"5:abc")            # строка короче заявленной
        with self.assertRaises(bencode.BencodeError):
            bencode.encode(True)                # bool - не bencode-значение

    def test_decode_dict_requires_mapping(self):
        with self.assertRaises(bencode.BencodeError):
            bencode.decode_dict(b"li1ee")


class TestBep42(unittest.TestCase):
    """BEP42: id от внешнего IP - без этого сеть не принимает анонсы.

    Проба на живой сети: announce_peer с sha1(pubkey)-id молча
    отбрасывается («invalid token»/тишина), с комплиантным - принимается.
    """

    VECTORS = [
        ("124.31.75.21", 1, "5fbfbf"),
        ("21.75.31.124", 86, "5a3ce9"),
        ("65.23.51.170", 22, "a5d432"),
        ("84.124.73.14", 65, "1b0321"),
        ("43.213.53.83", 90, "e56f6c"),
    ]

    def test_vectors_from_spec(self):
        for ip, rand, prefix in self.VECTORS:
            node = dht.bep42_id(ip, rand)
            expected = bytes.fromhex(prefix)
            self.assertEqual(node[0], expected[0], ip)
            self.assertEqual(node[1], expected[1], ip)
            self.assertEqual(node[2] & 0xF8, expected[2] & 0xF8,
                             "байт2: верхние 5 бит из crc, низ - случайный")
            self.assertEqual(node[19], rand, "последний байт = rand")

    def test_crc32c_check_value(self):
        self.assertEqual(dht._crc32c(b"123456789"), 0xE3069283)

    def test_ok_and_mismatch(self):
        good = dht.bep42_id("8.8.8.8", 5)
        self.assertTrue(dht.bep42_ok("8.8.8.8", good))
        self.assertFalse(dht.bep42_ok("9.9.9.9", good),
                         "id чужого IP не проходит")
        self.assertFalse(dht.bep42_ok("8.8.8.8", b"x" * 20),
                         "мусорный id не проходит")

    def test_private_ranges_exempt(self):
        for ip in ("10.1.2.3", "127.0.0.1", "172.16.5.5", "192.168.0.1",
                   "169.254.1.1", "не-адрес"):
            self.assertTrue(dht.is_private_ip(ip), ip)
        self.assertFalse(dht.is_private_ip("8.8.8.8"))
        # Правило хранения: LAN освобождена, публичный IP - только id.
        self.assertTrue(dht.DHTNode._may_store("192.168.1.5", None))
        self.assertFalse(dht.DHTNode._may_store("8.8.8.8", None))
        self.assertFalse(dht.DHTNode._may_store("8.8.8.8", b"x" * 20))
        self.assertTrue(dht.DHTNode._may_store(
            "8.8.8.8", dht.bep42_id("8.8.8.8", 7)))


class TestPeerStore(IdentityCase):
    """Хранилище адресов: своё значение видно всем, TTL и лимиты."""

    def _node(self) -> dht.DHTNode:
        node = dht.DHTNode(multicast=False, port=0)
        node.own = b"\x07" * 20
        return node

    def test_store_and_serve_own_value(self):
        node = self._node()
        key = dht.key_for("vid000000001")
        self.assertTrue(node._store_peer(key, "1.2.3.4", 38574))
        self.assertEqual(node._peers_for(key), [("1.2.3.4", 38574)])

    def test_dedupe_and_cap(self):
        node = self._node()
        key = dht.key_for("vid000000001")
        for index in range(dht.PEERS_PER_KEY + 5):
            node._store_peer(key, f"1.2.3.{index}", 40000 + index)
        peers = node._peers_for(key)
        self.assertEqual(len(peers), dht.PEERS_PER_KEY, "потолок на ключ")
        self.assertEqual(peers[-1],
                         (f"1.2.3.{dht.PEERS_PER_KEY + 4}",
                          40000 + dht.PEERS_PER_KEY + 4),
                         "свежие адреса должны оставаться")
        node._store_peer(key, "1.2.3.9", 49999)
        node._store_peer(key, "1.2.3.9", 49999)
        self.assertEqual(
            len([p for p in node._peers_for(key)
                 if p == ("1.2.3.9", 49999)]), 1,
            "повторная запись одного адреса - не дубль")

    def test_ttl_prunes_old(self):
        node = self._node()
        key = dht.key_for("vid000000001")
        node._store_peer(key, "5.6.7.8", 1111)
        node._peers[key] = [("5.6.7.8", 1111,
                             time.time() - dht.PEER_TTL - 1)]
        self.assertEqual(node._peers_for(key), [],
                         "просроченный адрес уходит сам")

    def test_parse_values_formats(self):
        compact = bytes([1, 2, 3, 4]) + (38574).to_bytes(2, "big")
        self.assertEqual(dht.DHTNode._parse_values([compact]),
                         [("1.2.3.4", 38574)])
        self.assertEqual(dht.DHTNode._parse_values(compact),
                         [("1.2.3.4", 38574)], "одиночный байтовый ответ")
        self.assertEqual(dht.DHTNode._parse_values([b"9.9.9.9:6881"]),
                         [("9.9.9.9", 6881)], "текстовый формат тоже читаем")
        self.assertEqual(dht.DHTNode._parse_values(
            ["мусор".encode("utf-8"), b"x", None]), [])
        self.assertEqual(dht.DHTNode._parse_values(None), [])


class TestNameRecords(IdentityCase):
    """Каталог имён (B1): ключ и правила ника.

    Записи имени - адреса через get_peers (см. TestBep42/TestPeerStore и
    сетевые тесты в DHTCase); здесь - то, что чисто вычисляется.
    """

    def test_name_key_casefold_and_separate_space(self):
        self.assertEqual(dht.name_key("Nick"), dht.name_key(" nick "),
                         "регистр и края не важны")
        self.assertNotEqual(dht.name_key("nick"), dht.name_key("nick2"))
        self.assertEqual(len(dht.name_key("Участник")), 20)
        self.assertNotEqual(dht.name_key("vid000000001"),
                            dht.key_for("vid000000001"),
                            "имена живут в своём пространстве ключей")

    def test_nick_rules(self):
        self.assertTrue(dht.nick_ok("Иван Петров"))
        self.assertTrue(dht.nick_ok("a-b_c"))
        self.assertFalse(dht.nick_ok("a"), "слишком короткий")
        self.assertFalse(dht.nick_ok("x" * 33), "слишком длинный")
        self.assertFalse(dht.nick_ok("ник<script>"), "лишние символы")
        self.assertFalse(dht.nick_ok("  "))
        self.assertEqual(dht.clean_nick("  Иван   Петров  "), "Иван Петров")


class TestRoutingTable(unittest.TestCase):
    def test_add_closest_and_self_rejected(self):
        own = bytes(20)
        table = dht.RoutingTable(own)
        self.assertFalse(table.add(dht.Contact(own, "127.0.0.1", 1)))
        far = dht.Contact(b"\xff" * 20, "127.0.0.1", 2)
        near = dht.Contact(b"\x00" * 19 + b"\x01", "127.0.0.1", 3)
        table.add(far)
        table.add(near)
        closest = table.closest(bytes(20))
        self.assertEqual(closest[0].node, near.node, "ближайший - первый")
        self.assertEqual(len(table), 2)

    def test_full_bucket_evicts_stale(self):
        own = bytes(20)
        table = dht.RoutingTable(own)
        # Наполняем БАКЕТ 159: узлы, отличающиеся ведущим битом
        # (distance >= 2^159 -> bit_length 160 -> индекс 159).
        for i in range(dht.K):
            node = b"\x80" + bytes(18) + bytes([1 + i])
            table.add(dht.Contact(node, "127.0.0.1", 100 + i))
        self.assertEqual(len(table), dht.K)
        # Все «свежие» - новому нет места...
        fresh = dht.Contact(b"\x80" + bytes(18) + b"\x33",
                            "127.0.0.1", 999)
        fresh.last_seen = time.time()
        self.assertFalse(table.add(fresh))
        # ...а устаревший вытесняется.
        for contact in table.all():
            contact.last_seen = time.time() - 1800
        self.assertTrue(table.add(fresh))


class DHTCase(unittest.TestCase):
    """Два узла на localhost; bootstrap и multicast в тестах выключены."""

    def setUp(self):
        self._prof = tempfile.TemporaryDirectory()
        self.addCleanup(self._prof.cleanup)
        patcher = mock.patch.object(identity, "profile_dir",
                                    lambda: Path(self._prof.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        identity.reset_cache()
        self.addCleanup(identity.reset_cache)
        patcher = mock.patch.object(dht, "BOOTSTRAP", ())
        patcher.start()
        self.addCleanup(patcher.stop)
        # Тесты развёрнуты на loopback: «свой» адрес для самозаписи -
        # 127.0.0.1, а не настоящий LAN-IP машины.
        patcher = mock.patch.object(dht, "local_ip", lambda: "127.0.0.1")
        patcher.start()
        self.addCleanup(patcher.stop)
        # _local_ips ходит в getaddrinfo(hostname): без патча он дал бы
        # РЕАЛЬНЫЕ адреса машины (LAN/VPN), и объявления были бы
        # непредсказуемы. Пустой список → fallback на local_ip выше.
        patcher = mock.patch.object(socket, "getaddrinfo", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

        # Узлы должны РАЗЛИЧАТЬСЯ: общий профиль дал бы им один node_id,
        # и они приняли бы друг друга за себя (таблицы остались бы пусты).
        # own задаём в конструкторе: подмена после создания оставила бы
        # в RoutingTable устаревший id (самопроверка не ловит свои пакеты).
        self.a = dht.DHTNode(multicast=False, port=0,
                             own=os.urandom(dht.NODE_ID_LEN))
        self.b = dht.DHTNode(multicast=False, port=0,
                             own=os.urandom(dht.NODE_ID_LEN))
        self.port_a = self.a.start_sync()
        self.port_b = self.b.start_sync()
        self.addCleanup(self.a.stop_sync)
        self.addCleanup(self.b.stop_sync)

    def _wire(self):
        """Познакомить узлы вручную (вместо bootstrap/hello)."""
        self.a.table.add(dht.Contact(self.b.own, "127.0.0.1", self.port_b))
        self.b.table.add(dht.Contact(self.a.own, "127.0.0.1", self.port_a))

    def test_ping_learns_peer(self):
        contact = dht.Contact(self.b.own, "127.0.0.1", self.port_b)
        self.assertTrue(self.a.ping_sync(contact))
        # B узнал A из id входящего запроса.
        self.assertTrue(any(c.node == self.a.own for c in self.b.table.all()))

    def test_announce_then_lookup(self):
        """АнOUNCE доходит по сети; локальное значение видно и себе же."""
        self._wire()
        accepted = self.a.announce_sync("vid000000001", 4321)
        self.assertGreater(accepted, 0, "B обязан принять announce_peer")
        found = self.b.lookup_sync("vid000000001")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["addr"], "127.0.0.1:4321",
                         "адрес = вид нами отправителя + объявленный порт")
        # ЛОКАЛЬНАЯ видимость: объявивший обязан найти своё же (ловушка
        # «get обязан отдавать и локальные значения»).
        mine = self.a.lookup_sync("vid000000001")
        self.assertEqual(len(mine), 1,
                         "объявивший обязан видеть своё объявление сам")

    def test_lookup_empty_for_unknown_key(self):
        self._wire()
        self.assertEqual(self.b.lookup_sync("vid999999999"), [])

    def test_announce_peer_rejects_bad_token(self):
        """Анонс с чужим токеном -> ошибка 203 (и ничего не записано)."""
        self._wire()
        key = dht.key_for("vid000000001")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        try:
            packet = bencode.encode(
                {b"t": b"\x01\x02\x03", b"y": b"q",
                 b"q": b"announce_peer",
                 b"a": {b"id": os.urandom(20), b"info_hash": key,
                        b"token": "чужой-токен".encode("utf-8"),
                        b"port": 9999}})
            sock.sendto(packet, ("127.0.0.1", self.port_b))
            data, _ = sock.recvfrom(4096)
        finally:
            sock.close()
        reply = bencode.decode_dict(data)
        self.assertEqual(reply.get(b"y"), b"e")
        self.assertEqual(reply.get(b"e"), [203, b"invalid token"])
        self.assertEqual(self.b._peers_for(key), [],
                         "с чужим токеном не должно записаться ничего")

    def test_implied_port_uses_source_port(self):
        """implied_port=1 -> берём порт источника UDP (стандарт BEP5)."""
        self._wire()
        key = dht.key_for("vid000000001")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Бинд до getsockname: на Windows несвязанный UDP-сокет
        # отдаёт WSAEINVAL (10022).
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(3)
        source_port = sock.getsockname()[1]

        def rpc(payload):
            sock.sendto(bencode.encode(payload), ("127.0.0.1", self.port_b))
            data, _ = sock.recvfrom(4096)
            return bencode.decode_dict(data)

        try:
            reply = rpc({b"t": b"\x0a\x0b\x0c", b"y": b"q",
                         b"q": b"get_peers", b"a": {b"id": os.urandom(20),
                                                    b"info_hash": key}})
            token = (reply.get(b"r") or {}).get(b"token")
            self.assertTrue(token, "get_peers обязан выдать токен")
            ack = rpc({b"t": b"\x0d\x0e\x0f", b"y": b"q",
                       b"q": b"announce_peer",
                       b"a": {b"id": os.urandom(20), b"info_hash": key,
                              b"token": token, b"implied_port": 1}})
            self.assertEqual(ack.get(b"y"), b"r")
        finally:
            sock.close()
        peers = self.b._peers_for(key)
        self.assertIn(("127.0.0.1", source_port), peers,
                      "implied_port: порт = источник UDP")

    def test_name_announce_and_lookup(self):
        """Объявление ника живым узлом -> поиск без регистра."""
        self._wire()
        accepted = self.a.announce_name_sync("Друг", 5551)
        self.assertGreater(accepted, 0, "B обязан принять анонс имени")
        found = self.b.name_lookup_sync("друг")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["addr"], "127.0.0.1:5551")
        self.assertEqual(self.b.name_lookup_sync("никого-нет"), [],
                         "пустой ключ - пусто")

    def test_nick_collision_lists_both_addresses(self):
        """Один ник у разных узлов: оба адреса честно списком."""
        self._wire()
        self.a.announce_name_sync("Общий", 5551)
        self.b.announce_name_sync("Общий", 5552)
        found = self.b.name_lookup_sync("ОБЩИЙ")
        self.assertEqual(len(found), 2, "коллизия обязана показаться списком")
        self.assertEqual({f["addr"] for f in found},
                         {"127.0.0.1:5551", "127.0.0.1:5552"},
                         "разные адреса - разные участники")

    def test_announce_stores_all_addresses(self):
        """Под своим ключом: LAN + VPN-класс + внешний (F1-многоадресность).

        Раньше клался только внешний: LAN-сосед, спрашивая напрямую,
        получал публичный адрес, недостижимый из сети (UPnP может не
        работать). Источник локальных - getaddrinfo(hostname); APIPA и
        чужие классы (Radmin) не объявляем.
        """
        self._wire()
        self.a._external_ip = "203.0.113.9"     # выученный внешний
        # getaddrinfo отдаёт 5-элементные кортежи (с canonname).
        def entry(addr):
            return (socket.AF_INET, socket.SOCK_STREAM, 0, "", (addr, 0))
        fake = [entry("192.168.9.9"), entry("100.64.0.9"),
                entry("169.254.7.7"), entry("26.201.28.9")]
        with mock.patch.object(socket, "getaddrinfo", return_value=fake):
            self.a.announce_sync("vid000000001", 4321)
            peers = set(self.a._peers_for(dht.key_for("vid000000001")))
        self.assertIn(("127.0.0.1", 4321), peers,
                      "fallback local_ip обязателен")
        self.assertIn(("192.168.9.9", 4321), peers, "LAN-адрес")
        self.assertIn(("100.64.0.9", 4321), peers,
                      "VPN-класс Tailscale/ZeroTier объявляем")
        self.assertIn(("203.0.113.9", 4321), peers, "внешний при пробросе")
        self.assertNotIn(("169.254.7.7", 4321), peers, "APIPA не объявляем")
        self.assertNotIn(("26.201.28.9", 4321), peers,
                         "Radmin-класс не объявляем")

    def test_manual_contacts_added_via_ping(self):
        """mesh_peers -> таблица контактов (страховка без multicast)."""
        added = self.a.add_contacts_sync([f"127.0.0.1:{self.port_b}",
                                          "кривой-адрес",
                                          "127.0.0.1:1"])
        self.assertEqual(added, 1, "живой адрес берётся, кривые отбрасываются")
        self.assertTrue(any(c.node == self.b.own for c in self.a.table.all()),
                        "настоящий id узла должен осесть в таблице")

    def test_direct_get_peers_values_are_standard_compact(self):
        """Прямой запрос к узлу обязан отдать СТАНДАРТНЫЕ 6-байтовые значения.

        Живой дефект B1.6: отдавали ip текстом + порт (14 байт) - наш же
        приёмник это не парсил, и самозапись была невидима для любого,
        кто спрашивал напрямую.
        """
        self._wire()
        self.a.announce_sync("vid000000001", 4321)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(3)
        try:
            packet = bencode.encode(
                {b"t": b"\x11\x22\x33", b"y": b"q", b"q": b"get_peers",
                 b"a": {b"id": os.urandom(20),
                        b"info_hash": dht.key_for("vid000000001")}})
            sock.sendto(packet, ("127.0.0.1", self.port_a))
            data, _ = sock.recvfrom(4096)
        finally:
            sock.close()
        reply = bencode.decode_dict(data)
        values = (reply.get(b"r") or {}).get(b"values") or []
        self.assertTrue(values, "объявленное обязано отдаваться")
        for item in values:
            self.assertEqual(len(item), 6,
                             f"компакт обязан быть 6 байт, получено "
                             f"{len(item)}: {item!r}")
        parsed = dht.DHTNode._parse_values(values)
        self.assertIn(("127.0.0.1", 4321), parsed,
                      "наш приёмник обязан разобрать собственные значения")

    def test_shortlist_always_includes_lan_contacts(self):
        """LAN-контакт опрашивается всегда, даже вне ближайших K*2.

        Держатель LAN-адресов (сосед) может быть далёк от ключа - раньше
        он выпадал из опроса и поиск приносил лишь недостижимые
        публичные адреса (замерено живым поиском).
        """
        key = dht.key_for("vid000000001")
        # own = key: тогда бакеты (расстояние ДО own) детерминированы и
        #16 «ближайших» ложатся по одному в бакет, без вытеснений.
        node = dht.DHTNode(multicast=False, port=0, own=key)
        # id = key XOR (1<<i): ближайшие с явно растущим расстоянием,
        # по одному на бакет - все K*2 помещаются без вытеснения.
        for i in range(dht.K * 2):
            node_id = int.from_bytes(key, "big") ^ (1 << i)
            node.table.add(dht.Contact(node_id.to_bytes(20, "big"),
                                       "8.8.4.4", 1000 + i))
        far_private = b"\xff" * 20
        node.table.add(dht.Contact(far_private, "192.168.1.9", 38574))
        closest = node.table.closest(key, k=dht.K * 2)
        self.assertNotIn(far_private, [c.node for c in closest],
                         "предусловие: дальний LAN-контакт вне ближайших")
        shortlist = node._shortlist(key)
        self.assertIn(far_private, [c.node for c in shortlist.values()],
                      "LAN-контакт обязан входить в шортлист всегда")

    def test_ip_change_detected(self):
        """Смена локального адреса (DHCP) ловится сразу, не через 20 минут."""
        node = dht.DHTNode(multicast=False, port=0,
                           own=os.urandom(dht.NODE_ID_LEN))
        node._last_local_ip = "192.168.0.30"
        logs = []
        node._log = logs.append
        with mock.patch.object(dht, "local_ip", lambda: "192.168.0.31"):
            self.assertTrue(node._refresh_on_ip_change(),
                            "новый адрес должен быть замечен")
            self.assertFalse(node._refresh_on_ip_change(),
                             "повторно не переобъявляем")
        self.assertTrue(any("сменился" in line for line in logs), logs)

    def test_publish_public_gate(self):
        """Без UPnP-проброса посторонним не разбрасываем адрес (Ф6).

        Парная ловушка: у машин за ОДНИМ НАТОМ публичный IP одинаковый
        (vilmpc/vilmnote = обе 2.60.152.240, порт 38574) - запись узла
        без проброса указывала бы на ЧУЖОЙ порт. С флагом выкл - только
        локальная самозапись (LAN-соседи читают её напрямую).
        """
        self._wire()
        self.a.set_publish_public(False)
        accepted = self.a.announce_sync("vid000000001", 4321)
        self.assertEqual(accepted, 0, "посторонним в сеть не отправляем")
        # Лан-сосед напрямую ВСЁ равно видит самозапись (это и цель):
        direct = self.b.lookup_sync("vid000000001")
        self.assertEqual([f["addr"] for f in direct],
                         ["127.0.0.1:4321"],
                         "прямой запрос LAN-соседа читает самозапись")
        self.assertEqual(
            self.a._peers_for(dht.key_for("vid000000001")),
            [("127.0.0.1", 4321)],
            "в самозаписи только локальный адрес (публичный не ложим)")
        # Проброс подтверждён - публикуем как раньше.
        self.a.set_publish_public(True)
        accepted = self.a.announce_sync("vid000000001", 4321)
        self.assertGreater(accepted, 0)
        self.assertEqual(len(self.b.lookup_sync("vid000000001")), 1)

    def test_start_stop_idempotent(self):
        self.assertEqual(self.a.port, self.port_a)
        self.assertEqual(self.a.start_sync(), self.port_a, "повтор - без вреда")
        self.assertTrue(self.a.running)


class TestPeerMemory(unittest.TestCase):
    """Память живых узлов между запусками (профиль)."""

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "peers.json")
            contacts = [dht.Contact(b"\x01" * 20, "192.168.1.2", 38574),
                        dht.Contact(b"\x02" * 20, "10.0.0.5", 6881)]
            dht.save_peers(path, contacts)
            loaded = dht.load_peers(path)
            self.assertEqual(
                [(c.node, c.host, c.port) for c in loaded],
                [(c.node, c.host, c.port) for c in contacts])

    def test_missing_and_corrupt_are_empty(self):
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "nope.json")
            self.assertEqual(dht.load_peers(missing), [])
            broken = os.path.join(td, "peers.json")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("{сломанный json")
            self.assertEqual(dht.load_peers(broken), [])

    def test_bad_records_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "peers.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"peers": ['
                             '{"node": "zz", "host": "1.2.3.4", "port": 1},'
                             '{"node": "' + "00" * 20 +
                             '", "host": "1.2.3.4", "port": 99999},'
                             '{"node": "' + "ff" * 20 +
                             '", "host": "5.6.7.8", "port": 6881}]}')
            loaded = dht.load_peers(path)
            self.assertEqual([(c.host, c.port) for c in loaded],
                             [("5.6.7.8", 6881)], "битые записи пропускаются")


class TestHello(unittest.TestCase):
    def test_multicast_discovery_or_skip(self):
        """Соседи видят друг друга по multicast; без multicast среда - skip.

        Ключевой урок: приём идёт на MULTICAST_PORT (38575), куда все
        УЗЛЫ И ШЛЮТ (свой mesh-порт у всех разный!) - раньше узлы
        слушали только свой mesh-порт и чужие приветствия не принимали
        ВООБЩЕ, а баг прятался за skip «multicast недоступен». Поэтому
        сначала сами убеждаемся, что среда несёт multicast (слышим свой
        же пакет на 38575), и только ПОТОМ требуем обнаружения: если
        среда несёт, а узел соседа не увидел - это отказ КОДА, не skip.
        """
        prof = tempfile.TemporaryDirectory()
        self.addCleanup(prof.cleanup)
        with mock.patch.object(identity, "profile_dir",
                               lambda: Path(prof.name)):
            identity.reset_cache()
            self.addCleanup(identity.reset_cache)
            with mock.patch.object(dht, "BOOTSTRAP", ()):
                # Свой слушатель - честная проверка среды ДО теста узла.
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("", dht.MULTICAST_PORT))
                    mreq = struct.pack(
                        "4s4s",
                        socket.inet_aton(dht.MULTICAST_GROUP),
                        socket.inet_aton("0.0.0.0"))
                    probe.setsockopt(socket.IPPROTO_IP,
                                     socket.IP_ADD_MEMBERSHIP, mreq)
                except OSError:
                    probe.close()
                    self.skipTest("multicast-порт недоступен в среде")
                probe.settimeout(0.5)

                a = dht.DHTNode(port=0, multicast=True,
                                own=os.urandom(dht.NODE_ID_LEN))
                try:
                    a.start_sync()
                    # 1) среда несёт: ловим чьё-то приветствие на 38575.
                    deadline = time.time() + 5
                    heard = False
                    while time.time() < deadline and not heard:
                        try:
                            probe.recvfrom(4096)
                            heard = True
                        except socket.timeout:
                            pass
                    if not heard:
                        self.skipTest("multicast не расходится в среде")

                    # 2) код принимает: поднимаем B - A обязан его увидеть.
                    b = dht.DHTNode(port=0, multicast=True,
                                    own=os.urandom(dht.NODE_ID_LEN))
                    try:
                        b.start_sync()
                        deadline = time.time() + 5
                        while time.time() < deadline:
                            if any(c.node == b.own for c in a.table.all()):
                                break
                            time.sleep(0.1)
                        self.assertTrue(
                            any(c.node == b.own for c in a.table.all()),
                            "приветствие дошло по сети (п.1 прошёл), но узел "
                            "A его не принял - приём multicast сломан")
                        self.assertGreater(a.hello_rx, 0,
                                           "счётчик приветствий должен расти")
                    finally:
                        b.stop_sync()
                        self.addCleanup(b.stop_sync)
                finally:
                    probe.close()
                    a.stop_sync()
                    self.addCleanup(a.stop_sync)


if __name__ == "__main__":
    unittest.main()
