r"""Иконки Omnistash и Stashroom: .ico собираются из палитры темы «Бренд».

    python tools/make_icon.py            # пишет assets/omnistash.ico и assets/stashroom.ico
    python tools/make_icon.py --stdout   # байты omnistash.ico в stdout (для diff)

Рисунка своя (не чужой набор иконок): закруглённый квадрат с полем кремовой
подложки, три ряда-строки библиотеки в нижней половине; над ними стрелка
вниз - «складывается в хранилище» (Omnistash). Stashroom - тот же квадрат
рядов без стрелки: хранилище без закачки, рамка navy вместо blue. Цвета
берутся из реестра темы `coolors` (app/themes.py), поэтому иконка совпадает
с брендной палитрой окна - решение человека от 06.11.2026 (перекраска
одобрена, палитра прежде лежала в литералах тёмной темы).

Контраст значимой графики на cream (--bg): navy ряды и рамка Stashroom
12.25, red стрелка 5.51, blue рамка Omnistash ~3.6, top row muted ~4.3 -
все выше порога 3:1 (WCAG SC 1.4.11); числа сверяет app/themes.py и
tests/test_brand_theme.py, иконка не изобретает своих тонов.

Формат: классический ICO с BMP-картками (32 бита, BGRA + AND-маска). PNG-картки
не используются - BMP читается всеми Windows-оболочками и PyInstaller без
зависимостей. Размер: 16, 32, 48, 64, 128, 256 пикселов - от списка файлов до
large-иконок проводника.

Рисунка детерминирована (без случайностей и без дат): повторный запуск даёт
байт-в-байт тот же файл, поэтому тест сверяет ассеты с генерацией целиком,
а не «иконка какая-то есть».
"""

from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import themes  # noqa: E402 - путь добавлен выше

