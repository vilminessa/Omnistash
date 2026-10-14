"""1.3/1.4: адресная книга устройств и health-check.

Критерий приёмки этапа 1: «смена IP восстанавливается сама» - адресная
книга + редиал в mesh.fetch (интеграция - в tests/test_mesh_net.py,
test_redial_after_ip_change). Здесь: repo-API (источники, лимит,
свежесть) и health-checker (смены статусов, джиттер, устойчивость).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import repo
from app.db import Database
from app.mesh import health as mesh_health

KEY_A = "a1" * 32
KEY_B = "b2" * 32


class PeerAddrCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "library.db")
        self.conn = self.db.conn
        # addCleanup идут в обратном порядке: сначала закрыть базу
        # (WAL держит дескриптор), потом снять временную папку.
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.db.close)


class TestPeerAddrs(PeerAddrCase):
    def test_add_list_freshness(self):
        repo.peer_addr_add(self.conn, KEY_A, "1.2.3.4:1", "probe")
        repo.peer_addr_add(self.conn, KEY_A, "5.6.7.8:1", "sas")
        rows = repo.peer_addr_list(self.conn, KEY_A)
        self.assertEqual(rows[0]["addr"], "5.6.7.8:1",
                         "свежий адрес первым")
        self.assertEqual(rows[0]["source"], "sas")
        self.assertEqual(len(rows), 2)

    def test_repeat_updates_last_seen(self):
        # фиксируем время: две записи в одну секунду не различимы по last_seen
        ticks = iter(["2026-01-01T00:00:01", "2026-01-01T00:00:02",
                      "2026-01-01T00:00:03"])
        with mock.patch("app.repo.now_iso", side_effect=lambda: next(ticks)):
            repo.peer_addr_add(self.conn, KEY_A, "1.2.3.4:1", "probe")
            repo.peer_addr_add(self.conn, KEY_A, "5.6.7.8:1", "catalog")
            repo.peer_addr_add(self.conn, KEY_A, "1.2.3.4:1", "health")
        rows = repo.peer_addr_list(self.conn, KEY_A)
        self.assertEqual(rows[0]["addr"], "1.2.3.4:1",
                         "повторная встреча оживляет адрес вверху")
        self.assertEqual(rows[0]["source"], "health")
        self.assertEqual(len(rows), 2, "дублей нет - PK (pubkey, addr)")

    def test_history_limit(self):
        for index in range(repo.PEER_ADDR_KEEP + 5):
            repo.peer_addr_add(self.conn, KEY_A, f"10.0.0.{index}:1",
                               "probe")
        rows = repo.peer_addr_list(self.conn, KEY_A, limit=100)
        self.assertEqual(len(rows), repo.PEER_ADDR_KEEP,
                         "история не растёт бесконечно")

    def test_empty_and_bad_key(self):
        repo.peer_addr_add(self.conn, KEY_A, "", "probe")   # молча пропуск
        self.assertEqual(repo.peer_addr_list(self.conn, KEY_A), [])
        with self.assertRaises(ValueError):
            repo.peer_addr_add(self.conn, "кривой", "1.2.3.4:1")


class TestPeerHealth(PeerAddrCase):
    def test_status_transition(self):
        self.assertIsNone(repo.peer_health_set(self.conn, KEY_A,
                                               "alive")["was"])
        self.assertEqual(repo.peer_health_set(self.conn, KEY_A,
                                              "dead")["was"], "alive")
        snapshot = repo.peer_health(self.conn)
        self.assertEqual(snapshot[KEY_A]["status"], "dead")


class TestHealthChecker(unittest.TestCase):
    def _checker(self, dead=(), events=None, calls=None):
        def probe(addr, expect=None):
            if calls is not None:
                calls.append((addr, expect))
            if expect in dead:
                raise OSError("молчит")

        def on_result(pubkey, status, detail=""):
            if events is not None:
                events.append((pubkey[:2], status))
            return None                    # «было» = None - всё логируем

        devices = [(KEY_A, "1.1.1.1:1"), (KEY_B, "2.2.2.2:2")]
        return mesh_health.HealthChecker(probe, lambda: devices, on_result)

    def test_check_now_transitions(self):
        calls = []
        events = []
        checker = self._checker(dead={KEY_B}, events=events, calls=calls)
        result = checker.check_now()
        self.assertEqual(result[KEY_A], "alive")
        self.assertEqual(result[KEY_B], "dead")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(events), 2)

    def test_devices_without_addr_skipped(self):
        seen = []
        checker = mesh_health.HealthChecker(
            lambda addr, expect=None: seen.append(addr),
            lambda: [(KEY_A, "1.1.1.1:1"), (KEY_B, "")],
            lambda *args: None)
        checker.check_now()
        self.assertEqual(seen, ["1.1.1.1:1"])

    def test_probe_exception_does_not_stop_loop(self):
        """check_now не должен ронять цикл на кривом устройстве."""
        def probe(addr, expect=None):
            if expect == KEY_A:
                raise ValueError("кривизна")
            return None
        checker = mesh_health.HealthChecker(
            probe, lambda: [(KEY_A, "1.1.1.1:1"), (KEY_B, "2.2.2.2:2")],
            lambda *args: None)
        result = checker.check_now()
        self.assertEqual(result[KEY_A], "dead")
        self.assertEqual(result[KEY_B], "alive")

    def test_start_stop(self):
        checker = self._checker()
        checker._interval = 0.05
        checker.start()
        self.assertTrue(checker.running)
        checker.stop()
        self.assertFalse(checker.running)


if __name__ == "__main__":
    unittest.main()
