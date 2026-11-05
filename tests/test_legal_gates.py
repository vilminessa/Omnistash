"""Стражи §5.3 LEGAL-REVIEW: возрастные и AUP-гейты не ослаблены.

Тесты не дублируют test_adult_gate.py и test_gui.py - они закрепляют то,
что те тесты не проверяют в коде:

  * legal.accept() вызывается ровно из одного места - фасада legal_accept
    (явный клик человека в окне); авто-accept нет в gui.py, mesh/* и
    wormhole.py (с алиасом импорта legal_mod - скан по обоим видам);
  * серверная смена настройки (mesh_enabled) не пишет юридические факты:
    включение mesh фиксирует согласие только UI по явному принятию правил;
  * гейт включения mesh в UI требует принятого AUP (aup_accepted_at) и
    регистрирует одноразовое предупреждение о передаче (first_send);
    отказ возвращает переключатель в выключенное состояние - это отказ,
    а не «мягкое предупреждение»;
  * операции mesh отказывают, когда участие выключено, и отказ называет
    причину (секция Mesh) - молчания нет;
  * первая передача по wormhole требует first_send_accepted, отказ называет
    причину (права на файл), и сам отказ НЕ пишет факт согласия;
  * даты не откатываются: повторное принятие с другим штампом сохраняет
    первую дату; first_send_accepted не сбрасывается пустым вызовом;
  * legal.json читают и пишут только через app/legal.py: ни один код-файл
    (включая тесты и инструменты) не открывает файл по имени напрямую;
  * массовый гейт 18+ - один помощник _bulk_adult, его вызывают пять
    массовых эндпоинтов, помощник читает факт, а не пишет его;
  * ключи согласий не попадают в форму настроек (app/settings_schema.py);
  * dedicated-тест возрастного гейта не уменьшается: test_adult_gate.py
    содержит не меньше 16 проверок.
"""

import importlib
import re
import unittest
from pathlib import Path
from unittest import mock

from tests.test_gui import GuiCase

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
UI_JS = ROOT / "ui_src" / "app.js"

CONSENT_KEYS = ("aup_accepted_at", "adult_ok", "first_send_accepted")


def enclosing_function(lines: list[str], index: int) -> str:
    """Имя функции, содержащей строку index (0-based), или "" вне функции."""
    for i in range(index, -1, -1):
        match = re.match(r"\s*def (\w+)\(", lines[i])
        if match:
            return match.group(1)
    return ""


def line_hits(paths, pattern: str) -> list[tuple[Path, int, str]]:
    """Строки файлов, совпадающие с регулярным выражением."""
    compiled = re.compile(pattern)
    hits: list[tuple[Path, int, str]] = []
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines, 1):
            if compiled.search(line):
                hits.append((path, index, line.strip()))
    return hits


def code_files() -> list[Path]:
    """Код репозитория: py, js, bat, cmd, html, css, воркфлоу (доки вне)."""
    skip_first_dirs = {"build", "dist", "release_pack", "client_dist",
                       "licenses", ".git"}
    out: list[Path] = []
    for glob in ("**/*.py", "**/*.js", "**/*.bat", "**/*.cmd", "**/*.html",
                 "**/*.css", ".github/workflows/*.yml"):
        for p in ROOT.glob(glob):
            if p.relative_to(ROOT).parts[0] in skip_first_dirs:
                continue
            out.append(p)
    return sorted(set(out))


