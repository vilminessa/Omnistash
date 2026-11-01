"""Темы и шрифт интерфейса (M15): палитра и стек-семейства одним источником.

Что сверяет тест:
  * реестр тем app/themes.py <-> палитра в ui_src/app.css (значения
    CSS-переменных в блоках :root и body[data-theme="<id>") - иначе список
    тем в карточке и цвета в CSS расходятся незаметно для глаза;
  * набор токенов: цвета (PALETTE_VARS) и токены, которые не цвета -
    кольцо фокуса и тени (COMPOSITE_VARS) - в каждой теме и в CSS;
  * вне блоков палитры в CSS не остаётся литералов цвета: правило с hex
    живёт вне темы и светлая/контрастная темы его не правят;
  * состояния различимы: hover и selected - разные фоны, кольцо фокуса
    отличается от акцента, границы контрола и фокус проверены по WCAG;
  * стек-семейства app/themes.py <-> FONT_STACKS в ui_src/app.js (окно
    применяет стек тем же значением, что держит реестр);
  * границы и множитель шрифта <-> поля settings_schema и расчёт --row-h
    (строки списка должны расти вместе с текстом, а не обрезаться);
  * читаемость тем: контраст подписей и подложек по WCAG AA; битое значение
    настройки возвращается дефолтом (окно не «серый экран»).
"""

import re
import unittest
from pathlib import Path

from app import i18n, settings_schema, themes
from tests.test_gui import GuiCase

UI = Path(__file__).resolve().parent.parent / "ui_src"
CSS = UI / "app.css"
JS = UI / "app.js"


