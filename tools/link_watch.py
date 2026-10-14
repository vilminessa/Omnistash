"""Супервизор link: поднимает демон/будильник, если они упали.

Живые инциденты показали: фоновые процессы умирают (смена суток,
перезапуск хоста, убийство) и никто их не поднимает - канал молчит, а
агенты «тыкают» юзера. watch проверяет при каждом запуске: демон
отвечает на status? процесс wait жив? - недостающее поднимает
отсоединёнными процессами (переживают выход watch) и пишет строку в
link_data/watch.log.

    python tools/link_watch.py once --id vilmpc --token T --peer IP:18571
    python tools/link_watch.py loop  --id vilmpc --token T --peer IP:18571

Задача Windows (раз в минуту, без админа):
    schtasks /Create /TN OmnistashLinkWatch /SC MINUTE /MO 1 /F /TR \\
      "\"C:\\Python314\\python.exe\" \"...\\tools\\link_watch.py\" once ..."

Токен в определении задачи хранится открытым текстом (та же модель
доверия, что аргументы CLI и файл профиля); задача живёт в сессии
пользователя.
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = ROOT / "tools" / "link.py"
WATCH_LOG = ROOT / "link_data" / "watch.log"

# Windows: отсоединённые флаги - процесс переживает выход watch
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
_FLAGS = (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW) \
    if os.name == "nt" else 0
_HIDE = CREATE_NO_WINDOW if os.name == "nt" else 0   # для проверок


def daemon_alive(port: int) -> bool:
    """Демон отвечает на status? (код 2 = контрольный порт молчит)."""
    try:
        proc = subprocess.run(
            [sys.executable, str(LINK), "status", "--port", str(port)],
            capture_output=True, timeout=20, creationflags=_HIDE)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def wait_running() -> bool:
    """Жив ли процесс link.py wait? (у него нет порта - только процесс)."""
    if os.name != "nt":
        return False                            # POSIX - не проверяем
    command = ('(Get-CimInstance Win32_Process -Filter '
               '"Name=\'python.exe\' OR Name=\'pythonw.exe\'" | '
               'Where-Object { '
               '$_.CommandLine -match "link.py wait" }).Count')
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, timeout=30, creationflags=_HIDE)
        return int(out.stdout.strip() or 0) > 0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return True                             # не смогли - не мешаем


def _spawn_logged(args: list, log_path: Path) -> int:
    """Отсоединённый процесс с дописью в watch.log; возвращает pid."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")
    try:
        handle.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] "
                     f"старт: {' '.join(args[-6:])}\n")
        proc = subprocess.Popen(
            args, cwd=str(ROOT), stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT, creationflags=_FLAGS)
    finally:
        handle.close()                         # у child свой дубликат
    return proc.pid


def ensure(id: str, token: str, peer: str, port: int) -> dict:
    """Один проход: поднять недостающее; вернуть {started: [...]}."""
    started = []
    if not daemon_alive(port):
        pid = _spawn_logged(
            [sys.executable, str(LINK), "serve", "--id", id,
             "--token", token, "--peer", peer, "--port", str(port)],
            WATCH_LOG)
        started.append(f"serve(pid {pid})")
    if not wait_running():
        pid = _spawn_logged(
            [sys.executable, str(LINK), "wait", "--port", str(port)],
            WATCH_LOG)
        started.append(f"wait(pid {pid})")
    return {"started": started}


def _say(message: str) -> None:
    """Печать без падения в pythonw (sys.stdout = None без консоли)."""
    if sys.stdout is not None:
        print(message, flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="link_watch",
        description="Супервизор link: поднимает демон/будильник, если "
                    "упали (задача Windows раз в минуту).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("once", "loop"):
        cmd = sub.add_parser(name, help="один проход / цикл")
        cmd.add_argument("--id", required=True, help="id этого ПК")
        cmd.add_argument("--token", required=True, help="токен link-канала")
        cmd.add_argument("--peer", required=True, metavar="IP[:PORT]",
                         help="адрес hub/пары")
        cmd.add_argument("--port", type=int, default=18571)
    args = parser.parse_args(argv)

    if args.cmd == "once":
        result = ensure(args.id, args.token, args.peer, args.port)
        if result["started"]:
            _say(f"link_watch: поднято - {', '.join(result['started'])}")
        else:
            _say("link_watch: демон и будильник живы")
        return 0

    # loop: сам себе планировщик (если задача Windows недоступна)
    while True:
        result = ensure(args.id, args.token, args.peer, args.port)
        if result["started"]:
            _say(f"link_watch: поднято - {', '.join(result['started'])}")
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
