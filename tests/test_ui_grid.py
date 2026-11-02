"""Страж: семантика и клавиатура списка библиотеки (DESIGN-UI шаг 2).

Поведение в браузере офлайн не проверить полностью, поэтому держим
наличие фактов разметки и обработчика: их удаление или «упрощение»
станет красным тестом, а не ночным баг-репортом.

Что проверяет тест:
  * грид-семантика: контейнер role="grid", ряды role="row" с tabindex="-1"
    (обход стрелками, контейнер - единственная точка Tab), ячейки
    role="gridcell"/"columnheader", aria-sort на самом заголовке колонки
    (не на вложенной «кнопке»), aria-selected/posinset/setsize на строках
    (размер списка читается AT даже при виртуализации);
  * клавиатура строк: ↑↓/Home/End перемещают фокус, Enter - карточка,
    Ctrl+Enter - плеер, Space - выбор без прокрутки;
  * перерисовка окна прокрутки возвращает фокус того же ряда (иначе
    клавиатурный обход обрывается на каждом скролле);
  * CSS: шапка колонок в обычном регистре (замена №9), активная колонка -
    полоса --accent вместе с aria-sort (SC 1.4.1 - не только цвет),
    выбранная строка - тон --selected + левая полоса, ряды списков держат
    content-visibility + contain-intrinsic-size (пропускают отрисовку вне
    экрана и оставляют строку в accessibility tree), числа - var(--nums).
"""

import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui_src"
CSS = UI / "app.css"
JS = UI / "app.js"
HTML = UI / "index.html"


