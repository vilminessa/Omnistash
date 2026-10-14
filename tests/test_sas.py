"""Этап S: SAS - канон форматов и фиксированные векторы.

Векторы - контракт стабильности: словарь/списки/нарезка битов
менять нельзя, иначе пары разных версий перестанут сходиться.
"""

import unittest

from app.mesh import sas
from app.mesh.sas_words import SAS_WORDS

# Общий вход для всех векторов: shared = 00..1f, идентичности aa/cc.
SHARED = bytes(range(32))
ID_A = "aa" * 32
ID_B = "cc" * 32
DIGEST_HEX = ("413b19380b64ce320f9df8cf0c88c8de"
              "899c6b09c55eb5d78be4022b49b3bf7d")
WORDS_VEC = ["посудомойка", "люлька", "минута", "заря", "катер",
             "заготовка", "карп", "рецепт", "призёр", "ковёр",
             "инвентарь", "чечевица"]
EMOJI_VEC = "🐱🐻🎁🐵🌽🍎🧩🎯"
ICONS_VEC = [51, 35, 8, 15, 39, 31, 35, 15]
DIGITS_VEC = "25480802"


class TestCanon(unittest.TestCase):
    def test_validate_canon(self):
        """Канон: словарь 1024 без дублей, эмодзи 64, сетки иконок."""
        sas.validate()
        self.assertEqual(len(SAS_WORDS), 1024)

    def test_sas_bytes_is_symmetric_and_bound(self):
        """Порядок идентичностей не важен; пара и версия - важны."""
        digest = sas.sas_bytes(SHARED, ID_A, ID_B)
        self.assertEqual(len(digest), 32)
        self.assertEqual(digest, sas.sas_bytes(SHARED, ID_B, ID_A))
        self.assertNotEqual(digest, sas.sas_bytes(b"\x01" * 32, ID_A, ID_B),
                            "другой ECDH-секрет - другой SAS (ловит MITM)")
        self.assertNotEqual(digest, sas.sas_bytes(SHARED, ID_A, "bb" * 32),
                            "другая пара устройств - другой SAS")

    def test_methods_exposed(self):
        self.assertEqual(sas.METHODS, ("words", "emoji", "icons", "digits"))
        for method in sas.METHODS:
            self.assertIn(method, sas.METHOD_LABELS)


class TestVector(unittest.TestCase):
    """Фиксированный вектор: вход -> ровно эти значения."""

    def setUp(self):
        self.digest = sas.sas_bytes(SHARED, ID_A, ID_B)

    def test_digest(self):
        self.assertEqual(self.digest.hex(), DIGEST_HEX)

    def test_words_vector(self):
        got = sas.words(self.digest)
        self.assertEqual(got, WORDS_VEC)
        self.assertEqual(len(got), 12, "ровно 12 слов")
        self.assertEqual(len(set(got)), 12,
                         "10 бит на позицию: повторы подозрительны")
        for word in got:
            self.assertIn(word, SAS_WORDS)

    def test_emoji_vector(self):
        got = sas.emoji(self.digest)
        self.assertEqual(got, list(EMOJI_VEC))
        self.assertEqual(len(got), 8, "ровно 8 эмодзи")

    def test_icons_vector(self):
        self.assertEqual(sas.icons(self.digest), ICONS_VEC)
        for index in ICONS_VEC:
            self.assertTrue(0 <= index < 64)

    def test_digits_vector(self):
        got = sas.digits(self.digest)
        self.assertEqual(got, DIGITS_VEC)
        self.assertEqual(len(got), 8)
        self.assertTrue(got.isdigit())

    def test_render_all_methods(self):
        self.assertEqual(sas.render("words", self.digest), WORDS_VEC)
        self.assertEqual(sas.render("digits", self.digest), DIGITS_VEC)
        with self.assertRaises(ValueError):
            sas.render("video", self.digest)


class TestIconsSvg(unittest.TestCase):
    def test_64_unique_icons(self):
        """64 пиктограммы: 8 форм x 8 цветов, все различны и валидны."""
        icons = [sas.svg_icon(i) for i in range(64)]
        self.assertEqual(len(set(icons)), 64)
        for svg in icons:
            self.assertIn("<svg", svg)
            self.assertIn('xmlns="http://www.w3.org/2000/svg"', svg)
            self.assertIn("fill=\"#", svg)

    def test_index_wraps(self):
        """Индекс за пределами 64 нормируется, а не падает."""
        self.assertEqual(sas.svg_icon(64), sas.svg_icon(0))
        self.assertEqual(sas.svg_icon(-1), sas.svg_icon(63))


if __name__ == "__main__":
    unittest.main()
