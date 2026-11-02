r"""Иконка Omnistash: .ico собирается из палитры окна (app/themes.py).

    python tools/make_icon.py            # пишет assets/omnistash.ico
    python tools/make_icon.py --stdout   # байты в stdout (для тестов и diff)

Рисунка своя (не чужой набор иконок): закруглённый квадрат с фоном тёмной
темы и три ряда-строки библиотеки в нижней половине, над ними стрелка вниз -
«складывается в хранилище». Цвета берутся из палитры тёмной темы, поэтому
иконка совпадает с окном программы.

Формат: классический ICO с BMP-картками (32 бита, BGRA + AND-маска). PNG-картки
не используются - BMP читается всеми Windows-оболочками и PyInstaller без
зависимостей. Размер: 16, 32, 48, 64, 128, 256 пикселов - от списка файлов до
large-иконок проводника.

Рисунка детерминирована (без случайностей и без дат): повторный запуск даёт
байт-в-байт тот же файл, поэтому тест сверяет asset с генерацией, а не «иконка
какая-то есть».
"""

from __future__ import annotations

import hashlib
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSET = ROOT / "assets" / "omnistash.ico"
SIZES = (16, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4          # 4x4 подпиксела на пиксел - сглаживание краёв

# Палитра тёмной темы (app/themes.py: themes()["dark"]).
BG = (12, 22, 34, 255)        # --bg
# Тон прежнего --line (#22344a) до пересчёта палитры шагом 1 DESIGN-UI:
# иконка держит его намеренно - ассет сверяет тест с генератором бит-в-бит,
# перекраска ассета под новый --line (#45617f) меняла бы exe без нужды.
LINE = (34, 52, 74, 255)
# Ряды стека светлее поля: на 16 пикселах три строки библиотеки должны
# читаться, поэтому tone поднят относительно --widget (27,47,71).
ROW = (46, 70, 96, 255)
ROW_TOP_TONE = (58, 84, 112, 255)
ACCENT = (98, 141, 124, 255)  # --accent
EMPTY = (0, 0, 0, 0)          # вне иконки - прозрачно

# Геометрия в нормированных координатах канвы 0..1.
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


def _pixel(x: float, y: float) -> tuple[int, int, int, int]:
    """Цвет точки канвы (0..1): стрелка -> ряды -> рамка -> фон -> прозрачно."""
    left, top, right, bottom = INSET, INSET, 1.0 - INSET, 1.0 - INSET
    if not _inside_round_rect(x, y, left, top, right, bottom, RADIUS):
        return EMPTY
    if _inside_arrow(x, y):
        return ACCENT
    for index in range(3):
        row_top = ROW_TOP + index * (ROW_H + ROW_GAP)
        if _inside_bar(x, y, ROW_LEFT, row_top, ROW_RIGHT, row_top + ROW_H):
            # верхний ряд светлее - стек читается как три строки библиотеки
            return ROW_TOP_TONE if index == 0 else ROW
    # тонкая рамка по краю квадрата - иконка читается и на светлом фоне
    inner = _inside_round_rect(x, y, left + 0.02, top + 0.02,
                               right - 0.02, bottom - 0.02, RADIUS * 0.92)
    return LINE if not inner else BG


def render(size: int) -> list[tuple[int, int, int, int]]:
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
                    colour = _pixel(x, y)
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


def build(sizes: tuple[int, ...] = SIZES) -> bytes:
    """Собрать .ico: DirectoryEntry[] + данные картинок."""
    cards = [(_bmp_card(size, render(size)), size) for size in sizes]
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
    data = build()
    if "--stdout" in argv[1:]:
        sys.stdout.buffer.write(data)
        return 0
    ASSET.parent.mkdir(exist_ok=True)
    previous = ASSET.read_bytes() if ASSET.exists() else None
    ASSET.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()[:12]
    changed = "изменена" if previous is not None and previous != data else \
              ("создана" if previous is None else "не изменилась")
    print(f"assets/omnistash.ico: {len(data)} байт, sizes {sizes_text()}, "
          f"sha256 {digest}, {changed}")
    return 0


def sizes_text() -> str:
    return ", ".join(f"{size}x{size}" for size in SIZES)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
