"""B1: разовая передача по коду (magic-wormhole в изолированном venv).

Библиотека в тестах НЕ ставится: пути проверяются на заглушках
подпроцессов; настоящая передача - живой тест под OMNISTASH_LIVE.
"""

import contextlib
import io
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app import wormhole


class _FakeStdin:
    def __init__(self):
        self.written: list = []
        self.closed = False

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    """Подпроцесс-заглушка: stdout - готовые строки, код возврата."""

    def __init__(self, lines=(), code=0):
        self.stdout = io.BytesIO(b"".join(line + b"\n" for line in lines))
        self.stdin = _FakeStdin()
        self.returncode = code

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class WormholeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(wormhole, "venv_dir",
                                    lambda: self.root / "venv")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.notes: list = []

    def fake_installed(self):
        python = self.root / "venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"")


class TestCodeRegex(unittest.TestCase):
    def test_short_and_long_codes(self):
        """Новая версия выдаёт короткие коды (3-replica-waffle)."""
        for text, expected in (
                ("Wormhole code is: 3-replica-waffle", "3-replica-waffle"),
                ("code: 7-correct-horse-staple", "7-correct-horse-staple"),
                ("код 12-alpha-beta-gamma-delta готов", "12-alpha-beta-gamma-delta")):
            match = wormhole.CODE_RE.search(text)
            self.assertIsNotNone(match, text)
            self.assertEqual(match.group(0), expected)
        self.assertIsNone(wormhole.CODE_RE.search("без кода здесь"))
        self.assertIsNone(wormhole.CODE_RE.search("2026-10-13 дата"))


class TestSetup(WormholeCase):
    def test_status_not_installed(self):
        status = wormhole.status()
        self.assertFalse(status["installed"])
        self.assertEqual(status["venv"], str(self.root / "venv"))

    def test_setup_skips_when_installed(self):
        self.fake_installed()
        with mock.patch.object(wormhole.subprocess, "run") as run:
            self.assertTrue(wormhole.setup(log=self.notes.append))
        run.assert_not_called()
        self.assertTrue(any("уже установлен" in line for line in self.notes))

    def test_setup_failure_is_honest(self):
        error = subprocess.CalledProcessError(
            1, ["pip"], stderr=b"no network here")
        with mock.patch.object(wormhole.subprocess, "run",
                               side_effect=error):
            ok = wormhole.setup(log=self.notes.append)
        self.assertFalse(ok)
        joined = " ".join(self.notes)
        self.assertIn("не удалась", joined)
        self.assertIn("no network here", joined)


class TestSendReceive(WormholeCase):
    def test_send_missing_file_and_missing_venv(self):
        self.assertEqual(wormhole.send(self.root / "нет.bin",
                                       log=self.notes.append), 2)
        self.assertTrue(any("файл не найден" in line for line in self.notes))

        self.notes.clear()
        self.assertEqual(wormhole.send(__file__, log=self.notes.append), 2)
        self.assertTrue(any("app.wormhole setup" in line
                            for line in self.notes))

    def test_send_extracts_code_and_returns_ok(self):
        self.fake_installed()
        source = self.root / "отчёт.txt"
        source.write_text("данные", encoding="utf-8")
        codes = []
        proc = FakeProc([b"Sending (1 file)...",
                         b"Wormhole code is: 7-correct-horse-staple",
                         b"File sent.. good-byE"])
        with mock.patch.object(wormhole, "_spawn",
                               return_value=proc) as spawn:
            code = wormhole.send(source, log=self.notes.append,
                                 on_code=codes.append)
        self.assertEqual(code, 0)
        self.assertEqual(codes, ["7-correct-horse-staple"])
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args[0][0], ["send", str(source)])

    def test_receive_confirms_and_uses_dest(self):
        self.fake_installed()
        dest = self.root / "принято"
        proc = FakeProc([b"Receiving wormhole code....",
                         b"Got wormhole code, diluting time..."])
        with mock.patch.object(wormhole, "_spawn",
                               return_value=proc) as spawn:
            code = wormhole.receive("7-correct-horse-staple", dest,
                                    log=self.notes.append)
        self.assertEqual(code, 0)
        self.assertIn(b"y\n", proc.stdin.written,
                      "подтверждение приёма должно уйти сразу")
        self.assertTrue(dest.is_dir(), "папка назначения создаётся")
        args, kwargs = spawn.call_args
        self.assertEqual(args[0], ["receive", "7-correct-horse-staple"])
        self.assertEqual(kwargs.get("cwd"), str(dest))

    def test_receive_validates_code(self):
        self.assertEqual(wormhole.receive("  ", self.root,
                                          log=self.notes.append), 2)
        self.assertTrue(any("код не указан" in line for line in self.notes))

    def test_pump_kills_on_hang(self):
        class Hanging(FakeProc):
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="wormhole", timeout=1)

        proc = Hanging([b"started..."])
        with mock.patch.object(wormhole, "RUN_TIMEOUT", 0.0):
            code = wormhole._pump(proc, self.notes.append)
        self.assertEqual(code, 1)
        self.assertEqual(proc.returncode, -9)
        self.assertTrue(any("зависла" in line for line in self.notes))


class TestCli(WormholeCase):
    def test_setup_command_when_installed(self):
        self.fake_installed()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wormhole.main(["setup"]), 0)
        self.assertIn("уже установлен", out.getvalue())

    def test_send_command_without_venv_hints_setup(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wormhole.main(["send", __file__]), 2)
        self.assertIn("app.wormhole setup", out.getvalue())

    def test_receive_command_without_code(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(wormhole.main(["receive", "  "]), 2)


@unittest.skipUnless(os.environ.get("OMNISTASH_LIVE") and wormhole.installed(),
                     "живой wormhole: OMNISTASH_LIVE и установленный venv")
class TestLiveTransfer(unittest.TestCase):
    """Без патча venv_dir: живой тесту нужен НАСТОЯЩИЙ venv профиля."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_send_receive_between_processes(self):
        """Живая передача между двумя подпроцессами (нужен интернет)."""
        source = self.root / "живой.txt"
        source.write_text("живой файл", encoding="utf-8")
        code_box = {}

        def on_code(code):
            code_box["code"] = code

        result = {}

        def sender():
            result["send"] = wormhole.send(source, log=lambda m: None,
                                           on_code=on_code)

        thread = threading.Thread(target=sender, daemon=True)
        thread.start()
        deadline = time.time() + 60
        while "code" not in code_box and time.time() < deadline:
            time.sleep(0.2)
        self.assertIn("code", code_box, "код должен появиться")
        dest = self.root / "забрали"
        result["recv"] = wormhole.receive(code_box["code"], dest,
                                          log=lambda m: None)
        thread.join(timeout=60)
        self.assertEqual(result.get("send"), 0)
        self.assertEqual(result.get("recv"), 0)
        files = list(dest.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(encoding="utf-8"), "живой файл")


if __name__ == "__main__":
    unittest.main()
