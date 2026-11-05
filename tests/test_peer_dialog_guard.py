"""Страж диалога «Участники» в ui_src/app.js: SAS-сверка и отказ не ослаблены.

Фасад (gui) сверён test_gui.py и test_mesh_net.py; здесь сверяется проводка
решения человека в интерфейсе: путь «показ -> окно сверки -> OK -> запись»
и отказ на каждой ветке с названной причиной. Ослабление любой из веток
(авто-запись без окна, молчаливый отказ, пропавший формат) меняет проверяемое
поведение, а не только внешний вид.

Проверяется:
  * у TOFU-устройства обе дороги подтверждения: сверка отпечатка
    («Отпечаток…») и выбор формата SAS (12 слов, эмодзи, иконки, цифры)
    с кнопкой «Сверить…»; без адреса кнопка выключена с причиной;
  * запись подтверждения только после явного OK: trust_sas_begin -> окно
    сверки (сказано, что сверять голосом) -> ветка отказа -> confirm;
    отказ не вызывает trust_sas_confirm и называет исход («Сверка отменена»);
  * отказ бэкенда на обеих целях показывается с причиной (toast с error),
    молчания нет;
  * подтверждённое устройство получает «Отозвать доверие…», отозванное -
    «Вернуть доверие»: цикл статусов закрыт в интерфейсе;
  * цели trust_sas_begin и trust_sas_confirm остаются mesh-скоупами
    в карте прав;
  * подсказка strict в зеркале настроек называет способы подтверждения
    (отпечаток или 12 слов) и авто-включение при первом подтверждённом.
"""

import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
JS = SRC / "ui_src" / "app.js"


def slice_between(text: str, start: str, end: str) -> str:
    """Фрагмент текста между маркерами (маркеры уникальны в своей секции)."""
    begin = text.index(start)
    stop = text.index(end, begin + len(start))
    return text[begin:stop]


SAS_ACTION = 'if (action === "trust-sas") {'
NEXT_ACTION = 'if (action === "catalog-refresh")'


class TestPeerDialog(unittest.TestCase):
    def setUp(self):
        self.text = JS.read_text(encoding="utf-8")

    def test_tofu_row_offers_both_confirmation_roads(self):
        row = slice_between(self.text, 'if (d.status === "tofu") {',
                            '} else if (d.status === "verified") {')
        self.assertIn('data-peer="trust-verify"', row,
                      "сверка отпечатка пропала из диалога «Участники»")
        self.assertIn("Отпечаток…", row)
        self.assertIn("data-peer-format", row,
                      "выбор формата SAS пропал из диалога")
        for value, label in (("words", "12 слов"), ("emoji", "эмодзи"),
                             ("icons", "иконки"), ("digits", "цифры")):
            self.assertIn(f'<option value="{value}">{label}</option>', row,
                          f"формат «{label}» пропал из выбора SAS")
        self.assertIn('data-peer="trust-sas"', row,
                      "кнопка SAS-сверки пропала из диалога")
        self.assertIn("data-addr=", row,
                      "кнопка сверки потеряла адрес устройства")
        self.assertIn("нет адреса устройства", row,
                      "без адреса кнопка обязана быть выключена с причиной, "
                      "а не молча")

    def test_confirm_happens_only_after_explicit_ok(self):
        action = slice_between(self.text, SAS_ACTION, NEXT_ACTION)
        begin = action.find('call("trust_sas_begin"')
        dialog = action.find("confirm(")
        refusal = action.find("if (!ok)")
        record = action.find('call("trust_sas_confirm"')
        for name, value in (("показ слов", begin), ("окно сверки", dialog),
                            ("ветка отказа", refusal), ("запись", record)):
            self.assertNotEqual(value, -1,
                                f"ветка «{name}» пропала из SAS-сверки")
        self.assertLess(begin, dialog,
                        "показ слов обязан предшествовать окну сверки")
        self.assertLess(dialog, refusal,
                        "окно сверки обязано спрашивать человека до записи")
        self.assertLess(refusal, record,
                        "запись подтверждения встала до ветки отказа - "
                        "решение человека обходится")
        self.assertIn("ГОЛОСОМ", action,
                      "окно сверки перестало называть способ сверки (голосом)")
        self.assertIn("Сверка отменена", action,
                      "отказ не называет исход: устройство не подтверждено")
        between = action[refusal:record]
        self.assertIn("return;", between,
                      "ветка отказа обязана вернуться до вызова "
                      "trust_sas_confirm")

    def test_backend_errors_are_shown_not_silent(self):
        action = slice_between(self.text, SAS_ACTION, NEXT_ACTION)
        self.assertRegex(
            action,
            r"if \(res && res\.error\) \{ toast\(res\.error, true\); return; \}",
            "отказ начала сверки показывается молча")
        self.assertRegex(
            action,
            r"if \(fin && fin\.error\) \{ toast\(fin\.error, true\); return; \}",
            "отказ записи подтверждения показывается молча")

    def test_status_cycle_buttons_present(self):
        verified = slice_between(self.text,
                                 '} else if (d.status === "verified") {',
                                 "} else {")
        self.assertIn('data-peer="trust-revoke"', verified)
        self.assertIn("Отозвать доверие…", verified,
                      "подтверждённое устройство потеряло путь отзыва")
        revoked = slice_between(self.text, "} else {",
                                'data-peer="doctor"')
        self.assertIn("Вернуть доверие", revoked,
                      "отозванное устройство обязано получать путь возврата")

    def test_sas_ops_are_mesh_scoped(self):
        for name in ("trust_sas_begin", "trust_sas_confirm"):
            self.assertRegex(self.text, rf'"{name}":\s*"mesh"',
                             f"{name}: цель сверки выпала из mesh-скоупов - "
                             "карта прав перестала её ограничивать")

    def test_strict_hint_names_the_methods_and_auto_enable(self):
        self.assertIn("сверка отпечатка или 12 слов", self.text,
                      "подсказка strict перестала называть способы "
                      "подтверждения")
        self.assertIn("Включается само при", self.text,
                      "подсказка strict перестала описывать авто-включение "
                      "при первом подтверждённом устройстве")


if __name__ == "__main__":
    unittest.main()
