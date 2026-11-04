"""Страж параллельного раннера тестов (tools/run_tests.py).

Раннер не меняет проверок: он запускает те же модули тем же `python -m
unittest tests.<модуль>` в отдельных процессах. Проверяем, что он называет
состав, отказ по выборке и по флагам, и что итог печатается в форме unittest -
и что упавший модуль называется своим именем, а не молчанием.
"""

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "run_tests", ROOT / "tools" / "run_tests.py")
    assert spec and spec.loader, "tools/run_tests.py не загружается"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = _load_tool()
CI = ROOT / ".github" / "workflows" / "ci.yml"
BETA = ROOT / ".github" / "workflows" / "beta.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
README = ROOT / "README.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
DEBUG_BAT = ROOT / "debug.bat"
SERVER_CLIENT = ROOT / "docs" / "SERVER-CLIENT.md"
BIND = ROOT / "app" / "http" / "bind.py"


class TestRunnerSelection(unittest.TestCase):
    def test_list_covers_the_test_modules(self):
        modules = RUNNER.modules([])
        self.assertGreaterEqual(len(modules), 70,
                                "раннер не покрывает каталог тестов - он бы гонял "
                                "часть набора молча")
        self.assertEqual(modules, sorted(modules), "порядок модулей нестабилен")
        for name in ("test_serve", "test_release_pack", "test_license_texts"):
            self.assertIn(name, modules, f"{name}: модуль вне прогона")

    def test_only_selection_normalizes_and_names_missing(self):
        self.assertEqual(RUNNER.modules(["i18n"]), ["test_i18n"])
        # порядок выборки задаётся человеком (для отладки), полный прогон -
        # сортировкой имён
        self.assertEqual(RUNNER.modules(["test_i18n", "crypt"]),
                         ["test_i18n", "test_crypt"])
        with self.assertRaises(ValueError) as ctx:
            RUNNER.modules(["chudo_module"])
        self.assertIn("не найдено", str(ctx.exception),
                      "несуществующий модуль не назван причиной отказа")

    def test_jobs_are_honest(self):
        self.assertEqual(RUNNER.jobs(1), 1)
        self.assertGreaterEqual(RUNNER.jobs(None), 1)
        self.assertLessEqual(RUNNER.jobs(None), 10,
                             "больше десяти процессов - только лишние импорты")
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                RUNNER.jobs(bad)


class TestRunnerReport(unittest.TestCase):
    def test_success_summary_is_unittest_shape(self):
        results = [
            {"module": "test_a", "code": 0, "timed_out": False, "ran": 10,
             "skipped": 2, "failures": 0, "errors": 0, "seconds": 1.0, "tail": []},
            {"module": "test_b", "code": 0, "timed_out": False, "ran": 5,
             "skipped": 0, "failures": 0, "errors": 0, "seconds": 2.0, "tail": []},
        ]
        code, lines = RUNNER.report(results, 2.5)
        self.assertEqual(code, 0)
        joined = "\n".join(lines)
        self.assertIn("Ran 15 tests", joined)
        self.assertIn("OK (skipped=2)", joined)
        self.assertIn("самые долгие: test_b", joined)
        self.assertIn("модулей: 2", joined)
        # честное сравнение: параллельный прогон называет и сумму процессов
        self.assertIn("последовательный discover дал бы", joined)

    def test_failure_names_the_module(self):
        results = [
            {"module": "test_bad", "code": 1, "timed_out": False, "ran": 7,
             "skipped": 0, "failures": 1, "errors": 0, "seconds": 3.0,
             "tail": ["FAIL: тест не прошёл"]},
            {"module": "test_ok", "code": 0, "timed_out": False, "ran": 4,
             "skipped": 0, "failures": 0, "errors": 0, "seconds": 1.0, "tail": []},
        ]
        code, lines = RUNNER.report(results, 4.0)
        self.assertEqual(code, 1)
        joined = "\n".join(lines)
        self.assertIn("ОШИБКА: модуль test_bad", joined,
                      "упавший модуль не назван - «OK» был бы враньём")
        self.assertIn("тест не прошёл", joined)
        self.assertIn("ИТОГ", joined)
        self.assertNotIn("Ran 11 tests in", joined)

    def test_timeout_is_named_not_silent(self):
        results = [{"module": "test_slow", "code": 1, "timed_out": True, "ran": 0,
                    "skipped": 0, "failures": 0, "errors": 0, "seconds": 420.0,
                    "tail": ["модуль не закончился за 420.0 с"]}]
        code, lines = RUNNER.report(results, 420.0)
        self.assertEqual(code, 1)
        joined = "\n".join(lines)
        self.assertIn("не закончился за отведённое время", joined)
        self.assertIn("test_slow", joined)


