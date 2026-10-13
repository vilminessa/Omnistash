"""Разовая передача файлов по короткому коду (B1, magic-wormhole).

Зачем при работающем mesh: wormhole - передача «от вас к собеседнику»
по коду вида 7-кот-слово-дерево (PAKE -> E2E-шифрование), без адресов,
портов и брандмауэра. Удобно, когда второй ПК только подключают или
mesh ещё не настроен; для постоянного обмена есть mesh и каталоги (B3).

Библиотека НЕ входит в зависимости и НЕ вшивается в exe (24
транзитивных пакета, Twisted/PyNaCl): она живёт в отдельном venv в
профиле, ставится только по явной команде (setup) и запускается
подпроцессом - основное окружение и сборка не трогаются. Лицензия
magic-wormhole MIT.

Консоль (для агентов и людей):

    python -m app.wormhole setup                  # один раз
    python -m app.wormhole send путь/к/файлу      # печатает код
    python -m app.wormhole receive 7-кот-слово-дерево --dest папка
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from .paths import profile_dir

CODE_RE = re.compile(r"\b\d{1,2}(?:-[a-z]+){2,4}\b")   # 3-replica-waffle и длиннее
SETUP_TIMEOUT = 600.0        # установка качает пакеты - до 10 минут
RUN_TIMEOUT = 3600.0         # передача - час, дальше считаем зависшей


def _console_safe(text: str) -> str:
    """Строка для журнала/консоли: непечатаемое заменяется кодировкой
    потока (иначе cp1251-консоль падает на символах замены из вывода
    подпроцесса)."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return (text.encode(encoding, "replace")
                .decode(encoding, "replace"))


def venv_dir() -> Path:
    """Отдельное окружение под wormhole - в профиле, как bin/ffmpeg."""
    return profile_dir() / "wormhole-venv"


def _venv_python() -> Path:
    if os.name == "nt":
        return venv_dir() / "Scripts" / "python.exe"
    return venv_dir() / "bin" / "python"


def installed() -> bool:
    return _venv_python().is_file()


def status() -> dict:
    return {"installed": installed(), "venv": str(venv_dir()),
            "python": str(_venv_python())}


def setup(log=None, timeout: float = SETUP_TIMEOUT) -> bool:
    """Создать venv и поставить magic-wormhole; False - отказ (логирует)."""
    log = log or (lambda message: None)
    if installed():
        log("wormhole: уже установлен")
        return True
    venv = venv_dir()
    log(f"wormhole: ставлю в {venv} (первый раз качает пакеты, минуты)…")
    steps = ([sys.executable, "-m", "venv", str(venv)],
             [str(_venv_python()), "-m", "pip", "install", "--quiet",
              "magic-wormhole"])
    for command in steps:
        try:
            subprocess.run(command, timeout=timeout, check=True,
                           capture_output=True)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace")[-300:]
            log(f"wormhole: установка не удалась (шаг «{' '.join(command[:3])}"
                f"…»){': ' + detail if detail else ''}")
            return False
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"wormhole: установка не удалась: {exc}")
            return False
    if not installed():
        log("wormhole: установка прошла, но запускальщик не найден в venv")
        return False
    log("wormhole: установлен")
    return True


def _spawn(args: list, cwd: str | None = None):
    """Подпроцесс wormhole через Python venv (запускальщик не падает)."""
    return subprocess.Popen(
        [str(_venv_python()), "-m", "wormhole", *args],
        cwd=cwd, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _pump(proc, log, on_code=None) -> int:
    """Читать вывод до конца (код - колбэку), ждать завершения."""
    code_shown = False
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            log(f"wormhole: {_console_safe(line)}")
            match = CODE_RE.search(line)
            if match and not code_shown:
                code_shown = True
                if on_code:
                    on_code(match.group(0))
        proc.wait(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        log("wormhole: передача зависла - процесс убит")
        return 1
    except OSError as exc:
        log(f"wormhole: вывод оборвался: {exc}")
        return 1
    finally:
        for stream in (getattr(proc, "stdin", None),
                       getattr(proc, "stdout", None)):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    return int(proc.returncode or 0)


def send(path, log=None, on_code=None) -> int:
    """Отправить файл; код передачи достаётся из вывода (on_code)."""
    log = log or (lambda message: None)
    source = Path(path)
    if not source.is_file():
        log(f"wormhole: файл не найден: {source}")
        return 2
    if not installed():
        log("wormhole: сначала установите: python -m app.wormhole setup")
        return 2
    log(f"wormhole: отправляю «{source.name}» "
        f"({source.stat().st_size} байт) - код для собеседника ниже")
    proc = _spawn(["send", str(source)])
    return _pump(proc, log, on_code=on_code)


def receive(code: str, dest_dir, log=None) -> int:
    """Принять файл по коду в папку; подтверждение приёма уходит сразу."""
    log = log or (lambda message: None)
    wormhole_code = str(code or "").strip()
    if not wormhole_code:
        log("wormhole: код не указан")
        return 2
    if not installed():
        log("wormhole: сначала установите: python -m app.wormhole setup")
        return 2
    dest = Path(dest_dir)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"wormhole: папка назначения недоступна: {exc}")
        return 2
    log(f"wormhole: принимаю по коду в {dest}")
    proc = _spawn(["receive", wormhole_code], cwd=str(dest))
    try:
        if proc.stdin is not None:
            proc.stdin.write(b"y\n")   # подтверждение имени/размера
            proc.stdin.flush()
    except OSError:
        pass                            # процесс мог умереть раньше
    return _pump(proc, log)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="wormhole",
        description="Разовая передача файла по короткому коду "
                    "(magic-wormhole в отдельном venv).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="установить magic-wormhole (один раз)")
    send_cmd = sub.add_parser("send", help="отправить файл и напечатать код")
    send_cmd.add_argument("path", help="путь к файлу")
    recv_cmd = sub.add_parser("receive", help="принять файл по коду")
    recv_cmd.add_argument("code", help="код вида 7-кот-слово-дерево")
    recv_cmd.add_argument("--dest", default=".",
                          help="папка назначения (по умолчанию текущая)")
    args = parser.parse_args(argv)

    if args.cmd == "setup":
        return 0 if setup(log=print) else 1
    if args.cmd == "send":
        return send(args.path, log=print,
                    on_code=lambda code: print(
                        f"КОД ДЛЯ СОБЕСЕДНИКА: {code}", flush=True))
    return receive(args.code, args.dest, log=print)


if __name__ == "__main__":
    sys.exit(main())
