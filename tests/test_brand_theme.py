"""Тема «Бренд» (coolors): вывод из палитры человека сверяется числами.

Что проверяется (без браузера и без подбора «красивых» hex):
  * реестр app/themes.py совпадает с выводом tools/build_brand_theme.py
    переменная-в-переменная (вывод детерминирован: без случайностей и без дат);
  * тема проходит валидатор app/themes.py (CONTRAST_PAIRS): подписи, приглушённая
    подпись, акцент, состояния и граница контрола против подложек держат пороги;
  * тема светлая и hint называет измеренную причину: red на navy 2.22 - ниже
    порога 3.0, поэтому акцентом на тёмной подложке red быть не может; на cream
    red держит 5.51 - это же число сверяется тестом;
  * структурные роли берут базовые цвета палитры (cream подложка, navy подпись,
    red акцент, maroon предупреждение), а не произвольные hex;
  * ни одна hex-переменная не вне палитры: цвет, её tint в один шаг смешения
    или tint её tint-a вторым шагом; битый цвет называется переменной;
  * роли различимы: --accent/--err/--warn/--focus, --ok/--info, --hover/--selected,
    --line-strong/--muted не совпадают и не сливаются в один тон с близкой
    яркостью; коллизия называется парой;
  * зеркальные списки темы не расходятся: CSS-блок body[data-theme="coolors"],
    THEME_IDS в app.js, choices поля theme в app/settings_schema.py и
    themes.ids() / themes.labels() - один порядок и тот же id;
  * настройка темы применяется: safe_theme("coolors") возвращает светлую тему,
    битое значение - дефолтную (окно не «серый экран»).
"""

import re
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC / "tools"))

import build_brand_theme as brand  # noqa: E402 - путь добавлен выше
from app import settings_schema, themes  # noqa: E402

CSS = SRC / "ui_src" / "app.css"
JS = SRC / "ui_src" / "app.js"


def _css_block(text: str, selector: str) -> dict:
    """Значения переменных из блока `selector { ... }` (без комментариев)."""
    clean = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", clean)
    if not match:
        raise AssertionError(f"в app.css нет блока {selector!r}: палитра темы "
                             "потеряна")
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", match.group(1)))


class BrandThemeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.vars = brand.build()
        self.theme = brand.theme()

    def test_registry_matches_derivation(self):
        """Реестр темы совпадает с выводом, порядок id темы - в реестре."""
        self.assertEqual(brand.registry_problems(), [],
                         f"реестр расходится с выводом: {brand.registry_problems()}")
        self.assertIn(brand.THEME_ID, themes.ids(),
                      "тема бренда не в списке themes.ids(): карточка настроек "
                      "не покажет её")
        item = themes.by_id(brand.THEME_ID)
        self.assertIsNotNone(item)
        self.assertEqual(item["vars"], themes.css_vars(brand.THEME_ID),
                         "css_vars не копия реестра: вызывающий правит реестр")

    def test_derivation_is_deterministic(self):
        """Повторный вывод даёт те же hex - иначе ассет правят руками."""
        again = brand.build()
        self.assertEqual(again, self.vars, "вывод темы не детерминирован")
        self.assertEqual(brand.problems(), [],
                         f"сверка вывода отказала: {brand.problems()}")
        self.assertEqual(brand.main(["--check"]), 0,
                         "--check отказал: реестр не совпадает с выводом")

    def test_theme_passes_contrast_pairs(self):
        """Все пары из CONTRAST_PAIRS выдерживают пороги (измерено)."""
        self.assertEqual(themes.check(self.theme), [],
                         f"тема нечитаема: {themes.check(self.theme)}")
        for fg, bg, floor in themes.CONTRAST_PAIRS:
            ratio = themes.contrast_ratio(self.vars[fg], self.vars[bg])
            self.assertGreaterEqual(ratio, floor,
                                    f"{fg} на {bg}: {ratio:.2f} < порога {floor}")

    def test_light_theme_reason_measured(self):
        """Тема светлая, потому что red на navy 2.22 - измерено и названо."""
        self.assertFalse(self.theme["dark"],
                         "бренд-тема объявлена тёмной: на navy подложке акцент "
                         "red не читается")
        self.assertIn("2.22", self.theme["hint"],
                      "hint темы не называет измеренную причину")
        self.assertAlmostEqual(brand.themes.contrast_ratio(brand.RED, brand.NAVY),
                               2.22, places=2)
        self.assertLess(brand.themes.contrast_ratio(brand.RED, brand.NAVY), 3.0,
                        "red на navy выдерживает порог значимой графики - причина "
                        "светлой темы названа неверно")
        self.assertGreaterEqual(brand.themes.contrast_ratio(brand.RED, brand.CREAM),
                                3.0, "red на cream не держит порог акцента SC 1.4.11")

    def test_structural_roles_take_palette_colors(self):
        """Подложка, подпись, акцент и предупреждение - базовые цвета палитры."""
        self.assertEqual(self.vars["--bg"], brand.CREAM)
        self.assertEqual(self.vars["--text"], brand.NAVY)
        self.assertEqual(self.vars["--accent"], brand.RED)
        self.assertEqual(self.vars["--warn"], brand.MAROON)
        self.assertEqual(self.vars["--scrim"], brand.NAVY)
        self.assertEqual(self.vars["--on-accent"], brand.CREAM)
        self.assertEqual(self.vars["--on-warn"], brand.CREAM)

    def test_no_hex_outside_palette_derivation(self):
        """Каждая hex-переменная - палитра, её tint или tint её tint-a."""
        self.assertEqual(brand.palette_problems(self.vars), [],
                         f"цвета вне палитры: {brand.palette_problems(self.vars)}")
        broken = dict(self.vars)
        broken["--accent"] = "#ff0000"     # не палитра и не её смешение
        problems = brand.palette_problems(broken)
        self.assertEqual(len(problems), 1,
                         "битый цвет не назван: палитра не сверяется")
        self.assertIn("--accent", problems[0])
        self.assertIn("#ff0000", problems[0])

    def test_roles_are_distinguishable(self):
        """Два состояния одним цветом - отказ; коллизия называется парой."""
        self.assertEqual(brand.role_problems(self.vars), [],
                         f"роли сливаются: {brand.role_problems(self.vars)}")
        broken = dict(self.vars)
        broken["--err"] = self.vars["--accent"]
        problems = brand.role_problems(broken)
        self.assertTrue(any("--err" in line for line in problems),
                        f"колизия accent/err не названа: {problems}")
        # порог подобран по действующим темам: более строгий отверг бы тёмную
        dark = themes.css_vars("dark")
        self.assertEqual(brand.role_problems(dark), [],
                         f"сверка ролей отвергла действующую тёмную тему: "
                         f"{brand.role_problems(dark)}")

    def test_ui_lists_mirror_the_registry(self):
        """CSS-блок, JS-список id и choices схемы - тот же порядок и тот же id."""
        css = _css_block(CSS.read_text(encoding="utf-8"),
                         'body[data-theme="coolors"]')
        registry = themes.css_vars(brand.THEME_ID)
        self.assertEqual(sorted(css), sorted(registry),
                         "CSS-блок бренда держит другой набор переменных")
        for name, value in registry.items():
            self.assertEqual(css[name].strip().lower(), value.lower(),
                             f"{name}: CSS {css[name]} != реестр {value}")
        ids = re.search(r"var THEME_IDS\s*=\s*\[([^\]]*)\]",
                        re.sub(r"//.*", "", JS.read_text(encoding="utf-8")))
        self.assertEqual([part.strip().strip('"') for part in ids.group(1).split(",")],
                         list(themes.ids()),
                         "THEME_IDS в app.js расходится с themes.ids(): окно "
                         "не перекрасит тему")
        field = next(item for item in settings_schema.FIELDS
                     if item["key"] == "theme")
        self.assertEqual(field["choices"], themes.labels(),
                         "choices поля theme не совпадают с реестром тем")
        self.assertIn([brand.THEME_ID, brand.LABEL], field["choices"],
                      "карточка настроек не предлагает тему «Бренд»")
        self.assertIn("coolors", field["hint"],
                      "hint поля theme не называет палитру бренда")

    def test_setting_applies_theme(self):
        """safe_theme возвращает светлую тему, битое значение - дефолт."""
        self.assertEqual(themes.normalize(brand.THEME_ID), brand.THEME_ID)
        self.assertFalse(themes.safe_theme(brand.THEME_ID)["dark"])
        self.assertEqual(themes.normalize("coolors-plus"), themes.DEFAULT_THEME)
        self.assertEqual(themes.css_vars("coolors-x"), themes.css_vars(themes.DEFAULT_THEME))


if __name__ == "__main__":
    unittest.main(verbosity=2)
