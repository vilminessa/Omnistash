"""Журнал аудита HTTP-сервера: ключ -> метод -> код -> адрес.

Зачем отдельный файл от omnistash.log: обращения клиента - это «кто и что
спросил», а не ход программы; человек смотрит журнал, чтобы понять, какой
ключ что делал (docs/SERVER-CLIENT.md §7).

Что НЕ пишется: значения токенов, пути дисков и тела ответов. Вместо токена
- его метка («read+watch»), вместо файла - id строки. Хвост обрезается при
старте сервера - тот же приём, что `Api.rotate_log_file` для omnistash.log.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from ..paths import serve_audit_path

MAX_AUDIT_BYTES = 400_000      # больше - обрезаем хвост
KEEP_AUDIT_BYTES = 200_000


class Audit:
    """Одна строка на обращение; ошибки записи не поднимаем (журнал не валит сервер)."""

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else serve_audit_path()
        self._lock = threading.Lock()

    def rotate(self) -> None:
        """Обрезать хвост, если журнал разросся (делается при старте сервера)."""
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size <= MAX_AUDIT_BYTES:
            return
        try:
            with open(self._path, "rb") as handle:
                handle.seek(-KEEP_AUDIT_BYTES, os.SEEK_END)
                tail = handle.read()
            self._path.write_bytes(
                b"--- rotated (audit log too big) ---\n" + tail)
        except OSError:
            pass

    def record(self, method: str, code: int, addr: str,
               key_label: str = "", scope: str = "", note: str = "") -> None:
        parts = [f"ключ {key_label or 'не привязан'}",
                 f"метод {method or '-'}", f"код {code}"]
        if scope:
            parts.append(f"право {scope}")
        if note:
            parts.append(note)
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] " + \
               " ".join(parts) + f" | клиент {addr}"
        try:
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            pass

    def tail(self, limit: int = 40) -> list[str]:
        """Хвост журнала для карточки diagnostics (без токенов и путей)."""
        try:
            with open(self._path, "rb") as handle:
                data = handle.read()
        except OSError:
            return []
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-limit:]
