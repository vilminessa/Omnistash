"""Страж: веяния (DESIGN-UI шаг 7, §4).

Правила, которые проверяет тест:
  * переходы только из токенов §2.7: ни одного литерала длительности или
    easing в shorthand `transition:` (кроме обнуления в reduced-блоке,
    где это служебное `transition-duration: 0.01ms !important`);
  * анимации CSS-only и только два keyframes: pulse (цикл-индикатор
    статуса) и cat-sweep (недетерминированная полоса) — новые запрещены;
  * prefers-reduced-motion глобально (§4.3): все переходы и циклы гасятся,
    стартовые состояния входа (overlay.anim-in, toast.enter) не прячут
    контент, hover без движения, :active без transform;
  * микровзаимодействия по компонентам (§4.2): кнопка/иконка/таб/дерево/
    сегмент/переключатель - fast-01, строка и плитка - fast-02, диалог -
    veil slow-02 + карточка slow-01 expressive, тост - вход moderate-02
    expressive, уход только opacity fast-02 ease-exit (асимметрия);
  * JS: опрос не порождает анимацию - classList.add ровно в трёх местах
    (тост enter/gone, диалог anim-in); тост читается AT (role статус/alert),
    при prefers-reduced-motion тост убирается сразу без таймаута перехода;
  * разметка: статус-строка и тост - live-регионы (§7 замена №17).
"""

import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui_src"
CSS = UI / "app.css"
JS = UI / "app.js"
HTML = UI / "index.html"


