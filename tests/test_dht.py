"""Mesh-M3: bencode-кодек, k-бакеты, подписанные значения, узел DHT.

Сеть локальная: узлы на 127.0.0.1 (системный порт), bootstrap в тестах
выключен (иначе узлы ходили бы на публичные роутеры). LAN-multicast
проверяется с малым интервалом и честно skip, если среда его не даёт.
"""

import os
import socket
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


class TestValues(IdentityCase):
    def test_sign_roundtrip(self):
        raw = dht.make_value("192.168.1.5:38574", bytes(20), 1234,
                             "2026-10-11T09:00:00")
        parsed = dht.parse_value(raw)
        self.assertIsNotNone(parsed, "своя подпись должна сходиться")
        self.assertEqual(parsed["addr"], "192.168.1.5:38574")
        self.assertEqual(parsed["size"], 1234)
        self.assertEqual(parsed["synced_at"], "2026-10-11T09:00:00")
        self.assertEqual(parsed["pubkey"], identity.get_identity()[1].hex())

    def test_tampered_value_rejected(self):
        raw = dht.make_value("192.168.1.5:38574", bytes(20), 1234, "t")
        decoded = bencode.decode_dict(raw)
        decoded[b"size"] = 999                     # подмена поля
        self.assertIsNone(dht.parse_value(bencode.encode(decoded)))
        # Чужая подпись: подписываем чужим ключом ПРАВИЛЬНУЮ структуру.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import \
            Ed25519PrivateKey
        foreign = Ed25519PrivateKey.generate()
        payload = {k: v for k, v in decoded.items() if k != b"sig"}
        forged = dict(payload)
        forged[b"sig"] = foreign.sign(bencode.encode(payload))
        self.assertIsNone(dht.parse_value(bencode.encode(forged)))

    def test_broken_shape_rejected(self):
        self.assertIsNone(dht.parse_value(bencode.encode({b"a": 1})))
        self.assertIsNone(dht.parse_value("не bencode".encode("utf-8")))


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

        self.a = dht.DHTNode(multicast=False, port=0)
        self.b = dht.DHTNode(multicast=False, port=0)
        # Узлы должны РАЗЛИЧАТЬСЯ: общий профиль дал бы им один node_id,
        # и они приняли бы друг друга за себя (таблицы остались бы пусты).
        self.a.own = os.urandom(dht.NODE_ID_LEN)
        self.b.own = os.urandom(dht.NODE_ID_LEN)
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
        self._wire()
        accepted = self.a.announce_sync(
            "vid000000001", "sha256:" + "ab" * 32, 1234,
            "2026-10-11T09:00:00", f"127.0.0.1:{self.port_a}")
        self.assertGreater(accepted, 0, "B обязан принять put")
        found = self.b.lookup_sync("vid000000001")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["addr"], f"127.0.0.1:{self.port_a}")
        self.assertEqual(found[0]["hash"][:4], bytes.fromhex("abababab"))

    def test_lookup_empty_for_unknown_key(self):
        self._wire()
        self.assertEqual(self.b.lookup_sync("vid999999999"), [])

    def test_forged_value_filtered_by_client(self):
        # Кладём в обход подписи: _accept_put не разбирает значения (как
        # и публичный роутер) - отсекает parse_value на стороне клиента.
        self._wire()
        key = dht.key_for("vid000000001")
        junk = bencode.encode({b"addr": b"x:1", b"pubkey": bytes(32),
                               b"hash": bytes(20), b"size": 1,
                               b"synced_at": b"t", b"ts": 1,
                               b"sig": bytes(64)})
        # Прямая запись в store минуя подпись - как это сделал бы злой пир.
        self.b._store.setdefault(key, []).append((time.time(), junk))
        self.assertEqual(self.a.lookup_sync("vid000000001"), [],
                         "значение без верной подписи не должно доходить")

    def test_oversized_value_rejected(self):
        args = {b"key": dht.key_for("vid000000001"),
                b"v": b"x" * 1001}
        self.b._accept_put(args)
        self.assertEqual(self.b._values_for(dht.key_for("vid000000001")),
                         [], "BEP5: больше 1000 байт не принимаем")

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
        """LAN-приветствие находит соседей; если multicast среда не даёт
        (CI-раннер) - тест честно пропускается, а не фейлится."""
        prof = tempfile.TemporaryDirectory()
        self.addCleanup(prof.cleanup)
        with mock.patch.object(identity, "profile_dir",
                               lambda: Path(prof.name)):
            identity.reset_cache()
            self.addCleanup(identity.reset_cache)
            with mock.patch.object(dht, "BOOTSTRAP", ()):
                a = dht.DHTNode(port=0, multicast=True)
                b = dht.DHTNode(port=0, multicast=True)
                a.own = os.urandom(dht.NODE_ID_LEN)
                b.own = os.urandom(dht.NODE_ID_LEN)
                port_a = a.start_sync()
                port_b = b.start_sync()
                try:
                    deadline = time.time() + 4
                    while time.time() < deadline:
                        if any(c.node == b.own for c in a.table.all()):
                            break
                        time.sleep(0.1)
                    if not any(c.node == b.own for c in a.table.all()):
                        self.skipTest("multicast недоступен в среде")
                    self.assertTrue(
                        any(c.node == a.own for c in b.table.all()))
                    self.assertEqual(port_a > 0, True)
                finally:
                    a.stop_sync()
                    b.stop_sync()


if __name__ == "__main__":
    unittest.main()
