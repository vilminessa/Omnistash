"""Mirror-страж токенов темы: Dart-ассет сверяется с реестром тем и с CSS.

Что проверяется (без Flutter SDK - ассет парсится как текст, поэтому страж
гоняется и на GitHub-раннере, где UI-тесты не запускаются):
  * ui_flutter/lib/theme_tokens.dart совпадает с генерацией
    tools/build_flutter_theme.py байт-в-байт (вывод детерминирован);
  * порядок id тем, дефолтный id, подписи и флаги dark совпадают с
    app/themes.py (themes.ids(), themes.labels(), темы реестра);
  * hex-роли каждой темы совпадают с themes.css_vars переменная-в-переменная,
    и в Dart нет ролей вне реестра;
  * пары контраста в Dart - те же имена и пороги, что themes.CONTRAST_PAIRS;
  * границы шрифта и формула высоты строки - те же числа, что константы
    app/themes.py (FONT_SIZE_*, FONT_SCALE_*, ROW_MIN, ROW_FACTOR);
  * метрики в Dart - те же числа, что блок :root в ui_src/app.css, включая
    тройки cubic-bezier (веяния DESIGN-UI §2.7);
  * theme.dart не держит hex-литералов, размеров и длительностей числом, и
    имена ролей, которые он читает, существуют в каждой теме реестра;
  * проводка вызовов фасада в Dart повторяет канонную форму тела
    (пустое тело `{}` когда аргумента нет, `[аргумент]` когда есть -
    ui_src/app.js:547);
  * экран библиотеки читает форму ответа фасада из app/repo.py (`rows`,
    `total`), размер страницы из `PAGE` и период опроса из `POLL_MS`
    ui_src/app.js - не изобретает свои числа;
  * карта цветов статусов строки совпадает с правилами `.status[data-s=...]`
    в ui_src/app.css (known и unavailable читаются базовой подписью muted);
  * choices поля theme в схеме настроек - тот же порядок, что список тем в
    Flutter, иначе настройка не применится к Flutter-окну;
  * расхождение называется: битый hex, переставленный порядок id, изменённая
    метрика, изменённый размер страницы, выдуманный ключ `rows` и литерал в
    production-коде дают отказ с текстом, а не молчание.
"""

import pathlib
import sys
import tempfile
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC / "tools"))

from app import settings_schema, themes  # noqa: E402 - путь добавлен выше
import build_flutter_theme as gen  # noqa: E402 - путь добавлен выше
import check_flutter_mirror as mirror  # noqa: E402 - путь добавлен выше


class FlutterMirrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = mirror.parse(mirror.TOKENS.read_text(encoding="utf-8"))

    def test_tokens_match_generation(self):
        """Ассет либо совпадает с генерацией, либо его правил кто-то руками."""
        self.assertEqual(mirror.generation_problems(), [],
                         mirror.generation_problems())
        self.assertEqual(mirror.TOKENS.read_text(encoding="utf-8"), gen.render(),
                         "theme_tokens.dart не совпадает с генерацией")

    def test_ids_labels_and_default_match_registry(self):
        self.assertEqual(self.parsed["ids"], list(themes.ids()),
                         "порядок id тем в Flutter расходится с themes.ids()")
        self.assertEqual(self.parsed["default"], themes.DEFAULT_THEME,
                         "дефолтная тема в Flutter не themes.DEFAULT_THEME")
        self.assertEqual(self.parsed["labels"],
                         {item["id"]: item["label"] for item in themes.themes()},
                         "подписи тем расходятся с реестром")
        self.assertEqual(self.parsed["dark"],
                         {item["id"]: bool(item["dark"]) for item in themes.themes()},
                         "флаги dark расходятся с реестром")

    def test_roles_match_registry_variable_by_variable(self):
        for theme_id in themes.ids():
            registry = themes.css_vars(theme_id)
            dart_roles = self.parsed["colors"][theme_id]
            for name in themes.PALETTE_VARS:
                key = name.lstrip("-")
                self.assertEqual(dart_roles[key], int(registry[name][1:], 16),
                                 f"{theme_id} {key}: Dart не равен реестру "
                                 f"{registry[name]}")
            self.assertEqual(set(dart_roles),
                             {name.lstrip("-") for name in themes.PALETTE_VARS},
                             f"{theme_id}: набор ролей не равен реестру")

    def test_contrast_pairs_match_registry(self):
        expected = [(fg.lstrip("-"), bg.lstrip("-"), float(floor))
                    for fg, bg, floor in themes.CONTRAST_PAIRS]
        self.assertEqual(self.parsed["pairs"], expected,
                         "пары контраста в Flutter расходятся с реестром")
        self.assertEqual(len(self.parsed["pairs"]), len(themes.CONTRAST_PAIRS),
                         "часть контрастных пар потеряна: тема не сверяется")

    def test_font_bounds_and_row_formula_match_constants(self):
        fonts = {name: float(value) for name, value in self.parsed["fonts"].items()}
        self.assertEqual(fonts, {
            "sizeMin": float(themes.FONT_SIZE_MIN),
            "sizeMax": float(themes.FONT_SIZE_MAX),
            "sizeDefault": float(themes.FONT_SIZE_DEFAULT),
            "scaleMin": float(themes.FONT_SCALE_MIN),
            "scaleMax": float(themes.FONT_SCALE_MAX),
            "scaleDefault": float(themes.FONT_SCALE_DEFAULT),
            "rowMin": float(themes.ROW_MIN),
            "rowFactor": float(themes.ROW_FACTOR),
        }, "границы шрифта и множитель строки расходятся с app/themes.py")
        # Тот же расчёт, что themes.row_height: max(rowMin, size * factor).
        size = float(themes.FONT_SIZE_DEFAULT)
        self.assertGreaterEqual(fonts["rowMin"], size * fonts["rowFactor"] - 1.0,
                                "нижняя граница строки мельчает относительно текста")

    def test_metrics_match_css_root(self):
        expected = gen.metric_values()
        self.assertEqual(self.parsed["metrics"], expected,
                         "метрики Flutter расходятся с :root в ui_src/app.css")
        # Веяния: тройки cubic-bezier переносятся числом, а не «кривая по памяти».
        for name, value in expected.items():
            if value.startswith("Cubic("):
                self.assertTrue(self.parsed["metrics"][name].startswith("Cubic("),
                                f"{name}: кривая движения не перенесена из CSS")

    def test_production_code_has_no_literals(self):
        """production-код Flutter читает токены, а не пишет цвета и размеры числом."""
        self.assertEqual(mirror.literal_problems(), [], mirror.literal_problems())
        for name in mirror.PRODUCTION_FILES:
            text = (mirror.LIB / name).read_text(encoding="utf-8")
            names = set(mirror.ROLE_CALL.findall(text))
            self.assertTrue(
                names <= {key.lstrip("-") for key in themes.PALETTE_VARS},
                f"{name} читает роли вне реестра: {sorted(names)}")
        theme_text = (mirror.LIB / "theme.dart").read_text(encoding="utf-8")
        self.assertTrue(mirror.ROLE_CALL.findall(theme_text),
                        "theme.dart не читает ни одной роли темы - палитра "
                        "берётся не из токенов")

    def test_status_roles_match_css(self):
        """Карта цветов статусов строки - тот же источник, что app.css."""
        css = mirror.css_status_roles()
        view = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        self.assertEqual(mirror.dart_status_roles(view), css,
                         "карта статусов Flutter расходится с ui_src/app.css")
        self.assertEqual(mirror.status_roles_problems(), [],
                         mirror.status_roles_problems())
        # known и unavailable не имеют отдельного правила - базовая подпись.
        self.assertEqual(css["known"], "muted",
                         "статус known должен читаться ролью muted")
        self.assertEqual(css["unavailable"], "muted",
                         "статус unavailable должен читаться ролью muted")

    def test_page_and_poll_match_the_canonical_sources(self):
        """Размер страницы и период опроса взяты из ui_src/app.js, не из головы."""
        self.assertEqual(mirror.contract_source_problems(), [],
                         mirror.contract_source_problems())
        js = (SRC / "ui_src" / "app.js").read_text(encoding="utf-8")
        view = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        self.assertRegex(js, r"var PAGE\s*=\s*120",
                         "источник PAGE не назван в ui_src/app.js")
        self.assertIn("const int kPageLimit = 120;", view,
                      "Flutter тянет не ту страницу: PAGE из app.js не применён")
        self.assertRegex(js, r"var POLL_MS\s*=\s*500",
                         "источник POLL_MS не назван в ui_src/app.js")
        self.assertIn("const int kPollIntervalMs = 500;", view,
                      "Flutter опрашивает с другой частотой: POLL_MS из app.js")

    def test_rows_key_matches_the_repo_source(self):
        """Форма ответа списка строк сверяется с app/repo.py: list_videos."""
        repo_src = (SRC / "app" / "repo.py").read_text(encoding="utf-8")
        self.assertIn('"rows": rows', repo_src,
                      "app/repo.py не возвращает rows - источник формы потерян")
        self.assertIn('"total":', repo_src,
                      "app/repo.py не возвращает total - счётчик строк потерян")
        view = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        self.assertIn("const String kRowsKey = 'rows'", view,
                      "Flutter читает не rows: форма ответа выдумана клиентом")

    def test_broken_status_map_is_named(self):
        view = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        broken = view.replace("'downloaded': 'ok'", "'downloaded': 'info'")
        problems = mirror.status_roles_problems(broken)
        self.assertTrue(any("downloaded" in line for line in problems),
                        f"расхождение статуса не названо: {problems}")

    def test_changed_page_size_is_named(self):
        view = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        broken = view.replace("const int kPageLimit = 120;",
                              "const int kPageLimit = 300;")
        problems = mirror.contract_source_problems(broken)
        self.assertTrue(any("kPageLimit" in line for line in problems),
                        f"расхождение страницы не названо: {problems}")

    def test_invented_rows_key_is_named(self):
        view = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        broken = view.replace("const String kRowsKey = 'rows'",
                              "const String kRowsKey = 'videos'")
        problems = mirror.contract_source_problems(broken)
        self.assertTrue(any("rows" in line for line in problems),
                        f"выдуманный ключ не назван: {problems}")

    def test_facade_contract_matches_the_canonical_body(self):
        """Dart шлет тело той же формы, что браузерный клиент и сервер."""
        self.assertEqual(mirror.contract_problems(), [], mirror.contract_problems())
        js = (SRC / "ui_src" / "app.js").read_text(encoding="utf-8")
        self.assertIn("(arg === undefined || arg === null) ? \"{}\"", js,
                      "канонная форма тела не найдена в ui_src/app.js - "
                      "сверка потеряла источник")
        field = next(item for item in settings_schema.FIELDS
                     if item["key"] == "theme")
        self.assertEqual([choice[0] for choice in field["choices"]],
                         self.parsed["ids"],
                         "choices поля theme не совпадают со списком тем Flutter")

    def test_all_mirrors_pass_together(self):
        self.assertEqual(mirror.problems(), [], mirror.problems())

    def test_broken_hex_is_named(self):
        """Расхождение называет тему, роль и оба значения - не молчит."""
        parsed = mirror.parse(mirror.TOKENS.read_text(encoding="utf-8"))
        theme_id = themes.ids()[0]
        parsed["colors"][theme_id]["bg"] = 0x123456
        problems = mirror.registry_problems(parsed)
        self.assertTrue(any(theme_id in line and "bg" in line for line in problems),
                        f"битый hex не назван: {problems}")

    def test_reordered_ids_is_named(self):
        parsed = mirror.parse(mirror.TOKENS.read_text(encoding="utf-8"))
        parsed["ids"] = list(reversed(parsed["ids"]))
        problems = mirror.registry_problems(parsed)
        self.assertTrue(any("порядок id" in line for line in problems),
                        f"переставленный порядок id не назван: {problems}")

    def test_changed_metric_is_named(self):
        parsed = mirror.parse(mirror.TOKENS.read_text(encoding="utf-8"))
        parsed["metrics"]["r3"] = "10.0"
        problems = mirror.metrics_problems(parsed)
        self.assertTrue(any("r3" in line for line in problems),
                        f"изменённая метрика не названа: {problems}")

    def test_literal_in_theme_code_is_named(self):
        """Color-литерал и размер числом в production-коде - названный отказ."""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="omnistash-mirror-"))
        (tmp / "theme.dart").write_text(
            "const Color c = Color(0xff123456);\n"
            "const TextStyle t = TextStyle(fontSize: 13);\n", encoding="utf-8")
        (tmp / "library_view.dart").write_text(
            "const Map<String, String> kStatusRoles = <String, String>{\n"
            "  'downloaded': 'ok',\n};\n", encoding="utf-8")
        (tmp / "app.dart").write_text("void main() {}\n", encoding="utf-8")
        problems = mirror.literal_problems(tmp)
        self.assertEqual(len(problems), 2, f"литералы не названы: {problems}")
        self.assertTrue(any("hex-литерал" in line for line in problems), problems)
        self.assertTrue(any("размер написан числом" in line for line in problems),
                        problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)