def _clean(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def css_reduced_block(css: str) -> str:
    """Тело блока prefers-reduced-motion (§4.3)."""
    m = re.search(r"@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\n\}", css)
    if not m:
        raise AssertionError("нет блока prefers-reduced-motion")
    return m.group(1)


class TestTransitionTokens(unittest.TestCase):
    def setUp(self):
        self.css = _clean(CSS.read_text(encoding="utf-8"))

    def test_all_transitions_use_tokens(self):
        decls = re.findall(r"(?m)^\s*transition:\s*([^;]+);", self.css)
        self.assertGreaterEqual(len(decls), 12,
                                "переходы потеряны — состояния без веяния")
        for d in decls:
            self.assertIn("var(--dur-", d, f"литерал длительности в переходе: {d}")
            self.assertIn("var(--ease-", d, f"литерал easing в переходе: {d}")

    def test_only_the_two_allowed_keyframes(self):
        names = re.findall(r"@keyframes\s+([A-Za-z0-9_-]+)\s*\{", self.css)
        self.assertEqual(sorted(set(names)), ["cat-sweep", "pulse"],
                         "новый @keyframes — шаг 7 запрещает (§8 шаг 7)")

    def test_cycles_are_the_two_indicators_only(self):
        anims = re.findall(r"(?m)^\s*animation:\s*([^;]+);", self.css)
        for a in anims:
            self.assertTrue("pulse" in a or "cat-sweep" in a,
                            f"цикл без токена-индикатора: {a}")


class TestMicroInteractions(unittest.TestCase):
    def setUp(self):
        self.css = _clean(CSS.read_text(encoding="utf-8"))

    def rule(self, selector):
        # Селектор с начала строки: внутри :where(...) тот же класс стоит в
        # списке и забрал бы чужое правило (focus-ring вместо перехода).
        m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertTrue(m, f"нет правила {selector}")
        return m.group(1)

    def test_fast_controls(self):
        for sel in (".btn", ".icon-btn", ".tab", ".tree-item", ".seg-btn"):
            body = self.rule(sel)
            self.assertIn("var(--dur-fast-01)", body,
                          f"{sel}: hover/press без fast-01 (§4.2)")

    def test_list_rows_and_tiles(self):
        row = self.rule(".row")
        self.assertIn("background var(--dur-fast-02)", row, "строка без fast-02")
        self.assertNotIn("box-shadow", row, "строка виртуального окна анимирует тень (§4.1)")
        tile = self.rule(".tile")
        self.assertIn("var(--dur-fast-02)", tile)
        self.assertIn("border-color", tile)

    def test_dialog_enter_and_toast_asymmetry(self):
        ov = self.rule(".overlay")
        self.assertIn("opacity var(--dur-slow-02) var(--ease-expressive)", ov)
        card = self.rule(".card")
        self.assertIn("var(--dur-slow-01) var(--ease-expressive)", card)
        self.assertIn("transform", card, "карточка входит без смещения (§4.2)")
        start = re.search(r"\.overlay\.anim-in[^{]*\{([^}]*)\}", self.css)
        self.assertTrue(start, "нет стартового состояния входа диалога")
        self.assertIn("transition: none", start.group(1),
                      "anim-in анимирует обратный ход 1→0")
        self.assertRegex(self.css, r"\.overlay\.anim-in \.card\s*\{[^}]*translateY\(8px\)")
        toast = self.rule(".toast")
        self.assertIn("opacity var(--dur-fast-02) var(--ease-exit)", toast,
                      "уход тоста не ease-exit (асимметрия потеряна)")
        self.assertIn("var(--dur-moderate-02) var(--ease-expressive)", toast)
        enter = re.search(r"\.toast\.enter\s*\{([^}]*)\}", self.css)
        self.assertTrue(enter, "нет стартового состояния тоста")
        self.assertIn("transition: none", enter.group(1))
        self.assertIn("translateY(-4px)", enter.group(1))
        self.assertIn("translateX(-50%)", enter.group(1), "enter ломает центровку тоста")
        gone = re.search(r"\.toast\.gone\s*\{([^}]*)\}", self.css)
        self.assertTrue(gone, "нет состояния ухода тоста")
        self.assertIn("opacity: 0", gone.group(1))
        self.assertNotIn("transform", gone.group(1), "уход тоста двигает его (§4.2)")


class TestReducedMotion(unittest.TestCase):
    def setUp(self):
        self.css = _clean(CSS.read_text(encoding="utf-8"))

    def test_global_reduced_block(self):
        block = re.search(r"@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\n\}", self.css)
        self.assertTrue(block, "блок reduced-motion удалён")
        body = block.group(1)
        self.assertRegex(body, r"\*, \*::before, \*::after\s*\{")
        for fact in ("transition-duration: 0.01ms !important",
                     "animation-duration: 0.01ms !important",
                     "animation-iteration-count: 1 !important"):
            self.assertIn(fact, body, f"reduced-motion не гасит {fact}")
        self.assertIn(".scan-fill.indet { animation: none", body)
        self.assertIn(".status[data-s=\"downloading\"]::before", body)
        self.assertIn(".btn:active { transform: none", body)
        self.assertIn(".tile:hover, .row:hover", body)
        self.assertIn(".star:hover { transform: none", body)

    def test_no_hover_or_press_transform_escapes_reduced_block(self):
        # Любое движение по наведению/нажатию (§4.3: hover без движения,
        # :active без transform). Центрирующие translate(-50%) — не движение.
        centering = ("translateX(-50%)", "translate(-50%, -50%)")
        for m in re.finditer(r"(?m)^([^{}]+)\{([^}]*)\}", self.css):
            sel, body = m.group(1).strip(), m.group(2)
            if ":hover" not in sel and ":active" not in sel:
                continue
            if "transform:" not in body:
                continue
            if all(c in body for c in centering):
                continue
            base = sel.split(":not")[0]
            self.assertIn(base, css_reduced_block(self.css),
                          f"{sel}: движение не гасится в reduced-блоке")

    def test_enter_states_visible_without_motion(self):
        body = css_reduced_block(self.css)
        self.assertRegex(body, r"\.overlay\.anim-in, \.overlay\.anim-in \.card \{ opacity: 1")
        self.assertRegex(body, r"\.toast\.enter \{ opacity: 1")
        self.assertRegex(body, r"\.toast \{ transform: translateX\(-50%\); \}")


class TestJsMotion(unittest.TestCase):
    def setUp(self):
        self.js = _clean(JS.read_text(encoding="utf-8"))

    def test_poll_does_not_spawn_animation(self):
        # Анимационные классы - ровно по одному месту: тост (enter/gone)
        # и диалог (anim-in). Остальной classList.add - состояние по клику
        # человека («active» у сегмента), опрос его не ставит.
        for cls in ("enter", "gone", "anim-in"):
            self.assertEqual(self.js.count(f'classList.add("{cls}")'), 1,
                             f"анимационный класс {cls}: не одно место")
        self.assertEqual(self.js.count("classList.add"), 4,
                         "новый анимационный класс появился в рендере опроса (§4.2)")

    def test_toast_reads_live_and_exit_sequence(self):
        toast = re.search(r"function toast\(([\s\S]*?)\n  \}", self.js)
        self.assertTrue(toast, "toast не найден")
        body = toast.group(1)
        self.assertIn('setAttribute("role", isError ? "alert" : "status")', body,
                      "тост без live-роли (замена №17)")
        self.assertIn("requestAnimationFrame", body)
        self.assertIn("motionReduced()", body, "reduced-motion: тост ждёт переход")
        self.assertIn('classList.add("gone")', body)
        self.assertRegex(body, r"el\.hidden = true")

    def test_reduced_motion_from_match_media(self):
        # Строка CSS-media внутри JS: скобки в самом имени feature.
        self.assertRegex(self.js, r'matchMedia\("\(prefers-reduced-motion: reduce\)"\)')

    def test_dialog_enter_in_watcher(self):
        watcher = re.search(r"var dialogWatcher = new MutationObserver\([\s\S]*?\n  \}\);", self.js)
        self.assertTrue(watcher, "наблюдатель диалогов удалён")
        body = watcher.group(0)
        self.assertIn('classList.add("anim-in")', body)
        self.assertIn("requestAnimationFrame", body)
        # Стек и клавиатура шага 5 не потеряны.
        for fact in ("dialogs.push", "dialogEnter(overlay)", "dialogLeave("):
            self.assertIn(fact, body, f"шаг 5 потерялся в watcher: {fact}")


class TestMarkupLiveRegions(unittest.TestCase):
    def setUp(self):
        self.html = _clean(HTML.read_text(encoding="utf-8"))

    def test_status_and_toast_are_live(self):
        status = re.search(r'<div class="foot-status" id="status"[^>]*>', self.html)
        self.assertTrue(status, "статус-строка пропала")
        self.assertIn('role="status"', status.group(0))
        self.assertIn('aria-live="polite"', status.group(0))
        toast = re.search(r'<div id="toast"[^>]*>', self.html)
        self.assertTrue(toast, "тост пропал из разметки")
        self.assertIn('aria-live="polite"', toast.group(0))
        self.assertIn('role="status"', toast.group(0))

    def test_sel_count_live_stays(self):
        self.assertIn('id="sel-count" aria-live="polite"', self.html)


if __name__ == "__main__":
    unittest.main()
