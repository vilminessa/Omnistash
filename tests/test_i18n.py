"""Подписи интерфейса (M15): русский по умолчанию, английский - второй язык.

Что сверяет тест:
  * переключение языка действительно меняет подписи, которые UI рисует
    (ключи из разметки и из t() в app.js покрыты словарём на обоих языках);
  * непокрытый ключ остаётся русским: UI держит русскую строку как fallback
    и не показывает пустоту (никто не переводит «всё подряд»);
  * карточка настроек: на русском подписи остаются из settings_schema
    (schema_labels возвращает пустую карту), на английском - покрываются;
  * нейтральные публичные тексты: взрослая площадка называется «взрослая
    площадка» / "adult site", без её имени и доменов;
  * превью (mock в app.js) покрывает те же ключи: проверка переключения
    языка в превью не «рисуется» другим словарём.
"""

import re
import unittest
from pathlib import Path

from app import i18n, settings_schema
from tests.test_gui import GuiCase

UI = Path(__file__).resolve().parent.parent / "ui_src"
HTML = UI / "index.html"
JS = UI / "app.js"

# Сознательно не переводим: английский вариант этого длинного диалога не
# вводили, UI показывает русскую строку из кода (fallback).
RU_ONLY = ("dlg.first_send.text",)


def referenced_keys() -> set:
    """Ключи подписей, которые UI реально использует (разметка + код)."""
    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    keys = set(re.findall(r'data-l(?:-tip|-ph)?="([^"]+)"', html))
    keys |= set(re.findall(r'\bt\(\s*"([^"]+)"', js))
    return keys


class TestLanguageRegistry(unittest.TestCase):
    def test_languages_and_normalize(self):
        self.assertEqual(i18n.languages(),
                         [["ru", "Русский"], ["en", "English"]])
        self.assertEqual(i18n.language_ids(), ("ru", "en"))
        self.assertEqual(i18n.DEFAULT_LANGUAGE, "ru")
        self.assertEqual(i18n.normalize("en"), "en")
        self.assertEqual(i18n.normalize("de"), "ru")      # битый -> русский
        self.assertEqual(i18n.normalize(None), "ru")
        self.assertEqual(i18n.normalize(""), "ru")

    def test_translation_and_fallback(self):
        self.assertEqual(i18n.t("tab.library", "ru"), "Библиотека")
        self.assertEqual(i18n.t("tab.library", "en"), "Library")
        # Битый язык: русская подпись, а не пустота и не английский.
        self.assertEqual(i18n.t("tab.library", "klingon"), "Библиотека")
        # Ключ без перевода остаётся русским.
        self.assertEqual(i18n.t("dlg.first_send.text", "en"),
                         i18n.t("dlg.first_send.text", "ru"))
        # Совсем неизвестный ключ - пустая строка: UI подставит свою
        # русскую подпись из разметки/кода.
        self.assertEqual(i18n.t("tab.unknown", "en"), "")
        self.assertFalse(i18n.has("tab.unknown"))

    def test_referenced_keys_are_covered(self):
        keys = referenced_keys()
        self.assertTrue(keys, "UI не использует ни одной подписи словаря")
        missing = sorted(key for key in keys if not i18n.has(key))
        self.assertEqual(missing, [],
                         f"ключи UI отсутствуют в словаре: {missing}")
        no_en = sorted(key for key in keys if key not in RU_ONLY
                       and not i18n.t(key, "en"))
        self.assertEqual(no_en, [],
                         f"переключение языка не покрывает эти подписи UI: {no_en}")

    def test_markup_has_attributes_for_switched_labels(self):
        html = HTML.read_text(encoding="utf-8")
        for key in ("tab.library", "tab.sync", "tab.queue", "tab.settings",
                    "btn.rescan", "btn.add", "btn.stop", "col.status",
                    "col.title", "col.channel", "col.dur", "col.date",
                    "col.size", "ph.search"):
            self.assertRegex(html, rf'data-l(?:-tip|-ph)?="{re.escape(key)}"',
                             f"разметка не помечена ключом {key}: переключение "
                             "языка её не тронет")
        # Язык страницы (атрибут lang) меняет app.js - признак смены языка.
        self.assertRegex(JS.read_text(encoding="utf-8"),
                         r"document\.documentElement\.lang\s*=")

    def test_texts_maps_by_language(self):
        ru = i18n.ui_texts("ru")
        self.assertEqual(ru["tab.library"], "Библиотека")
        self.assertEqual(ru["tab.settings"], "Настройки")
        self.assertIn("dlg.first_send.text", ru)   # русская подпись есть всегда

        en = i18n.ui_texts("en")
        self.assertEqual(en["tab.library"], "Library")
        self.assertNotIn("dlg.first_send.text", en,
                         "непокрытый ключ не должен попадать в карту второго "
                         "языка: UI показывает русскую подпись из разметки")
        # Битый язык -> полная русская карта (не смесь двух языков).
        self.assertEqual(i18n.ui_texts("klingon"), i18n.ui_texts("ru"))
        # Английская карта действительно английская: в ней нет кириллицы,
        # и непокрытые ключи в неё не попадают (см. dlg.first_send.text выше).
        self.assertFalse(any(any(char in "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯабвгде"
                                 "жзиклмнопрстуфхцчшщэюя" for char in value)
                             for value in en.values()),
                         "карта en содержит русские подписи - переключение "
                         "языка показало бы смесь")
        self.assertTrue(set(en) < set(ru),
                        "карта en должна быть подмножеством русских подписей")

    def test_adult_site_name_is_neutral(self):
        self.assertEqual(i18n.adult_site_name("ru"), "Взрослая площадка")
        self.assertEqual(i18n.adult_site_name("en"), "Adult site")
        for language in ("ru", "en"):
            name = i18n.adult_site_name(language)
            self.assertNotIn("pornhub", name.lower())
            self.assertNotIn("PornHub", name)

    def test_dictionary_has_no_site_names_or_domains(self):
        """Публичные тексты не содержат имени площадки и её домена."""
        blob = "\n".join(value for entry in i18n._TEXTS.values()
                         for value in entry.values())
        self.assertNotIn("PornHub", blob)
        self.assertNotIn("pornhub.com", blob.lower())
        self.assertNotIn("PornHub", settings_schema.field("col_tags")["hint"])

    def test_schema_labels_only_for_second_language(self):
        self.assertEqual(i18n.schema_labels("ru"), {},
                         "русские подписи карточки берются из схемы - "
                         "переписывать их нечем")
        labels = i18n.schema_labels("en")
        visible = {field["key"] for field in settings_schema.schema()}
        self.assertTrue(labels)
        self.assertTrue(set(labels) <= visible,
                        f"перевод полей, которых нет в карточке: {set(labels) - visible}")
        for key, item in labels.items():
            self.assertTrue(item.get("label"), f"{key}: нет подписи поля")
            self.assertTrue(item.get("hint"), f"{key}: нет пояснения поля")
        self.assertEqual(labels["theme"]["label"], "Theme")
        self.assertEqual(labels["language"]["label"], "Interface language")
        self.assertEqual(labels["font_size"]["label"], "Font size, pixels")
        self.assertEqual(labels["font_family"]["label"], "Font family")
        self.assertEqual(labels["font_scale"]["label"], "Text size multiplier")
        self.assertEqual(labels["show_adult_tab"]["label"],
                         "Show the adult site tab")

    def test_sections_cover_card_sections(self):
        sections = i18n.sections("en")
        names = {field["section"] for field in settings_schema.schema()
                 if field["section"]}
        self.assertTrue(names <= set(sections),
                        f"названия разделов не переведены: {names - set(sections)}")
        self.assertEqual(sections["Библиотека"], "Library")
        self.assertEqual(sections["Внешний вид"], "Appearance")
        self.assertEqual(sections["Аккаунт Google"], "Google account")
        # fallback: раздел без перевода остаётся русским именем из схемы
        self.assertEqual(i18n.sections("ru")["Загрузка"], "Загрузка")

    def test_preview_mock_covers_same_keys(self):
        """Превью переключает язык тем же словарём - иначе проверка пустая."""
        js = JS.read_text(encoding="utf-8")
        match = re.search(r"var MOCK_EN\s*=\s*\{(.*?)\};", js, flags=re.DOTALL)
        self.assertTrue(match, "в превью нет карты второго языка (MOCK_EN)")
        covered = set(re.findall(r'"([^"]+)"\s*:', match.group(1)))
        missing = sorted(key for key in referenced_keys()
                         if key not in RU_ONLY and key not in covered)
        self.assertEqual(missing, [],
                         f"мок превью не покрывает подписи: {missing}")