def _clean(text: str) -> str:
    """Файл без комментариев: комментарии объясняют ловушки и не код."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


class TestGridSemantics(unittest.TestCase):
    def setUp(self):
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.js = _clean(JS.read_text(encoding="utf-8"))
        self.html = _clean(HTML.read_text(encoding="utf-8"))

    # ------------------------------ разметка ------------------------------

    def test_container_is_a_grid(self):
        self.assertIn('role="grid"', self.html)
        self.assertRegex(self.html, r'role="grid"[^>]*aria-label="')
        self.assertIn('role="rowgroup"', self.html)
        self.assertRegex(self.html, r'class="grid-head"[^>]*role="row"')
        # Контейнер остаётся точкой Tab (как был до шага 2).
        self.assertRegex(self.html, r'id="grid-body"[^>]*tabindex="0"')

    def test_rows_are_focusable_with_selection_state(self):
        row = re.search(r"function rowHtml\([^)]*\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(row, "rowHtml не найден")
        body = row.group(1)
        # Строки - tabindex="-1": обходятся стрелками, не Tab'ом (120+
        # точек на окно списка были бы регрессией навигации).
        self.assertIn('role="row"', body)
        self.assertIn('tabindex="-1"', body)
        self.assertIn('aria-selected="', body)
        self.assertRegex(body, r'\(picked \? "true" : "false"\)',
                         "aria-selected строки должен следовать за picked")
        self.assertIn('aria-posinset="', body)
        self.assertIn('aria-setsize="', body)
        self.assertIn('role="gridcell"', body)
        self.assertIn('role="columnheader"', self.js)

    def test_sort_header_is_columnheader_with_aria_sort(self):
        # aria-sort на самой ячейке-заголовке, а не на role="button":
        # AT читает состояние сортировки колонки (замена №15).
        self.assertNotIn('role="button"', self.js,
                         'заголовок колонки снова role="button" - aria-sort '
                         'перестанет описывать колонку')
        head = re.search(r"function renderGridHead\(\)(.*?)\n  }",
                         self.js, re.DOTALL)
        self.assertTrue(head, "renderGridHead не найден")
        self.assertIn('" tabindex="0"', head.group(1))
        self.assertIn('aria-sort="', head.group(1))

    def test_toggle_keeps_aria_selected(self):
        # Чекбокс и Space меняют тон строки; aria-selected обновляется там же.
        self.assertRegex(
            self.js,
            r'node\.setAttribute\("aria-selected", on \? "true" : "false"\)')

    # ------------------------------ клавиатура ------------------------------

    def test_row_keyboard_navigation(self):
        handler = re.search(r'\$\("grid-body"\)\.addEventListener\("keydown"'
                            r"[^{]*\{(.+?)\n    \}\);", self.js, re.DOTALL)
        self.assertTrue(handler, "обработчик клавиатуры строк удалён")
        body = handler.group(1)
        for fact in ("ArrowDown", "ArrowUp", '"Home"', '"End"',
                     '"Enter"', "ctrlKey", '" '):
            self.assertIn(fact, body, f"клавиатура строк потеряла {fact}")
        self.assertIn("openDetail(id)", body)
        self.assertIn("playVideo(id)", body)
        self.assertIn("toggleSelected(id, on, row)", body)
        # Чекбокс внутри строки: Space/Enter нативные, не перехватываются.
        self.assertIn('.row-check', body)

    def test_focus_survives_repaint(self):
        # Виртуализация заменяет innerHTML окна: без возврата фокуса того же
        # ряда клавиатурный обход обрывается на каждом скролле.
        paint = re.search(r"function paintGrid\(\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(paint, "paintGrid не найден")
        self.assertIn("document.activeElement", paint.group(1))
        self.assertRegex(paint.group(1), r"rows\.querySelector\('\.row\[data-i")
        self.assertIn("back.focus()", paint.group(1))

    def test_row_height_from_css_token(self):
        # Виртуализация считает по --row-h (замена №23): getComputedStyle для
        # custom property отдаёт подстановку var(), а не вычисление max()/calc(),
        # поэтому px меряем пробником с тем же var() - прежний parseFloat
        # на "max(34px, ...)" всегда давал NaN и large-шрифт ездил по 34 px.
        rh = re.search(r"function rowHeight\(\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(rh, "rowHeight не найден")
        self.assertIn('"var(--row-h)"', rh.group(1),
                      "rowHeight перестал мерить --row-h из CSS")
        self.assertIn("rowHeight._px = 0", self.js,
                      "applyChrome должен сбрасывать мерку при смене шрифта")

    # ------------------------------ CSS ------------------------------

    def test_head_is_normal_case_and_active_column_marked(self):
        head = re.search(r"\.grid-head\s*\{([^}]*)\}", self.css)
        self.assertTrue(head, "нет правила .grid-head")
        self.assertNotIn("text-transform", head.group(1),
                         "шапка колонок вернулась в ALL CAPS (замена №9)")
        self.assertRegex(
            self.css,
            r"\.grid-head \[data-col\]\[aria-sort\][^{}]*\{"
            r"[^}]*box-shadow[^}]*inset 0 -2px[^}]*var\(--accent\)")
        # Сортируемый заголовок различим как контрол: --text, hover --accent-2.
        self.assertRegex(self.css,
                         r"\.grid-head \[data-col\][^{]*\{[^}]*color: var\(--text\)")
        self.assertRegex(self.css,
                         r"\.grid-head \[data-col\]:hover[^{}]*\{[^}]*var\(--accent-2\)")

    def test_picked_row_has_accent_bar(self):
        # Состояние «выбрано» видно без цвета: тон + левая полоса 3 px.
        self.assertRegex(self.css,
                         r"\.row\.picked[^{}]*\{[^}]*var\(--selected\)"
                         r"[^}]*inset 3px 0 0 var\(--accent\)")

    def test_rows_skip_offscreen_paint(self):
        # (?m)^ - одиночный блок .row: список-селектор ".grid-head, .row"
        # тоже содержит ".row {", но там нет content-visibility.
        row = re.search(r"(?m)^\.row\s*\{([^}]*)\}", self.css)
        self.assertTrue(row, "нет правила .row")
        self.assertIn("content-visibility: auto", row.group(1),
                      "ряды вне экрана снова отрисовываются целиком")
        self.assertIn("contain-intrinsic-size", row.group(1),
                      "без intrinsic-размера пропущенный ряд схлопнется")
        self.assertIn("var(--row-h)", row.group(1))

    def test_numbers_are_tabular(self):
        # Счётчики и даты не «пляшут» при прокрутке и сортировке.
        self.assertRegex(
            self.css,
            r"\.row \.c-dur[^{]*\{[^}]*font-variant-numeric: var\(--nums\)")
        self.assertRegex(self.css, r"#sel-count[^{]*\{[^}]*var\(--nums\)")


if __name__ == "__main__":
    unittest.main()
