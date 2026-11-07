r"""Mirror-страж токенов темы: Dart-ассет сверяется с реестром и с CSS.

    python tools/check_flutter_mirror.py           # сверка, отказ называет причину
    python tools/check_flutter_mirror.py --check   # тот же ответ для CI-строки

GitHub-раннер Flutter не гоняет (docs/FLUTTER-TRANSITION.md §6: UI-тесты и
Impeller меряются на машине человека), поэтому расхождение двух клиентов
сверяется текстом ассета без тулчейна: ui_flutter/lib/theme_tokens.dart
парсится как текст и сравнивается с app/themes.py (роли, подписи, флаги dark,
пары контраста, границы шрифта, формула строки) и с ui_src/app.css (:root -
метрики DESIGN-UI §2). С волной W2 сюда легли и экраны: форма ответа списка
(`rows`/`total` из app/repo.py), `PAGE`/`POLL_MS` из ui_src/app.js, карта
статусов против `.status[data-s=...]`, сортируемые колонки против `LIST_COLS`
и `ORDER_COLS`, гейты и sort_col/sort_dir против схемы настроек и табы против
`TAB_RU`. На шаге карточки видео (W2, шаг 3) сверяются подписи kv против
`add(...)` в `openDetail`, списки статусов по действиям против guard-ов
канона, построенные действия против `data-detail` и прав из
`app/http/scopes.py` (SCOPE_OF), не построенные подписи и действия обязаны
называть волну или «нативная цель», поля карточки читаются только из
`repo.video_detail`, метки аудио - из `app/audio.py`, тексты отказа - из
`app/i18n.py`, а `GET /whoami` и `GET /watch/<id>` - против ui_src/app.js и
app/http/server.py. Плюс проверка, что production-код Flutter не держит
литералов: ни hex-цвета вне токенов, ни размера или длительности числом -
имена ролей читаются из реестра, барьер диалога читает --veil темы, кольцо
строки - --focus-ring.

Это тот же приём, что tests/test_ui_settings_mirror.py для браузерного клиента:
значение берётся из одного источника, а второй сверяется с ним, иначе два
клиента на одном бэкенде расходятся незаметно для глаза.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import i18n, settings_schema, themes  # noqa: E402 - путь добавлен выше
from app.http import scopes  # noqa: E402 - путь добавлен выше
import build_flutter_theme as gen  # noqa: E402 - путь добавлен выше

TOKENS = ROOT / "ui_flutter" / "lib" / "theme_tokens.dart"
LIB = ROOT / "ui_flutter" / "lib"
CSS = ROOT / "ui_src" / "app.css"
JS = ROOT / "ui_src" / "app.js"
REPO = ROOT / "app" / "repo.py"
AUDIO = ROOT / "app" / "audio.py"
SERVER = ROOT / "app" / "http" / "server.py"
MEDIA = ROOT / "app" / "http" / "media.py"
DB = ROOT / "app" / "db.py"

# Production-код Flutter: обязан читать токены, а не писать литералы.
# main.dart и spike-файлы здесь нет намеренно: стенд W0 держит фиксированную
# геометрию списка (высота ряда 44 px), и его числа описаны в
# docs/FLUTTER-TRANSITION.md §9 - это мерка, а не дизайн-токен.
PRODUCTION_FILES = ("theme.dart", "library_view.dart", "app.dart", "tabs.dart",
                    "video_card.dart", "access.dart", "player.dart")

# Статусы без отдельного правила в CSS (.status[data-s=...]) читаются базовой
# подписью --muted: known и unavailable не имеют своей роли - это названо
# здесь же, а не молча.
DEFAULT_STATUS_ROLE = "muted"
DEFAULT_STATUSES = ("known", "unavailable")

HEX_LITERAL = re.compile(r"0x[0-9A-Fa-f]{6,8}")
SIZE_LITERAL = re.compile(r"(fontSize|letterSpacing|height):\s*[\d.]+")
DURATION_LITERAL = re.compile(r"Duration\(milliseconds:\s*[\d.]+\)")
ROLE_CALL = re.compile(r"role(?:Color)?\([^)]*?,\s*'([a-z0-9-]+)'\)")
JS_BODY_CANON = re.compile(r"arg == null \? '\{\}' : jsonEncode\(\[arg\]\)")


def _block(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.S)
    if not match:
        raise AssertionError(f"в {TOKENS.name} не найден блок {pattern!r}: "
                             "токены темы потеряны")
    return match.group(1)


def parse(text: str) -> dict:
    """Значения сгенерированного Dart-ассета (текст-в-значение)."""
    ids = [part.strip().strip("'")
           for part in _block(text, r"const List<String> kThemeIds = <String>\[([^\]]*)\]").split(",")
           if part.strip()]
    default = re.search(r"const String kDefaultThemeId = '([^']*)';", text)
    labels = dict(re.findall(r"'([a-z0-9-]+)':\s*'([^']*)'",
                             _block(text, r"kThemeLabels = <String, String>\{([^}]*)\}")))
    darks = {key: value == "true" for key, value in
             re.findall(r"'([a-z0-9-]+)':\s*(true|false)",
                        _block(text, r"kThemeDark = <String, bool>\{([^}]*)\}"))}
    colors: dict[str, dict[str, int]] = {}
    body = _block(text, r"kThemeColors =\s*<String, Map<String, int>>\{(.*)\};")
    for theme_id, inner in re.findall(r"'([a-z0-9-]+)': <String, int>\{(.*?)\}",
                                      body, flags=re.S):
        colors[theme_id] = {name: int(value, 16) for name, value in
                            re.findall(r"'([a-z0-9-]+)':\s*0xff([0-9a-f]{6})", inner)}
    pairs = [(fg, bg, float(floor)) for fg, bg, floor in re.findall(
        r"ContrastPair\('([a-z0-9-]+)', '([a-z0-9-]+)', ([\d.]+)\)",
        _block(text, r"kContrastPairs = <ContrastPair>\[(.*?)\];"))]
    fonts = dict(re.findall(r"(\w+):\s*([\d.]+)",
                            _block(text, r"kFontBounds = FontBounds\((.*?)\)")))
    metrics_block = _block(text, r"const Metrics kMetrics = Metrics\((.*?)\);")
    metrics = {}
    for name, value in re.findall(r"(\w+):\s*(Cubic\([^)]*\)|[\d.]+)", metrics_block):
        metrics[name] = value
    veils = {theme_id: int(value, 16) for theme_id, value in
             re.findall(r"'([a-z0-9-]+)':\s*0x([0-9a-f]{8})",
                        _block(text, r"kThemeVeils = <String, int>\{([^}]*)\}"))}
    return {
        "ids": ids,
        "default": default.group(1) if default else "",
        "labels": labels,
        "dark": darks,
        "colors": colors,
        "pairs": pairs,
        "fonts": fonts,
        "metrics": metrics,
        "veils": veils,
    }


def registry_problems(parsed: dict) -> list[str]:
    """Ассет против реестра тем app/themes.py: роли, подписи, флаги, пары."""
    problems = []
    if parsed["ids"] != list(themes.ids()):
        problems.append(f"порядок id тем в Dart {parsed['ids']} != "
                        f"themes.ids() {list(themes.ids())}: карточка настроек "
                        f"и Flutter перекрасят не то")
    if parsed["default"] != themes.DEFAULT_THEME:
        problems.append(f"дефолтная тема в Dart {parsed['default']!r} != "
                        f"themes.DEFAULT_THEME {themes.DEFAULT_THEME!r}")
    expected_labels = {item["id"]: item["label"] for item in themes.themes()}
    if parsed["labels"] != expected_labels:
        problems.append(f"подписи тем расходятся: Dart {parsed['labels']}, "
                        f"реестр {expected_labels}")
    expected_dark = {item["id"]: bool(item["dark"]) for item in themes.themes()}
    if parsed["dark"] != expected_dark:
        problems.append(f"флаги dark расходятся: Dart {parsed['dark']}, "
                        f"реестр {expected_dark}")
    for theme_id in themes.ids():
        dart_roles = parsed["colors"].get(theme_id)
        if dart_roles is None:
            problems.append(f"тема {theme_id}: роли не выведены в Dart - "
                            "окно Flutter останется без палитры")
            continue
        registry = themes.css_vars(theme_id)
        for name in themes.PALETTE_VARS:
            css = name.lstrip("-")
            expected = int(registry[name][1:], 16)
            got = dart_roles.get(css)
            if got is None:
                problems.append(f"{theme_id} {css}: роли нет в Dart")
                continue
            if got != expected:
                problems.append(f"{theme_id} {css}: Dart 0xff{got:06x} != "
                                f"реестр {registry[name]}")
        extra = sorted(set(dart_roles) - {name.lstrip("-") for name in themes.PALETTE_VARS})
        if extra:
            problems.append(f"{theme_id}: в Dart есть роли вне реестра {extra}")
    expected_pairs = [(fg.lstrip("-"), bg.lstrip("-"), float(floor))
                      for fg, bg, floor in themes.CONTRAST_PAIRS]
    if parsed["pairs"] != expected_pairs:
        problems.append(f"пары контраста расходятся: Dart {parsed['pairs'][:4]}..., "
                        f"реестр {expected_pairs[:4]}... (всего пар "
                        f"{len(expected_pairs)})")
    fonts = parsed["fonts"]
    expected_fonts = {
        "sizeMin": float(themes.FONT_SIZE_MIN),
        "sizeMax": float(themes.FONT_SIZE_MAX),
        "sizeDefault": float(themes.FONT_SIZE_DEFAULT),
        "scaleMin": float(themes.FONT_SCALE_MIN),
        "scaleMax": float(themes.FONT_SCALE_MAX),
        "scaleDefault": float(themes.FONT_SCALE_DEFAULT),
        "rowMin": float(themes.ROW_MIN),
        "rowFactor": float(themes.ROW_FACTOR),
    }
    got_fonts = {name: float(value) for name, value in fonts.items()}
    if got_fonts != expected_fonts:
        problems.append(f"границы шрифта и формула строки расходятся: Dart "
                        f"{got_fonts}, константы app/themes.py {expected_fonts}")
    return problems


def metrics_problems(parsed: dict) -> list[str]:
    """Метрики в Dart против :root в ui_src/app.css (DESIGN-UI §2)."""
    expected = gen.metric_values()
    problems = []
    if set(parsed["metrics"]) != set(expected):
        missing = sorted(set(expected) - set(parsed["metrics"]))
        extra = sorted(set(parsed["metrics"]) - set(expected))
        if missing:
            problems.append(f"метрики не выведены в Dart: {missing}")
        if extra:
            problems.append(f"в Dart есть метрики вне :root: {extra}")
    for name, value in expected.items():
        got = parsed["metrics"].get(name)
        if got is None:
            continue
        if got != value:
            problems.append(f"{name}: Dart {got} != CSS {value} "
                            "(значение берётся из блока :root в app.css)")
    return problems


def _clean_css(text: str) -> str:
    """CSS без комментариев: комментарии объясняют ловушки и не код."""
    return re.sub(r"/\s*/\*.*?\*/\s*", "", text, flags=re.S)


def css_status_roles() -> dict[str, str]:
    """Роли цвета статусов из ui_src/app.css (.status[data-s=...]).

    Статусы без отдельного правила (known, unavailable) читаются базовой
    подписью `.status { color: ... }` - это названо, а не молча.
    """
    text = _clean_css(CSS.read_text(encoding="utf-8"))
    explicit = dict(re.findall(
        r'\.status\[data-s="([a-z]+)"\]\s*\{\s*color:\s*var\(--([a-z0-9-]+)\)',
        text))
    base = re.search(r'\.status\s*\{([^}]*)\}', text)
    base_role = None
    if base:
        match = re.search(r'color:\s*var\(--([a-z0-9-]+)\)', base.group(1))
        base_role = match.group(1) if match else None
    roles = dict(explicit)
    for status in DEFAULT_STATUSES:
        roles.setdefault(status, base_role or DEFAULT_STATUS_ROLE)
    if not roles:
        raise AssertionError("в ui_src/app.css нет правил .status[data-s=...] - "
                             "сверка карты статусов потеряла источник")
    return roles


def dart_status_roles(text: str) -> dict[str, str]:
    match = re.search(
        r"const Map<String, String> kStatusRoles = <String, String>\{(.*)\}",
        _clean_css(text), flags=re.S)
    if not match:
        raise AssertionError("в library_view.dart нет kStatusRoles - карта "
                             "цветов статусов потеряна")
    return dict(re.findall(r"'([a-z]+)':\s*'([a-z0-9-]+)'", match.group(1)))


def status_roles_problems(view_text: str | None = None) -> list[str]:
    """Карта статусов Flutter совпадает с CSS (тот же источник правды)."""
    expected = css_status_roles()
    text = (view_text if view_text is not None
            else (LIB / "library_view.dart").read_text(encoding="utf-8"))
    got = dart_status_roles(text)
    problems = []
    for status, role_name in sorted(expected.items()):
        value = got.get(status)
        if value is None:
            problems.append(f"статус {status}: роли нет в kStatusRoles - "
                            f"CSS красит её как {role_name}")
        elif value != role_name:
            problems.append(f"статус {status}: Flutter {value} != CSS {role_name}")
    extra = sorted(set(got) - set(expected))
    if extra:
        problems.append(f"kStatusRoles держит статусы вне CSS: {extra}")
    for role_name in sorted(set(got.values())):
        for theme_id in themes.ids():
            roles = {key.lstrip("-") for key in themes.css_vars(theme_id)}
            if role_name not in roles:
                problems.append(f"kStatusRoles: роль {role_name} не в теме "
                                f"{theme_id} - статус строки потеряет цвет")
    return problems


def contract_source_problems(view_text: str | None = None) -> list[str]:
    """Форма ответа списка и размеры страниц/опроса сверяются с источниками.

    Ключ `rows` и `total` - из app/repo.py: list_videos; размер страницы -
    из `PAGE` в ui_src/app.js; период опроса - из `POLL_MS` там же. Клиент
    обязан повторять значения, а не изобретать свои.
    """
    view = (view_text if view_text is not None
            else (LIB / "library_view.dart").read_text(encoding="utf-8"))
    repo_src = REPO.read_text(encoding="utf-8")
    js_src = JS.read_text(encoding="utf-8")
    problems = []
    if not re.search(r"const String kRowsKey\s*=\s*'rows'", view):
        problems.append("library_view.dart: ключ строк не 'rows' - форма ответа "
                        "фасада выдумана клиентом")
    if '"rows": rows' not in repo_src:
        problems.append("app/repo.py: list_videos не возвращает \"rows\" - "
                        "сверка потеряла источник формы ответа")
    if '"total":' not in repo_src:
        problems.append("app/repo.py: list_videos не возвращает \"total\" - "
                        "сверка потеряла источник счётчика строк")
    js_page = re.search(r"var PAGE\s*=\s*(\d+)", js_src)
    dart_page = re.search(r"const int kPageLimit\s*=\s*(\d+)", view)
    if not js_page or not dart_page:
        problems.append("размер страницы не найден в одном из источников "
                        "(ui_src/app.js: PAGE или library_view: kPageLimit)")
    elif js_page.group(1) != dart_page.group(1):
        problems.append(f"kPageLimit {dart_page.group(1)} != PAGE "
                        f"{js_page.group(1)} из ui_src/app.js: два клиента тянут "
                        "разное число строк за запрос")
    js_poll = re.search(r"var POLL_MS\s*=\s*(\d+)", js_src)
    dart_poll = re.search(r"const int kPollIntervalMs\s*=\s*(\d+)", view)
    if not js_poll or not dart_poll:
        problems.append("период опроса не найден в одном из источников "
                        "(ui_src/app.js: POLL_MS или library_view: kPollIntervalMs)")
    elif js_poll.group(1) != dart_poll.group(1):
        problems.append(f"kPollIntervalMs {dart_poll.group(1)} != POLL_MS "
                        f"{js_poll.group(1)} из ui_src/app.js: состояние опрачивается "
                        "с разной частотой")
    return problems


# Колонка «check» в LIST_COLS не имеет sortable - она не сортируется, и
# сверка колоннок исключает её; это названо здесь, а не молча.
NON_SORTABLE_COL = "check"
# platform в TAB_RU - динамические вкладки площадок (app.js:5955), не часть
# основной панели табов; сверка табов исключает их; это названо здесь же.
DYNAMIC_TAB = "platform"


def js_list_columns() -> list[tuple[str, str, str]]:
    """Сортируемые колонки из `LIST_COLS` в ui_src/app.js: (label, col, gate)."""
    js = JS.read_text(encoding="utf-8")
    block = re.search(r"var LIST_COLS = \[(.*?)\];", js, flags=re.S)
    if not block:
        raise AssertionError("в ui_src/app.js нет блока LIST_COLS - "
                             "сверка колонок потеряла источник")
    entries = re.findall(
        r'\{\s*key:\s*"([a-z0-9_-]+)"[^}]*?label:\s*"([^"]*)"'
        r'(?:[^}]*?sortable:\s*"([a-z_]+)")?(?:[^}]*?setting:\s*"([a-z_]+)")?\s*\}',
        block.group(1))
    columns = [(label, sortable, setting)
               for key, label, sortable, setting in entries if sortable]
    if not columns:
        raise AssertionError("в LIST_COLS нет ни одной sortable колонки - "
                             "источник сверки пуст")
    return columns


def repo_order_cols() -> list[str]:
    """Белый список колонок сортировки сервера: ключи ORDER_COLS в app/repo.py."""
    src = REPO.read_text(encoding="utf-8")
    block = re.search(r"ORDER_COLS: dict\[str, str\] = \{(.*?)\n\}", src,
                      flags=re.S)
    if not block:
        raise AssertionError("в app/repo.py нет ORDER_COLS - белый список "
                             "сервера потерян, сверка не имеет источника")
    keys = re.findall(r'"([a-z_]+)":\s', block.group(1))
    if not keys:
        raise AssertionError("ORDER_COLS пуст - сервер не принимает ни одной "
                             "колонки сортировки")
    return keys


def dart_sortable_columns(text: str) -> list[tuple[str, str, str]]:
    """Сортируемые колонки из `kSortableColumns` в library_view.dart."""
    block = re.search(
        r"const List<\(\{String label, String col, String gate\}\)>\s*"
        r"kSortableColumns\s*=\s*<\(\{String label, String col, String gate\}\)>\[(.*?)\];",
        text, flags=re.S)
    if not block:
        raise AssertionError("в library_view.dart нет kSortableColumns - "
                             "список колонок Flutter потерян")
    return [tuple(match) for match in re.findall(
        r"\(\s*label:\s*'([^']*)',\s*col:\s*'([^']*)',\s*gate:\s*'([^']*)'\s*\)",
        block.group(1))]


def sortable_columns_problems(view_text: str | None = None) -> list[str]:
    """Колонки Flutter сверены с ui_src/app.js и с белым списком сервера.

    Порядок и подписи - из `LIST_COLS`; ключи сортировки - обязаны входить в
    `ORDER_COLS` app/repo.py (иначе сервер молча держит default order);
    гейты колонок - существующие ключи схемы настроек; sort_col/sort_dir -
    тоже в схеме, иначе сохранение порядка пишет несуществующую настройку.
    """
    expected = js_list_columns()
    view = (view_text if view_text is not None
            else (LIB / "library_view.dart").read_text(encoding="utf-8"))
    got = dart_sortable_columns(view)
    problems = []
    if got != expected:
        problems.append(f"колонки Flutter {got} != LIST_COLS из ui_src/app.js "
                        f"{expected}: порядок или подписи расходятся - два "
                        "клиента рисуют разные колонки и разно порядок строк")
    dart_keys = [col for _label, col, _gate in got]
    repo_keys = repo_order_cols()
    missing = sorted(set(repo_keys) - set(dart_keys))
    extra = sorted(set(dart_keys) - set(repo_keys))
    if missing:
        problems.append(f"Flutter не сортирует ключи {missing}, которые "
                        "принимает сервер (app/repo.py: ORDER_COLS) - клик по "
                        "колонке теряет половину порядка браузера")
    if extra:
        problems.append(f"Flutter шлёт ключи {extra}, которых нет в белом "
                        "списке сервера ORDER_COLS - сервер молча держит "
                        "default order, и список выглядит рассортированным зря")
    schema_keys = {item["key"] for item in settings_schema.FIELDS}
    for label, _col, gate in got:
        if gate and gate not in schema_keys:
            problems.append(f"колонка {label}: гейт {gate} не в схеме "
                            "настроек - колонка не скрывается по настройке")
    js_gates = {gate for _label, _col, gate in expected if gate}
    unknown = sorted(js_gates - schema_keys)
    if unknown:
        problems.append(f"гейты {unknown} из LIST_COLS не в схеме настроек - "
                        "сверка потеряла источник скрытия колонок")
    for key in ("sort_col", "sort_dir"):
        if key not in schema_keys:
            problems.append(f"схема настроек не имеет ключа {key} - "
                            "сохранение порядка сортировки пишет в никуда")
    return problems


def js_tab_labels() -> list[str]:
    """Подписи табов из `TAB_RU` в ui_src/app.js:5739, в порядке словаря."""
    js = JS.read_text(encoding="utf-8")
    block = re.search(r"var TAB_RU = \{(.*?)\};", js, flags=re.S)
    if not block:
        raise AssertionError("в ui_src/app.js нет TAB_RU - подписи табов "
                             "потеряны, сверка не имеет источника")
    return [label for key, label in re.findall(r'([a-z_]+):\s*"([^"]*)"',
                                               block.group(1))
            if key != DYNAMIC_TAB]


def dart_tab_labels(tabs_text: str) -> list[str]:
    match = re.search(r"const List<String> kTabLabels = <String>\[(.*?)\];",
                      tabs_text, flags=re.S)
    if not match:
        raise AssertionError("в tabs.dart нет kTabLabels - порядок табов потерян")
    return re.findall(r"'([^']*)'", match.group(1))


def dart_tab_stubs(tabs_text: str) -> dict[str, str]:
    """Заглушки unbuilt вкладок: подпись -> первый фрагмент текста панели."""
    match = re.search(
        r"const Map<String, String> kTabStubs = <String, String>\{(.*?)\};",
        tabs_text, flags=re.S)
    if not match:
        raise AssertionError("в tabs.dart нет kTabStubs - заглушки не "
                             "построенных вкладок потеряны")
    return dict(re.findall(r"'([^']+)':\s*'([^']*)'", match.group(1)))


def tabs_problems(tabs_text: str | None = None) -> list[str]:
    """Табы Flutter сверены с TAB_RU; заглушки называют, а не молчат.

    Первая вкладка (Библиотека) построена на W2 - заглушки для неё быть не
    должно; каждая unbuilt вкладка обязана называть волну, иначе человек не
    знает, когда экран появится, и пустая панель выглядит «пустым экраном».
    """
    if tabs_text is None:
        tabs_file = LIB / "tabs.dart"
        if not tabs_file.is_file():
            return ["tabs.dart не построен: табы окна (W2, шаг 2) отсутствуют"]
        tabs_text = tabs_file.read_text(encoding="utf-8")
    problems = []
    labels = dart_tab_labels(tabs_text)
    expected = js_tab_labels()
    if labels != expected:
        problems.append(f"порядок табов Flutter {labels} != TAB_RU из "
                        f"ui_src/app.js {expected} (без динамических вкладок "
                        "площадок): два клиента показывают вкладки в разном порядке")
    stubs = dart_tab_stubs(tabs_text)
    if labels and labels[0] in stubs:
        problems.append(f"вкладка {labels[0]} имеет заглушку, но её экран "
                        "построен на W2: заглушка прячет построенную панель")
    missing = [label for label in labels[1:] if label not in stubs]
    if missing:
        problems.append(f"вкладки {missing} не имеют заглушки: не построенный "
                        "экран показан молча - это выглядит пустым экраном")
    extra = sorted(set(stubs) - set(labels[1:]))
    if extra:
        problems.append(f"kTabStubs держит подписи вне табов: {extra}")
    for label, text in stubs.items():
        if not re.search(r"волна W\d", text):
            problems.append(f"заглушка {label} не называет волну: человек не "
                            "знает, когда экран будет построен")
    return problems


# -- карточка видео (W2, шаг 3): подписи, статусы, права и тексты ---------------

def _detail_kv_block(js_text: str) -> str:
    start = js_text.find("function openDetail(")
    if start < 0:
        raise AssertionError("в ui_src/app.js нет openDetail - сверка карточки "
                             "потеряла источник")
    end = js_text.find("<dl class=\"kv\">", start)
    if end < 0:
        raise AssertionError("в ui_src/app.js openDetail не строит kv-блок - "
                             "список подписей не найден")
    return js_text[start:end]


def _actions_block(js_text: str) -> str:
    start = js_text.find("var actions = [];")
    if start < 0:
        raise AssertionError("в ui_src/app.js openDetail не строит ряд действий - "
                             "сверка действий потеряла источник")
    end = js_text.find("if (actions.length)", start)
    if end < 0:
        raise AssertionError("в ui_src/app.js openDetail не завершает ряд действий")
    return js_text[start:end]


def js_card_labels(js_text: str | None = None) -> list[str]:
    """Подписи kv карточки в порядке `add(...)` из openDetail (app.js:3581+)."""
    text = js_text if js_text is not None else JS.read_text(encoding="utf-8")
    return re.findall(r'add\("([^"]+)"', _detail_kv_block(text))


def dart_card_labels(card_text: str) -> list[str]:
    match = re.search(r"const List<String> kCardLabels = <String>\[(.*?)\];",
                      card_text, flags=re.S)
    if not match:
        raise AssertionError("в video_card.dart нет kCardLabels - список подписей "
                             "карточки Flutter потерян")
    return re.findall(r"'([^']*)'", match.group(1))


def _dart_map(text: str, name: str) -> dict[str, str]:
    """Карта `Map<String, String>` из Dart-ассета: подпись -> текст.

    Значение в Dart пишется несколькими соседними литералами (перенос строк
    для читаемости), поэтому собираются все литералы до следующего ключа:
    иначе сверка «называет волну» смотрела бы только первый фрагмент и
    отказывала на целой фразе (найдено фактом прогона стража).
    """
    match = re.search(
        rf"const Map<String, String> {name} = <String, String>\{{(.*?)\}};",
        text, flags=re.S)
    if not match:
        raise AssertionError(f"в video_card.dart нет {name} - карта потеряна, "
                             "сверка не имеет источника")
    block = match.group(1)
    literals = [(m.group(1), m.end()) for m in re.finditer(r"'([^']*)'", block)]
    out: dict[str, str] = {}
    index = 0
    while index < len(literals):
        key, end = literals[index]
        if ":" not in block[end:end + 3]:
            index += 1
            continue
        parts: list[str] = []
        scan = index + 1
        while scan < len(literals):
            nxt_key, nxt_end = literals[scan]
            if ":" in block[nxt_end:nxt_end + 3]:
                break
            parts.append(nxt_key)
            scan += 1
        out[key] = "".join(parts)
        index = scan
    return out


def dart_card_not_built(card_text: str) -> dict[str, str]:
    return _dart_map(card_text, "kCardFieldsNotBuilt")


def dart_actions_not_built(card_text: str) -> dict[str, str]:
    return _dart_map(card_text, "kCardActionsNotBuilt")


def _label_from(middle: str) -> str:
    """Метка кнопки: текст перед `</button>`. Канон доклеивает метку отдельной
    строкой (`data-detail="play">' + "Открыть</button>")`), поэтому берётся
    хвост после последнего кавычки/`>` - разметка отбрасывается.
    """
    tail = middle.split('"')[-1]
    return tail.lstrip(">").strip()


def js_action_labels(js_text: str | None = None) -> list[tuple[str, str]]:
    """Действия канона: (имя data-detail, подпись кнопки) в порядке push."""
    text = js_text if js_text is not None else JS.read_text(encoding="utf-8")
    block = _actions_block(text)
    found = re.findall(r'data-detail="([a-z0-9_-]+)"([\s\S]*?)</button>', block)
    if not found:
        raise AssertionError("в ui_src/app.js openDetail не нашёл ни одной кнопки "
                             "ряда действий - источник сверки пуст")
    return [(name, _label_from(middle)) for name, middle in found]


def dart_card_actions(card_text: str) -> list[tuple[str, str, str]]:
    match = re.search(
        r"const List<\(\{String label, String scope, String target\}\)>\s*"
        r"kCardActions\s*=\s*<\(\{String label, String scope, String target\}\)>\[(.*?)\];",
        card_text, flags=re.S)
    if not match:
        raise AssertionError("в video_card.dart нет kCardActions - список "
                             "действий Flutter потерян")
    return [tuple(part) for part in re.findall(
        r"\(\s*label:\s*'([^']*)',\s*scope:\s*'([^']*)',\s*target:\s*'([^']*)'\s*\)",
        match.group(1))]


def _dart_list(text: str, name: str) -> list[str]:
    match = re.search(rf"const List<String> {name} =\s*<String>\[(.*?)\];",
                      text, flags=re.S)
    if not match:
        raise AssertionError(f"в video_card.dart нет {name} - список статусов потерян")
    return re.findall(r"'([a-z_]+)'", match.group(1))


def dart_status_lists(card_text: str) -> dict[str, list[str]]:
    return {
        "download": _dart_list(card_text, "kDownloadStatuses"),
        "queue": _dart_list(card_text, "kQueueStatuses"),
        "playable": _dart_list(card_text, "kPlayableStatuses"),
        "mesh": _dart_list(card_text, "kMeshStatuses"),
    }


def js_status_guards(js_text: str | None = None) -> dict[str, list[str]]:
    """Статусы по действиям из канона: массивы `indexOf(data.status)` и пары
    `data.status === "..."` в порядке появления.

    Первый indexOf - «Скачать», второй - mesh; первые два `===` - «К очереди»,
    следующие два - «Открыть» (остальные `===` в этом блоке - отзыв mesh-копии,
    он проверяется на принадлежность скачанному статусу).
    """
    text = js_text if js_text is not None else JS.read_text(encoding="utf-8")
    block = _actions_block(text)
    indexed = [re.findall(r'"([a-z_]+)"', inner) for inner in
               re.findall(r"\[([^\]]*?)\]\s*\.indexOf\(data\.status\)", block)]
    if len(indexed) != 2:
        raise AssertionError(f"в ui_src/app.js найдено массивов статусов {len(indexed)}, "
                             "ожидается 2 (Скачать и mesh) - сверка потеряла источник")
    eq = re.findall(r'data\.status === "([a-z_]+)"', block)
    if len(eq) < 4:
        raise AssertionError(f"в ui_src/app.js пар `===` найдено {len(eq)}, "
                             "ожидается минимум 4 (очередь и плеер) - источник неполон")
    return {"download": indexed[0], "mesh": indexed[1],
            "queue": eq[:2], "playable": eq[2:4], "rest": eq[4:]}


def _select_columns(select_text: str) -> set[str]:
    """Имена колонок из SELECT: `id, kind, path`, `p.title`, `c.title AS channel`."""
    names = set()
    for part in select_text.split(","):
        part = part.strip()
        if not part:
            continue
        alias = re.search(r"\bAS\s+([a-z_]+)", part)
        if alias:
            names.add(alias.group(1))
            continue
        plain = re.match(r"(?:[a-z_]+\.)?([a-z_]+)", part)
        if plain:
            names.add(plain.group(1))
    return names


def _table_columns(db_src: str, table: str) -> set[str]:
    """Колонки таблицы из схемы db.py: строки `name TYPE` внутри CREATE."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \(([\s\S]*?)\n        \);", db_src)
    if not match:
        return set()
    return set(re.findall(r"^\s+([a-z_]+)\s+[A-Z]", match.group(1), flags=re.M))


