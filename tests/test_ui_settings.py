"""Страж: настройки (DESIGN-UI шаг 6, §3.5).

Было (замена №22): один длинный список секций — десятки полей подряд, без
группировки по смыслу и без вторичного слоя; подпись поля в жёстких 260 px
(русский расширяется на 10–20 % и обрезается); хинт --fs-cap на --surface
(мелкий текст вне AA-размера); bool — галка, для AT это checkbox, а не
переключатель; числовое поле не читает хинт.

Что проверяет тест:
  * секции-карточки: --surface + граница + shadow-1 + отступы на шкале,
    заголовок --fs-title (не --fs-body);
  * поля на minmax(220px,1fr) minmax(240px,1fr) вместо 260 px; на узком
    экране колонка схлопывается в одну;
  * хинты и заметки секций на --fs-sm (не --fs-cap); контролы на --ctl-h-lg;
  * bool в настройках — switch: role="switch" + aria-checked в markup,
    синхрония aria-checked с checked в bindSettings (включение и сброс),
    visual track/knob на токенах (--r-full, --dur-fast-01, --accent);
  * числовое поле ссылается на хинт через aria-describedby, хинт имеет
    живой id (hint-<key>), id существует только когда хинт есть;
  * progressive disclosure: mesh и обход/PO-токены уходят в <details>
    «Дополнительно» (summary через словарь секций), раскрытие — по факту
    отказа выкачки (state.stats.by_status.failed), не выдумано;
  * i18n: «Дополнительно» переводится (sec.extra: Advanced);
  * галки диалогов (.check) остаются галками — это выбор, не переключатель;
  * нейтральные хинты схемы: имена/домены взрослой площадки не возвращаются.
"""

import re
import unittest
from pathlib import Path

from app import i18n, settings_schema

UI = Path(__file__).resolve().parent.parent / "ui_src"
CSS = UI / "app.css"
JS = UI / "app.js"

NO_NAME = re.compile(r"PornHub|PORNHUB|Pornhub|pornhub\.com|pornhub\.org|pornhub\.net")