SIZES = (16, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4          # 4x4 подпиксела на пиксел - сглаживание краёв
BRAND_THEME = "coolors"  # тема «Бренд» из реестра app/themes.py
EMPTY = (0, 0, 0, 0)     # вне иконки - прозрачно

# Роль -> пиксел RGBA: тон берётся из реестра переменная-в-переменную,
# литералов палитры в генераторе нет - тот же приём, что токены Flutter
# (tools/build_flutter_theme.py).
def hex_rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def brand_vars() -> dict[str, str]:
    return themes.css_vars(BRAND_THEME)


def role(name: str) -> tuple[int, int, int, int]:
    return (*hex_rgb(brand_vars()[f"--{name}"]), 255)


# Глифы: имена ролей, а не hex - сверка с реестром ведётся тестом.
GLYPHS: dict[str, dict[str, object]] = {
    "omnistash": {
        "field": "bg",
        "rim": "line-strong",
        "rows": "text",
        "row_top": "muted",
        "arrow": "accent",
    },
    "stashroom": {
        "field": "bg",
        "rim": "text",
        "rows": "text",
        "row_top": "muted",
        "arrow": None,   # хранилище без закачки - стрелки нет, это названо
    },
}

ASSETS = {
    "omnistash": ROOT / "assets" / "omnistash.ico",
    "stashroom": ROOT / "assets" / "stashroom.ico",
}

# Геометрия в нормированных координатах канвы 0..1 - та же, что мерилась
# прежней рисункой (ряды и стрелка читались на 16 пикселах).
INSET = 0.055               # отступ квадрата от края канвы
RADIUS = 0.17               # скругление углов квадрата
ROW_TOP = 0.46              # верхний ряд стека
ROW_H = 0.11                # высота ряда
ROW_GAP = 0.055             # зазор между рядами
ROW_LEFT = 0.22
ROW_RIGHT = 0.78
ARROW_TOP = 0.12            # верх стрелки
ARROW_W = 0.30              # ширина «треугольника» стрелки
STEM_W = 0.10


def _inside_round_rect(x: float, y: float, left: float, top: float,
                       right: float, bottom: float, radius: float) -> bool:
    """Точка внутри закруглённого прямоугольника."""
    if x < left or x > right or y < top or y > bottom:
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def _inside_bar(x: float, y: float, left: float, top: float,
                right: float, bottom: float) -> bool:
    """Ряд-строка: прямоугольник со скруглёнными торцами (радиус = высоте/2)."""
    radius = (bottom - top) / 2.0
    return _inside_round_rect(x, y, left, top, right, bottom, radius)


def _inside_arrow(x: float, y: float) -> bool:
    """Стрелка вниз: ствол + треугольник остриём к верхнему ряду стека."""
    stem_bottom = ARROW_TOP + (ROW_TOP - ARROW_TOP) * 0.55
    if ARROW_TOP <= y <= stem_bottom:
        if abs(x - 0.5) <= STEM_W / 2.0:
            return True
    head_top = stem_bottom - 0.02
    head_bottom = ROW_TOP + 0.03
    if head_top <= y <= head_bottom:
        span = (ARROW_W / 2.0) * ((y - head_top) / max(head_bottom - head_top, 1e-6))
        return abs(x - 0.5) <= span
    return False


def _pixel(x: float, y: float, glyph: dict[str, object]) -> tuple[int, int, int, int]:
    """Цвет точки канвы (0..1): стрелка -> ряды -> рамка -> поле -> прозрачно."""
    left, top, right, bottom = INSET, INSET, 1.0 - INSET, 1.0 - INSET
    if not _inside_round_rect(x, y, left, top, right, bottom, RADIUS):
        return EMPTY
    arrow = glyph["arrow"]
    if arrow is not None and _inside_arrow(x, y):
        return role(str(arrow))
    for index in range(3):
        row_top = ROW_TOP + index * (ROW_H + ROW_GAP)
        if _inside_bar(x, y, ROW_LEFT, row_top, ROW_RIGHT, row_top + ROW_H):
            # верхний ряд светлее (muted) - стек читается как три строки
            return role(str(glyph["row_top"] if index == 0 else glyph["rows"]))
    # тонкая рамка по краю квадрата - иконка читается и на светлом фоне,
    # и на тёмном (рамка navy у Stashroom держит 12.25 на cream)
    inner = _inside_round_rect(x, y, left + 0.02, top + 0.02,
                               right - 0.02, bottom - 0.02, RADIUS * 0.92)
    return role(str(glyph["rim"])) if not inner else role(str(glyph["field"]))


def render(size: int, glyph: dict[str, object]) -> list[tuple[int, int, int, int]]:
    """RGBA-пикселы канвы size x size (сглаживание 4x4 подпикселами)."""
    total = size * SUPERSAMPLE          # подпикселов на сторону канвы
    pixels: list[tuple[int, int, int, int]] = []
    for row in range(size):
        for column in range(size):
            red = green = blue = alpha = 0
            covered = 0          # подпикселов с непрозрачным цветом
            for sy in range(SUPERSAMPLE):
                y = (row * SUPERSAMPLE + sy + 0.5) / total
                for sx in range(SUPERSAMPLE):
                    x = (column * SUPERSAMPLE + sx + 0.5) / total
                    colour = _pixel(x, y, glyph)
                    if colour[3] == 0:
                        continue
                    covered += 1
                    red += colour[0]
                    green += colour[1]
                    blue += colour[2]
                    alpha += colour[3]
            if covered:
                pixels.append((red // covered, green // covered,
                               blue // covered, alpha // covered))
            else:
                pixels.append(EMPTY)
    return pixels


def _bmp_card(size: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    """BMP-картка ICO: заголовок + BGRA (строки снизу вверх) + AND-маска."""
    mask_row = ((size + 31) // 32) * 4
    xor_bytes = bytearray()
    for row in range(size - 1, -1, -1):          # bottom-up
        for index in range(row * size, row * size + size):
            red, green, blue, alpha = pixels[index]
            xor_bytes += struct.pack("<BBBB", blue, green, red, alpha)
    mask = bytearray()
    for row in range(size):                      # маска идёт сверху вниз
        bits = 0
        emitted = 0
        for column in range(size):
            # 0 = непрозрачный пиксел, 1 = прозрачный
            if pixels[row * size + column][3] == 0:
                bits |= 1 << (7 - (column % 8))
            if (column + 1) % 8 == 0:
                mask.append(bits)
                bits = 0
                emitted += 1
        if size % 8:
            mask.append(bits << (8 - (size % 8)))
            emitted += 1
        mask += b"\x00" * (mask_row - emitted)   # строка маски выровнена по 4 байта
    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
        len(xor_bytes) + len(mask), 0, 0, 0, 0)
    return header + xor_bytes + bytes(mask)


def build(name: str, sizes: tuple[int, ...] = SIZES) -> bytes:
    """Собрать .ico для глифа: DirectoryEntry[] + данные картинок."""
    glyph = GLYPHS[name]
    cards = [(_bmp_card(size, render(size, glyph)), size) for size in sizes]
    entries = bytearray()
    offset = 6 + 16 * len(cards)
    for card, size in cards:
        width = size if size < 256 else 0        # 256 пишется как 0
        height = size if size < 256 else 0
        entries += struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32,
                               len(card), offset)
        offset += len(card)
    out = bytearray(struct.pack("<HHH", 0, 1, len(cards)))
    out += entries
    for card, _size in cards:
        out += card
    return bytes(out)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    if "--stdout" in argv[1:]:
        sys.stdout.buffer.write(build("omnistash"))
        return 0
    lines = []
    for name, path in ASSETS.items():
        data = build(name)
        if "--check" in argv[1:]:
            if path.is_file() and path.read_bytes() == data:
                lines.append(f"{path.name}: совпадает с генерацией")
            else:
                print(f"{path.name}: не совпадает с генерацией tools/make_icon.py "
                      "- python tools/make_icon.py", file=sys.stderr)
                return 1
            continue
        path.parent.mkdir(exist_ok=True)
        previous = path.read_bytes() if path.exists() else None
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()[:12]
        changed = ("изменена" if previous is not None and previous != data
                   else "создана" if previous is None else "не изменилась")
        lines.append(f"{path.name}: {len(data)} байт, sizes {sizes_text()}, "
                     f"sha256 {digest}, {changed}")
    print("; ".join(lines))
    return 0


def sizes_text() -> str:
    return ", ".join(f"{size}x{size}" for size in SIZES)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