def repo_detail_fields() -> set[str]:
    """Поля, которые отдаёт `repo.video_detail`: колонки videos (`v.*`) - из
    схемы db.py (CREATE videos, videos_v2) и колонок, добавленных `_add_column`
    и миграциями; алиасы SELECT, производные ключи и колонки files/playlists.
    """
    repo_src = REPO.read_text(encoding="utf-8")
    db_src = DB.read_text(encoding="utf-8")
    columns = (_table_columns(db_src, "videos")
               | _table_columns(db_src, "videos_v2")
               | set(re.findall(r'_add_column\(\s*conn,\s*"videos",\s*"([a-z_]+)\s+', db_src))
               | set(re.findall(r"ADD COLUMN\s*\n?\s*\"?([a-z_]+)\s+[A-Z]", db_src)))
    if not columns:
        raise AssertionError("в app/db.py не разобраны колонки videos - список "
                             "полей карточки потерян (v.* не разбор)")
    start = repo_src.find("def video_detail(")
    if start < 0:
        raise AssertionError("в app/repo.py нет video_detail - источник карточки потерян")
    block = repo_src[start:start + 1200]
    derived = set(re.findall(r'data\["([a-z_]+)"\]', block))
    main = re.search(r"SELECT ([\s\S]*?) FROM videos", block)
    aliases = _select_columns(main.group(1)) if main else set()
    files = re.search(r"SELECT ([\s\S]*?) FROM files", block)
    file_cols = _select_columns(files.group(1)) if files else set()
    playlists = re.search(r"SELECT ([\s\S]*?) FROM playlist_items", block)
    playlist_cols = _select_columns(playlists.group(1)) if playlists else set()
    return columns | derived | aliases | {"raw_json"} | file_cols | playlist_cols


