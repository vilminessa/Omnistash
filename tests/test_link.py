"""tools/link: канал двух ПК - рукопожатие, журнал, догон, состояние.

Пара демонов на 127.0.0.1 (порт 0 = эфемерный) проигрывает живую
ситуацию двух машин: пара набирает друг друга и держит одно соединение,
журнал переживает перезапуск, дыры закрываются req/replay пачками,
состояние - LWW со снимком при соединении. Широковещание (--announce)
в тесты не берём: это единственная часть, проверяемая вживую на паре.
"""

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import link  # noqa: E402 - путь добавлен выше


def wait_for(cond, timeout=8.0, interval=0.02):
    """Ждать условия (медленный CI); возвращает результат cond (для списков)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = cond()
        if result:
            return result
        time.sleep(interval)
    return cond()


class LinkCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.links = []

    def tearDown(self):
        for one in self.links:
            one.stop_link()               # повторный вызов безопасен
        self.tmp.cleanup()

    def make(self, name, token="secret", dir_name=None, **kwargs):
        params = dict(mesh_port=0, ctrl_port=0, dial_interval=0.15,
                      handshake_timeout=2.0, ping_interval=1.0,
                      dead_timeout=4.0)
        params.update(kwargs)
        one = link.Link(data_dir=self.root / (dir_name or name),
                        peer_id=name, token=token, **params)
        one.start()
        self.links.append(one)
        return one

    def pair(self, token="secret"):
        """Два демона, набирающие друг друга (проверяем и дедупликацию)."""
        a, b = self.make("pcA", token), self.make("pcB", token)
        a.add_peer("127.0.0.1", b.mesh_port)
        b.add_peer("127.0.0.1", a.mesh_port)
        self.assertTrue(wait_for(
            lambda: a.conns.get("pcB") and b.conns.get("pcA")),
            "пара не сошлась за отведённое время")
        return a, b


class TestHandshake(LinkCase):
    def test_pair_links_once(self):
        """Оба набирают - но живёт одно соединение с каждой стороны."""
        a, b = self.pair()
        time.sleep(0.5)                    # время на попытку второго набора
        self.assertEqual(list(a.conns), ["pcB"])
        self.assertEqual(list(b.conns), ["pcA"])
        self.assertEqual(a.conns["pcB"].peer_id, "pcB")

    def test_wrong_token_refused(self):
        a = self.make("pcA", token="один")
        b = self.make("pcB", token="два")
        a.add_peer("127.0.0.1", b.mesh_port)
        b.add_peer("127.0.0.1", a.mesh_port)
        time.sleep(1.0)                    # несколько попыток набора
        self.assertEqual(a.conns, {})
        self.assertEqual(b.conns, {})
        notes = " ".join(text for _ts, text in a.notes)
        self.assertIn("токен не совпал", notes)

    def test_same_id_refused(self):
        """Два узла с одним --id не синхронизируются - честный отказ."""
        a = self.make("pcA")
        b = self.make("pcA", dir_name="pcA-second")
        a.add_peer("127.0.0.1", b.mesh_port)
        b.add_peer("127.0.0.1", a.mesh_port)
        time.sleep(1.0)
        self.assertEqual(a.conns, {})
        self.assertEqual(b.conns, {})
        notes = " ".join(text for _ts, text in list(a.notes) + list(b.notes))
        self.assertIn("наш же id", notes)


class TestJournal(LinkCase):
    def test_delivery_in_order(self):
        a, b = self.pair()
        for index in range(25):
            a.add_log(f"строка {index}")
        entries = wait_for(
            lambda: len(b.journal.read(origin="pcA")) >= 25 and
            b.journal.read(origin="pcA"))
        self.assertEqual([e["seq"] for e in entries], list(range(1, 26)))
        self.assertEqual(entries[-1]["txt"], "строка 24")
        # у отправителя свой seq тот же - оба журнала сходятся
        own = a.journal.read(origin="pcA")
        self.assertEqual([e["seq"] for e in own], list(range(1, 26)))

    def test_catch_up_after_restart(self):
        """Пир ушёл, у него пропали строки - вернулся и докусил всё."""
        a = self.make("pcA")
        for index in range(30):
            a.add_log(f"строка {index}")
        a.stop_link()
        a = self.make("pcA")               # тот же каталог - seq продолжается
        self.assertEqual(a.next_seq, 31)

        b = self.make("pcB")
        b.add_peer("127.0.0.1", a.mesh_port)
        self.assertTrue(wait_for(lambda: b.conns.get("pcA")))
        entries = wait_for(
            lambda: len(b.journal.read(origin="pcA")) >= 30 and
            b.journal.read(origin="pcA"))
        self.assertEqual([e["seq"] for e in entries], list(range(1, 31)))

        a.add_log("после рестарта")
        entries = wait_for(
            lambda: len(b.journal.read(origin="pcA")) >= 31 and
            b.journal.read(origin="pcA"))
        self.assertEqual(entries[-1]["seq"], 31)
        self.assertEqual(entries[-1]["txt"], "после рестарта")

    def test_replay_in_batches(self):
        """REPLAY_BATCH=7: 20 строк доезжают пачками, ничего не теряется."""
        a = self.make("pcA")
        for index in range(20):
            a.add_log(f"строка {index}")
        original = link.REPLAY_BATCH
        link.REPLAY_BATCH = 7
        try:
            b = self.make("pcB")
            b.add_peer("127.0.0.1", a.mesh_port)
            self.assertTrue(wait_for(lambda: b.conns.get("pcA")))
            self.assertTrue(wait_for(
                lambda: len(b.journal.read(origin="pcA")) >= 20, timeout=10))
        finally:
            link.REPLAY_BATCH = original
        entries = b.journal.read(origin="pcA")
        self.assertEqual([e["seq"] for e in entries], list(range(1, 21)))
        self.assertEqual([e["txt"] for e in entries],
                         [f"строка {i}" for i in range(20)])


class TestState(LinkCase):
    def test_lww_and_snapshot(self):
        a, b = self.pair()
        a.add_state("queue", [1, 2])
        self.assertTrue(wait_for(
            lambda: b.state_merged().get("queue") == [1, 2]))
        time.sleep(0.05)                   # ts чужой записи строго новее
        b.add_state("queue", [9])
        self.assertTrue(wait_for(
            lambda: a.state_merged().get("queue") == [9]))

        # перезапуск пира: состояние на диске, снимок при новом
        # соединении не ломает карту
        b.stop_link()
        b2 = self.make("pcB")
        self.assertEqual(b2.state_merged().get("queue"), [9])
        b2.add_peer("127.0.0.1", a.mesh_port)
        self.assertTrue(wait_for(lambda: b2.conns.get("pcA")))
        self.assertTrue(wait_for(lambda: a.conns.get("pcB")))
        self.assertEqual(b2.state_merged().get("queue"), [9])

        a.add_state("x", 1)
        self.assertTrue(wait_for(lambda: b2.state_merged().get("x") == 1))


class TestCli(LinkCase):
    def test_log_state_status(self):
        a = self.make("pcA")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = link.main(["log", "из", "CLI",
                              "--ctrl", str(a.ctrl_port)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        self.assertTrue(wait_for(
            lambda: [e for e in a.journal.read(origin="pcA")
                     if e["txt"] == "из CLI"]))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            link.main(["state", "set", "cli_key", "[1, 2]",
                       "--ctrl", str(a.ctrl_port)])
        self.assertTrue(wait_for(
            lambda: a.state_merged().get("cli_key") == [1, 2]))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            link.main(["state", "get", "--ctrl", str(a.ctrl_port)])
        self.assertEqual(json.loads(out.getvalue()).get("cli_key"), [1, 2])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            link.main(["state", "get", "cli_key",
                       "--ctrl", str(a.ctrl_port)])
        self.assertEqual(json.loads(out.getvalue()), [1, 2])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = link.main(["status", "--ctrl", str(a.ctrl_port)])
        self.assertEqual(code, 0)
        status = json.loads(out.getvalue())
        self.assertEqual(status["id"], "pcA")
        self.assertTrue(status["ok"])
        self.assertEqual(status["next_seq"], 2)   # одна запись сделана

    def test_state_get_offline(self):
        """Демон упал - состояние читается с диска (с честной пометкой)."""
        a = self.make("pcA")
        a.add_state("queue", [1])
        a.stop_link()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = link.main(["state", "get", "queue",
                              "--dir", str(self.root / "pcA")])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), [1])
        self.assertIn("state.json", err.getvalue())

    def test_tail_offline_and_json(self):
        a = self.make("pcA")
        a.add_log("первая")
        a.add_log("вторая")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = link.main(["tail", "--last", "10",
                              "--dir", str(self.root / "pcA")])
        self.assertEqual(code, 0)
        self.assertIn("первая", out.getvalue())
        self.assertIn("вторая", out.getvalue())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            link.main(["tail", "--last", "10", "--json",
                       "--dir", str(self.root / "pcA")])
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r["txt"] for r in rows], ["первая", "вторая"])


if __name__ == "__main__":
    unittest.main()
