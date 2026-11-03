"""Страж: панель мультивыбора (DESIGN-UI шаг 4).

Полоса `sel-bar` - девять целей над списком: порядок, размеры и разрушительные
диалоги проверяются по фактам разметки/кода, иначе «упрощение» вернёт промахи
по кнопкам и removal без подтверждения.

Что проверяет тест:
  * полоса - role="toolbar" с aria-label, счётчик aria-live="polite"
    (AT объявляет «Выбрано: N» при мультивыборе; aria-hidden на полосе
    ставить нельзя);
  * порядок целей фиксирован: групповые операции -> primary «Скачать» ->
    «Снять выбор»; разрушительное «Удалить папку» - последней справа и с
    --err-границей (.danger), не в одном ряду с обычными;
  * CSS: граница --line-strong + --shadow-2 (полоса - отдельный слой),
    цели --target с min-width, --gap-cta между соседними, select на
    min-width вместо прежнего max-width 280px (замена №21);
  * токены --target/--target-min/--ctl-h-lg/--gap-cta объявлены;
  * разрушительные операции (delete папки, purge связей) подтверждаются
    диалогом приложения, где названо N, и не трогают native confirm;
    отказ - дефолт (accept disabled до галки в legalDialog).
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
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


class TestSelBar(unittest.TestCase):
    def setUp(self):
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.js = _clean(JS.read_text(encoding="utf-8"))
        self.html = _clean(HTML.read_text(encoding="utf-8"))

    # ------------------------------ разметка ------------------------------

    def test_bar_is_toolbar_with_live_count(self):
        self.assertRegex(self.html,
                         r'id="sel-bar"[^>]*role="toolbar"',
                         "полоса выделения перестала быть toolbar для AT")
        self.assertRegex(self.html, r'id="sel-bar"[^>]*aria-label="')
        self.assertRegex(self.html, r'id="sel-count"[^>]*aria-live="polite"',
                         "счётчик перестал объявляться AT")
        # aria-hidden на полосе не ставим: AT читает счётчик (замена №21).
        self.assertNotRegex(self.html, r'id="sel-bar"[^>]*aria-hidden')

    def test_button_order_locked(self):
        bar = re.search(r'<div class="sel-bar"[\s\S]*?</div>', self.html)
        self.assertTrue(bar, "блок sel-bar не найден")
        body = bar.group(0)
        order = ["sel-collect", "sel-collect-remove", "sel-collect-rename",
                 "sel-mark", "sel-move", "sel-bulk", "sel-download",
                 "sel-clear", "sel-collect-delete"]
        positions = [body.index('id="' + id_ + '"') for id_ in order]
        self.assertTrue(all(p >= 0 for p in positions), "кнопка полосы пропала")
        self.assertEqual(positions, sorted(positions),
                         "порядок кнопов полосы поехал: групповые -> «Скачать»"
                         " -> «Снять выбор» -> разрушительное последним")

    def test_destructive_is_last_and_marked(self):
        self.assertRegex(self.html,
                         r'class="btn danger" id="sel-collect-delete"',
                         "«Удалить папку» вернулась в ряд обычных без --err")
        self.assertRegex(self.html,
                         r'id="sel-collect-delete"[^>]*hidden')

    # ------------------------------ CSS ------------------------------

    def test_tokens_declared(self):
        self.assertRegex(self.css, r"--target:\s*44px")
        self.assertRegex(self.css, r"--target-min:\s*24px")
        self.assertRegex(self.css, r"--ctl-h-lg:\s*40px")
        self.assertRegex(self.css, r"--gap-cta:\s*8px")

    def test_bar_layer_and_targets(self):
        bar = re.search(r"(?m)^\.sel-bar\s*\{([^}]*)\}", self.css)
        self.assertTrue(bar, "нет правила .sel-bar")
        body = bar.group(1)
        self.assertIn("border: 1px solid var(--line-strong)", body,
                      "полоса снова с акцентной границей вместо границы контрола")
        self.assertIn("box-shadow: var(--shadow-2)", body,
                      "без --shadow-2 полоса не читается слоем над контентом")
        self.assertIn("gap: var(--gap-cta)", body,
                      "соседние цели снова впритык (замена №21)")
        self.assertRegex(self.css,
                         r"\.sel-bar \.btn[^{}]*\{[^}]*min-height: var\(--target\)")
        self.assertRegex(self.css, r"\.sel-bar \.btn[^{}]*\{[^}]*min-width")

    def test_storage_select_grows(self):
        sel = re.search(r"\.sel-bar select\s*\{([^}]*)\}", self.css)
        self.assertTrue(sel, "нет правила .sel-bar select")
        self.assertIn("min-width", sel.group(1),
                      "select вернулся на max-width: подпись обрезается")
        self.assertNotIn("max-width", sel.group(1))
        self.assertIn("var(--ctl-h-lg)", sel.group(1))

    # ------------------------------ диалоги ------------------------------

    def test_folder_delete_dialog_names_N(self):
        handler = re.search(r'\$\("sel-collect-delete"\)\.addEventListener\("click"'
                            r"[^{]*\{(.+?)\n    \}\);", self.js, re.DOTALL)
        self.assertTrue(handler, "обработчик «Удалить папку» удалён")
        body = handler.group(1)
        self.assertIn("legalDialog(", body,
                      "native confirm не называет N и не имеет дефолта-отказа")
        self.assertNotIn("confirm(", body)
        self.assertIn("pluralRows(", body, "диалог не называет число строк")
        self.assertIn("collection_delete", body)
        # Файлы не трогаются - диалог говорит об этом прямо.
        self.assertIn("файлы на диске", body)

    def test_purge_dialog_names_N(self):
        purge = re.search(r"function purgeCollection\(\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(purge, "purgeCollection не найден")
        body = purge.group(1)
        self.assertIn("legalDialog(", body)
        self.assertNotIn("confirm(", body)
        self.assertIn("pluralRows(", body)
        self.assertIn('runCollect("remove", null)', body)
        self.assertIn("файлы на диске", body)

    def test_legal_dialog_default_decline(self):
        # Дефолт разрушительного диалога - отказ: accept disabled до галки.
        dlg = re.search(r"function legalDialog\([^)]*\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(dlg, "legalDialog не найден")
        self.assertIn("accept.disabled = true", dlg.group(1))
        self.assertIn("accept.disabled = !check.checked", dlg.group(1))


if __name__ == "__main__":
    unittest.main()