def audio_meta_keys() -> set[str]:
    """Ключи меток аудио-файла: что пишет app/audio.py в format_json."""
    text = AUDIO.read_text(encoding="utf-8")
    keys = set(re.findall(r'meta\["([a-z_]+)"\]', text))
    probe_start = text.find("def probe(")
    if probe_start >= 0:
        probe_block = text[probe_start:text.find("def ", probe_start + 4)]
        probe_out = re.search(r"out = \{([\s\S]*?)\}", probe_block)
        if probe_out:
            keys |= set(re.findall(r'"([a-z_]+)":', probe_out.group(1)))
    tags = re.search(r"for key in \(([\s\S]*?)\)", text)
    if tags:
        keys |= set(re.findall(r'"([a-z_]+)"', tags.group(1)))
    return keys


def dart_card_reads(card_text: str) -> tuple[set[str], set[str], set[str]]:
    """Ключи, которые карточка читает: из ответа `get_video`, из записей `files`
    и из меток `format_json`.
    """
    data = set(re.findall(r"data\['([a-z_]+)'\]", card_text))
    files = set(re.findall(r"file\['([a-z_]+)'\]", card_text))
    audio = set(re.findall(r"audio\['([a-z_]+)'\]", card_text))
    return data, files, audio


def status_vocabulary() -> set[str]:
    """Статусы строки видео: список CHECK из схемы videos_v2 (app/db.py) плюс
    подписи реестра STATUS_LABELS (app/repo.py).

    `detached` есть в CHECK, но не в STATUS_LABELS: для него подпись читается
    текстом статуса как есть (repo.py: `STATUS_LABELS.get(status, status)`), и
    экран называет это своей подписью «статус: ...» - это не выдуманный статус,
    поэтому словарь включает оба источника, а не только подписи.
    """
    db_src = DB.read_text(encoding="utf-8")
    repo_src = REPO.read_text(encoding="utf-8")
    checks = re.findall(r"CHECK \(status IN \(([\s\S]*?)\)\)", db_src)
    if not checks:
        raise AssertionError("в app/db.py нет CHECK статуса videos - словарь "
                             "статусов потерян")
    names = {name for block in checks for name in re.findall(r"'([a-z_]+)'", block)}
    labels = re.search(r"STATUS_LABELS = \{(.*?)\n\}", repo_src, flags=re.S)
    if not labels:
        raise AssertionError("в app/repo.py нет STATUS_LABELS - реестр подписей потерян")
    return names | set(re.findall(r'"([a-z_]+)":', labels.group(1)))


