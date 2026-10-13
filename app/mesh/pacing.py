"""Темп переноса: consume(n) спит, пока разрешено отправить n байт.

Общий для клиента (приём) и сервера (отдача) - пользователь просил
лимит «отправки/получения», каждая сторона сдерживает СВОЮ часть;
итоговая скорость на потоке - медленнее из двух, они стыкуются через
TCP-бэкпрессур, а не складываются. Расписание абсолютное
(start + объём/скорость): паузы не дрейфуют от порции к порции.
"""

from __future__ import annotations

import time

MBIT = 1_000_000              # бит в мегабите (десятичный, как каналы)
MIB = 1024 * 1024             # байт в мебибайте (сколько качаем)


def mb_to_bytes(megabytes) -> int:
    """МиБ -> байты; 0 (и меньше) = без лимита."""
    value = int(megabytes or 0)
    return value * MIB if value > 0 else 0


def mbit_to_bps(megabits) -> int:
    """Мбит/с -> байты/с; 0 (и меньше) = без ограничения."""
    value = int(megabits or 0)
    return value * MBIT // 8 if value > 0 else 0


class RateLimiter:
    """Ограничитель пропускной способности (байты/с). rate<=0 - свободно."""

    def __init__(self, rate_bps):
        self.rate = float(rate_bps or 0)
        self._start: float | None = None
        self._passed = 0

    def consume(self, nbytes: int) -> None:
        """Учесть nbytes отправленных/принятых байт; спит при необходимости."""
        if self.rate <= 0 or nbytes <= 0:
            return
        now = time.monotonic()
        if self._start is None:
            self._start = now
        self._passed += int(nbytes)
        due = self._start + self._passed / self.rate
        if due > now:
            time.sleep(due - now)
