"""Расписание: периодические задачи внутри открытого окна.

Два разных механизма, не путать:
  * этот таймер - живёт, пока открыто окно: «каждые 30 минут, пока я за
    компьютером»; простой, без прав системы и без файлов;
  * `python omnistash.py --sync` под Register-ScheduledTask - для работы
    без окна (см. README).

Правила:
  * интервал 0 = выключено (и это значение по умолчанию: ничего не должно
    происходить само без явного решения пользователя);
  * смена интервала переносит срок, а не ждёт старого;
  * если задача занята (окно уже что-то делает) - повтор через минуту,
    а не каждый тик и не с очередью в журнал.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

POLL_SECONDS = 1.0
BUSY_RETRY_SECONDS = 60.0
MAX_MINUTES = 1440
KEYS = ("scan", "sync")


class Scheduler:
    """Вызывает колбэки по интервалам. Интервал 0 - задача выключена."""

    def __init__(self, on_scan, on_sync, log=None):
        self._callbacks = {"scan": on_scan, "sync": on_sync}
        self._log = log or (lambda message: None)
        self._interval = {key: 0 for key in KEYS}
        self._next = {key: 0.0 for key in KEYS}
        self._last = {key: None for key in KEYS}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- интервалы --------------------------------------------------------

    def set_interval(self, key: str, minutes) -> int:
        """Задать интервал (минуты); смена значения переносит срок."""
        try:
            value = int(minutes or 0)
        except (TypeError, ValueError):
            value = 0
        value = min(max(value, 0), MAX_MINUTES)
        with self._lock:
            if value == self._interval[key]:
                return value
            self._interval[key] = value
            self._next[key] = time.time() + value * 60 if value else 0.0
        return value

    def apply_settings(self, config: dict) -> None:
        """Применить интервалы из настроек (0 = выключено)."""
        self.set_interval("scan", config.get("scan_interval_min"))
        self.set_interval("sync", config.get("sync_interval_min"))

    # -- цикл -------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="omnistash-schedule")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - таймер не должен умирать
                self._log(f"Расписание: ошибка шага - {exc}")

    def tick(self, now: float | None = None) -> list[str]:
        """Проверить сроки. Возвращает имена реально запущенных задач."""
        moment = time.time() if now is None else now
        fired: list[str] = []
        for key in KEYS:
            with self._lock:
                interval = self._interval[key]
                due = self._next[key]
            if not interval or not due or moment < due:
                continue
            try:
                ok = bool(self._callbacks[key]())
            except Exception as exc:  # noqa: BLE001 - колбэк не должен ронять таймер
                self._log(f"Расписание {key}: {exc}")
                ok = False
            with self._lock:
                if ok:
                    self._last[key] = datetime.now().replace(
                        microsecond=0).isoformat()
                    self._next[key] = time.time() + interval * 60
                else:
                    # Занято или нечего делать: пробуем через минуту.
                    self._next[key] = time.time() + BUSY_RETRY_SECONDS
            if ok:
                fired.append(key)
                self._log(f"Расписание: запущена задача «{key}»")
        return fired

    # -- состояние для окна ----------------------------------------------

    def state(self) -> dict:
        """Что показать в интерфейсе: интервал, когда следующий, когда был."""
        now = time.time()
        out = {}
        with self._lock:
            for key in KEYS:
                interval = self._interval[key]
                next_at = self._next[key]
                out[key] = {
                    "interval": interval,
                    "next_in": int(round(next_at - now))
                    if interval and next_at else None,
                    "last": self._last[key],
                }
        return out


def describe(entry: dict, name: str) -> str:
    """Строка состояния задачи для интерфейса/журнала."""
    if not entry or not entry.get("interval"):
        return f"{name}: выключен"
    text = f"{name}: каждые {entry['interval']} мин"
    if entry.get("next_in") is not None:
        text += f" · следующий через {entry['next_in']} с"
    if entry.get("last"):
        text += f" · последний {str(entry['last'])[11:16]}"
    return text