def card_problems(card_text: str | None = None) -> list[str]:
    """Карточка Flutter сверена с JS-каноном, сервером и реестром полей.

    Порядок подписей kv - из openDetail; подписи, которые Flutter не рисует,
    обязаны быть в kCardFieldsNotBuilt и называть волну; действия канона -
    либо построены (подпись, право и цель совпадают со SCOPE_OF), либо
    названы не построенными (волна или «нативная цель»); списки статусов
    совпадают с guard-ами канона и существуют в STATUS_LABELS; поля читаются
    только те, что отдаёт video_detail (app/repo.py), а метки аудио - те, что
    пишет app/audio.py.
    """
    card = (card_text if card_text is not None
            else (LIB / "video_card.dart").read_text(encoding="utf-8"))
    js_text = JS.read_text(encoding="utf-8")
    problems = []
    labels = js_card_labels(js_text)
    not_built = dart_card_not_built(card)
    got_labels = dart_card_labels(card)
    expected = [label for label in labels if label not in not_built]
    if got_labels != expected:
        problems.append(f"подписи карточки Flutter {got_labels[:6]}... != канон "
                        f"openDetail {expected[:6]}... (всего {len(expected)}): два "
                        "клиента рисуют разные поля и в разных порядке")
    missing = sorted(set(labels) - set(got_labels) - set(not_built))
    if missing:
        problems.append(f"карточка Flutter не рисует и не называет подписи {missing} - "
                        "поле канона показано молча (пустая карточка выглядит успехом)")
    extra = sorted(set(not_built) - set(labels))
    if extra:
        problems.append(f"kCardFieldsNotBuilt держит подписи вне канона: {extra}")
    for label, text in not_built.items():
        if not re.search(r"волна W\d", text):
            problems.append(f"поле {label} не называет волну: человек не знает, "
                            "когда оно появится")
    js_actions = js_action_labels(js_text)
    built = dart_card_actions(card)
    actions_not_built = dart_actions_not_built(card)
    js_labels = [label for _name, label in js_actions]
    built_labels = [label for label, _scope, _target in built]
    if built_labels != [label for label in js_labels if label in built_labels]:
        problems.append(f"порядок построенных действий Flutter {built_labels} != канон "
                        f"{js_labels}: первичная кнопка в DOM слева - её кладёт первым app.js")
    unknown = sorted(set(built_labels) - set(js_labels))
    if unknown:
        problems.append(f"Flutter шлёт действия {unknown}, которых нет в каноне openDetail")
    unnamed = sorted(set(js_labels) - set(built_labels) - set(actions_not_built))
    if unnamed:
        problems.append(f"действия канона {unnamed} не построены и не названы: экран "
                        "выглядит полным, а кнопки отсутствуют молча")
    extra_actions = sorted(set(actions_not_built) - set(js_labels))
    if extra_actions:
        problems.append(f"kCardActionsNotBuilt держит подписи вне канона: {extra_actions}")
    for label, text in actions_not_built.items():
        if not re.search(r"волна W\d|нативная цель", text):
            problems.append(f"действие {label} не называет волну или нативную цель: "
                            "отсутствующая кнопка без причины")
    # Блоки карточки, кроме ряда действий: обложка, редактор пометок, raw_json.
    js_blocks = [name for name in ("detailThumbHtml", "detailMarksHtml", "raw_json")
                 if name in js_text]
    if len(js_blocks) != 3:
        problems.append(f"в ui_src/app.js не найдены блоки карточки {js_blocks} - "
                        "сверка состава карточки потеряла источник")
    blocks = _dart_map(card, "kCardBlocksNotBuilt")
    missing_blocks = sorted(set(js_blocks) - set(blocks))
    if missing_blocks:
        problems.append(f"карточка Flutter не строит и не называет блоки {missing_blocks} - "
                        "часть карточки канона отсутствует молча")
    extra_blocks = sorted(set(blocks) - set(js_blocks))
    if extra_blocks:
        problems.append(f"kCardBlocksNotBuilt держит имена вне канона: {extra_blocks}")
    for name, text in blocks.items():
        if not re.search(r"волна W\d", text):
            problems.append(f"блок {name} не называет волну: человек не знает, "
                            "когда он появится")
    scope_names = {scope for _label, scope, _target in built if scope}
    unknown_scopes = sorted(scope_names - set(scopes.SCOPES))
    if unknown_scopes:
        problems.append(f"карточка требует скоупы {unknown_scopes}, которых нет в "
                        "app/http/scopes.py SCOPES - сервер никогда не выдаёт такое право")
    for label, scope, target in built:
        if target:
            required = scopes.scope_for(target)
            if required is None:
                problems.append(f"действие {label}: цель {target} не экспортируется по "
                                "HTTP (SCOPE_OF её нет) - клиент шлёт цель, которой нет")
            elif required != scope:
                problems.append(f"действие {label}: Flutter подписывает право {scope}, "
                                f"а сервер требует {required} для {target} - кнопка "
                                "пройдёт с отказом 403, который клиент не предсказал")
        elif scope == "watch":
            # Право на байты решает маршрут, не метод: SCOPE_OF его не содержит.
            if scopes.scope_for("watch") is not None:
                problems.append("SCOPE_OF имеет цель 'watch': право на байты "
                                "перестало быть маршрутным (media.py/hls.py)")
            if 'scope="watch"' not in MEDIA.read_text(encoding="utf-8"):
                problems.append("app/http/media.py не требует право watch - карточка "
                                "подписывает отказ по праву, которого сервер не проверяет")
    statuses = dart_status_lists(card)
    guards = js_status_guards(js_text)
    for name in ("download", "queue", "playable", "mesh"):
        if statuses[name] != guards[name]:
            problems.append(f"статусы {name}: Flutter {statuses[name]} != канон "
                            f"{guards[name]} - кнопка появляется на чужом статусе")
    for status in guards.get("rest", []):
        if status not in statuses["playable"]:
            problems.append(f"канон сравнивает статус {status} вне списков Flutter - "
                            "сверка потеряла часть guard-а")
    known = status_vocabulary()
    invented = sorted({status for list_ in statuses.values() for status in list_} - known)
    if invented:
        problems.append(f"карточка держит статусы {invented}, которых нет в словаре "
                        "статусов сервера (CHECK videos и STATUS_LABELS): подпись "
                        "строки выдумана")
    allowed = repo_detail_fields()
    data_keys, file_keys, audio_keys = dart_card_reads(card)
    playlist_keys = set(re.findall(r"playlist\['([a-z_]+)'\]", card))
    bad_data = sorted(data_keys - allowed)
    if bad_data:
        problems.append(f"карточка читает поля {bad_data}, которых не отдаёт "
                        "repo.video_detail - поле выдумано клиентом")
    bad_files = sorted(file_keys - allowed)
    if bad_files:
        problems.append(f"карточка читает записи files с ключами {bad_files}, которых "
                        "нет в SELECT files - путь файла выдуман")
    bad_playlists = sorted(playlist_keys - allowed)
    if bad_playlists:
        problems.append(f"карточка читает плейлисты с ключами {bad_playlists}, которых "
                        "нет в SELECT playlist_items - позиция и уборка выдуманы")
    bad_audio = sorted(audio_keys - audio_meta_keys())
    if bad_audio:
        problems.append(f"карточка читает метки format_json {bad_audio}, которых не "
                        "пишет app/audio.py - метки файла выдуманы")
    return problems


