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

    def test_sidecar_mode_choice(self):
        # Новый выбор «Метаданные рядом с видео»: по умолчанию - всё в видео.
        self.assertEqual(settings_schema.coerce("sidecar_mode", "embed"), "embed")
        self.assertEqual(settings_schema.coerce("sidecar_mode", "files"), "files")
        self.assertEqual(settings_schema.coerce("sidecar_mode", "мусор"), "embed")
        self.assertEqual(settings_schema.defaults()["sidecar_mode"], "embed")

    def test_sync_depth_choice(self):
        # Глубина синка: по умолчанию полная (лайки/даты), «мусор» -> full.
        self.assertEqual(settings_schema.coerce("sync_depth", "fast"), "fast")
        self.assertEqual(settings_schema.coerce("sync_depth", "full"), "full")
        self.assertEqual(settings_schema.coerce("sync_depth", "мусор"), "full")
        self.assertEqual(settings_schema.defaults()["sync_depth"], "full")

    def test_column_visibility_defaults_on(self):
        for key in ("col_views", "col_likes", "col_comments", "col_sync"):
            self.assertTrue(settings_schema.defaults()[key], key)

    def test_keep_sidecar_hidden_but_supported(self):
        # Старый флаг ушёл из карточки, но остался в схеме: чужие
        # settings.json с явным False должны продолжать читаться.
        self.assertTrue(settings_schema.field("keep_sidecar").get("hidden"))
        self.assertFalse(settings_schema.coerce("keep_sidecar", False))

    def test_int_clamped(self):
        self.assertEqual(settings_schema.coerce("delay_ms", 999999), 60000)
        self.assertEqual(settings_schema.coerce("delay_ms", -5), 0)
        self.assertEqual(settings_schema.coerce("delay_ms", "250"), 250)
        self.assertEqual(settings_schema.coerce("delay_ms", "строка"), 500)

    def test_choice_falls_back(self):
        self.assertEqual(settings_schema.coerce("quality", "low"), "low")
        self.assertEqual(settings_schema.coerce("quality", "ultra"), "high")
        self.assertEqual(settings_schema.coerce("default_sync_mode", "мусор"), "partial")

    def test_coerce_unknown_key_passes_through(self):
        # Ключа нет в схеме - значение не трогаем (отсечётся при записи).
        self.assertEqual(settings_schema.coerce("что-то", 42), 42)

    def test_deprecated_keys_are_gone(self):
        # library_roots/dest_dir перенесены в таблицу storages: их не должно
        # быть ни в дефолтах, ни в схеме - иначе файл настроек будет
        # тихо хранить два источника правды.
        defaults = settings_schema.defaults()
        self.assertNotIn("library_roots", defaults)
        self.assertNotIn("dest_dir", defaults)
        keys = {f["key"] for f in settings_schema.FIELDS}
        self.assertNotIn("library_roots", keys)
        self.assertNotIn("dest_dir", keys)
        # Вместо них - поле-виджет хранилищ (не пишется в файл) и выбор
        # глобального хранилища.
        self.assertIn("_storages", keys)
        self.assertIn("default_storage_id", keys)
        self.assertTrue(settings_schema.field("_storages").get("transient"))


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
        self.assertEqual(sections, {"Библиотека", "Загрузка", "Mesh",
                                    "Доступ по HTTPS",
                                    "Расписание", "Аккаунт Google",
                                    "Метаданные API", "Внешний вид"})

    def test_every_field_has_label(self):
        for spec in settings_schema.FIELDS:
            if spec.get("hidden"):
                continue
            self.assertTrue(spec.get("label"), spec["key"])


class TestSecretsNeverReachUi(unittest.TestCase):
    """Секреты человек пишет один раз, но в снимок окна они не уходят."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        patcher = mock.patch("app.settings.settings_path",
                             return_value=self.dir / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_secret_keys_are_declared(self):
        self.assertEqual(settings.SECRET_KEYS, frozenset({"bot_token",
                                                          "data_api_key"}))
        self.assertTrue(settings.is_secret("bot_token"))
        self.assertTrue(settings.is_secret("data_api_key"))
        self.assertFalse(settings.is_secret("quality"))
        self.assertFalse(settings.is_secret(None))

    def test_as_public_blanks_values_and_reports_flags(self):
        data = settings.load()
        data["bot_token"] = "123456:со-секретный-токен"
        data["data_api_key"] = "AIza-секретный-ключ"
        public = settings.as_public(data)
        self.assertEqual(public["bot_token"], "")
        self.assertEqual(public["data_api_key"], "")
        self.assertIs(public["bot_token_set"], True)
        self.assertIs(public["data_api_key_set"], True)
        blob = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("со-секретный-токен", blob,
                         "значение токена не должно уйти окну")
        self.assertNotIn("AIza-секретный-ключ", blob,
                         "значение API-ключа не должно уйти окну")

    def test_flags_false_when_nothing_set(self):
        public = settings.as_public(settings.load())
        self.assertIs(public["bot_token_set"], False)
        self.assertIs(public["data_api_key_set"], False)
        self.assertEqual(public["bot_token"], "")

    def test_whitespace_is_not_a_set_value(self):
        self.assertIs(settings.secret_status({"bot_token": "   \n"})["bot_token_set"],
                      False)

    def test_internal_keys_stay_excluded(self):
        public = settings.as_public({"_mtime": 12345, "quality": "low"})
        self.assertNotIn("_mtime", public)
        self.assertEqual(public["quality"], "low")

    def test_clear_secret_removes_open_copy(self):
        """Значение уже лежит в профиле шифрованием - открытой копии быть не должно."""
        settings.set_value("bot_token", "123456:со-секретный-токен")
        self.assertTrue(settings.clear_secret("bot_token"))
        self.assertEqual(settings.load()["bot_token"], "")
        raw = (self.dir / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("со-секретный-токен", raw)

    def test_clear_secret_ignores_non_secret_and_empty(self):
        self.assertFalse(settings.clear_secret("quality"))
        settings.set_value("data_api_key", "")
        self.assertFalse(settings.clear_secret("data_api_key"))
        self.assertEqual(settings.load()["data_api_key"], "")


if __name__ == "__main__":
    unittest.main()
