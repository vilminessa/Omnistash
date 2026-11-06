"""Иконки exe: assets/omnistash.ico и assets/stashroom.ico сверяются с генерацией.

Что проверяется (без PyInstaller и без чужих библиотек - формат читается struct):
  * это действительно ICO (reserved=0, type=1), карточки 32 бит с AND-маской,
    размеры карточек = заявленные SIZES, смещения entries совпадают с данными;
  * ассеты сверяются с генерацией байт-в-байт целиком (оба файла, все
    размеры) - рисунка детерминирована, и «иконка какая-то есть» не считается
    успехом: битый или ручной ассет называется;
  * палитра генератора - реестр темы «Бренд» (app/themes.py: coolors): имена
    ролей глифов существуют в теме, hex совпадает переменная-в-переменную;
  * Omnistash: поле cream читается точным тоном реестра, стрелка - accent,
    ряды - text и muted, рамка - line-strong; углы канвы прозрачны;
  * Stashroom: тот же квадрат рядов без стрелки - точного accent в ассете нет
    (хранилище без закачки, это названо в генераторе), рамка и ряды navy;
  * специка ссылается на omnistash.ico и терпит его отсутствие (частичный
    checkout); сборка Stashroom (W5) получит stashroom.ico отдельно.
"""

import struct
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC / "tools"))

import make_icon  # noqa: E402 - путь добавлен выше
from app import themes  # noqa: E402

ASSETS = {
    "omnistash": SRC / "assets" / "omnistash.ico",
    "stashroom": SRC / "assets" / "stashroom.ico",
}
SPEC = SRC / "Omnistash.spec"
HEADER_BYTES = 40


def entries(data: bytes) -> list[tuple[int, int, int, int]]:
    """DirectoryEntry[]: (width, height, bytesInRes, offset)."""
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert reserved == 0 and kind == 1, f"не ICO: reserved={reserved} type={kind}"
    out = []
    for index in range(count):
        width, height, _colors, _reserved, planes, compression, size, offset = \
            struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        out.append((width or 256, height or 256, size, offset))
    return out


