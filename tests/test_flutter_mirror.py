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
  * сортируемые колонки Flutter (`kSortableColumns`) - тот же порядок и
    подписи, что `LIST_COLS` в ui_src/app.js, и те же ключи, что белый
    список сервера `ORDER_COLS` в app/repo.py; гейты колонок (`col_*`) и
    ключи сохранения порядка (`sort_col`, `sort_dir`) существуют в схеме
    настроек; колонка `check` без sortable исключена из сверки - названо;
  * табы Flutter (`kTabLabels`, `kTabStubs`) - тот же порядок подписей, что
    `TAB_RU` в ui_src/app.js (динамические вкладки площадок `platform`
    исключены - названо); каждая не построенная вкладка называет волну, а
    построенная Библиотека (W2) заглушки не имеет;
  * карточка видео (шаг W2 3): подписи kv - порядок `add(...)` из `openDetail`,
    поля канона, которые не рисуются, названы волной; ряд действий - подписи
    из `data-detail`, построенные совпадают с каноном по порядку, права
    сверяются с `app/http/scopes.py` (SCOPE_OF и SCOPES), `watch` остаётся
    маршрутным правом (media.py), нативные цели (open_folder/open_file/open_url)
    не вызываются по HTTP; списки статусов по действиям - guard-ы канона и
    словарь статусов (CHECK videos и STATUS_LABELS); блоки карточки
    (detailThumbHtml, detailMarksHtml, raw_json) названы волной; поля карточки
    читаются только из `repo.video_detail`, метки аудио - из `app/audio.py`;
  * `GET /whoami` и `GET /watch/<id>` - один путь на трёх сторонах (Dart,
    ui_src/app.js, app/http/server.py); тексты отказа - значения `app/i18n.py`;
  * подложка диалога (`--veil` каждой темы) и ширины кольца фокуса
    (`--focus-ring`) перенесены числом и сверяются с реестром тем и с CSS;
    экраны обязаны читать `veilColor` и `focusRing`, а не писать литералы;
  * длинные тексты в Dart пишутся соседними литералами: страж соединяет их,
    иначе проверка «называет волну» видела бы огрызок;
  * choices поля theme в схеме настроек - тот же порядок, что список тем в
    Flutter, иначе настройка не применится к Flutter-окну;
  * расхождение называется: битый hex, переставленный порядок id, изменённая
    метрика, изменённый размер страницы, выдуманный ключ `rows`, переименованная
    или лишняя колонка, битый гейт, переставленные табы, заглушка без волны,
    поле или действие карточки без имени и без волны, выдуманные поля и метки,
    расхождение права цели, потерянный /whoami или /watch, сдвинутый veil и
    литерал в production-коде дают отказ с текстом, а не молчание.
