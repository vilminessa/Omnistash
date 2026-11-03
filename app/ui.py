"""Сборка страницы окна: ui_src/{index.html,app.css,app.js} -> одна строка.

Окно создаётся через `webview.create_window(html=...)`: страница отдаётся
строкой, поэтому внешних загрузок не бывает - CSS и JS вшиваются прямо в
разметку (тот же приём, что в Synfronia, только без генерации ui.py:
для MVP исходники читаются с диска при старте).

`marker` - метка сервера (`window.OMNISTASH_HTTP`), которую ставит только
`--serve` (docs/SERVER-CLIENT.md §3): без неё страница остаётся окном с
мостом pywebview, а превью в браузере - выдуманными данными. Метка не
содержит токена: ключ приходит привязкой (S6) и живёт в localStorage клиента.
"""

from __future__ import annotations

import json
import re

from .paths import ui_dir


def _read(name: str) -> str:
    path = ui_dir() / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Не найден файл интерфейса {path}. В заморозке exe нужно "
            f"собирать с данными ui_src/ (--add-data \"ui_src;ui_src\")."
        ) from exc


def _with_marker(page: str, marker: dict) -> str:
    """Вставить метку сервера сразу после `<body>` (чистая строка)."""
    script = ("<script>window.OMNISTASH_HTTP = "
              + json.dumps(marker, ensure_ascii=False) + ";</script>")
    body = re.search(r"<body[^>]*>", page)
    if body is None:
        return page + script
    start = body.end()
    return page[:start] + "\n" + script + page[start:]


def build_page(marker: dict | None = None) -> str:
    """Полная HTML-страница главного окна (или серверная страница с меткой)."""
    page = _read("index.html")
    page = (page.replace("__CSS__", _read("app.css"))
                .replace("__JS__", _read("app.js")))
    if marker is not None:
        page = _with_marker(page, marker)
    return page
