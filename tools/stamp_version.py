r"""Вписывает версию из тега в рабочее дерево - коммиты с бампом не нужны.

    python tools/stamp_version.py v0.1.5
    python tools/stamp_version.py b0.1.4

Формат тега: [vb]X.Y.Z (максимум четыре компонента - Windows VersionInfo
не принимает больше). Префикс отбрасывается: из b0.1.4 получается 0.1.4.

Пишет __version__ в app/__init__.py (version.py импортирует оттуда же):
байтовой заменой внутри строки - переводы строк и кодировки не
трогаются (ни одного лишнего диффа, если запустить локально).

Кто зовёт: beta.yml/release.yml в джобе build ПЕРЕД PyInstaller - exe
уезжает с теговой версией; в git ничего не попадает (рабочее дерево
CI одноразовое). Локально - перед ручной сборкой exe из тега.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG_RE = re.compile(r"^([vb])(\d+\.\d+\.\d+(?:\.\d+)?)$")


def stamp(tag: str) -> str:
    """Версия из тега; ValueError - формат не распознан."""
    match = TAG_RE.match((tag or "").strip())
    if not match:
        raise ValueError(
            f"тег {tag!r} не распознан: ожидается v0.1.5 или b0.1.4")
    return match.group(2)


def _replace(path: Path, pattern: bytes, replacement: bytes) -> bool:
    """Байтовая замена внутри файла: кодировки и переводы строк не трогаем."""
    raw = path.read_bytes()
    updated, count = re.subn(pattern, replacement, raw, count=1)
    if count:
        path.write_bytes(updated)
    return bool(count)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip())
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    try:
        version = stamp(argv[1])
    except ValueError as exc:
        print(f"ошибка: {exc}")
        return 1
    ok = _replace(ROOT / "app" / "__init__.py",
                  rb'__version__\s*=\s*"[^"]*"',
                  f'__version__ = "{version}"'.encode("ascii"))
    if not ok:
        print("ошибка: не нашёл __version__ в app/__init__.py")
        return 1
    print(f"app/__init__.py: __version__ = {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
