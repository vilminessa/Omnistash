"""Страж: диалоги (DESIGN-UI шаг 5).

APG Dialog (Modal) (источник №29): имя через aria-labelledby/aria-label,
Tab по кругу внутри диалога, Escape закрывает, фокус входит в диалог и
возвращается на инициатора. Раньше (замена №6) диалоги имели только
aria-modal="true": AT объявлял безымянное модальное окно, Tab уходил в
затемнённый фон, Escape молчал.

Что проверяет тест:
  * каждый role="dialog" имеет aria-labelledby, и указанный id реально есть
    в разметке (имя диалога читается AT, а не выдумывается из класса);
  * ловушка фокуса и Escape ведятся по верхнему диалогу из стека
    (MutationObserver на hidden): стек растёт при вложении (legalDialog
    поверх «Операций…») и Tab работает по верхнему;
  * Tab замыкается внутри диалога (Shift+Tab с первой цели - на последнюю,
    Tab с последней - на первую), а если activeElement выпал из диалога
    (innerHTML перерисован во время открытого окна), фокус возвращается;
  * выход диалога - сначала отказ («-decline»/«-later», дефолт по §3.8),
    потом крестик; Escape кликает кнопку, а не ставит hidden сам: обработчик
    кнопки решает, что перенос/переупаковку при работающем процессе
    закрывать нельзя;
  * возврат фокуса не кидается на снятый узел (иначе focus() уходит на body);
  * разрушительное действие карточки (отзыв mesh-копии) идёт через диалог
    приложения с названным файлом, без native confirm;
  * CSS: ряд действий диалога на --ctl-h и --gap-cta, мета карточки на
    var(--nums), фон оверлея на --veil.
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


class TestDialogs(unittest.TestCase):
    def setUp(self):
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.js = _clean(JS.read_text(encoding="utf-8"))
        self.html = _clean(HTML.read_text(encoding="utf-8"))

    # ------------------------------ имя диалога ------------------------------

    def test_every_dialog_has_a_name(self):
        dialogs = re.findall(r"<div[^>]*role=\"dialog\"[^>]*>", self.html)
        self.assertGreaterEqual(len(dialogs), 10,
                                "диалоги в разметке пропали - AT не что объявлять")
        for d in dialogs:
            self.assertRegex(d, r'aria-modal="true"', "aria-modal снят: ловушка не объявлена")
            self.assertRegex(d, r'aria-labelledby="[^"]+"',
                             "диалог снова без имени (замена №6)")
        ids = set(re.findall(r'id="([^"]+)"', self.html))
        for d in dialogs:
            label = re.search(r'aria-labelledby="([^"]+)"', d).group(1)
            self.assertIn(label, ids,
                          "aria-labelledby указывает на id, которого нет в разметке")

    def test_dialog_titles_are_settable(self):
        # Имя меняет JS (legalDialog, detail-title) - заголовок должен быть тем
        # узлом, на который ссылается aria-labelledby.
        self.assertRegex(self.html, r'id="legal-title"')
        self.assertRegex(self.html, r'id="detail-title"')
        self.assertIn('$("legal-title").textContent = title', self.js)
        self.assertIn('$("detail-title").textContent', self.js)

    # ------------------------------ ловушка и Escape -------------------------

    def test_dialog_stack_follows_hidden_attribute(self):
        # Оверлеи открываются/закрываются атрибутом hidden (~20 мест), поэтому
        # стек ведётся наблюдателем за hidden, а не обходом мест открытия.
        self.assertRegex(self.js, r"new MutationObserver\(")
        self.assertRegex(self.js, r'attributeFilter:\s*\["hidden"\]')
        self.assertRegex(self.js, r'document\.querySelectorAll\("\.overlay"\)')
        self.assertRegex(self.js, r"dialogWatcher\.observe\(")

    def test_tab_trap_and_return_focus(self):
        # В app.js несколько keydown-слушателей (список, карточки каталога,
        # сегмент) - берём тот, что работает по верхнему диалогу.
        blocks = [m.group(0) for m in
                  re.finditer(r'document\.addEventListener\("keydown"[\s\S]*?\n  \}\);', self.js)
                  if "dialogTop()" in m.group(0)]
        self.assertEqual(len(blocks), 1, "слушатель клавиатуры диалогов удалён или задвоился")
        body = blocks[0]
        for fact in ('ev.key === "Escape"', 'ev.key !== "Tab"',
                     "dialogExit(top.overlay)", "ev.preventDefault()",
                     "exit.click()", "ev.shiftKey",
                     "top.overlay.contains(cur)", "list[0].focus()",
                     "last.focus()", "first.focus()"):
            self.assertIn(fact, body, f"клавиатура диалогов потеряла {fact}")
        # Escape не ставит hidden сам: закрытие решает обработчик кнопки
        # (перенос и переупаковка отказывают, пока процесс идёт).
        self.assertNotIn("hidden = true", body)

    def test_dialog_stack_push_and_pop(self):
        watcher = re.search(r"var dialogWatcher = new MutationObserver\([\s\S]*?\n  \}\);", self.js)
        self.assertTrue(watcher, "наблюдатель диалогов удалён")
        body = watcher.group(0)
        for fact in ("dialogs.push", "dialogs.splice", "dialogEnter(overlay)",
                     "dialogLeave(", "opener: document.activeElement"):
            self.assertIn(fact, body, f"наблюдатель диалогов потерял {fact}")

        enter = re.search(r"function dialogEnter\(([^)]*)\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(enter, "dialogEnter не найден")
        self.assertIn("target.focus()", enter.group(2),
                      "фокус не входит в диалог (SC 2.4.3)")

        leave = re.search(r"function dialogLeave\(([^)]*)\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(leave, "dialogLeave не найден")
        body = leave.group(2)
        self.assertIn("opener.focus()", body, "фокус не возвращается на инициатора")
        self.assertIn("document.body.contains(opener)", body,
                      "возврат фокуса на снятый узел - focus() уходит на body")
        self.assertIn("offsetParent", body)

    def test_decline_is_the_default_exit(self):
        exit_fn = re.search(r"function dialogExit\(([^)]*)\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(exit_fn, "dialogExit не найден")
        body = exit_fn.group(2)
        self.assertIn('[id$="-decline"], [id$="-later"]', body,
                      "отказ перестал быть выходом по умолчанию (§3.8)")
        self.assertIn('[id$="-close"]', body)
        self.assertRegex(self.js, r"function dialogEnter\([\s\S]*?exit\s+&&[\s\S]*\? exit",
                         "диалог открывается без фокуса на отказе")

    # ------------------------------ разрушительное в карточке ---------------

    def test_mesh_reject_uses_dialog_not_confirm(self):
        reject = re.search(r'action === "reject"([\s\S]*?)action === "source"', self.js)
        self.assertTrue(reject, "ветка отзыва mesh-копии не найдена")
        body = reject.group(1)
        self.assertIn("legalDialog(", body,
                      "native confirm не называет файл и не имеет дефолта-отказа")
        self.assertNotIn("confirm(", body)
        self.assertIn("mesh_reject", body)
        self.assertIn("файл удалится с диска", body)
        self.assertIn("Отзыв файла отменён", body)

    def test_public_texts_stay_neutral(self):
        blob = self.html + self.js
        self.assertFalse(re.search(r"PornHub|PORNHUB|Pornhub", blob),
                         "имя взрослой площадки в публичных текстах UI")
        self.assertFalse(re.search(r"pornhub\.com|pornhub\.org|pornhub\.net", blob),
                         "домены взрослой площадки в публичных текстах UI")

    # ------------------------------ CSS ------------------------------

    def test_dialog_action_row_targets(self):
        row = re.search(r"(?m)^\.card-actions\s*\{([^}]*)\}", self.css)
        self.assertTrue(row, "нет правила .card-actions")
        self.assertIn("gap: var(--gap-cta)", row.group(1),
                      "соседние цели ряда снова впритык")
        self.assertRegex(self.css,
                         r"\.card-actions \.btn[^{}]*\{[^}]*min-height: var\(--ctl-h\)")
        sel = re.search(r"\.card-actions select\s*\{([^}]*)\}", self.css)
        self.assertTrue(sel, "нет правила .card-actions select")
        self.assertIn("var(--ctl-h)", sel.group(1))
        self.assertIn("min-width", sel.group(1), "подпись хранилища обрезается")

    def test_meta_numbers_tabular(self):
        dd = re.search(r"(?m)^\.kv dd\s*\{([^}]*)\}", self.css)
        self.assertTrue(dd, "нет правила .kv dd")
        self.assertIn("var(--nums)", dd.group(1),
                      "мета карточки снова без tabular цифр (§3.6)")

    def test_veil_behind_dialog(self):
        overlay = re.search(r"(?m)^\.overlay\s*\{([^}]*)\}", self.css)
        self.assertTrue(overlay, "нет правила .overlay")
        self.assertIn("var(--veil)", overlay.group(1),
                      "фон диалога перестал затемнять контент")


if __name__ == "__main__":
    unittest.main()
