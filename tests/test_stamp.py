"""tools/stamp_version: формат тега и байтовая замена версии.

Стампер вшивает версию из тега в рабочее дерево CI (коммиты с бампом
не нужны - как в Synfronia); здесь проверяем парсер и саму замену на
временных файлах (рабочий файл не трогаем).
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import stamp_version  # noqa: E402 - путь добавлен выше
from app import __version__  # noqa: E402 - версия программы


class TestStamp(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(stamp_version.stamp("b0.1.4"), "0.1.4")
        self.assertEqual(stamp_version.stamp("v0.1.5"), "0.1.5")
        self.assertEqual(stamp_version.stamp("v1.2.3.4"), "1.2.3.4")
        for bad in ("0.1.4", "b1.2", "x0.1.1", "b0.1.4-beta", "", None):
            with self.assertRaises(ValueError):
                stamp_version.stamp(bad)

    def test_replace_keeps_file_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app_init.py"
            original = ('__version__ = "0.1.3"\n'
                        '# русский комментарий и перевод строк\n'
                        'OTHER = "не трогается"\r\n')   # CRLF тоже цел
            path.write_bytes(original.encode("utf-8"))
            ok = stamp_version._replace(
                path, rb'__version__\s*=\s*"[^"]*"',
                b'__version__ = "9.9.9"')
            self.assertTrue(ok)
            data = path.read_bytes()
            self.assertIn(b'__version__ = "9.9.9"', data)
            self.assertIn("не трогается".encode("utf-8"), data)
            self.assertIn(b"\r\n", data, "переводы строк не должны пострадать")

    def test_replace_missing_field_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.py"
            path.write_text("нет такого поля\n", encoding="utf-8")
            self.assertFalse(stamp_version._replace(
                path, rb'__version__\s*=\s*"[^"]*"', b'__version__ = "1.0.0"'))

    def test_current_version_is_stampable(self):
        """Значение в исходнике - той же формы, что даёт стампер.

        Стампер меняет только строку __version__ и берёт версию из тега;
        значение в git - линия бета-сборок, оно должно парситься тем же
        регулярным выражением, иначе spec и CI получают нечитаемую версию.
        """
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        self.assertEqual(stamp_version.stamp(f"b{__version__}"), __version__)
        self.assertEqual(stamp_version.stamp(f"v{__version__}"), __version__)

    def test_version_has_one_source(self):
        """version.py переэкспортирует app.__version__ - второго числа нет."""
        sys.path.insert(0, str(stamp_version.ROOT))
        import version
        self.assertEqual(version.__version__, __version__)

    def test_spec_reads_version_from_version_py(self):
        spec = (stamp_version.ROOT / "Omnistash.spec").read_text(encoding="utf-8")
        self.assertIn("from version import __version__", spec,
                      "специка не читает версию из version.py")


if __name__ == "__main__":
    unittest.main()
