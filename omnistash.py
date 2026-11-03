"""Omnistash - загрузка, синхронизация и организация YouTube-архивов.

Точка входа: поднимает профиль (%LOCALAPPDATA%\\Omnistash), открывает базу
библиотеки и показывает главное окно (pywebview).

Запуск из исходников:
    python omnistash.py            # окно
    python omnistash.py --scan     # переиндексация без окна (для watchdog)
    python omnistash.py --sync     # синхронизация + очередь, без окна
                                  # (для планировщика Windows)
    python omnistash.py --serve    # окно + HTTPS-слушатель клиента: слушатель
                                  # поднимается только при включённом
                                  # serve_enabled (доступ по HTTPS - согласие)
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

    if "--serve" in sys.argv:
        # SV-C S1: тот же процесс, что окно, только добавляется слушатель
        # клиента. База, хранилища и очередь остаются здесь - второго
        # владельца не появляется; флаг serve_enabled решает, поднимать ли его.
        from app.gui import run
        return run(serve=True)

    from app.gui import run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