# -- проводка прав и адреса страницы плеера --------------------------------------

def route_problems(client_text: str | None = None) -> list[str]:
    """GET /whoami и GET /watch/<id>: тот же путь, что у браузерного клиента и
    сервера; тексты отказа - значения из словаря бэкенда (app/i18n.py).
    """
    client = (client_text if client_text is not None
              else (LIB / "api_client.dart").read_text(encoding="utf-8"))
    access_text = (LIB / "access.dart").read_text(encoding="utf-8")
    js_text = JS.read_text(encoding="utf-8")
    server_src = SERVER.read_text(encoding="utf-8")
    problems = []
    if "https://$host:$port/whoami" not in client:
        problems.append("api_client.dart: GET /whoami не построен - клиент не знает "
                        "права ключа и подписывает цели наугад (ui_src/app.js: fetchAccess)")
    if 'httpBaseUrl() + "/whoami"' not in js_text:
        problems.append("ui_src/app.js не читает /whoami - сверка прав потеряла источник")
    if 'path == "/whoami"' not in server_src:
        problems.append("app/http/server.py не отвечает на /whoami - клиент ходит в маршрут, "
                        "которого нет")
    if "watchUrl" not in client or "/watch/$videoId" not in client:
        problems.append("api_client.dart: адрес /watch/<id> не построен - карточка "
                        "не имеет пути к плееру")
    if 'httpBaseUrl() + "/watch/" + id' not in js_text:
        problems.append("ui_src/app.js не ходит на /watch/<id> - сверка пути плеера "
                        "потеряла источник")
    if 'path.startswith("/watch/")' not in server_src:
        problems.append("app/http/server.py не принимает /watch/ - Flutter открыл бы "
                        "адрес, которого сервер не обслуживает")
    if i18n.t("http.scope", "ru") != dart_text_constant(access_text, "kScopeDeniedText",
                                                        "access.dart"):
        problems.append("access.dart: текст отказа по праву не равен i18n http.scope - "
                        "клиент называет причину словами, которых сервер не выдаёт")
    if i18n.t("http.pairing", "ru") != dart_text_constant(access_text, "kUnpairedText",
                                                          "access.dart"):
        problems.append("access.dart: текст «не привязан» не равен i18n http.pairing - "
                        "401 подписан выдуманным текстом")
    return problems


