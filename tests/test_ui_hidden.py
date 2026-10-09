"""Страж: глобальное правило [hidden] должно оставаться в CSS.

Почему оно нужно: правило [hidden]{display:none} живёт в UA-стилях
браузера, а любой авторский display (класс .empty{display:flex} и
т.п.) его перебивает. Без глобального правила «скрытые» блоки остаются
на экране поверх содержимого: заглушка «Источников пока нет» лежала на
таблице источников, полосы прогресса и заметки не убирались, панель
выделения висела всегда. Точечные исключения (.panel[hidden] и подобные)
решали часть случаев и прятали проблему.

Поведение в браузере офлайн не проверить, поэтому держим факт наличия
правила - его удаление станет красным тестом, а не ночным баг-репортом.
"""

import unittest
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "ui_src" / "app.css"


class TestHiddenWinsOverDisplay(unittest.TestCase):
    def setUp(self):
        self.text = CSS.read_text(encoding="utf-8")

    def test_global_rule_is_present(self):
        import re
        self.assertRegex(
            self.text,
            r"(?m)^\[hidden\]\s*\{\s*display:\s*none\s*!important;\s*\}",
            "глобальное [hidden] { display: none !important; } удалено или "
            "испорчено: скрытые блоки вернутся на экран поверх контента")

    def test_no_point_hidden_rules_left(self):
        # Точечные правила (вроде .overlay[hidden]) создавали иллюзию
        # решения - глобальное правило их заменяет. Новое точечное -
        # только осознанно и с пониманием, зачем оно лучше общего.
        # Комментарии выкидываем: в них мы сами объясняем ловушку и
        # упоминаем [hidden] словами.
        import re
        code = re.sub(r"/\*.*?\*/", "", self.text, flags=re.DOTALL)
        leftovers = [line.strip() for line in code.splitlines()
                     if "[hidden]" in line
                     and not line.strip().startswith("[hidden]")]
        self.assertEqual(leftovers, [],
                         f"остались точечные hidden-правила: {leftovers}")


if __name__ == "__main__":
    unittest.main()
