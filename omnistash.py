"""Omnistash - загрузка, синхронизация и организация архивного контента.

Точка входа: поднимает профиль (%LOCALAPPDATA%\\Omnistash), открывает базу
библиотеки и показывает главное окно (pywebview).

Запуск из исходников:
    python omnistash.py            # окно
    python omnistash.py --scan     # переиндексация всех корней без окна
"""

from __future__ import annotations

import sys


def main() -> int:
    if "--scan" in sys.argv:
        # Головной запуск для watchdog/планировщика: индекс без интерфейса.
        from app.indexer import headless_scan
        return headless_scan()

    from app.gui import run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