def dart_text_constant(text: str, name: str, source: str) -> str:
    match = re.search(rf"const String {name} = '([^']*)';", text)
    if not match:
        raise AssertionError(f"в {source} нет константы {name} - текст сверки потерян")
    return match.group(1)


def canon_texts_problems(card_text: str | None = None) -> list[str]:
    """Тексты канона карточки (нет хранилищ, нет носителя, ответ очереди) -
    те же строки, что печатает ui_src/app.js.
    """
    card = (card_text if card_text is not None
            else (LIB / "video_card.dart").read_text(encoding="utf-8"))
    js_text = JS.read_text(encoding="utf-8")
    problems = []
    for name, needle in (("kNoStoragesText", "нет хранилищ - добавьте в настройках"),
                         ("kNoMediaText", " (нет носителя)"),
                         ("kEnqueuedText", "В очередь поставлено: ")):
        if needle not in js_text:
            problems.append(f"ui_src/app.js не содержит текст {needle!r} - сверка "
                            "потеряла источник канона")
        if dart_text_constant(card, name, "video_card.dart") != needle:
            problems.append(f"video_card.dart: {name} != канон app.js ({needle!r}) - "
                            "два клиента называют одно состояние разными словами")
    return problems


def token_usage_problems() -> list[str]:
    """Новые токены (veil, кольцо фокуса) реально потребляются экранами.

    Барьер диалога и кольцо строки обязаны читать veilColor и focusRing: без
    этого цвет затемнения и ширина кольца пишались бы литералом, и literal-
    страж их не поймал бы (он сверяет hex и размеры fontSize/height).
    """
    problems = []
    library = (LIB / "library_view.dart").read_text(encoding="utf-8")
    theme_text = (LIB / "theme.dart").read_text(encoding="utf-8")
    if "veilColor(" not in library:
        problems.append("library_view.dart: барьер диалога не читает veilColor - "
                        "затемнение --veil темы не применено (литерал или Colors.black)")
    if "focusRing(" not in library:
        problems.append("library_view.dart: кольцо фокуса строки не читает focusRing - "
                        "ширина и цвета --focus-ring не применены")
    if "kThemeVeils" not in theme_text:
        problems.append("theme.dart не читает kThemeVeils - подложка диалога не из токенов")
    if "focusRingFocus" not in theme_text or "focusRingGap" not in theme_text:
        problems.append("theme.dart не читает ширины кольца из kMetrics - кольцо "
                        "строки написано числом")
    return problems


