"""Сборка страницы окна: ui_src/{index.html,app.css,app.js} -> одна строка.

Окно создаётся через `webview.create_window(html=...)`: страница отдаётся
строкой, поэтому внешних загрузок не бывает - CSS и JS вшиваются прямо в
разметку (тот же приём, что в Synfronia, только без генерации ui.py:
для MVP исходники читаются с диска при старте).
"""

from __future__ import annotations

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


def build_page() -> str:
    """Полная HTML-страница главного окна."""
    page = _read("index.html")
    return (page.replace("__CSS__", _read("app.css"))
                .replace("__JS__", _read("app.js")))