def _clean(text: str) -> str:
    """Файл без комментариев: комментарии объясняют ловушки и не код."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return text


def css_rule(css: str, selector: str) -> str:
    """Тело правила для точного селектора (первое совпадение)."""
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    if not m:
        raise AssertionError(f"нет правила {selector}")
    return m.group(1)


class TestSettingsLayout(unittest.TestCase):
    def setUp(self):
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.js = _clean(JS.read_text(encoding="utf-8"))

    # ------------------------------ секции-карточки ------------------------------

    def test_sections_are_cards_with_token_spacing(self):
        sec = css_rule(self.css, ".set-section")
        self.assertIn("var(--surface)", sec)
        self.assertIn("var(--shadow-1)", sec, "секция без elevation — плоский список")
        self.assertIn("var(--s-5)", sec, "отступ секции снова литералом")
        h3 = css_rule(self.css, ".set-section > h3")
        self.assertIn("var(--fs-title)", h3, "заголовок секции снова --fs-body")
        self.assertNotIn("var(--fs-body)", h3)

    def test_field_grid_is_minmax_not_260(self):
        field = css_rule(self.css, ".field")
        self.assertIn("minmax(220px, 1fr)", field)
        self.assertIn("minmax(240px, 1fr)", field)
        self.assertNotIn("260px", field, "жёсткие 260 px вернулись")
        # На узком экране колонка схлопывается в одну (media-правило ниже).
        self.assertRegex(self.css, r"(?m)\.field\s*\{\s*grid-template-columns:\s*1fr;")

    def test_hints_are_fs_sm_on_surface(self):
        hint = css_rule(self.css, ".field-hint")
        self.assertIn("var(--fs-sm)", hint, "хинт снова на --fs-cap мелком")
        self.assertNotIn("var(--fs-cap)", hint)
        note = css_rule(self.css, ".set-section > .set-note")
        self.assertIn("var(--fs-sm)", note)

    def test_settings_controls_are_large_targets(self):
        for selector in (".field-control input[type=\"text\"]",
                         ".field-control input[type=\"number\"]",
                         ".field-control .btn"):
            rule = re.search(re.escape(selector) + r"[^{]*\{([^}]*)\}", self.css)
            self.assertTrue(rule, f"нет правила {selector}")
            self.assertIn("min-height: var(--ctl-h-lg)", rule.group(1),
                          f"{selector}: контрол снова компактный")


class TestSwitchAndNumbers(unittest.TestCase):
    def setUp(self):
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.js = _clean(JS.read_text(encoding="utf-8"))

    def test_bool_renders_as_switch(self):
        bool_block = re.search(r'if \(field\.type === "bool"\)([\s\S]*?)\n    \}', self.js)
        self.assertTrue(bool_block, "ветка bool в fieldHtml пропала")
        body = bool_block.group(1)
        self.assertIn('class="switch"', body, "галка вернулась в настройки")
        self.assertIn('role="switch"', body)
        self.assertIn('aria-checked', body, "switch без aria-checked — AT молчит")
        self.assertIn('type="checkbox"', body, "native checkbox снят: bindSettings слепнет")

    def test_aria_checked_syncs_with_checked(self):
        sync = re.search(r"function syncSwitch\(([^)]*)\)([\s\S]*?)\n          \}", self.js)
        self.assertTrue(sync, "syncSwitch удалён")
        body = sync.group(2)
        self.assertIn('getAttribute("role")', body)
        self.assertIn('setAttribute("aria-checked"', body)
        self.assertIn("el.checked", body)
        # Синхрония вызывается на change и на сбросе после отказа.
        self.assertRegex(self.js, r"syncSwitch\(input\);\s*\n\s*var key|var value[\s\S]*syncSwitch\(input\)")
        self.assertGreater(self.js.count("syncSwitch(input)"), 2,
                           "сброс после отказа не синхронизирует aria-checked")

    def test_switch_visual_uses_tokens(self):
        sw = re.search(r"\.switch input\s*\{([^}]*)\}", self.css)
        self.assertTrue(sw, "нет стиля track переключателя")
        self.assertIn("var(--r-full)", sw.group(1))
        self.assertIn("var(--line-strong)", sw.group(1), "граница track не различима")
        knob = re.search(r"\.switch input::after\s*\{([^}]*)\}", self.css)
        self.assertTrue(knob, "нет knob переключателя")
        self.assertIn("var(--dur-fast-01)", knob.group(1), "knob без перехода из токена")
        checked = re.search(r"\.switch input:checked::after\s*\{([^}]*)\}", self.css)
        self.assertTrue(checked, "checked-knob не смещается")
        self.assertIn("translateX", checked.group(1))

    def test_number_field_reads_hint(self):
        int_block = re.search(r'else if \(field\.type === "int"\)([\s\S]*?)\n    \}', self.js)
        self.assertTrue(int_block, "ветка int в fieldHtml пропала")
        self.assertIn('aria-describedby="', int_block.group(1),
                      "числовое поле снова глухо к хинту")
        self.assertRegex(self.js, r'function hintId\(field\)[\s\S]*"hint-" \+ field\.key')
        self.assertIn('(hid ? \' id="\' + hid', self.js,
                      "блок хинта без id — ссылка aria-describedby ведёт в никуда")

    def test_hint_id_only_when_hint_exists(self):
        # hid = hint ? hintId(field) : "" — пустой хинт не даёт мёртвый id.
        self.assertRegex(self.js, r'var hid = hint \? hintId\(field\) : ""')

    def test_dialog_checkboxes_stay_checkboxes(self):
        self.assertRegex(self.css, r"(?m)^\.check\s*\{")
        self.assertIn('id="detail-watched"', self.js)   # галка карточки не switch


class TestProgressiveDisclosure(unittest.TestCase):
    def setUp(self):
        self.js = _clean(JS.read_text(encoding="utf-8"))

    def test_rare_fields_move_into_extra_details(self):
        keys = re.search(r"var EXTRA_KEYS = \[([\s\S]*?)\];", self.js)
        self.assertTrue(keys, "список редких полей удалён")
        for key in ("bypass_enabled", "bypass_max_attempts",
                    "bot_token_enabled", "bot_token", "impersonate"):
            self.assertIn(key, keys.group(1), f"{key}: вернулся в основной список")
        self.assertRegex(self.js, r'var EXTRA_SECTION = "Mesh"')
        self.assertRegex(self.js, r'isExtraField\(field\)')
        self.assertRegex(self.js, r'<details class="set-section set-extra"')
        self.assertRegex(self.js, r"<summary>")
        self.assertRegex(self.js, r'state\.sections\[EXTRA_NAME\] \|\| EXTRA_NAME')

    def test_extra_open_is_real_failure_not_invented(self):
        # Раскрытие — по failed из снимка poll (тот же источник, что счётчики).
        self.assertRegex(self.js, r"var failed = \(state\.stats && state\.stats\.by_status")
        self.assertRegex(self.js, r"by_status\.failed")
        self.assertRegex(self.js, r'failed > 0 \? " open" : ""')
        self.assertRegex(self.js, r"state\.stats = stats;")

    def test_extra_sections_keep_schema_fields_intact(self):
        # Secondary слой не меняет ключи и схему: те же поля, те же ключи.
        keys = {f["key"] for f in settings_schema.schema()}
        self.assertTrue({"bypass_enabled", "bot_token", "mesh_enabled"} <= keys,
                        "ключи схемы исчезли — вторичный слой не право на них")
        # В основном списке renderSettings редкие поля не рисуются.
        self.assertRegex(self.js, r"if \(isExtraField\(field\)\) \{ extraFields\.push\(field\); return; \}")


class TestI18nAndNeutrality(unittest.TestCase):
    def test_extra_section_translation(self):
        secs = i18n.sections("en")
        self.assertEqual(secs.get("Дополнительно"), "Advanced",
                         "«Дополнительно» без перевода — EN окно русское")
        secs_ru = i18n.sections("ru")
        self.assertEqual(secs_ru.get("Дополнительно"), "Дополнительно")

    def test_schema_hints_stay_neutral(self):
        blob = "\n".join(
            str(f.get("hint", "")) + " " + str(f.get("label", ""))
            for f in settings_schema.schema())
        self.assertFalse(NO_NAME.search(blob), "имя/домен взрослой площадки в хинтах схемы")
        self.assertFalse(NO_NAME.search(CSS.read_text(encoding="utf-8") +
                        JS.read_text(encoding="utf-8")),
                        "имя/домен взрослой площадки в CSS/JS настроек")


if __name__ == "__main__":
    unittest.main()