def veil_problems(parsed: dict) -> list[str]:
    """Подложка диалога: ARGB каждой темы совпадает с --veil из реестра тем."""
    problems = []
    for theme_id in themes.ids():
        value = themes.css_vars(theme_id).get("--veil")
        if value is None:
            problems.append(f"тема {theme_id}: реестр не объявляет --veil - барьер "
                            "диалога потерял источник затемнения")
            continue
        match = re.match(r"rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\s*\)", value.strip())
        if not match:
            problems.append(f"тема {theme_id}: --veil = {value!r} не rgba - "
                            "затемнение не переносится числом")
            continue
        red, green, blue, alpha = match.groups()
        expected = (round(float(alpha) * 255) << 24) | (int(red) << 16) | (int(green) << 8) | int(blue)
        got = parsed["veils"].get(theme_id)
        if got is None:
            problems.append(f"тема {theme_id}: veil не выведен в Dart - диалог Flutter "
                            "останется без затемнения")
            continue
        if got != expected:
            problems.append(f"тема {theme_id}: Dart 0x{got:08x} != реестр {value.strip()} "
                            "- барьер карточки красит не той палитрой")
    extra = sorted(set(parsed["veils"]) - set(themes.ids()))
    if extra:
        problems.append(f"kThemeVeils держит темы вне реестра: {extra}")
    return problems