def card_bytes(size: int) -> int:
    """Размер BMP-картки: заголовок + BGRA + AND-маска (строка по 4 байта)."""
    mask_row = ((size + 31) // 32) * 4
    return HEADER_BYTES + size * size * 4 + mask_row * size


def hex_rgb(value: str) -> tuple[int, int, int]:
    """'#rrggbb' -> (r, g, b)."""
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def decoded(data: bytes, size: int) -> list[tuple[int, int, int, int]]:
    """Пикселы картки (сверху вниз) из BMP-данных (строки снизу вверх)."""
    for width, _height, _size, offset in entries(data):
        if width != size:
            continue
        header = struct.unpack_from("<IiiHHIIiiII", data, offset)
        assert header[0] == HEADER_BYTES, f"не BMP-заголовок: {header[0]}"
        assert header[1] == size and header[2] == size * 2, f"размеры: {header[1:3]}"
        assert header[3] == 1 and header[4] == 32, f"planes/bitcount: {header[3:5]}"
        assert header[5] == 0, f"сжатие BMP должно быть 0: {header[5]}"
        xor = data[offset + HEADER_BYTES:offset + HEADER_BYTES + size * size * 4]
        pixels = []
        for row in range(size - 1, -1, -1):
            for column in range(size):
                blue, green, red, alpha = struct.unpack_from(
                    "<BBBB", xor, (row * size + column) * 4)
                pixels.append((red, green, blue, alpha))
        return pixels
    raise AssertionError(f"картки {size} нет в asset")


class TestIconAssets(unittest.TestCase):
    def setUp(self):
        self.data = {}
        for name, path in ASSETS.items():
            if not path.exists():
                self.skipTest(f"assets/{name}.ico не собран (python tools/make_icon.py)")
            self.data[name] = path.read_bytes()

    def test_both_icons_are_lists_of_bmp_cards(self):
        for name, data in self.data.items():
            listed = entries(data)
            self.assertEqual([size for size, _h, _s, _o in listed],
                             list(make_icon.SIZES),
                             f"{name}: карточки не равны заявленным SIZES")
            cursor = 6 + 16 * len(listed)
            for size, _height, card, offset in listed:
                self.assertEqual(offset, cursor,
                                 f"{name}: смещение картки {size} не совпадает")
                self.assertEqual(card, card_bytes(size),
                                 f"{name}: картка {size} не равна BMP + маске")
                cursor += card
            self.assertEqual(cursor, len(data),
                             f"{name}: в конце файла лишние байты (или карток меньше)")

    def test_assets_match_generation_byte_for_byte(self):
        """Ассеты сверяются с генерацией целиком: детерминированность, не «есть файл»."""
        for name, data in self.data.items():
            self.assertEqual(make_icon.build(name), data,
                             f"{name}.ico отличается от генерации tools/make_icon.py - "
                             "ассет правил кто-то руками или битый")

    def test_palette_is_the_brand_registry(self):
        """Роли глифов существуют в теме «Бренд» и hex читается из реестра."""
        brand = themes.css_vars(make_icon.BRAND_THEME)
        self.assertEqual(make_icon.role("accent"), (*hex_rgb(brand["--accent"]), 255),
                         "accent генератора не равен реестру coolors")
        self.assertEqual(make_icon.role("bg"), (*hex_rgb(brand["--bg"]), 255),
                         "bg генератора не равен реестру coolors")
        for glyph in make_icon.GLYPHS.values():
            for key, value in glyph.items():
                if value is None:
                    continue
                self.assertIn(f"--{value}", brand,
                              f"роль {value} глифа не существует в теме «Бренд»")
        # Контраст значимой графики на cream (WCAG SC 1.4.11) держат те же
        # роли, что сверяет tests/test_brand_theme.py - иконка не изобретает тонов.
        ratio = themes.contrast_ratio
        self.assertGreaterEqual(ratio(brand["--accent"], brand["--bg"]), 3.0,
                                "red стрелка на cream ниже порога 3:1 - значимая графика")
        self.assertGreaterEqual(ratio(brand["--text"], brand["--bg"]), 3.0,
                                "navy ряды на cream ниже порога 3:1 - значимая графика")

    def test_omnistash_pixels_read_the_brand_glyph(self):
        """Поле, стрелка, ряды и рамка Omnistash - точные тона реестра на чистых участках.

        Пиксел на границе двух цветов - среднее двух тонов палитры, поэтому
        проверяются чистые участки (центр ряда, ствол стрелки, середина рамки)
        и яркость поля, а не каждый пиксел.
        """
        pixels = decoded(self.data["omnistash"], 16)
        brand = themes.css_vars(make_icon.BRAND_THEME)
        self.assertEqual(pixels[3 * 16 + 3][:3], hex_rgb(brand["--bg"]),
                         "поле квадрата не cream из реестра")
        self.assertEqual(pixels[8 * 16 + 8][:3], hex_rgb(brand["--muted"]),
                         "верхний ряд стека не muted - стек не различает ряды")
        self.assertEqual(pixels[13 * 16 + 8][:3], hex_rgb(brand["--text"]),
                         "второй ряд стека не navy из реестра - ряды библиотеки не читаются")
        # Ствол стрелки (10% канвы) и полоса рамки (2%) тонкие: на 16 пикселах
        # пиксел никогда не лежит в них целиком - их точные тона сверяются на
        # 256-картке, где пиксел внутри чистой области.
        big = decoded(self.data["omnistash"], 256)
        self.assertEqual(big[60 * 256 + 128][:3], hex_rgb(brand["--accent"]),
                         "ствол стрелки не красится accent - Omnistash без закачки")
        self.assertEqual(big[16 * 256 + 128][:3], hex_rgb(brand["--line-strong"]),
                         "рамка квадрата не blue-strong - иконка не читается на светлом фоне")
        opaque = {pixel[:3] for pixel in pixels if pixel[3] > 0}
        bg = hex_rgb(brand["--bg"])
        brighter = [rgb for rgb in opaque
                    if rgb[0] > bg[0] or rgb[1] > bg[1] or rgb[2] > bg[2]]
        self.assertEqual(brighter, [],
                         f"пикселы ярче cream вне палитры: {brighter}")
        cream = [pixel for pixel in pixels if pixel[3] > 0 and pixel[:3] == bg]
        self.assertGreater(len(cream), len(pixels) // 4,
                           "поля cream в иконке меньше четверти канвы - светлой подложки нет")
        for corner in ((0, 0), (0, 15), (15, 0), (15, 15)):
            self.assertEqual(pixels[corner[1] * 16 + corner[0]][3], 0,
                             f"угол {corner} непрозрачен - иконка не скруглённая")

    def test_stashroom_pixels_are_shelves_without_arrow(self):
        """Stashroom: ряды и navy рамка, точного accent нет - хранилище без закачки."""
        pixels = decoded(self.data["stashroom"], 16)
        brand = themes.css_vars(make_icon.BRAND_THEME)
        self.assertEqual(pixels[3 * 16 + 3][:3], hex_rgb(brand["--bg"]),
                         "поле Stashroom не cream из реестра")
        self.assertEqual(pixels[8 * 16 + 8][:3], hex_rgb(brand["--muted"]),
                         "верхний ряд стека Stashroom не muted - ряды не различаются")
        self.assertEqual(pixels[13 * 16 + 8][:3], hex_rgb(brand["--text"]),
                         "второй ряд стека Stashroom не navy - ряды не читаются")
        big = decoded(self.data["stashroom"], 256)
        self.assertEqual(big[16 * 256 + 128][:3], hex_rgb(brand["--text"]),
                         "рамка Stashroom не navy - два глифа неразличимы по рамке")
        opaque = {pixel[:3] for pixel in pixels if pixel[3] > 0}
        self.assertNotIn(hex_rgb(brand["--accent"]), opaque,
                         "Stashroom держит red стрелку: глиф называет закачку, которой нет")
        for corner in ((0, 0), (0, 15), (15, 0), (15, 15)):
            self.assertEqual(pixels[corner[1] * 16 + corner[0]][3], 0,
                             f"угол {corner} непрозрачен - иконка не скруглённая")

    def test_spec_uses_the_icon_asset(self):
        spec = SPEC.read_text(encoding="utf-8")
        self.assertIn('assets", "omnistash.ico"', spec,
                      "специка не ссылается на asset иконки")
        self.assertIn("icon=_icon", spec,
                      "EXE не получает иконку")
        self.assertIn("os.path.exists(_icon)", spec,
                      "специка не терпит отсутствие иконки (частичный checkout)")


class TestIconDeterminism(unittest.TestCase):
    def test_regeneration_is_byte_identical(self):
        for name in make_icon.GLYPHS:
            first = make_icon.build(name, (16, 48))
            second = make_icon.build(name, (16, 48))
            self.assertEqual(first, second,
                             f"генерация {name} не детерминирована")

    def test_mask_rows_are_padded_to_four_bytes(self):
        """Строка AND-маски выровнена по 4 байтам (48 -> 8 байт, не 6)."""
        card = make_icon._bmp_card(48, make_icon.render(48, make_icon.GLYPHS["omnistash"]))
        mask_len = len(card) - HEADER_BYTES - 48 * 48 * 4
        self.assertEqual(mask_len, ((48 + 31) // 32) * 4 * 48)


if __name__ == "__main__":
    unittest.main()