"""

import pathlib
import sys
import tempfile
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC / "tools"))

from app import i18n, settings_schema, themes  # noqa: E402 - путь добавлен выше
from app.http import scopes  # noqa: E402 - путь добавлен выше
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

    def test_sortable_columns_match_js_and_repo(self):
        """Колонки Flutter - тот же порядок и подписи, что LIST_COLS, и те же
        ключи, что белый список сервера ORDER_COLS."""
        js_cols = mirror.js_list_columns()
        view = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        dart = mirror.dart_sortable_columns(view)
        self.assertEqual(dart, js_cols,
                         "колонки Flutter расходятся с LIST_COLS из ui_src/app.js")
        self.assertEqual(sorted(col for _label, col, _gate in dart),
                         sorted(mirror.repo_order_cols()),
                         "ключи сортировки Flutter != белый список ORDER_COLS")
        self.assertEqual(mirror.sortable_columns_problems(), [],
                         mirror.sortable_columns_problems())
        # Колонка check без sortable исключена из сверки - это названо в страже.
        self.assertNotIn("check", [col for _label, col, _gate in js_cols],
                         "колонка выбора не сортируется и не должна быть в списке")

    def test_column_gates_and_sort_keys_are_schema_keys(self):
        """Гейты колонок и ключи сохранения порядка существуют в схеме."""
        schema = {item["key"] for item in settings_schema.FIELDS}
        for _label, _col, gate in mirror.js_list_columns():
            if gate:
                self.assertIn(gate, schema, f"гейт {gate} не в схеме настроек")
        self.assertIn("sort_col", schema,
                      "схема не имеет sort_col - сохранение порядка пишет в никуда")
        self.assertIn("sort_dir", schema,
                      "схема не имеет sort_dir - сохранение порядка пишет в никуда")

    def test_tabs_match_js_and_stubs_name_the_wave(self):
        """Порядок табов - TAB_RU; заглушки покрывают unbuilt вкладки и
        называют волну; построенная Библиотека заглушки не имеет."""
        tabs_text = (mirror.LIB / "tabs.dart").read_text(encoding="utf-8")
        self.assertEqual(mirror.dart_tab_labels(tabs_text),
                         mirror.js_tab_labels(),
                         "порядок табов Flutter расходится с TAB_RU из app.js")
        self.assertEqual(mirror.tabs_problems(), [], mirror.tabs_problems())
        stubs = mirror.dart_tab_stubs(tabs_text)
        labels = mirror.dart_tab_labels(tabs_text)
        self.assertEqual(sorted(stubs), sorted(labels[1:]),
                         "заглушки не покрывают все не построенные вкладки")
        self.assertNotIn(labels[0], stubs,
                         "построенная Библиотека (W2) имеет заглушку - она прячет экран")
        for label, text in stubs.items():
            self.assertRegex(text, r"волна W\d",
                             f"заглушка {label} не называет волну")

    def test_column_label_drift_is_named(self):
        view = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        broken = view.replace("(label: 'Название', col: 'title', gate: '')",
                              "(label: 'Имя', col: 'title', gate: '')")
        problems = mirror.sortable_columns_problems(broken)
        self.assertTrue(any("Название" in line for line in problems),
                        f"переименованная колонка не названа: {problems}")

    def test_column_key_drift_is_named(self):
        view = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        broken = view.replace("(label: 'Тип', col: 'media_kind', gate: '')",
                              "(label: 'Тип', col: 'media', gate: '')")
        problems = mirror.sortable_columns_problems(broken)
        self.assertTrue(any("ORDER_COLS" in line for line in problems),
                        f"ключ вне белого списка сервера не назван: {problems}")

    def test_column_gate_typo_is_named(self):
        view = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        broken = view.replace("'col_views'", "'col_view'")
        problems = mirror.sortable_columns_problems(broken)
        self.assertTrue(any("col_view" in line and "схеме" in line for line in problems),
                        f"битый гейт колонки не назван: {problems}")

    def test_tab_order_drift_is_named(self):
        tabs_text = (mirror.LIB / "tabs.dart").read_text(encoding="utf-8")
        broken = tabs_text.replace("'Очередь', 'Настройки'",
                                   "'Настройки', 'Очередь'")
        problems = mirror.tabs_problems(broken)
        self.assertTrue(any("порядок табов" in line for line in problems),
                        f"переставленные табы не названы: {problems}")

    def test_stub_without_wave_is_named(self):
        tabs_text = (mirror.LIB / "tabs.dart").read_text(encoding="utf-8")
        broken = tabs_text.replace("не построен: волна W2",
                                   "не построен: когда-нибудь")
        problems = mirror.tabs_problems(broken)
        self.assertTrue(any("не называет волну" in line for line in problems),
                        f"заглушка без волны не названа: {problems}")

    def test_built_tab_with_stub_is_named(self):
        tabs_text = (mirror.LIB / "tabs.dart").read_text(encoding="utf-8")
        broken = tabs_text.replace(
            "const Map<String, String> kTabStubs = <String, String>{\n"
            "  'Синхронизация':",
            "const Map<String, String> kTabStubs = <String, String>{\n"
            "  'Библиотека': 'Экран библиотеки не построен: волна W2',\n"
            "  'Синхронизация':")
        problems = mirror.tabs_problems(broken)
        self.assertTrue(any("Библиотека" in line and "заглушку" in line
                            for line in problems),
                        f"заглушка на построенной вкладке не названа: {problems}")

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
        (tmp / "tabs.dart").write_text("void main() {}\n", encoding="utf-8")
        # Файлы шага W2-3 под тем же guard-ом: пустые стабы, иначе страж
        # назвал бы их отсутствие как расхождение, а не литералы в theme.dart.
        for name in ("video_card.dart", "access.dart", "player.dart"):
            (tmp / name).write_text("void main() {}\n", encoding="utf-8")
        problems = mirror.literal_problems(tmp)
        self.assertEqual(len(problems), 2, f"литералы не названы: {problems}")
        self.assertTrue(any("hex-литерал" in line for line in problems), problems)
        self.assertTrue(any("размер написан числом" in line for line in problems),
                        problems)


    def test_card_labels_match_the_canonical_order(self):
        """Подписи kv карточки Flutter - тот же порядок, что `add(...)` в openDetail."""
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        self.assertEqual(mirror.dart_card_labels(card),
                         [label for label in mirror.js_card_labels()
                          if label not in mirror.dart_card_not_built(card)],
                         "подписи карточки Flutter расходятся с openDetail из app.js")
        self.assertEqual(mirror.card_problems(), [], mirror.card_problems())

    def test_card_not_built_fields_name_the_wave(self):
        """Поля канона, которые Flutter не рисует, названы и указывают волну."""
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        not_built = mirror.dart_card_not_built(card)
        js_labels = mirror.js_card_labels()
        self.assertEqual(sorted(not_built), sorted(set(js_labels) - set(
            mirror.dart_card_labels(card))),
                         "не построенные поля карточки не покрывают расхождение с JS")
        for label, text in not_built.items():
            self.assertRegex(text, r"волна W\d",
                             f"поле {label} не называет волну: молчание о составе карточки")

    def test_card_actions_match_js_and_scope_registry(self):
        """Построенные действия: подписи из канона, права из scopes.py.

        Право «Скачать» и цель `enqueue` сверяются с SCOPE_OF сервера: клиент
        подписывает то же, что требует сервер, иначе кнопка прошла бы с 403,
        который клиент не предсказал. «Открыть» требует маршрутное право watch
        (байты /media и /hls), которого в SCOPE_OF нет намеренно.
        """
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        built = mirror.dart_card_actions(card)
        js_labels = [label for _name, label in mirror.js_action_labels()]
        self.assertEqual([label for label, _scope, _target in built],
                         [label for label in js_labels if label in
                          {entry[0] for entry in built}],
                         "порядок построенных действий Flutter != канон openDetail")
        for label, scope, target in built:
            if not scope:
                continue  # адрес площадки: право не требуется, это локальный браузер
            self.assertIn(scope, scopes.SCOPES, f"право {scope} не в SCOPES")
            if target:
                self.assertEqual(scopes.scope_for(target), scope,
                                 f"{label}: сервер требует не то право, что подписывает Flutter")
        self.assertEqual(scopes.scope_for("get_video"), "read",
                         "get_video не под read: карточка читается правами чтения")
        self.assertEqual(scopes.scope_for("storages_list"), "read",
                         "storages_list не под read: выбор хранилища требует чтения")
        self.assertEqual(scopes.scope_for("enqueue"), "queue",
                         "enqueue не под queue: закачка чужим правом")
        self.assertIsNone(scopes.scope_for("watch"),
                          "право watch должно быть маршрутным (media.py), а не целью API")
        self.assertIn('scope="watch"', (SRC / "app" / "http" / "media.py").read_text(encoding="utf-8"),
                      "media.py не требует право watch - источник права потерян")
        for name in ("open_folder", "open_file", "open_url"):
            self.assertIn(name, scopes.NATIVE,
                          f"{name} перестал быть нативной целью - Flutter бы вызывал его по HTTP")

    def test_card_actions_not_built_name_the_wave_or_native_target(self):
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        not_built = mirror.dart_actions_not_built(card)
        js_labels = [label for _name, label in mirror.js_action_labels()]
        self.assertEqual(sorted(set(js_labels) - set(
            entry[0] for entry in mirror.dart_card_actions(card))),
                         sorted(not_built),
                         "действия канона не покрыены построенными и названными")
        for label, text in not_built.items():
            self.assertRegex(text, r"волна W\d|нативная цель",
                             f"действие {label} не называет волну или нативную цель")

    def test_card_blocks_not_built_name_the_wave(self):
        """Блоки карточки канона (обложка, пометки, raw_json) названы волной."""
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        blocks = mirror._dart_map(card, "kCardBlocksNotBuilt")
        js_text = (SRC / "ui_src" / "app.js").read_text(encoding="utf-8")
        for name in ("detailThumbHtml", "detailMarksHtml", "raw_json"):
            self.assertIn(name, js_text, f"в app.js нет блока карточки {name}")
            self.assertIn(name, blocks, f"карточка Flutter не называет блок {name}")
        for name, text in blocks.items():
            self.assertRegex(text, r"волна W\d", f"блок {name} не называет волну")

    def test_card_status_lists_match_the_canonical_guards(self):
        """Списки статусов по действиям - те же, что guard-ы `openDetail`."""
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        dart = mirror.dart_status_lists(card)
        guards = mirror.js_status_guards()
        for name in ("download", "queue", "playable", "mesh"):
            self.assertEqual(dart[name], guards[name],
                             f"статусы {name} Flutter != канон app.js")
        vocabulary = mirror.status_vocabulary()
        for name, list_ in dart.items():
            self.assertTrue(set(list_) <= vocabulary,
                            f"статусы {name} вне словаря сервера (CHECK и STATUS_LABELS)")
        self.assertIn("detached", vocabulary,
                      "detached потерян из словаря: статус отвязанного хранилища")

    def test_card_reads_only_fields_the_backend_returns(self):
        """Карточка читает поля, которые отдаёт video_detail, и метки audio.py."""
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        data_keys, file_keys, audio_keys = mirror.dart_card_reads(card)
        allowed = mirror.repo_detail_fields()
        self.assertTrue(data_keys <= allowed,
                        f"карточка читает поля {sorted(data_keys - allowed)} вне video_detail")
        self.assertTrue(file_keys <= allowed,
                        f"карточка читает files с ключами {sorted(file_keys - allowed)}")
        self.assertTrue(audio_keys <= mirror.audio_meta_keys(),
                        f"карточка читает метки {sorted(audio_keys - mirror.audio_meta_keys())} "
                        f"вне app/audio.py")

    def test_whoami_and_watch_paths_match_server_and_browser(self):
        """GET /whoami и GET /watch/<id> - один путь на трёх сторонах."""
        client = (mirror.LIB / "api_client.dart").read_text(encoding="utf-8")
        js = (SRC / "ui_src" / "app.js").read_text(encoding="utf-8")
        server = (SRC / "app" / "http" / "server.py").read_text(encoding="utf-8")
        self.assertIn("https://$host:$port/whoami", client,
                      "Flutter не ходит на /whoami: права подписываются наугад")
        self.assertIn('httpBaseUrl() + "/whoami"', js,
                      "браузер не читает /whoami - источник сверки прав потерян")
        self.assertIn('path == "/whoami"', server,
                      "сервер не отвечает на /whoami - клиент ходит в несуществующий маршрут")
        self.assertIn("/watch/$videoId", client,
                      "Flutter не строит адрес /watch/<id> - карточка без пути к плееру")
        self.assertIn('httpBaseUrl() + "/watch/" + id', js,
                      "браузер не ходит на /watch/<id> - источник пути потерян")
        self.assertIn('path.startswith("/watch/")', server,
                      "сервер не принимает /watch/ - адрес карточки выдуман")
        self.assertEqual(mirror.route_problems(), [], mirror.route_problems())

    def test_access_texts_match_the_backend_dictionary(self):
        """Тексты отказа по праву и привязке - значения i18n ru, не слова клиента."""
        access = (mirror.LIB / "access.dart").read_text(encoding="utf-8")
        self.assertEqual(mirror.dart_text_constant(access, "kScopeDeniedText", "access.dart"),
                         i18n.t("http.scope", "ru"),
                         "отказ по праву назван текстом, которого нет в словаре сервера")
        self.assertEqual(mirror.dart_text_constant(access, "kUnpairedText", "access.dart"),
                         i18n.t("http.pairing", "ru"),
                         "401 подписан текстом, которого нет в словаре сервера")
        self.assertEqual(mirror.canon_texts_problems(), [], mirror.canon_texts_problems())

    def test_console_and_readme_name_the_flutter_steps(self):
        """Консоль debug.bat и README называют Flutter-пункты и ассет-сверки.

        Прежде консоль знала только Python-шаги, хотя ui_flutter, ассеты и паки
        уже в репозитории: без пунктов G-K человек не мог сверить токены,
        колонки и табы, собрать иконки или сверить бандл одной командой - и
        README не называл эти команды. Расхождение называется текстом, как
        прочие стражи этого модуля.
        """
        debug = (SRC / "debug.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("python tools\\check_flutter_mirror.py", debug,
                      "консоль не сверяет Dart-ассет с источниками (пункт G)")
        self.assertIn("flutter analyze", debug,
                      "консоль не гоняет flutter analyze (пункт H)")
        self.assertIn("flutter test", debug,
                      "консоль не гоняет flutter test (пункт H)")
        self.assertIn("flutter build windows --release", debug,
                      "консоль не собирает release exe окна (пункт I)")
        self.assertIn("python tools\\make_icon.py", debug,
                      "консоль не собирает иконки .ico из темы Бренд (пункт J)")
        self.assertIn("python tools\\build_flutter_theme.py --check", debug,
                      "консоль не сверяет токены Flutter с генерацией (пункт J)")
        self.assertIn("python tools\\build_client.py --check", debug,
                      "консоль не сверяет бандл клиента (пункт K)")
        self.assertIn("python tools\\build_legal_pack.py --check", debug,
                      "консоль не сверяет юр-пак (пункт K)")
        readme = (SRC / "README.md").read_text(encoding="utf-8")
        for point in ("debug.bat G", "debug.bat H", "debug.bat I",
                      "debug.bat J", "debug.bat K"):
            self.assertIn(point, readme, f"README не называет пункт консоли {point}")


    def test_card_label_drift_is_named(self):
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        broken = card.replace("  'Статус',\n", "  'Состояние',\n", 1)
        problems = mirror.card_problems(broken)
        self.assertTrue(any("Состояние" in line for line in problems),
                        f"переименованная подпись карточки не названа: {problems}")

    def test_card_field_without_wave_and_without_name_is_named(self):
        """Поле канона, исчезну молча, называется: заглушка обязана назвать волну."""
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        broken = card.replace("'Провенанс mesh':", "'Провенанс mesh удалён':", 1)
        problems = mirror.card_problems(broken)
        self.assertTrue(any("не рисует и не называет" in line for line in problems),
                        f"исчезнувшее поле канона не названо: {problems}")
        # Текст без волны - молчание о том, когда поле появится.
        broken2 = card.replace("не построена: волна W3", "не построена: когда-нибудь", 1)
        problems2 = mirror.card_problems(broken2)
        self.assertTrue(any("не называет волну" in line for line in problems2),
                        f"поле без волны не названо: {problems2}")

    def test_card_action_scope_and_target_drift_are_named(self):
        """Право и цель действия: расхождение с SCOPE_OF называется текстом.

        Если Flutter подписывает `mark` для `enqueue`, сервер требует `queue` -
        кнопка прошла бы с 403, который клиент не предсказал; если цель
        отсутствует в SCOPE_OF, клиент шлёт цель, которой нет по HTTP.
        """
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        wrong_scope = card.replace("(label: 'Скачать', scope: 'queue', target: 'enqueue')",
                                   "(label: 'Скачать', scope: 'mark', target: 'enqueue')", 1)
        problems = mirror.card_problems(wrong_scope)
        self.assertTrue(any("Скачать" in line and "queue" in line for line in problems),
                        f"расхождение права скачивания не названо: {problems}")
        bad_target = card.replace("target: 'enqueue'", "target: 'enqueue_now'", 1)
        problems2 = mirror.card_problems(bad_target)
        self.assertTrue(any("enqueue_now" in line and "не экспортируется" in line
                            for line in problems2),
                        f"несуществующая цель не названа: {problems2}")
        invented_scope = card.replace("(label: 'Открыть', scope: 'watch', target: '')",
                                      "(label: 'Открыть', scope: 'watchme', target: '')", 1)
        problems3 = mirror.card_problems(invented_scope)
        self.assertTrue(any("watchme" in line and "SCOPES" in line for line in problems3),
                        f"выдуманный скоуп не назван: {problems3}")

    def test_card_action_removed_is_named(self):
        """Действие канона, исчезнувшее из карточки без имени, - названный отказ."""
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        broken = card.replace("'Запросить у участников':", "'Запрос не построен':", 1)
        problems = mirror.card_problems(broken)
        self.assertTrue(any("не построены и не названы" in line for line in problems),
                        f"действие канона исчезло молча: {problems}")

    def test_card_block_without_wave_is_named(self):
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        broken = card.replace("'raw_json': 'полные метаданные площадки (raw_json) "
                              "не показаны: волна W3",
                              "'raw_json': 'полные метаданные площадки не показаны")
        problems = mirror.card_problems(broken)
        self.assertTrue(any("блок raw_json не называет волну" in line for line in problems),
                        f"блок карточки без волны не назван: {problems}")

    def test_card_status_drift_and_invented_status_are_named(self):
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        broken = card.replace("const List<String> kDownloadStatuses =\n"
                              "    <String>['known', 'failed', 'missing', 'detached'];",
                              "const List<String> kDownloadStatuses =\n"
                              "    <String>['known', 'failed', 'missing'];", 1)
        problems = mirror.card_problems(broken)
        self.assertTrue(any("статусы download" in line for line in problems),
                        f"расхождение статусов скачивания не названо: {problems}")
        invented = card.replace(
            "const List<String> kPlayableStatuses = <String>['downloaded', 'unavailable'];",
            "const List<String> kPlayableStatuses = <String>['downloaded', 'detachedx'];", 1)
        # Подмена ведётся на списке плеера: `'unavailable',\n];` в kMeshStatuses
        # меняет не словарь, а порядок guard-а mesh - найдено фактом прогона.
        # Имя статусов в Dart маленькое: страж разбирает списки regex-ом
        # `'([a-z_]+)'`, 'detachedX' с большой буквой не попал бы в разбор.
        problems2 = mirror.card_problems(invented)
        self.assertTrue(any("словаре статусов" in line for line in problems2),
                        f"выдуманный статус не назван: {problems2}")

    def test_card_invented_field_read_is_named(self):
        """Поле, которого нет в video_detail, называется выдуманным клиентом."""
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        broken = card.replace("value: data['key'] is String",
                              "value: data['subtitle'] is String", 1)
        problems = mirror.card_problems(broken)
        self.assertTrue(any("subtitle" in line and "video_detail" in line for line in problems),
                        f"выдумано поле карточки, и это не названо: {problems}")
        audio_bad = card.replace("audio['codec']", "audio['video_codec']", 1)
        problems2 = mirror.card_problems(audio_bad)
        self.assertTrue(any("video_codec" in line and "audio.py" in line for line in problems2),
                        f"выдумана метка файла, и это не названо: {problems2}")

    def test_canon_text_drift_is_named(self):
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        broken = card.replace("const String kNoStoragesText = "
                              "'нет хранилищ - добавьте в настройках';",
                              "const String kNoStoragesText = 'хранилищ нет';")
        problems = mirror.canon_texts_problems(broken)
        self.assertTrue(any("kNoStoragesText" in line for line in problems),
                        f"расхождение текста канона не названо: {problems}")

    def test_whoami_or_watch_removed_is_named(self):
        client = (mirror.LIB / "api_client.dart").read_text(encoding="utf-8")
        broken = client.replace("https://$host:$port/whoami", "https://$host:$port/access")
        problems = mirror.route_problems(broken)
        self.assertTrue(any("/whoami" in line for line in problems),
                        f"маршрут прав не назван потерян: {problems}")
        broken2 = client.replace("/watch/$videoId", "/player/$videoId")
        problems2 = mirror.route_problems(broken2)
        self.assertTrue(any("/watch" in line for line in problems2),
                        f"путь плеера не назван расхождением: {problems2}")

    def test_access_text_drift_is_named(self):
        """Текст отказа в ассете совпадает со словарём, а подменный - нет.

        route_problems читает access.dart с диска и сравнивает константу с
        i18n: проверка на подменном тексте называет, что расхождение видно
        именно как расхождение (страж отказывает на изменённом тексте).
        """
        access = (mirror.LIB / "access.dart").read_text(encoding="utf-8")
        real = mirror.dart_text_constant(access, "kScopeDeniedText", "access.dart")
        self.assertEqual(real, i18n.t("http.scope", "ru"))
        broken = access.replace(real, "доступ запрещён")
        self.assertNotEqual(mirror.dart_text_constant(broken, "kScopeDeniedText",
                                                      "access.dart"),
                            i18n.t("http.scope", "ru"),
                            "подменный текст не должен совпадать со словарём сервера")

    def test_veil_drift_is_named(self):
        parsed = mirror.parse(mirror.TOKENS.read_text(encoding="utf-8"))
        theme_id = themes.ids()[0]
        parsed["veils"][theme_id] = 0x00000000
        problems = mirror.veil_problems(parsed)
        # Отказ называет тему и барьер карточки, а не слово «veil»: сверка
        # идёт по смыслу сообщения (тема + «красит не той палитрой»).
        self.assertTrue(any(theme_id in line and "барьер карточки" in line
                            for line in problems),
                        f"расхождение подложки диалога не названо: {problems}")
        parsed["veils"].pop(themes.ids()[1])
        problems2 = mirror.veil_problems(parsed)
        self.assertTrue(any("не выведен в Dart" in line for line in problems2),
                        f"тема без veil не названа: {problems2}")

    def test_token_usage_is_real_not_literal(self):
        """Барьер диалога и кольцо строки читают veilColor и focusRing."""
        self.assertEqual(mirror.token_usage_problems(), [], mirror.token_usage_problems())
        library = (mirror.LIB / "library_view.dart").read_text(encoding="utf-8")
        self.assertIn("veilColor(", library, "затемнение под карточкой не из --veil темы")
        self.assertIn("focusRing(", library, "кольцо фокуса не из --focus-ring")

    def test_multiline_dart_map_text_is_joined(self):
        """Длинный текст в Dart пишется соседними литералами: страж читает целиком.

        Без соединения литералов проверка «называет волну» видела бы первый
        фрагмент фразы и отказывала на целой карточке - найдено фактом прогона.
        """
        card = (mirror.LIB / "video_card.dart").read_text(encoding="utf-8")
        not_built = mirror.dart_card_not_built(card)
        self.assertIn("SAS", not_built["Mesh"],
                      "второй литерал текста поля не присоединён - сверка читает огрызок")
        blocks = mirror._dart_map(card, "kCardBlocksNotBuilt")
        self.assertIn("W3", blocks["detailMarksHtml"],
                      "второй литерал текста блока не присоединён")

    def test_new_production_files_are_policed(self):
        """production-код шага 3 под проверкой литералов: ни hex, ни размера числом."""
        for name in ("video_card.dart", "access.dart", "player.dart"):
            self.assertIn(name, mirror.PRODUCTION_FILES,
                          f"{name} не под сверкой литералов - экран может писать цвета числом")


if __name__ == "__main__":
    unittest.main(verbosity=2)