class TestLanguageApi(GuiCase):
    """API окна: подписи текущего языка и смена языка (факт в настройках)."""

    def test_ui_texts_reports_current_language(self):
        api = self.make_api()
        got = api.ui_texts()
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["language"], "ru")
        self.assertEqual(got["texts"]["tab.library"], "Библиотека")
        self.assertEqual(got["labels"], {},
                         "на русском подписи карточки берются из схемы")
        self.assertEqual(got["adult_site"], "Взрослая площадка")

    def test_language_switch_persists_and_relabels(self):
        api = self.make_api()
        res = api.language_set("en")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["language"], "en")
        self.assertEqual(res["texts"]["tab.library"], "Library")
        self.assertEqual(res["sections"]["Внешний вид"], "Appearance")
        self.assertEqual(res["adult_site"], "Adult site")
        self.assertEqual(res["settings"]["language"], "en")

        again = api.ui_texts()
        self.assertEqual(again["language"], "en",
                         "язык должен сохраняться в настройках между вызовами")
        self.assertEqual(again["texts"]["col.status"], "Status")
        self.assertEqual(again["adult_site"], "Adult site")

    def test_broken_language_returns_russian(self):
        api = self.make_api()
        api.language_set("de")
        got = api.ui_texts()
        self.assertEqual(got["language"], "ru")
        self.assertEqual(got["labels"], {})
        self.assertEqual(got["texts"]["tab.library"], "Библиотека")

    def test_language_list(self):
        api = self.make_api()
        got = api.language_list()
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["languages"], i18n.languages())
        self.assertEqual(got["language"], "ru")

    def test_theme_list_reports_language(self):
        api = self.make_api()
        api.language_set("en")
        self.assertEqual(api.theme_list()["language"], "en",
                         "список тем должен называть текущий язык")


if __name__ == "__main__":
    unittest.main()
