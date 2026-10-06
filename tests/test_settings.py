"""Настройки: схема, приведение типов, чтение/запись, переносы."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import settings, settings_schema
from app.paths import settings_path


class TestCoerce(unittest.TestCase):
    def test_bool_strings(self):
        self.assertTrue(settings_schema.coerce("keep_sidecar", "true"))
        self.assertFalse(settings_schema.coerce("keep_sidecar", "0"))
        self.assertTrue(settings_schema.coerce("keep_sidecar", True))
        self.assertTrue(settings_schema.coerce("keep_sidecar", "мусор"))  # дефолт True

    def test_int_clamped(self):
        self.assertEqual(settings_schema.coerce("delay_ms", 999999), 60000)
        self.assertEqual(settings_schema.coerce("delay_ms", -5), 0)
        self.assertEqual(settings_schema.coerce("delay_ms", "250"), 250)
        self.assertEqual(settings_schema.coerce("delay_ms", "строка"), 500)

    def test_choice_falls_back(self):
        self.assertEqual(settings_schema.coerce("quality", "low"), "low")
        self.assertEqual(settings_schema.coerce("quality", "ultra"), "high")
        self.assertEqual(settings_schema.coerce("default_sync_mode", "мусор"), "partial")

    def test_unknown_key_passes_through(self):
        # Ключа нет в схеме - значение не трогаем (отсечётся при записи).
        self.assertEqual(settings_schema.coerce("что-то", 42), 42)

    def test_roots_normalization(self):
        roots = settings_schema.coerce("library_roots", [
            "C:/видео",
            {"path": "  D:\\films  ", "recursive": False, "enabled": "нет"},
            {"path": ""},                       # пустой отбрасывается
            {"path": "C:/видео"},               # дубль отбрасывается
            42,                                 # мусор отбрасывается
        ])
        self.assertEqual(roots, [
            {"path": "C:/видео", "recursive": True, "enabled": True},
            {"path": "D:\\films", "recursive": False, "enabled": False},
        ])

    def test_defaults_are_independent(self):
        first = settings_schema.defaults()
        first["library_roots"].append({"path": "x", "recursive": True, "enabled": True})
        self.assertEqual(settings_schema.defaults()["library_roots"], [])


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        patcher = mock.patch("app.settings.settings_path",
                             return_value=self.dir / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_defaults_written_on_first_load(self):
        data = settings.load()
        self.assertEqual(data["default_sync_mode"], "partial")
        self.assertTrue((self.dir / "settings.json").exists())

    def test_roundtrip_and_unknown_keys_dropped(self):
        settings.set_value("quality", "low")
        # Чужой ключ в файле не должен пережить перезапись.
        raw = json.loads((self.dir / "settings.json").read_text(encoding="utf-8"))
        raw["левый_ключ"] = "значение"
        (self.dir / "settings.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        data = settings.load()
        self.assertEqual(data["quality"], "low")
        self.assertNotIn("левый_ключ", data)

    def test_broken_file_recovers(self):
        (self.dir / "settings.json").write_text("{сломано", encoding="utf-8")
        self.assertEqual(settings.load()["default_sync_mode"], "partial")

    def test_schema_sections_visible(self):
        fields = settings_schema.schema()
        self.assertTrue(all(not f.get("hidden") for f in fields))
        sections = {f["section"] for f in fields}
        self.assertEqual(sections, {"Библиотека", "Загрузка", "Внешний вид"})

    def test_every_field_has_label(self):
        for spec in settings_schema.FIELDS:
            if spec.get("hidden"):
                continue
            self.assertTrue(spec.get("label"), spec["key"])


if __name__ == "__main__":
    unittest.main()