class TestRunnerRunsModules(unittest.TestCase):
    def test_module_run_reports_counts(self):
        result = RUNNER.run_module("test_stamp", 120.0)
        self.assertEqual(result["code"], 0, result["tail"])
        self.assertGreaterEqual(result["ran"], 1,
                                "модуль отработал без числа проверок - итог был бы пустой")
        self.assertFalse(result["timed_out"])

    def test_cli_main_reports_ok_for_a_sample(self):
        self.assertEqual(RUNNER.main(["--only", "stamp", "crypt", "--jobs", "2"]), 0)


class TestPipelineUsesTheRunner(unittest.TestCase):
    """Конвейер и документы называют тот же прогон, что и локально."""

    def test_workflows_run_the_runner(self):
        for path in (CI, BETA, RELEASE):
            text = path.read_text(encoding="utf-8")
            self.assertIn("python tools/run_tests.py", text,
                          f"{path.name}: тесты гоняются последовательным discover - "
                          "прогон медленный и результат тот же")
            self.assertIn("- name: Офлайн-тесты", text,
                          f"{path.name}: шаг тестов потерян")
            self.assertIn("независим", text,
                          f"{path.name}: не названо, почему параллельный прогон право")

    def test_readme_contributing_debug_and_plan_name_the_runner(self):
        self.assertIn("python tools/run_tests.py", README.read_text(encoding="utf-8"))
        self.assertIn("--jobs 1", README.read_text(encoding="utf-8"),
                      "последовательный режим не описан")
        self.assertIn("python tools/run_tests.py",
                      CONTRIBUTING.read_text(encoding="utf-8"))
        self.assertIn("python tools/run_tests.py",
                      DEBUG_BAT.read_text(encoding="utf-8", errors="replace"))
        self.assertIn("python tools/run_tests.py",
                      SERVER_CLIENT.read_text(encoding="utf-8"))

    def test_live_tests_stay_separate(self):
        """Живые тесты не въезжают в параллельный прогон: они остаются отдельной
        командой с OMNISTASH_LIVE, как и раньше."""
        debug = DEBUG_BAT.read_text(encoding="utf-8", errors="replace")
        self.assertIn("OMNISTASH_LIVE=1", debug)
        self.assertIn("python -m unittest discover", debug,
                      "прогон живых тестов обязан остаться discover: параллельный "
                      "раннер гоняет офлайн набор")
        self.assertIn("OMNISTASH_LIVE", RUNNER.__doc__,
                      "раннер не говорит, что живые тесты его не трогают")

    def test_addresses_read_only_when_the_mode_needs_them(self):
        """Ускорение S6-режима: loopback и 0.0.0.0 не режут адреса машины."""
        text = BIND.read_text(encoding="utf-8")
        self.assertIn("def read() -> list:", text)
        self.assertIn("_lan_address(read())", text)
        self.assertIn("_tailscale_address(read())", text)
        self.assertNotIn("addresses = list(addresses()) if addresses else read_addresses()",
                         text, "адреса машины читаются на каждый вызов резать - "
                         "петля PowerShell ~1.2 с на loopback")
        self.assertIn("forget_addresses", text,
                      "кэш адресов без сброса был бы «не смотрели» вместо «не найден»")
        self.assertIn("ADDRESSES_TTL", text)

    def test_cache_does_not_change_the_refusal(self):
        """Отказ по режиму остаётся честным: без согласия адреса не читаются."""
        from app.http import bind
        calls = []

        def reader():
            calls.append(1)
            return ["192.168.1.5"]

        refused = bind.resolve("lan", False, "", reader)
        self.assertEqual(refused["error"],
                         "согласия «что уходит наружу» нет - слушатель "
                         "остаётся на этом компьютере")
        self.assertEqual(calls, [], "отказ без согласия читал адреса машины - "
                          "это работа без нужды")
        opened = bind.resolve("lan", True, "", reader)
        self.assertEqual(opened["address"], "192.168.1.5")
        self.assertEqual(calls, [1])
        loopback = bind.resolve("loopback", False, "", reader)
        self.assertEqual(loopback["address"], "127.0.0.1")
        self.assertEqual(calls, [1], "loopback не должен звать адреса")
        wildcard = bind.resolve("wildcard", True, "192.168.1.0/24", reader)
        self.assertEqual(wildcard["address"], "0.0.0.0")
        self.assertEqual(calls, [1], "wildcard решает allowlist, адреса ему не нужны")


if __name__ == "__main__":
    unittest.main()
