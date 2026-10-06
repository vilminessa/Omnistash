"""Сборка превью интерфейса: ui_src -> preview.html (открывается в браузере).

Зачем: окно Omnistash живёт в pywebview, и без запущенного Python оно не
покажет ничего. Превью - та же самая страница, но с вымышленными данными:
app.js замечает, что мост pywebview не подключился, и подставляет
мок-бэкенд (installPreview).

    python tools/build_preview.py            # собрать preview.html
    python tools/build_preview.py --open     # собрать и открыть в браузере
    python tools/build_preview.py --first    # то же, но «первый запуск»:
                                             # стартовый диалог выбора папки

Preview самодостаточен (CSS/JS вшиты в файл), поэтому открывается по
file:// без какого-либо сервера.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ui import build_page  # noqa: E402  (корень в sys.path уже добавлен)


def main() -> int:
    out = ROOT / "preview.html"
    out.write_text(build_page(), encoding="utf-8")
    url = out.as_uri()
    first_run = "--first" in sys.argv
    if first_run:
        url += "?empty=1"          # мок-бэкенд читает location.search

    print(f"собрано: {out} ({out.stat().st_size} байт)")
    if first_run:
        print("режим первого запуска (хранилищ нет): " + url)

    if "--open" in sys.argv:
        import webbrowser
        webbrowser.open(url)
        print("открыто в браузере: " + url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