def _clean(text: str) -> str:
    """CSS/JS без комментариев: комментарии объясняют ловушки и не код."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _css_block(text: str, selector: str) -> dict:
    """Значения переменных из блока `selector { ... }`."""
    clean = _clean(text)
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", clean)
    if not match:
        raise AssertionError(f"в app.css нет блока {selector!r}: "
                             "палитра темы потеряна")
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", match.group(1)))


def _js_object(text: str, name: str) -> dict:
    """Значения из `var <name> = { ... }` в app.js."""
    match = re.search(r"var " + re.escape(name) + r"\s*=\s*\{(.*?)\};",
                      _clean(text), flags=re.DOTALL)
    if not match:
        raise AssertionError(f"в app.js нет {name}: UI потерял этот реестр")
    return dict(re.findall(r"([A-Za-z0-9_-]+)\s*:\s*'([^']*)'", match.group(1)))


class TestThemesRegistry(unittest.TestCase):
    """Реестр тем: набор переменных, читаемость, поведение на битом значении."""

    def test_palette_vars_complete(self):
        for item in themes.themes():
            missing = [var for var in themes.PALETTE_VARS
                       if var not in item["vars"]]
            self.assertEqual(missing, [], f"{item['id']}: нет переменных {missing}")
            missing = [var for var in themes.COMPOSITE_VARS
                       if var not in item["vars"]]
            self.assertEqual(missing, [], f"{item['id']}: нет токенов {missing}")

    def test_composite_tokens_are_not_colours(self):
        """Кольцо фокуса и тени - строки box-shadow, а не hex.

        hex внутри тени прячется от сверки палитры с app.css: тема тогда
        «проходит» проверку, но цвет в тени не правится по теме.
        """
        for item in themes.themes():
            for var in themes.COMPOSITE_VARS:
                value = item["vars"][var]
                self.assertTrue(themes._composite_ok(value),
                                f"{item['id']}: {var} = {value!r}")
        broken = {"id": "x", "vars": {**themes.css_vars("dark"),
                                      "--shadow": "0 2px 8px #000000"}}
        self.assertIn("--shadow = '0 2px 8px #000000' - не значение для box-shadow",
                      themes.vars_problems(broken))
        self.assertIn("--shadow отсутствует",
                      themes.vars_problems({"id": "x",
                                            "vars": {k: v for k, v in
                                                     themes.css_vars("dark").items()
                                                     if k != "--shadow"}}))

    def test_hover_and_selected_are_different(self):
        """Наведённая и выбранная строка различаются тоном, а не только именем.

        Один и тот же фон на hover и selected делает «выбранное» неотличимым
        от «наведённое» (SC 1.4.11 - состояние должно быть различимо).
        """
        for item in themes.themes():
            vars_ = item["vars"]
            self.assertNotEqual(vars_["--hover"], vars_["--selected"],
                                f"тема {item['id']}: hover == selected")
            self.assertNotEqual(vars_["--focus"], vars_["--accent"],
                                f"тема {item['id']}: кольцо фокуса совпадает "
                                f"с акцентом - фокус неотличим от кнопки")

    def test_all_themes_readable(self):
        for item in themes.themes():
            self.assertEqual(themes.check(item), [],
                             f"тема {item['id']} нечитаема: {themes.check(item)}")
            self.assertTrue(themes.readable(item["id"]))

    def test_ids_labels_and_order(self):
        self.assertEqual(themes.ids(),
                         tuple(item["id"] for item in themes.themes()))
        self.assertEqual([pair[0] for pair in themes.labels()],
                         list(themes.ids()))

    def test_unknown_and_broken_value_returns_default(self):
        self.assertEqual(themes.normalize("purple"), themes.DEFAULT_THEME)
        self.assertEqual(themes.normalize(None), themes.DEFAULT_THEME)
        self.assertEqual(themes.safe_theme("nope")["id"], themes.DEFAULT_THEME)
        self.assertEqual(themes.css_vars("nope"), themes.css_vars("dark"))

    def test_contrast_ratio_is_wcag(self):
        self.assertAlmostEqual(themes.contrast_ratio("#ffffff", "#000000"),
                               21.0, places=3)
        self.assertAlmostEqual(themes.contrast_ratio("#ffffff", "#ffffff"),
                               1.0, places=3)
        # Подпись того же цвета, что подложка, - контраст 1 (нечитаемо).
        self.assertLess(themes.contrast_ratio("#101010", "#101010"), 2)

    def test_broken_theme_reports_problems(self):
        broken_hex = {"id": "x", "vars": {**themes.css_vars("dark"),
                                          "--text": "not-a-color"}}
        problems = themes.vars_problems(broken_hex)
        self.assertTrue(any("--text" in line for line in problems), problems)

        missing = {"id": "x", "vars": {k: v for k, v in themes.css_vars("dark").items()
                                       if k != "--muted"}}
        self.assertIn("--muted отсутствует", themes.vars_problems(missing))

        invisible = {"id": "x", "vars": {**themes.css_vars("dark"),
                                         "--text": "#0c1622"}}
        self.assertTrue(themes.contrast_problems(invisible),
                        "подпись цвета подложки должна быть нечитаемой")

        thin = {"id": "x", "vars": {**themes.css_vars("dark"),
                                    "--accent": "#1a2634"}}
        self.assertTrue(themes.contrast_problems(thin),
                        "акцент ниже порога контраста должен быть отмечен")

    def test_line_is_not_a_caption_pair(self):
        # --line - декоративный разделитель: его низкий контраст сознательный,
        # проверка читаемости не должна требовать «жирные» границы.
        self.assertNotIn("--line", [pair[0] for pair in themes.CONTRAST_PAIRS])
        self.assertNotIn("--line", [pair[1] for pair in themes.CONTRAST_PAIRS])

    def test_line_strength_and_focus_are_checked_as_components(self):
        """Граница контрола и кольцо фокуса проверяются: это компонент (SC 1.4.11).

        В отличие от --line они несут смысл (граница input/select, индикатор
        фокуса), поэтому порог 3.0 для них обязателен - иначе контрол на
        подложке не различим, а фокус не виден.
        """
        pairs = {pair for pair in themes.CONTRAST_PAIRS}
        for var, floor in (("--line-strong", "--surface"), ("--line-strong", "--widget"),
                           ("--focus", "--surface"), ("--focus", "--widget")):
            self.assertIn((var, floor, 3.0), pairs,
                          f"пара ({var}, {floor}) с порогом 3.0 должна проверяться")
        self.assertIn(("--on-scrim", "--scrim", 4.5), pairs,
                      "подпись на подложке обложки должна читаться")
        self.assertIn(("--log-text", "--log-bg", 4.5), pairs,
                      "подпись журнала на своей подложке должна читаться")


class TestFontRegistry(unittest.TestCase):
    """Шрифт: границы, множитель, семейства и высота строк списка."""

    def test_size_and_scale_clamped(self):
        self.assertEqual(themes.normalize_size("abc"), themes.FONT_SIZE_DEFAULT)
        self.assertEqual(themes.normalize_size(None), themes.FONT_SIZE_DEFAULT)
        self.assertEqual(themes.normalize_size(4), themes.FONT_SIZE_MIN)
        self.assertEqual(themes.normalize_size(99), themes.FONT_SIZE_MAX)
        self.assertEqual(themes.normalize_scale(50), themes.FONT_SCALE_DEFAULT)
        self.assertEqual(themes.normalize_scale(400), themes.FONT_SCALE_MAX)

    def test_multiplier_applied_to_base(self):
        self.assertEqual(themes.font_px(13, 100), 13)
        self.assertEqual(themes.font_px(13, 150), 20)   # 19.5 -> округление вверх
        # Границы размера держим и после множителя: крупный шрифт не «расползается».
        self.assertEqual(themes.font_px(18, 200), themes.FONT_SIZE_MAX)
        self.assertEqual(themes.font_px("битое", "битое"), themes.FONT_SIZE_DEFAULT)

    def test_font_family_broken_returns_system(self):
        self.assertEqual(themes.normalize_font("comic"), themes.FONT_DEFAULT)
        self.assertEqual(themes.font_stack("comic"), themes.font_stack("system"))
        self.assertEqual([pair[0] for pair in themes.font_labels()],
                         list(themes.font_ids()))

    def test_row_height_grows_with_text(self):
        self.assertEqual(themes.row_px(13, 100), float(themes.ROW_MIN))
        self.assertGreater(themes.row_px(18, 120), themes.ROW_MIN)
        self.assertAlmostEqual(themes.row_px(18, 120),
                               themes.font_px(18, 120) * themes.ROW_FACTOR,
                               places=3)
        # Мелкий шрифт не делает строки меньше нижней границы.
        self.assertEqual(themes.row_px(10, 100), float(themes.ROW_MIN))


class TestCssPaletteMatchesRegistry(unittest.TestCase):
    """app.css обязан держать те же значения палитры, что app/themes.py."""

    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")

    def test_root_block_is_default_theme(self):
        root = _css_block(self.css, ":root")
        for var, value in themes.css_vars(themes.DEFAULT_THEME).items():
            self.assertEqual(root.get(var), value,
                             f"{var} в :root не совпадает с темой "
                             f"{themes.DEFAULT_THEME!r}")

    def test_theme_blocks_match_registry(self):
        for item in themes.themes():
            if item["id"] == themes.DEFAULT_THEME:
                continue        # дефолтная тема живёт в :root
            block = _css_block(self.css, f'body[data-theme="{item["id"]}"]')
            self.assertEqual(block, themes.css_vars(item["id"]),
                             f"палитра темы {item['id']} в CSS расходится с "
                             f"реестром app/themes.py")

    def test_font_vars_and_row_height(self):
        root = _css_block(self.css, ":root")
        self.assertEqual(root.get("--font-size"),
                         f"{themes.FONT_SIZE_DEFAULT}px")
        self.assertEqual(root.get("--font-family"), themes.font_stack("system"))
        # Расчёт высоты строки списка: тот же множитель и та же граница,
        # что считает виртуализация (rowHeight() читает computed --row-h).
        self.assertRegex(
            self.css,
            r"max\(" + re.escape(str(themes.ROW_MIN)) + r"px,\s*calc\("
            + r"var\(--font-size[^)]*\)\s*\*\s*" + re.escape(str(themes.ROW_FACTOR))
            + r"\)\)",
            "--row-h должен расти по --font-size теми же значениями, что "
            "themes.row_px (ROW_MIN/ROW_FACTOR)")

    def test_mono_stack_from_registry(self):
        """Журнал и JSON читаются моноширинным стеком из реестра тем.

        Литерал стека в двух правилах - второй источник правды: реестр
        меняется, а окно остаётся на прежнем шрифте.
        """
        root = _css_block(self.css, ":root")
        self.assertEqual(root.get("--mono"), themes.font_stack("mono"),
                         "--mono в app.css расходится с реестром стеков")
        self.assertEqual(len(re.findall(r"var\(--mono\)", _clean(self.css))), 2,
                         "журнал и JSON должны брать стек из переменной")

    def test_scale_tokens_defined_in_root(self):
        """Токены отступов/радиуса/типографики/веяния объявлены в :root (§2.4-2.7)."""
        root = _css_block(self.css, ":root")
        for var in ("--s-1", "--s-2", "--s-3", "--s-4", "--s-5", "--s-6",
                    "--s-7", "--s-8", "--r-1", "--r-2", "--r-3", "--r-4",
                    "--r-full", "--radius", "--fs-cap", "--fs-sm", "--fs-body",
                    "--fs-lg", "--fs-title", "--fs-head", "--lh-tight",
                    "--lh-body", "--track", "--nums", "--mono",
                    "--dur-fast-01", "--dur-fast-02", "--dur-moderate-01",
                    "--dur-moderate-02", "--dur-slow-01", "--dur-slow-02",
                    "--ease-productive", "--ease-expressive", "--ease-exit"):
            self.assertIn(var, root, f":root не объявляет токен {var}")

    def test_no_colour_literals_outside_palette(self):
        """Вне блоков палитры не остаётся hex и rgba: правила читают токены.

        Литерал цвета вне темы - это цвет, которого в теме нет: светлая и
        контрастная темы его не правят, и сверка реестра с CSS его не ловит.
        """
        clean = _clean(self.css)
        rest = clean[clean.index("* { box-sizing"):]      # после блоков тем
        values = ";".join(part.split(":", 1)[1] for part in rest.split(";")
                          if ":" in part)
        literals = re.findall(r"#[0-9a-fA-F]{3,6}\b", values) + re.findall(r"rgba\(", values)
        self.assertEqual(literals, [],
                         f"литералы цвета вне палитры: {literals[:10]}")

    def test_state_rules_read_state_tokens(self):
        """hover/selected/фокус/disabled - из токенов состояния, не из подложек.

        var(--widget)/var(--surface-2) вместо --selected/--hover - состояние
        правится не по теме, а «наведено» и «выбрано» выглядят одинаково.
        """
        for selector, token in ((".row:hover", "--hover"),
                                (".row.selected", "--selected"),
                                (".row.picked", "--selected"),
                                (".tree-item:hover", "--hover"),
                                (".tree-item.active", "--selected"),
                                (".table tbody tr:hover", "--hover"),
                                (".btn:hover:not(:disabled)", "--hover"),
                                (".btn.primary:disabled", "--accent-dim"),
                                (".mode-item:has(input:checked)", "--selected"),
                                (".view-btn.active", "--selected")):
            # селектор может быть в списке через запятую - между ним и {
            # других скобок быть не должно
            block = re.search(re.escape(selector) + r"[^{}]*\{([^}]*)\}",
                              _clean(self.css))
            self.assertTrue(block, f"нет правила {selector}")
            self.assertIn(f"var({token})", block.group(1),
                          f"{selector} должен брать {token}")

    def test_focus_ring_used(self):
        """Индикатор фокуса есть: кольцо из --focus-ring (две полосы, §5.2)."""
        clean = _clean(self.css)
        self.assertRegex(clean, r"focus-visible[^{]*\{[^}]*var\(--focus-ring\)",
                         "правило :focus-visible должно давать кольцо --focus-ring")
        self.assertNotRegex(clean, r"input:focus[^{]*\{[^}]*border-color:\s*var\(--accent\)",
                            "акцент как единственный индикатор фокуса: "
                            "акцентная кнопка и фокус выглядят одним цветом")

    def test_on_accent_used_on_accent_surfaces(self):
        # Подпись на акцентной подложке: кнопка и badge читаемы в любой теме
        # (в светлой акцент тёмный, поэтому «тёмная подпись» была бы невидимой).
        for selector in (".btn.primary", ".badge"):
            block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}",
                              _clean(self.css))
            self.assertTrue(block, f"нет правила {selector}")
            self.assertIn("var(--on-accent)", block.group(1),
                          f"{selector} должен брать подпись из --on-accent")
        self.assertNotRegex(_clean(self.css), r"color:\s*#08150f",
                            "жёсткая подпись на акценте осталась: тема её сломает")


class TestJsChromeMatchesRegistry(unittest.TestCase):
    """app.js применяет тему/шрифт теми же идентификаторами и стеками."""

    def setUp(self):
        self.js = JS.read_text(encoding="utf-8")

    def test_theme_ids_match_registry(self):
        match = re.search(r"var THEME_IDS\s*=\s*\[([^\]]*)\];", _clean(self.js))
        self.assertTrue(match, "в app.js нет THEME_IDS: резерв списка тем потерян")
        ids = re.findall(r'"([^"]+)"', match.group(1))
        self.assertEqual(ids, list(themes.ids()))

    def test_font_stacks_match_registry(self):
        stacks = _js_object(self.js, "FONT_STACKS")
        self.assertEqual(sorted(stacks), sorted(themes.font_ids()))
        for item in themes.FONTS:
            self.assertEqual(stacks[item["id"]], item["stack"],
                             f"стек {item['id']} в app.js расходится с themes.py")

    def test_font_defaults_match_registry(self):
        self.assertRegex(self.js,
                         r"var FONT_SIZE_DEFAULT\s*=\s*" + str(themes.FONT_SIZE_DEFAULT))
        self.assertRegex(self.js,
                         r"var FONT_SCALE_DEFAULT\s*=\s*" + str(themes.FONT_SCALE_DEFAULT))
        self.assertRegex(self.js,
                         r"var THEME_DEFAULT\s*=\s*\"" + themes.DEFAULT_THEME + "\"")
        self.assertRegex(self.js, r"var ROW_H\s*=\s*" + str(themes.ROW_MIN))

    def test_chrome_applied_through_css_vars(self):
        # Тема - data-theme на body, шрифт - CSS-переменные на <html>.
        self.assertRegex(self.js, r"document\.body\.dataset\.theme\s*=\s*themeId")
        self.assertRegex(self.js, r'setProperty\("--font-size"')
        self.assertRegex(self.js, r'setProperty\("--font-family"')
        # Высота строк берётся из той же переменной, что CSS (один источник).
        self.assertRegex(self.js, r'getPropertyValue\("--row-h"\)')
        self.assertRegex(self.js, r"rowH\s*=\s*grid\s*\?\s*cfg\.row\s*:\s*rowHeight\(\)")


class TestSettingsFields(unittest.TestCase):
    """Поля темы/шрифта/языка в схеме - из того же источника значений."""

    def test_theme_field_from_registry(self):
        field = settings_schema.field("theme")
        self.assertEqual(field["choices"], themes.labels())
        self.assertEqual(field["default"], themes.DEFAULT_THEME)
        self.assertEqual(field["section"], "Внешний вид")

    def test_font_fields_match_bounds(self):
        size = settings_schema.field("font_size")
        self.assertEqual((size["min"], size["max"], size["default"]),
                         (themes.FONT_SIZE_MIN, themes.FONT_SIZE_MAX,
                          themes.FONT_SIZE_DEFAULT))
        scale = settings_schema.field("font_scale")
        self.assertEqual((scale["min"], scale["max"], scale["default"]),
                         (themes.FONT_SCALE_MIN, themes.FONT_SCALE_MAX,
                          themes.FONT_SCALE_DEFAULT))
        family = settings_schema.field("font_family")
        self.assertEqual(family["choices"], themes.font_labels())
        self.assertEqual(family["default"], themes.FONT_DEFAULT)

    def test_language_and_adult_tab_fields(self):
        self.assertEqual(settings_schema.field("language")["choices"],
                         i18n.languages())
        self.assertEqual(settings_schema.field("language")["default"],
                         i18n.DEFAULT_LANGUAGE)
        tab = settings_schema.field("show_adult_tab")
        self.assertIs(tab["default"], False,
                      "вкладка взрослой площадки по умолчанию скрыта")
        self.assertEqual(tab["section"], "Внешний вид")

    def test_defaults_contain_new_keys(self):
        defaults = settings_schema.defaults()
        self.assertEqual(defaults["theme"], themes.DEFAULT_THEME)
        self.assertEqual(defaults["font_size"], themes.FONT_SIZE_DEFAULT)
        self.assertEqual(defaults["font_scale"], themes.FONT_SCALE_DEFAULT)
        self.assertEqual(defaults["font_family"], themes.FONT_DEFAULT)
        self.assertEqual(defaults["language"], i18n.DEFAULT_LANGUAGE)
        self.assertIs(defaults["show_adult_tab"], False)

    def test_coerce_broken_values_return_default(self):
        self.assertEqual(settings_schema.coerce("theme", "purple"),
                         themes.DEFAULT_THEME)
        self.assertEqual(settings_schema.coerce("font_size", "abc"),
                         themes.FONT_SIZE_DEFAULT)
        self.assertEqual(settings_schema.coerce("font_size", 999),
                         themes.FONT_SIZE_MAX)
        self.assertEqual(settings_schema.coerce("font_scale", 1),
                         themes.FONT_SCALE_MIN)
        self.assertEqual(settings_schema.coerce("font_family", "comic"),
                         themes.FONT_DEFAULT)
        self.assertEqual(settings_schema.coerce("language", "de"),
                         i18n.DEFAULT_LANGUAGE)
        self.assertIs(settings_schema.coerce("show_adult_tab", "нет"), False)

    def test_new_fields_are_visible_in_card(self):
        visible = {field["key"]: field for field in settings_schema.schema()}
        for key in ("theme", "font_size", "font_scale", "font_family",
                    "language", "show_adult_tab"):
            self.assertIn(key, visible, f"{key} скрыт в карточке настроек")


class TestThemeApi(GuiCase):
    """API окна: список тем, смена темы и шрифта, битое значение -> дефолт."""

    def test_theme_list_reports_registry_and_chrome(self):
        api = self.make_api()
        got = api.theme_list()
        self.assertTrue(got["ok"], got)
        self.assertEqual([item["id"] for item in got["themes"]],
                         list(themes.ids()))
        self.assertEqual(got["theme_ids"], list(themes.ids()))
        self.assertEqual(got["current"], themes.DEFAULT_THEME)
        self.assertEqual(got["vars"], themes.css_vars(themes.DEFAULT_THEME))
        self.assertTrue(got["readable"], got["problems"])
        self.assertEqual(got["problems"], [])
        self.assertEqual(got["font_stack"], themes.font_stack("system"))
        self.assertEqual(got["font_px"], themes.font_px(13, 100))
        self.assertEqual(got["row_h"], themes.row_px(13, 100))
        self.assertEqual(got["language"], i18n.DEFAULT_LANGUAGE)

    def test_theme_set_persists_and_broken_returns_default(self):
        api = self.make_api()
        res = api.theme_set({"theme": "light"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["settings"]["theme"], "light")
        self.assertEqual(res["vars"], themes.css_vars("light"))
        self.assertEqual(api.theme_list()["current"], "light")

        broken = api.theme_set("не-тема")
        self.assertEqual(broken["theme"], themes.DEFAULT_THEME)
        self.assertEqual(api.theme_list()["current"], themes.DEFAULT_THEME)

    def test_font_set_clamps_and_reports_row_height(self):
        api = self.make_api()
        res = api.font_set({"font_size": "18", "font_scale": 120,
                            "font_family": "serif"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["font_size"], 18)
        self.assertEqual(res["font_scale"], 120)
        self.assertEqual(res["font_family"], "serif")
        self.assertEqual(res["font_stack"], themes.font_stack("serif"))
        self.assertEqual(res["font_px"], themes.font_px(18, 120))
        self.assertEqual(res["row_h"], themes.row_px(18, 120))
        self.assertGreater(res["row_h"], themes.ROW_MIN,
                           "строки списка должны расти вместе с текстом")
        self.assertEqual(res["settings"]["font_size"], 18)
        self.assertEqual(res["settings"]["font_family"], "serif")

        junk = api.font_set({"font_size": "гигант", "font_scale": "abc",
                             "font_family": "comic"})
        self.assertEqual(junk["font_size"], themes.FONT_SIZE_DEFAULT)
        self.assertEqual(junk["font_scale"], themes.FONT_SCALE_DEFAULT)
        self.assertEqual(junk["font_family"], themes.FONT_DEFAULT)
        # Число вне границ поля сжимается в границы (как coerce схемы), а не
        # становится дефолтом: человек указал 900 - получаем максимум поля.
        clamped = api.font_set({"font_size": 999, "font_scale": 900})
        self.assertEqual(clamped["font_size"], themes.FONT_SIZE_MAX)
        self.assertEqual(clamped["font_scale"], themes.FONT_SCALE_MAX)

    def test_settings_rev_moves_with_theme(self):
        api = self.make_api()
        before = api.get_initial()["settings_rev"]
        after = api.theme_set("contrast")["settings_rev"]
        self.assertGreater(after, before,
                           "смена темы должна поднимать rev: иначе окно "
                           "не перерисует палитру")


if __name__ == "__main__":
    unittest.main()