def generation_problems() -> list[str]:
    """Ассет совпадает с генерацией байт-в-байт (детерминированный вывод)."""
    if not TOKENS.is_file():
        return [f"{TOKENS.relative_to(ROOT)} не построен: "
                "python tools/build_flutter_theme.py"]
    if TOKENS.read_text(encoding="utf-8") != gen.render():
        return [f"{TOKENS.relative_to(ROOT)} не совпадает с генерацией "
                "tools/build_flutter_theme.py - ассет правил кто-то руками"]
    return []


def literal_problems(lib: pathlib.Path = LIB) -> list[str]:
    """Production-код Flutter не держит литералов цвета, размера и длительности.

    Стенд (main.dart и spike-файлы) под проверкой не стоит: его геометрия
    названа в docs/FLUTTER-TRANSITION.md §9 и в README, это мерка, а не токен.
    """
    problems = []
    for name in PRODUCTION_FILES:
        path = lib / name
        if not path.is_file():
            problems.append(f"{name} не построен: production-код темы и экрана "
                            "потерян")
            continue
        if path.name == "theme_tokens.dart":
            continue
        text = path.read_text(encoding="utf-8")
        for match in HEX_LITERAL.finditer(text):
            problems.append(f"{path.name}: hex-литерал {match.group(0)} - роль "
                            "должна читаться из токенов темы")
        for match in SIZE_LITERAL.finditer(text):
            problems.append(f"{path.name}: размер написан числом "
                            f"({match.group(0)}) - читается из kMetrics или из "
                            "настройки (ThemeIdentity)")
        for match in DURATION_LITERAL.finditer(text):
            problems.append(f"{path.name}: длительность написана числом "
                            f"({match.group(0)}) - читается из kMetrics или из "
                            "канонного POLL_MS")
        names = set(ROLE_CALL.findall(text))
        known = {name.lstrip("-") for name in themes.PALETTE_VARS}
        unknown = sorted(names - known)
        if unknown:
            problems.append(f"{path.name}: имена ролей вне реестра {unknown}")
        for role_name in sorted(names):
            for theme_id in themes.ids():
                roles = {key.lstrip("-") for key in themes.css_vars(theme_id)}
                if role_name not in roles:
                    problems.append(f"{path.name}: роль {role_name} не в теме "
                                    f"{theme_id} - часть окна потеряет цвет")
    return problems


def contract_problems() -> list[str]:
    """Проводка вызовов фасада: `{}` без аргумента, `[аргумент]` с аргументом.

    Канонный источник - ui_src/app.js:547 и app/http/server.py: _body; Dart
    обязан повторять ту же форму, иначе два клиента одного бэкенда шлют разное.
    """
    problems = []
    client = LIB / "api_client.dart"
    if not client.is_file():
        return ["ui_flutter/lib/api_client.dart не построен: транспорт к --serve "
                "проверен стендом W0, а не кодом репозитория"]
    text = client.read_text(encoding="utf-8")
    if not JS_BODY_CANON.search(text):
        problems.append("api_client.dart: форма тела не совпадает с канонной "
                        "({} когда аргумента нет, [аргумент] когда есть) - "
                        "ui_src/app.js:547")
    js = (ROOT / "ui_src" / "app.js").read_text(encoding="utf-8")
    if "(arg === undefined || arg === null) ? \"{}\"" not in js:
        problems.append("ui_src/app.js: канонная форма тела не найдена - "
                        "сверка не имеет источника")
    field = next(item for item in settings_schema.FIELDS if item["key"] == "theme")
    ids_in_dart = parse(TOKENS.read_text(encoding="utf-8"))["ids"]
    if [choice[0] for choice in field["choices"]] != ids_in_dart:
        problems.append(f"choices поля theme ({[choice[0] for choice in field['choices']]}) "
                        f"!= порядок тем в Flutter ({ids_in_dart}): настройка "
                        "не применится к Flutter-окну")
    return problems


def problems() -> list[str]:
    """Все сверки одним списком; каждая строка называет причину."""
    if not TOKENS.is_file():
        return generation_problems()
    parsed = parse(TOKENS.read_text(encoding="utf-8"))
    out = generation_problems()
    out += registry_problems(parsed)
    out += metrics_problems(parsed)
    out += literal_problems()
    out += status_roles_problems()
    out += contract_source_problems()
    out += sortable_columns_problems()
    out += tabs_problems()
    out += contract_problems()
    out += card_problems()
    out += route_problems()
    out += canon_texts_problems()
    out += token_usage_problems()
    out += veil_problems(parsed)
    return out


def main(argv: list[str]) -> int:
    found = problems()
    if found:
        print("отказ mirror-стража токенов темы:", file=sys.stderr)
        for line in found:
            print("  ", line, file=sys.stderr)
        return 1
    parsed = parse(TOKENS.read_text(encoding="utf-8"))
    if "--verbose" in argv:
        print(f"тем {len(parsed['ids'])}, ролей на тему "
              f"{len(themes.PALETTE_VARS)}, метрик {len(parsed['metrics'])}, "
              f"контрастных пар {len(parsed['pairs'])}")
        return 0
    print("токены Flutter совпадают с app/themes.py, ui_src/app.css и "
          f"списком тем настроек ({len(parsed['ids'])} тем, "
          f"{len(parsed['metrics'])} метрик, {len(parsed['pairs'])} пар, "
          f"veil {len(parsed['veils'])} тем); колонки ({len(js_list_columns())}) "
          f"и табы ({len(js_tab_labels())}) сверены с ui_src/app.js, ORDER_COLS "
          f"app/repo.py и схемой настроек; карточка: подписей "
          f"{len(js_card_labels())}, действий {len(js_action_labels())}, "
          "права целей сверены с app/http/scopes.py, /whoami и /watch/ - с "
          "ui_src/app.js и app/http/server.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
