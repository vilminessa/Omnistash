"""Omnistash - загрузка, синхронизация и организация YouTube-архивов.

Точка входа: поднимает профиль (%LOCALAPPDATA%\\Omnistash), открывает базу
библиотеки и показывает главное окно (pywebview).

Запуск из исходников:
    python omnistash.py            # окно
    python omnistash.py --scan     # переиндексация без окна (для watchdog)
    python omnistash.py --sync     # синхронизация + очередь, без окна
                                  # (для планировщика Windows)
"""

from __future__ import annotations

import sys


def main() -> int:
    if "--scan" in sys.argv:
        # Головной запуск для watchdog/планировщика: индекс без интерфейса.
        from app.indexer import headless_scan
        return headless_scan()

    if "--sync" in sys.argv:
        # Тот же сценарий, но для источников: синк и доедание очереди.
        from app.gui import headless_sync
        return headless_sync()

    from app.gui import run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
