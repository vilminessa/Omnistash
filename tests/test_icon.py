"""Иконка exe: assets/omnistash.ico сверяется с генерацией tools/make_icon.py.

Что проверяется (без PyInstaller и без чужих библиотек - формат читается struct):
  * это действительно ICO (reserved=0, type=1), карточки 32 бит с AND-маской;
  * размеры карточек = заявленные SIZES, смещения entries совпадают с данными;
  * рисунок детерминирован: повторная генерация тех же размеров даёт те же
    байты - иконка либо совпадает с инструментом, либо кто-то правил вручную;
  * специка ссылается на asset: сборка exe без иконки - случай частичного
    checkout, но нормальная сборка обязана её брать;
  * центр квадрата непрозрачный и цвет из палитры тёмной темы, углы канвы
    прозрачны (иконка не прямоугольный блок с углами).
"""

import struct
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC / "tools"))

import make_icon  # noqa: E402 - путь добавлен выше
from app import themes  # noqa: E402

ASSET = SRC / "assets" / "omnistash.ico"
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


class TestIconAsset(unittest.TestCase):
    def setUp(self):
        if not ASSET.exists():
            self.skipTest("assets/omnistash.ico не собран (python tools/make_icon.py)")
        self.data = ASSET.read_bytes()

    def test_icon_is_a_list_of_bmp_cards(self):
        listed = entries(self.data)
        self.assertEqual([size for size, _h, _s, _o in listed],
                         list(make_icon.SIZES))
        cursor = 6 + 16 * len(listed)
        for size, _height, card, offset in listed:
            self.assertEqual(offset, cursor,
                             f"смещение картки {size} не совпадает с данными")
            self.assertEqual(card, card_bytes(size),
                             f"картка {size} не равна BMP + маске")
            cursor += card
        self.assertEqual(cursor, len(self.data),
                         "в конце файла есть лишние байты (или карток меньше)")

    def test_asset_matches_the_generator(self):
        """Детерминированность: те же размеры - те же байты картки."""
        generated = make_icon.build((16, 32))
        for size in (16, 32):
            made = [entry for entry in entries(generated) if entry[0] == size][0]
            asset = [entry for entry in entries(self.data) if entry[0] == size][0]
            self.assertEqual(generated[made[3]:made[3] + made[2]],
                             self.data[asset[3]:asset[3] + asset[2]],
                             f"картка {size} отличается от генерации tools/make_icon.py")

    def test_pixel_shape_reads_at_small_size(self):
        """Центр непрозрачен, углы канвы прозрачны, поле тёмное, акцент есть.

        Точные цвета проверяются только на чистых участках без сглаживания:
        пиксел на границе двух цветов - это среднее двух тонов палитры, и
        такого цвета в палитре нет. Поэтому проверяется наличие ключевых
        тонов и потолок яркости (иконка тёмная, не светлая).
        """
        pixels = decoded(self.data, 16)
        centre = pixels[8 * 16 + 8]
        self.assertEqual(centre[3], 255, "центр иконки прозрачен")
        dark = {theme["id"]: theme for theme in themes.themes()}["dark"]["vars"]
        opaque = {pixel[:3] for pixel in pixels if pixel[3] > 0}
        self.assertIn(hex_rgb(dark["--bg"]), opaque,
                      "поля квадрата (тёмного фона) в иконке нет")
        self.assertIn(hex_rgb(dark["--accent"]), opaque,
                      "акцента (стрелки) в иконке нет")
        text = hex_rgb(dark["--text"])
        bright = [rgb for rgb in opaque
                  if rgb[0] > text[0] or rgb[1] > text[1] or rgb[2] > text[2]]
        self.assertEqual(bright, [], f"светлые пикселы вне темы: {bright}")
        darkish = [pixel for pixel in pixels
                   if pixel[3] > 0 and (pixel[0] + pixel[1] + pixel[2]) < 200]
        self.assertGreater(len(darkish), len(pixels) // 4,
                           "тёмного поля в иконке меньше четверти канвы")
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
        first = make_icon.build((16, 48))
        second = make_icon.build((16, 48))
        self.assertEqual(first, second, "генерация иконки не детерминирована")

    def test_mask_rows_are_padded_to_four_bytes(self):
        """Строка AND-маски выровнена по 4 байтам (48 -> 8 байт, не 6)."""
        card = make_icon._bmp_card(48, make_icon.render(48))
        mask_len = len(card) - HEADER_BYTES - 48 * 48 * 4
        self.assertEqual(mask_len, ((48 + 31) // 32) * 4 * 48)


if __name__ == "__main__":
    unittest.main()