class LegalFactsCase(GuiCase):
    """Факты согласий - в изолированном профиле теста.

    Без патча гейты читали бы настоящие принятия человека (legal.json в
    профиле) и не срабатывали бы.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch("app.legal.legal_path",
                             return_value=self.dir / "legal.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = self.make_api()

    def logs(self) -> list[str]:
        return self.api.poll(0)["logs"]


class TestAcceptOnlyFromUi(LegalFactsCase):
    def test_accept_called_once_from_facade(self):
        """legal.accept - единственный вызов, из фасада legal_accept."""
        hits = line_hits(sorted(APP.rglob("*.py")),
                        r"legal(_mod)?\.accept\(")
        self.assertEqual(len(hits), 1,
                         f"авто-accept вне фасада: {hits[1:]}")
        path, index, _line = hits[0]
        self.assertEqual(path.name, "gui.py",
                         "legal.accept вызывается не из gui.py")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(enclosing_function(lines, index - 1), "legal_accept",
                         "legal.accept вызывается не из фасада явного "
                         "принятия (legal_accept)")

    def test_facade_never_called_from_python_internals(self):
        """Никакой путь кода не вызывает legal_accept сам (нет авто-accept)."""
        hits = line_hits(sorted(APP.rglob("*.py")), r"self\.legal_accept\(")
        self.assertEqual(hits, [], "gui.py или mesh/wormhole вызывают "
                                   "принятие сами - авто-accept")

    def test_server_setting_change_never_writes_consent(self):
        """Смена mesh_enabled на сервере НЕ пишет юридические факты.

        UI фиксирует принятие отдельным явным действием (legal_accept);
        фасад настроек не имеет права создавать факт согласия. Старт
        сервера заглушен: проверяется именно отсутствие записи фактов.
        """
        with mock.patch("app.gui.Api._mesh_apply", lambda self: None):
            saved = self.api.save_setting({"key": "mesh_enabled", "value": True})
        self.assertIs(saved["settings"]["mesh_enabled"], True,
                      "настройка не сохранена - проверка не состоялась")
        state = self.api.legal_state()
        self.assertEqual(state["aup_accepted_at"], "",
                         "включение mesh на сервере само записало принятие "
                         "правил - авто-accept")
        self.assertFalse(state["first_send_accepted"],
                         "включение mesh само записало предупреждение о "
                         "передаче - авто-accept")
        self.assertEqual(state["adult_ok"], "",
                         "включение mesh само записало подтверждение 18+ - "
                         "авто-accept")


class TestMeshUiGate(LegalFactsCase):
    def test_mesh_enable_requires_aup_and_blocks_refusal(self):
        """Гейт включения mesh в UI: без принятого AUP включение не состояться.

        Сверяется структура блока, а не слова: вызов legal_state, проверка
        aup_accepted_at, диалог правил, отказ возвращает переключатель
        (input.checked = false + syncSwitch) и НЕ сохраняет настройку,
        принятие регистрирует aup И first_send.
        """
        text = UI_JS.read_text(encoding="utf-8")
        marker = re.search(r"if \(key === \"mesh_enabled\" && value\) \{",
                           text)
        self.assertIsNotNone(marker,
                             "гейт mesh_enabled в UI не найден - включение "
                             "участия стало молчаливым")
        accept_match = re.search(
            r'call\("legal_accept",\s*\{\s*aup: true,\s*first_send: true\s*\}',
            text[marker.start():])
        self.assertIsNotNone(accept_match,
                             "в гейте mesh_enabled нет вызова legal_accept с "
                             "aup и first_send - принятие не регистрируется")
        block = text[marker.start():marker.start() + accept_match.end() + 40]
        # Порядок: proceed defined -> saveSetting внутри proceed -> проверка
        # факта -> отказ -> принятие -> proceed.
        idx = {
            "proceed_def": block.find("var proceed = function"),
            "save": block.find("saveSetting(key, value);"),
            "state": block.find('call("legal_state")'),
            "aup_check": block.find("legal.aup_accepted_at"),
            "refusal": block.find("if (!accepted)"),
            "dialog": block.find("legalDialog("),
            "accept_call": block.find('call("legal_accept"'),
        }
        for name, value in idx.items():
            self.assertNotEqual(value, -1,
                                f"в гейте mesh_enabled нет элемента {name} - "
                                "проверка принятия ослаблена")
        self.assertLess(idx["proceed_def"], idx["save"])
        self.assertLess(idx["save"], idx["state"])
        self.assertLess(idx["state"], idx["aup_check"])
        self.assertLess(idx["dialog"], idx["accept_call"])
        # Отказ - возврат переключателя, а не мягкое предупреждение.
        self.assertRegex(block,
                         r"if \(!accepted\) \{[^}]*input\.checked = false;"
                         r"[^}]*syncSwitch\(input\)[^}]*return;",
                         "отказ непринявшего rules должен возвращать "
                         "переключатель в выключенное состояние")
        self.assertEqual(block.count("saveSetting(key, value);"), 1,
                         "mesh_enabled сохраняется больше одного раза - "
                         "появился путь в обход принятия")
        self.assertRegex(block,
                         r"call\(\"legal_accept\",\s*\{\s*aup: true,\s*"
                         r"first_send: true\s*\}\)",
                         "включение mesh не регистрирует принятие правил или "
                         "предупреждение о передаче (aup/first_send)")
        # proceed вызывается только после факта или после принятия.
        self.assertEqual(block.count("proceed"), 3,
                         "в блоке появились дополнительные пути включения "
                         "mesh без сверки факта")

    def test_mesh_ops_refuse_with_named_reason(self):
        """Операции mesh отказывают при выключенном участии - причина названа."""
        for op, payload in (("nick_search", {"query": "ник"}),
                            ("catalog_refresh", {}),
                            ("nick_probe", {"addr": "127.0.0.1:1"})):
            result = getattr(self.api, op)(payload)
            self.assertIn("error", result,
                          f"{op}: отказ молчаливый - вернулся успех без причины")
            self.assertIn("mesh выключено", result["error"],
                          f"{op}: отказ не называет причину выключенного "
                          "участия")


class TestFirstSendGate(LegalFactsCase):
    def test_wormhole_send_blocks_until_accepted(self):
        """Первая передача по wormhole требует first_send_accepted.

        Отказ называет причину (файл уходит другому лицу, права), и сам
        отказ НЕ пишет факт: согласие создаёт только явное принятие.
        """
        with mock.patch("app.gui.wormhole_mod.installed",
                        return_value=False):
            gated = self.api.wormhole_send({"path": "x.mp4"})
        self.assertIn("need_confirm", gated,
                      "wormhole_send отправил файл без принятого "
                      "предупреждения")
        self.assertIn("ДРУГОМУ ЛИЦУ", gated["need_confirm"],
                      "отказ не называет, что файл уходит другому лицу")
        self.assertFalse(self.api.legal_state()["first_send_accepted"],
                         "отказ-гейт сам записал согласие - авто-accept")
        self.assertFalse(any("wormhole" in line for line in self.logs()),
                         "заблокированная передача оставила ход в журнале - "
                         "отказ-гейт должен быть без хода")

        self.api.legal_accept({"first_send": True})
        with mock.patch("app.gui.wormhole_mod.installed",
                        return_value=False):
            after = self.api.wormhole_send({"path": "x.mp4"})
        self.assertNotIn("need_confirm", after,
                         "после принятия гейт не снят - передача блокируется")
        self.assertIn("не установлен", after.get("error", ""),
                      "отказ не называет, что wormhole не установлен")

    def test_dates_and_flags_do_not_roll_back(self):
        """Повторное принятие с другим штампом сохраняет первую дату.

        Юридически важен первый момент: штампы подменяются, чтобы повтор
        не выглядел победой времени. first_send_accepted не сбрасывается
        пустым вызовом accept.
        """
        stamps = ["2026-01-01T00:00:00", "2099-12-31T23:59:59"]

        def fake_stamp(fmt: str) -> str:
            return stamps.pop(0) if stamps else "2099-12-31T23:59:59"

        with mock.patch("app.legal.time.strftime", side_effect=fake_stamp):
            first = self.api.legal_accept({"aup": True, "adult": True})
            self.assertEqual(first["aup_accepted_at"], "2026-01-01T00:00:00")
            self.assertEqual(first["adult_ok"], "2026-01-01T00:00:00")
            second = self.api.legal_accept(
                {"aup": True, "adult": True, "first_send": True})
        self.assertEqual(second["aup_accepted_at"], "2026-01-01T00:00:00",
                         "второе принятие откатило дату принятия правил")
        self.assertEqual(second["adult_ok"], "2026-01-01T00:00:00",
                         "второе принятие откатило дату подтверждения 18+")
        self.assertTrue(second["first_send_accepted"])
        empty = self.api.legal_accept({})
        self.assertTrue(empty["first_send_accepted"],
                        "пустой вызов сбросил предупреждение о передаче")
        self.assertEqual(empty["aup_accepted_at"], "2026-01-01T00:00:00",
                         "пустой вызов откатил дату правил")


class TestBulkAdultGate(LegalFactsCase):
    def test_bulk_gate_is_one_helper_for_five_endpoints(self):
        """Массовые эндпоинты держат 18+ через один помощник - пять вызовов."""
        gui = APP / "gui.py"
        hits = line_hits([gui], r"self\._bulk_adult\(")
        call_sites = [hit for hit in hits if hit[2].startswith("gate =")]
        self.assertEqual(len(call_sites), 5,
                         f"число массовых эндпоинтов с гейтом изменилось: "
                         f"{len(call_sites)} - сверка с docs/REVIEW-WAVE1.md "
                         "нужна вручную")
        lines = gui.read_text(encoding="utf-8").splitlines()
        for _path, index, _line in call_sites:
            name = enclosing_function(lines, index - 1)
            self.assertTrue(name.startswith("bulk"),
                            f"массовый гейт вызывается из {name} - не из "
                            "массового эндпоинта")

    def test_bulk_helper_reads_consent_not_writes(self):
        """Помощник читает adult_ok и не вызывает принятие."""
        gui = APP / "gui.py"
        hits = line_hits([gui], r"_bulk_adult\(ids\)")
        self.assertEqual(len(hits), 5,
                         "число вызовов массового гейта изменилось")
        writes = line_hits([gui], r"legal(_mod)?\.accept\(")
        self.assertTrue(all(enclosing_function(
            gui.read_text(encoding="utf-8").splitlines(), index - 1)
            == "legal_accept" for _p, index, _l in writes),
                        "write-путь принятия появился вне фасада явного клика")

    def test_consent_keys_absent_from_settings_form(self):
        """Факты согласий не вылезают в форму настроек (схема их не содержит)."""
        schema = APP / "settings_schema.py"
        hits = line_hits([schema], "|".join(re.escape(k) for k in CONSENT_KEYS))
        self.assertEqual(hits, [],
                         "ключ юридического факта попал в settings_schema - "
                         "он должен жить в app/legal.py")

    def test_adult_gate_dedicated_test_not_shrunk(self):
        """Dedicated-тест возрастного гейта не уменьшается ниже 16 проверок."""
        module = importlib.import_module("tests.test_adult_gate")
        count = unittest.TestLoader().loadTestsFromModule(module).countTestCases()
        self.assertGreaterEqual(count, 16,
                                f"test_adult_gate сокращён до {count} проверок "
                                "- возрастной гейт остался без dedicated-теста")


class TestLegalFileAccess(LegalFactsCase):
    def test_no_direct_read_or_write_of_legal_json(self):
        """legal.json открывают только через app/legal.py (по имени файла).

        Ни один код-файл - включая тесты, инструменты, воркфлоу и UI - не
        читает и не пишет файл по строке «legal.json»: путь дают только
        app/paths.py (определение) и app/legal.py (работает через
        legal_path()). Тесты патчат app.legal.legal_path, а не открывают
        файл.
        """
        read_write = re.compile(r"read_text|read_bytes|write_text|"
                                r"write_bytes|open\(|cat |Get-Content|Set-Content")
        bad = []
        for path in code_files():
            for index, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if "legal.json" in line and read_write.search(line):
                    bad.append(f"{path.relative_to(ROOT)}:{index}: {line.strip()[:110]}")
        self.assertEqual(bad, [],
                         "появился путь, читающий/пишущий legal.json мимо "
                         "app/legal.py")
        # Файл с фактами создаёт только legal._write (через legal_path).
        writers = line_hits(sorted(APP.rglob("*.py")), r"legal\.json")
        self.assertEqual([w[0].name for w in writers], ["paths.py"],
                         "имя legal.json упомянут вне app/paths.py "
                         "(единственного места, где путь определён)")


if __name__ == "__main__":
    unittest.main()
