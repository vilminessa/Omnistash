"""wait: пробуждение агента - будит только запись ПИРА, своё не будит.

Механика фонового пробуждения: CLI wait висит в фоне хоста, по записи
пира демон шлёт done, клиент выходит с кодом 0 - хост подаёт уведомление
о завершении процесса. Это снимает с человека «тыкай проверь почту».
"""

import contextlib
import io
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import link  # noqa: E402


class WaitCase(unittest.TestCase):
    """Два демона: «я» (18571-подобный ephemeral) и «пир»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.links = []

    def tearDown(self):
        for one in self.links:
            one.stop_link()
        self.tmp.cleanup()

    def make(self, name, **kw):
        params = dict(mesh_port=0, ctrl_port=0, dial_interval=0.15,
                      handshake_timeout=2.0, ping_interval=1.0,
                      dead_timeout=4.0)
        params.update(kw)
        one = link.Link(data_dir=self.root / name, peer_id=name,
                        token="t", **params)
        one.start()
        self.links.append(one)
        return one

    def test_wait_wakes_only_on_peer_entry(self):
        me = self.make("me")
        peer = self.make("peer")
        me.add_peer("127.0.0.1", peer.mesh_port)
        peer.add_peer("127.0.0.1", me.mesh_port)
        self.assertTrue(wait_for(
            lambda: me.conns.get("peer") and peer.conns.get("me")),
            "пара не сошлась")

        woke = {}

        def waiter():
            woke["code"] = link.main(["wait", "--timeout", "15",
                                      "--ctrl", str(me.ctrl_port),
                                      "--json"])

        out = io.StringIO()
        thread = threading.Thread(target=waiter, daemon=True)
        with contextlib.redirect_stdout(out):
            thread.start()
            time.sleep(0.4)
            me.add_log("своё - не должно будить")   # СВОЯ запись
            time.sleep(0.6)
            self.assertTrue(thread.is_alive(),
                            "своя запись не должна разбудить wait")
            peer.add_log("привет от пира")          # запись ПИРА
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive(), "wait должен выйти по записи пира")
        self.assertEqual(woke.get("code"), 0)
        self.assertIn("привет от пира", out.getvalue())

    def test_wait_timeout_is_code_3(self):
        me = self.make("me")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = link.main(["wait", "--timeout", "0.4",
                              "--ctrl", str(me.ctrl_port)])
        self.assertEqual(code, 3, "таймаут - отдельный код, не успех")
        self.assertIn("таймаут", err.getvalue())

    def test_wait_without_daemon_is_code_2(self):
        # свободный (незанятый) порт - демона нет
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
        probe.close()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            code = link.main(["wait", "--timeout", "0.4",
                              "--ctrl", str(dead)])
        self.assertEqual(code, 2)
        self.assertIn("не поднялся", err.getvalue())

    def test_wait_survives_daemon_restart(self):
        """Будильник переживает перезапуск демона (автовосстановление).

        Ловушка живой пары: wait висел на сокете убитого демона и почта
        не будила - теперь клиент переподключается сам.
        """
        a = self.make("pcA")
        mesh_port, ctrl_port = a.mesh_port, a.ctrl_port
        b = self.make("pcB")
        b.add_peer("127.0.0.1", mesh_port)
        self.assertTrue(wait_for(lambda: a.conns.get("pcB")))

        result = {}
        out = io.StringIO()

        def waiter():
            with contextlib.redirect_stdout(out):
                result["code"] = link.main(["wait", "--timeout", "30",
                                            "--ctrl", str(ctrl_port),
                                            "--json"])

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        time.sleep(0.3)
        a.stop_link()                       # демон умер под будильником
        time.sleep(0.5)
        a2 = link.Link(data_dir=self.root / "pcA", peer_id="pcA",
                       token="t", mesh_port=mesh_port,
                       ctrl_port=ctrl_port, dial_interval=0.15,
                       handshake_timeout=2.0)
        a2.start()
        self.links.append(a2)               # убьём в teardown
        time.sleep(3.0)                     # будильник переподключается (1с)
        self.assertTrue(thread.is_alive(),
                        "wait должен переподключиться, а не умереть")
        b.add_log("почта после рестарта")
        thread.join(timeout=10)
        self.assertEqual(result.get("code"), 0,
                         "после рестарта почта должна разбудить")
        self.assertIn("почта после рестарта", out.getvalue())


def wait_for(cond, timeout=8.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = cond()
        if result:
            return result
        time.sleep(interval)
    return cond()


if __name__ == "__main__":
    unittest.main()
