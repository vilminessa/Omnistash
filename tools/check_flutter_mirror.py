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
app/http/server.py. На шаге грида и группы (W2, шаг 4) сверяются ячейки
`rowHtml` против полного `LIST_COLS` (ключ, подпись, гейт, ширины wide/narrow
и breakpoint `max-width: 900px`), единицы `humanSize`, плюрализация
`plural`, aria-подписи чекбоксов, цели `sel-bar` из ui_src/index.html
(порядок и `data-need`; не построенные цели обязаны называть волну), канонные
тексты диалогов и сводок (`openMark`/`openBulk`/`bulkApply`/`runCollect`/
`bulkRemoveRows`), ключи проводок `bulk_*` и `save_fields` против
`request.get(...)` в app/gui.py и whitelist `repo.save_fields`, чтение
`adult_confirm` из отказа `_bulk_adult` (текст сервера не дублируется в
клиенте), механизм диалога §3.8 (`dialogExit`/`dialogEnter`: выход
`-decline`/`-close`, Enter на выход, Escape, disabled пропускаются) и
потребление новых токенов: барьеры диалогов (не менее четырёх) читают
`veilColor`. На шаге плиток (W2, шаг 4б) сверяются числа `TILE` против
    app.js:28-32, лимиты батча и кэша обложек и коалесинг против
    app.js:33-34, 3020-3042 и `THUMB_BATCH_LIMIT` бэка, формула `tileCols`
    против app.js:2967-2972, 44 px play-кнопки и 16/9 обложки против
    app.css:670-691, подписи плитки и ряда вида против `tileHtml` и
    index.html:109-124, ключи и `choices` `view_mode`/`tile_size` против
    схемы настроек и guard записи «только при изменении», проводка
    `get_thumbs` (`ids`/`thumbs`, право `read` из scopes, `has_thumb` из
    app/repo.py, LRU-кэш и снимание ids до вызова) против app.js и бэка,
    типы `THUMB_MIME` и тексты отказов бэка (не дублируются в клиенте),
    скрытие шапки и ряда размера (`hidden` из `applyViewState`) и
    потребление `--scrim`/`--on-scrim`/`--surface-2` плиткой. Плюс проверка,
    что production-код Flutter не держит
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
HTML = ROOT / "ui_src" / "index.html"
REPO = ROOT / "app" / "repo.py"
GUI = ROOT / "app" / "gui.py"
AUDIO = ROOT / "app" / "audio.py"
SERVER = ROOT / "app" / "http" / "server.py"
MEDIA = ROOT / "app" / "http" / "media.py"
DB = ROOT / "app" / "db.py"

# Production-код Flutter: обязан читать токены, а не писать литералы.
# main.dart и spike-файлы здесь нет намеренно: стенд W0 держит фиксированную
# геометрию списка (высота ряда 44 px), и его числа описаны в
# docs/FLUTTER-TRANSITION.md §9 - это мерка, а не дизайн-токен.
# Шаг 4 добавил ячейки грида, панель целей и диалог-механизм: они тоже
# production-код (ширины колонок - канонные px из LIST_COLS, не числа из
# головы; тексты - канонные строки app.js/index.html, не выдуманные). Шаг 4б
# добавил плитки и ряд вида (tiles.dart): его числа - канонный TILE и
# лимиты обложек из app.js/app.css/gui.py, подписи - строки tileHtml и
# index.html, и он под сверкой стража.
PRODUCTION_FILES = ("theme.dart", "library_view.dart", "app.dart", "tabs.dart",
                    "video_card.dart", "access.dart", "player.dart",
                    "grid_cells.dart", "bulk_bar.dart", "dialog_trap.dart",
                    "tiles.dart")

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
        raise AssertionError("в grid_cells.dart нет kStatusRoles - карта "
                             "цветов статусов потеряна")
    return dict(re.findall(r"'([a-z]+)':\s*'([a-z0-9-]+)'", match.group(1)))


def status_roles_problems(view_text: str | None = None) -> list[str]:
    """Карта статусов Flutter совпадает с CSS (тот же источник правды).

    На шаге 4 карта переехала в grid_cells.dart: маркер статуса рисуется
    ячейкой грида, а подпись строки для AT читает её из library_view.dart
    импортом - источник тот же, файл теперь другой (названо здесь).
    """
    expected = css_status_roles()
    text = (view_text if view_text is not None
            else (LIB / "grid_cells.dart").read_text(encoding="utf-8"))
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


def _dart_map(text: str, name: str, source: str = "video_card.dart") -> dict[str, str]:
    """Карта `Map<String, String>` из Dart-ассета: подпись -> текст.

    Значение в Dart пишется несколькими соседними литералами (перенос строк
    для читаемости), поэтому собираются все литералы до следующего ключа:
    иначе сверка «называет волну» смотрела бы только первый фрагмент и
    отказывала на целой фразе (найдено фактом прогона стража). Ключ и значение
    могут быть именем `const String` (kToolbarScopes ссылается на подписи
    панели) - имя разворачивается в его текст (факт прогона: карта читалась
    пустой).
    """
    match = re.search(
        rf"const Map<String, String> {name} = <String, String>\{{(.*?)\}};",
        text, flags=re.S)
    if not match:
        raise AssertionError(f"в {source} нет {name} - карта потеряна, "
                             "сверка не имеет источника")
    block = _strip_comments(match.group(1))
    consts = _dart_string_consts(text)
    tokens = [(m.group(0), m.end()) for m in
              re.finditer(r"'[^']*'|[A-Za-z_][A-Za-z0-9_]*", block)]
    out: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        key_tok, end = tokens[index]
        if ":" not in block[end:end + 3]:
            index += 1
            continue
        parts: list[str] = []
        scan = index + 1
        while scan < len(tokens):
            nxt_tok, nxt_end = tokens[scan]
            if ":" in block[nxt_end:nxt_end + 3]:
                break
            parts.append(_dart_entry(text, nxt_tok, consts, source))
            scan += 1
        out[_dart_entry(text, key_tok, consts, source)] = "".join(parts)
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


# -- ячейки грида, панель выделения и bulk-цели (W2, шаг 4) -------------------------

BULK_TARGETS = ("bulk_download", "bulk_tags", "bulk_rating",
                "bulk_collection", "bulk_remove_rows", "save_fields")


def _unescape(text: str) -> str:
    """Разворачивает `\n`/`\r`/`\t` в исходном тексте литерала: сверка
    сравнивает значение, что человек читает на экране, а не запись в файле
    (канон диалога удаления пишет `\n\n` в разметке, Dart - тот же перенос)."""
    for needle, value in (("\\n", "\n"), ("\\r", "\r"), ("\\t", "\t")):
        text = text.replace(needle, value)
    return text


def _strip_comments(text: str) -> str:
    """Код без комментариев: сверки «клиент не выдумывает текст» читают код,
    а не пояснения, где канонный отказ сервера назван цитатой (факт прогона
    стража: комментарий bulk_bar.dart:156 давал ложное «дублирует отказ»)."""
    return re.sub(r"//[^\n]*", "", text)


def _dart_string_consts(text: str) -> dict[str, str]:
    """Имена `const String` файла -> значение целиком. Нужны, когда список или
    карта собраны из имён констант, а не из литералов (kToolbarBuilt и
    kToolbarScopes в bulk_bar.dart - факт прогона стража: прежний разбор
    литералов давал пустой список и цели канона казались «не названными»)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"const String (\w+) =([\s\S]*?);", text):
        out[m.group(1)] = _unescape("".join(re.findall(r"'([^']*)'", m.group(2))))
    return out


def _dart_entry(text: str, token: str, consts: dict[str, str], source: str) -> str:
    """Элемент списка/карты: строковый литерал или имя `const String`."""
    literal = re.fullmatch(r"'([^']*)'", token)
    if literal:
        return _unescape(literal.group(1))
    if token in consts:
        return consts[token]
    raise AssertionError(f"{source}: элемент {token!r} не литерал и не const String - "
                         "значение не прочитать, сверка без источника")


def _dart_named_list(text: str, name: str, source: str) -> list[str]:
    match = re.search(rf"const List<String> {name} =\s*<String>\[(.*?)\];",
                      text, flags=re.S)
    if not match:
        raise AssertionError(f"в {source} нет {name} - список потерян, "
                             "сверка не имеет источника")
    consts = _dart_string_consts(text)
    return [_dart_entry(text, tok, consts, source)
            for tok in re.findall(r"'[^']*'|[A-Za-z_][A-Za-z0-9_]*",
                                  _strip_comments(match.group(1)))]


def _dart_joined_constant(text: str, name: str, source: str) -> str:
    """Значение `const String`, написанное соседними литералами (перенос для
    читаемости) - собранное целиком, иначе сверка видела бы огрызок."""
    match = re.search(rf"const String {name} =([\s\S]*?);", text)
    if not match:
        raise AssertionError(f"в {source} нет константы {name} - текст потерян")
    return _unescape("".join(re.findall(r"'([^']*)'", match.group(1))))


def _parse_literal(text: str, quote: int) -> tuple[str, int]:
    """Литерал, начинающийся на позиции `quote` (кавычка): содержимое с
    развёрнутыми `\n`/`\r`/`\t` и позиция закрывающей кавычки."""
    closing = text[quote]
    j = quote + 1
    buf: list[str] = []
    while j < len(text):
        ch = text[j]
        if ch == "\\":
            buf.append({"\\n": "\n", "\\r": "\r", "\\t": "\t"}.get(
                "\\" + text[j + 1], text[j + 1]))
            j += 2
            continue
        if ch == closing:
            break
        buf.append(ch)
        j += 1
    return "".join(buf), j


def _literal_containing(text: str, needle: str) -> tuple[int, str, int]:
    """Литерал, содержащий `needle`: (кавычка начала, содержимое, кавычка конца).
    Кавычки внутри разметки (`class="muted"`) не начало строки: читаем назад,
    пока литерал не окажется тем, что содержит needle (факт прогона стража:
    первая найденная кавычка давала 'muted' и цепочка обрывалась)."""
    i = text.find(needle)
    if i < 0:
        raise AssertionError(f"в ui_src/app.js не найден текст {needle!r} - "
                             "сверка потеряла источник подсказки")
    search = i
    while search >= 0:
        quote = max(text.rfind("'", 0, search), text.rfind('"', 0, search))
        if quote < 0:
            break
        content, j = _parse_literal(text, quote)
        # Настоящий литерал не пересекает перенос строки (JS так не пишет):
        # candidate, у которого содержимое со `\n`, - кавычка внутри разметки,
        # он не строка подсказки (факт прогона: такая пара кавычек обрывала
        # цепочку посреди фразы).
        if "\n" in text[quote + 1:j]:
            search = quote - 1
            continue
        if needle in text[quote + 1:j]:
            return quote, content, j
        search = quote - 1
    raise AssertionError(f"строковый литерал с {needle!r} не найден - "
                         "сверка потеряла источник подсказки")


def _js_concat_text(text: str, start: str, end: str) -> str:
    """Канонный текст из цепочки конкатенации литералов: литерал, содержащий
    `start`, и его соседи через `+` до литерала с `end`. разметка HTML
    вырезается, переносы разворачиваются - сравнивается значение, что человек
    читает на экране.

    Прежняя форма (склейка всех литералов окна между подстроками) давала
    огрызки с `+` и обрывки посреди фразы - kMarkHint читался пустым (факт
    прогона стража).
    """
    _quote, content, j = _literal_containing(text, start)
    parts: list[str] = [content]
    while end not in "".join(parts):
        k = j + 1
        while k < len(text) and (text[k].isspace() or text[k] == "+"):
            k += 1
        if k >= len(text) or text[k] not in "'\"":
            break  # цепочка обрывается на вызове (bulkLabel()) - конец дальше
        content, j = _parse_literal(text, k)
        if "\n" in text[k + 1:j]:
            break  # за k не строка подсказки, а разметка/код - цепочка обрывается
        parts.append(content)
    joined = re.sub(r"<[^>]*>", "", "".join(parts)).strip()
    if end not in joined:
        raise AssertionError(f"цепочка литералов от {start!r} не доходит до "
                             f"{end!r} - сверка текста потеряла источник")
    return joined


def js_all_list_cols(js_text: str | None = None) -> list[tuple[str, str, str, str, float, float]]:
    """Все колонки `LIST_COLS`: (key, label, gate, sortable, wide, narrow).

    Ширины - канонные px числа; для `title` канон пишет `minmax(180px, 1fr)` -
    берётся минимум px (остаток ширины), это тот вес, что переносится в flex.
    """
    text = js_text if js_text is not None else JS.read_text(encoding="utf-8")
    block = re.search(r"var LIST_COLS = \[(.*?)\];", text, flags=re.S)
    if not block:
        raise AssertionError("в ui_src/app.js нет блока LIST_COLS - "
                             "сверка колонок потеряла источник")
    cols = []
    for body in re.findall(r"\{([^{}]*?)\}", block.group(1), flags=re.S):
        key = re.search(r'key:\s*"([^"]+)"', body)
        label = re.search(r'label:\s*"([^"]*)"', body)
        widths = re.search(r"w:\s*\[([^\]]*)\]", body)
        if not key or not label or not widths:
            continue
        parts = re.findall(r'"([^"]*)"|([\d.]+)', widths.group(1))
        nums = []
        for quoted, bare in parts:
            source = quoted or bare
            px = re.search(r"(\d+)px", source)
            nums.append(float(px.group(1)) if px else float(source))
        if len(nums) != 2:
            raise AssertionError(f"колонка {key.group(1)}: w не пара wide/narrow - "
                                 "сверка весов потеряла источник")
        sortable = re.search(r'sortable:\s*"([a-z_]+)"', body)
        setting = re.search(r'setting:\s*"([a-z_]+)"', body)
        cols.append((key.group(1), label.group(1),
                     setting.group(1) if setting else "",
                     sortable.group(1) if sortable else "", nums[0], nums[1]))
    if not cols:
        raise AssertionError("LIST_COLS разобрался пустым - источник сверки пуст")
    return cols


def dart_row_cells(grid_text: str) -> list[tuple[str, str, str, float, float]]:
    match = re.search(
        r"const List<\(\{String key, String label, String gate, double wide, double narrow\}\)>\s*"
        r"kRowCells\s*=\s*<\(\{String key, String label, String gate, double wide, double narrow\}\)>\[(.*?)\];",
        grid_text, flags=re.S)
    if not match:
        raise AssertionError("в grid_cells.dart нет kRowCells - состав колонок "
                             "Flutter потерян")
    return [(key, label, gate, float(wide), float(narrow)) for key, label, gate, wide, narrow in
            re.findall(r"\(\s*key:\s*'([^']*)',\s*label:\s*'([^']*)',\s*gate:\s*'([^']*)',"
                       r"\s*wide:\s*([\d.]+),\s*narrow:\s*([\d.]+)\s*\)", match.group(1))]


def dart_cell_sort_keys(grid_text: str) -> dict[str, str]:
    match = re.search(
        r"const Map<String, String> kCellSortKeys = <String, String>\{(.*?)\};",
        grid_text, flags=re.S)
    if not match:
        raise AssertionError("в grid_cells.dart нет kCellSortKeys - ключи "
                             "сортировки колонок потеряны")
    return dict(re.findall(r"'([a-z_]+)':\s*'([a-z_]+)'", match.group(1)))


def grid_problems(grid_text: str | None = None, js_text: str | None = None,
                  html_text: str | None = None) -> list[str]:
    """Ячейки строки сверены с канонным составом колонок и их числами.

    Порядок, подписи, гейты и веса wide/narrow - из `LIST_COLS` (числа px
    перенесены как flex-весы - форма названа расхождением, но значения те же);
    ключи сортировки - канонные `sortable` и белый список ORDER_COLS;
    breakpoint узкого экрана - канонный `max-width: 900px`; единицы размера,
    плюрализация, aria-подписи чекбоксов и значки badges - те же строки канона.
    """
    grid = (grid_text if grid_text is not None
            else (LIB / "grid_cells.dart").read_text(encoding="utf-8"))
    js = js_text if js_text is not None else JS.read_text(encoding="utf-8")
    html = html_text if html_text is not None else HTML.read_text(encoding="utf-8")
    cols = js_all_list_cols(js)
    cells = dart_row_cells(grid)
    problems = []
    expected = [(key, label, gate, wide, narrow)
                for key, label, gate, _sortable, wide, narrow in cols]
    if cells != expected:
        problems.append(f"колонки ячеек Flutter {cells[:4]}... != LIST_COLS "
                        f"{expected[:4]}... (всего {len(expected)}): порядок, "
                        "подписи, гейты или веса wide/narrow расходятся - два "
                        "клиента рисуют разные гриды")
    schema_keys = {item["key"] for item in settings_schema.FIELDS}
    for key, _label, gate, _w, _n in cells:
        if gate and gate not in schema_keys:
            problems.append(f"колонка {key}: гейт {gate} не в схеме настроек - "
                            "колонка не скрывается по настройке")
    sort_map = {key: sortable for key, _l, _g, sortable, _w, _n in cols if sortable}
    cell_sorts = dart_cell_sort_keys(grid)
    if set(cell_sorts) != set(sort_map):
        problems.append(f"ключи сортировки ячеек {sorted(cell_sorts)} != sortable "
                        f"колонок {sorted(sort_map)} (колонка check не сортируется - "
                        "это названо в ассете): грид предлагает чужой порядок")
    wrong = sorted(f"{k}: {cell_sorts[k]} != {sort_map[k]}"
                   for k in set(cell_sorts) & set(sort_map) if cell_sorts[k] != sort_map[k])
    if wrong:
        problems.append(f"ключи ORDER_COLS ячеек расходятся с каноном: {wrong}")
    unknown = sorted(set(cell_sorts.values()) - set(repo_order_cols()))
    if unknown:
        problems.append(f"ячейки шлют ключи сортировки {unknown} вне белого "
                        "списка сервера ORDER_COLS - сервер молча держит default order")
    narrow_js = re.search(r"\(max-width:\s*(\d+)px\)", js)
    narrow_dart = re.search(r"const double kGridNarrowPx\s*=\s*(\d+)", grid)
    if not narrow_js or not narrow_dart:
        problems.append("breakpoint узкого экрана не найден в одном из источников "
                        "(ui_src/app.js: max-width, grid_cells: kGridNarrowPx)")
    elif narrow_js.group(1) != narrow_dart.group(1):
        problems.append(f"kGridNarrowPx {narrow_dart.group(1)} != канонный "
                        f"max-width {narrow_js.group(1)}px: узкий экран пересчитывает "
                        "весы на разной ширине")
    units_js = re.search(r"var units = \[([^\]]*)\]", js)
    if not units_js:
        problems.append("в ui_src/app.js нет единиц humanSize - сверка размера "
                        "потеряла источник")
    else:
        js_units = re.findall(r'"([^"]+)"', units_js.group(1))
        dart_units = _dart_named_list(grid, "kSizeUnits", "grid_cells.dart")
        if dart_units != js_units:
            problems.append(f"единицы размера Flutter {dart_units} != канон humanSize "
                            f"{js_units}: подписи байтов расходятся на один знак")
    for name, needle, source in (("kSelectRowAria", 'aria-label="Выбрать строку"', js),
                                 ("kSelectAllAria", 'aria-label="Выбрать всё"', html),
                                 ("kSelectAllTitle", 'title="Выбрать всё на странице"', html)):
        if needle not in source:
            problems.append(f"в источнике нет подписи {needle!r} - сверка чекбоксов "
                            "потеряла канон")
        if dart_text_constant(grid, name, "grid_cells.dart") not in source:
            problems.append(f"grid_cells.dart: {name} не повторает канонную подпись "
                            f"{needle!r} - чекбоксы двух клиентов называются разными словами")
    for glyph in ("★", "✓", "—"):
        if glyph not in js:
            problems.append(f"канонный значок/прочерк {glyph!r} не найден в ui_src/app.js")
        if glyph not in grid:
            problems.append(f"grid_cells.dart не рисует {glyph!r} - ячейки теряют "
                            "значки рейтинга/просмотрено и прочерк пустого")
    if "slice(0, 3)" not in js:
        problems.append("канонная колонка «Теги» не ограничена тремя тегами "
                        "(slice(0, 3)) - сверка потеряла источник")
    if "take(3)" not in grid:
        problems.append("grid_cells.dart не берёт первые три тега площадки (take(3))")
    return problems


def js_sel_bar_targets(html_text: str | None = None) -> list[tuple[str, str]]:
    """Цели панели `sel-bar` из ui_src/index.html: (подпись, право data-need)."""
    html = html_text if html_text is not None else HTML.read_text(encoding="utf-8")
    block = re.search(r'<div class="sel-bar"[\s\S]*?</div>', html)
    if not block:
        raise AssertionError("в ui_src/index.html нет sel-bar - панель выделения "
                             "потеряла источник")
    targets = []
    for m in re.finditer(r'<button[^>]*>([\s\S]*?)</button>', block.group(0)):
        open_tag = m.group(0)[:m.group(0).find(">") + 1]
        need = re.search(r'data-need="([a-z]+)"', open_tag)
        label = m.group(1).strip()
        if label:
            targets.append((label, need.group(1) if need else ""))
    if not targets:
        raise AssertionError("sel-bar разобрался без целей - источник сверки пуст")
    return targets


def toolbar_problems(bulk_text: str | None = None, html_text: str | None = None) -> list[str]:
    """Построенные цели панели сверены с канонным рядом `sel-bar`.

    Порядок построенных - тот же DOM (без пропущенных); право цели - тот же
    `data-need` (для «Операции…» права смешиваются внутри диалога и полоса её
    не гасит - канонный комментарий index.html); каждая не построенная цель
    обязана быть названа с волной, иначе полоса выглядит полной молча.
    """
    bulk = (bulk_text if bulk_text is not None
            else (LIB / "bulk_bar.dart").read_text(encoding="utf-8"))
    targets = js_sel_bar_targets(html_text)
    canon_labels = [label for label, _need in targets]
    need_map = dict(targets)
    built = _dart_named_list(bulk, "kToolbarBuilt", "bulk_bar.dart")
    scopes_map = _dart_map(bulk, "kToolbarScopes", "bulk_bar.dart")
    not_built = _dart_map(bulk, "kToolbarTargetsNotBuilt", "bulk_bar.dart")
    problems = []
    if built != [label for label in canon_labels if label in built]:
        problems.append(f"порядок построенных целей {built} != канонный sel-bar "
                        f"{canon_labels} без пропущенных: первичная и ряд целей "
                        "переставлены")
    unknown = sorted(set(built) - set(canon_labels))
    if unknown:
        problems.append(f"панель шлёт цели {unknown}, которых нет в каноне sel-bar")
    unnamed = sorted(set(canon_labels) - set(built) - set(not_built))
    if unnamed:
        problems.append(f"цели канона {unnamed} не построены и не названы: полоса "
                        "выглядит полной, а кнопки отсутствуют молча")
    extra = sorted(set(not_built) - set(canon_labels))
    if extra:
        problems.append(f"kToolbarTargetsNotBuilt держит подписи вне канона: {extra}")
    for label, text in not_built.items():
        if not re.search(r"волна W\d", text):
            problems.append(f"цель {label} не называет волну: человек не знает, "
                            "когда она появится")
    for label in built:
        got = scopes_map.get(label, "")
        required = need_map.get(label, "")
        if got != required:
            problems.append(f"цель {label}: Flutter подписывает право {got!r}, "
                            f"канон требует {required!r} (data-need) - кнопка "
                            "пройдёт с отказом, который клиент не предсказал")
        if got and got not in set(scopes.SCOPES):
            problems.append(f"цель {label}: скоуп {got} не существует в "
                            "app/http/scopes.py SCOPES")
    return problems


def bulk_texts_problems(bulk_text: str | None = None, js_text: str | None = None,
                        html_text: str | None = None) -> list[str]:
    """Тексты панели, диалогов и сводок - те же строки канона (app.js и
    index.html): клиент не выдумывает слова для состояний и ответов."""
    bulk = (bulk_text if bulk_text is not None
            else (LIB / "bulk_bar.dart").read_text(encoding="utf-8"))
    js = js_text if js_text is not None else JS.read_text(encoding="utf-8")
    html = html_text if html_text is not None else HTML.read_text(encoding="utf-8")
    hay = js + "\n" + html
    problems = []
    pairs = (
        ("kSelCountPrefix", "Выбрано: "), ("kToolbarAria", "Действия над выбранными"),
        ("kStorageLabel", "Хранилище:"), ("kTagsAddLabel", "Теги добавить"),
        ("kTagsRemoveLabel", "Теги убрать"), ("kRatingLabel", "Рейтинг"),
        ("kFolderLabel", "Папка"), ("kTagsLabel", "Теги"), ("kNotesLabel", "Заметка"),
        ("kWatchedLabel", "просмотрено"),
        ("kTagsAddPlaceholder", "через запятую, несколько"),
        ("kTagsRemovePlaceholder", "теги, которые надо снять"),
        ("kTagsPlaceholder", "через запятую"), ("kResetLabel", "сбросить"),
        ("kUnmarkLabel", "снять отметку"), ("kCancelLabel", "Отмена"),
        ("kCloseLabel", "Закрыть"), ("kApplyLabel", "Применить"),
        ("kRemoveConfirmTitle", "Убрать строки из индекса"),
        ("kRemoveConfirmCheck", "Я понимаю и подтверждаю"),
        ("kAcceptLabel", "Принять"), ("kDeclineLabel", "Отклонить"),
        ("kDeleteCancelled", "Удаление отменено"),
        ("kNothingFilled", "Ничего не заполнено"),
        ("kNoStorageText", "Не выбрано хранилище - добавьте папку в настройках"),
        ("kNoSelectionText", "Сначала выберите строки или откройте папку"),
        ("kNoFoldersOption", "папок пока нет"),
        ("kQueuedPrefix", "В очередь: "), ("kSkipPrefix", " · пропущено: "),
        ("kGonePrefix", " · строк в индексе нет: "), ("kSummaryJoin", " · "),
        ("kTagsLine", "Теги: "), ("kRatingLine", "Рейтинг "),
        ("kMarkedPrefix", "Помечено строк: "), ("kCollectAddPrefix", "В папку «"),
        ("kCollectRemovePrefix", "Из папки «"), ("kCollectSuffix", "»: "),
        ("kFromPrefix", " из "), ("kRemoveSuccessPrefix", "Убраны строки из индекса: "),
        ("kFilesKeptNote", " (файлы на диске не тронуты)"),
    )
    for name, needle in pairs:
        if needle not in hay:
            problems.append(f"канон не содержит текст {needle!r} - сверка потеряла "
                            "источник (app.js/index.html)")
        if dart_text_constant(bulk, name, "bulk_bar.dart") != needle:
            problems.append(f"bulk_bar.dart: {name} != канон {needle!r} - два "
                            "клиента называют одно состояние разными словами")
    # Подсказки диалогов: канон - цепочка конкатенации литералов разметки.
    for name, start, end in (("kMarkHint", "Пустые поля не меняются",
                             "в строке поиска."),
                             ("kBulkHint", "Теги - ваши локальные метки",
                              "подтверждения диалога."),
                             ("kRemoveConfirmTail", ") уйдут из индекса",
                              "переиндексацией папок.")):
        joined = _js_concat_text(js, start, end)
        got = _dart_joined_constant(bulk, name, "bulk_bar.dart")
        if got != joined:
            problems.append(f"bulk_bar.dart: {name} != канонный текст подсказки "
                            f"(сверка по склейке литералов app.js): {got[:60]}... "
                            f"vs {joined[:60]}...")
    head_tail = dart_text_constant(bulk, "kRemoveConfirmHead", "bulk_bar.dart")
    if head_tail not in js:
        problems.append(f"начало разрушительного текста {head_tail!r} не найдено "
                        "в каноне - диалог, называющий N, потерял источник")
    # Плюрализация и заголовки диалогов группы: счёт строк и титул панели
    # живут в bulk_bar.dart (сводка и диалоги), поэтому сверка здесь, а не
    # в grid_problems - иначе страж искал бы список не в том файле (факт
    # прогона: kRowPlural в grid_cells.dart не найден).
    plural_js = re.search(r'\["строка", "строки", "строк"\]', js)
    if not plural_js:
        problems.append("канонная плюрализация строк не найдена в ui_src/app.js - "
                        "сводки группы потеряли источник")
    elif _dart_named_list(bulk, "kRowPlural", "bulk_bar.dart") != ["строка", "строки", "строк"]:
        problems.append("kRowPlural в bulk_bar.dart != канонная плюрализация plural "
                        "(app.js:1476,1554): сводка считает строки по другому правилу")
    for prefix_name, prefix, js_needle in (("kMarkPrefix", "Пометить: ", '"Пометить: "'),
                                           ("kBulkPrefix", "Операции: ", '"Операции: "')):
        if js_needle not in js:
            problems.append(f"в ui_src/app.js нет заголовка {js_needle} - диалог "
                            "группы потерял источник")
        if dart_text_constant(bulk, prefix_name, "bulk_bar.dart") != prefix:
            problems.append(f"подпись заголовка {prefix_name} != канон {prefix!r} - "
                            "диалог двух клиентов назван разными словами")
    if '" строку"' not in js or '" строк"' not in js:
        problems.append("канонная singular-форма заголовка пометок не найдена в "
                        "app.js:1376 - сверка потеряла источник")
    if "' строку'" not in bulk or "' строк'" not in bulk:
        problems.append("markTitleFor не повторяет singular-форму канона «1 строку» - "
                        "титул диалога на одну строку расходится с браузером")
    return problems


def payload_problems(bulk_text: str | None = None, library_text: str | None = None) -> list[str]:
    """Проводки массовых целей: ключи запроса - те же, что читает сервер, и
    право подписывается до вызова. whitelist пометок - из repo.save_fields;
    отказ бэка (без confirm, adult_confirm) не дублируется текстом клиента,
    а приходит из ответа; после adult-отказа повтор не отправляется."""
    bulk = (bulk_text if bulk_text is not None
            else (LIB / "bulk_bar.dart").read_text(encoding="utf-8"))
    lib = (library_text if library_text is not None
           else (LIB / "library_view.dart").read_text(encoding="utf-8"))
    gui = GUI.read_text(encoding="utf-8")
    repo_src = REPO.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    combined = bulk + "\n" + lib
    problems = []
    for target in BULK_TARGETS:
        # Ключи собираются из окна вокруг вызова: payload может быть объявлен
        # строкой выше (bulk_collection в bulk_bar.dart), а прежний разбор
        # inline-литерала `<String, Object?>{...}` не работал - `?` в дженерике
        # regex читал как квантификатор, и ни одна цель не находилась (факт
        # прогона стража).
        hits = list(re.finditer(rf"'{target}'", combined))
        if not hits:
            problems.append(f"цель {target} не вызывается из production Dart - "
                            "панель не имеет пути к ней")
            continue
        keys: set[str] = set()
        for hit in hits:
            window = combined[max(0, hit.start() - 320): hit.start() + 320]
            keys |= set(re.findall(r"'([a-z_]+)':", window))
        server = _gui_keys(target, gui) | {"ids"}
        extra = sorted(keys - server)
        if extra:
            problems.append(f"{target}: проводка шлёт ключи {extra}, которых сервер "
                            "не читает (app/gui.py request.get) - ключ выдуман")
        if target == "bulk_remove_rows" and "confirm" not in keys:
            problems.append("bulk_remove_rows отправляется без confirm:true - "
                            "разрушительная цель уйдёт без галки человека (app/gui.py:931)")
        if target == "bulk_collection" and "collection_id" not in combined:
            problems.append("bulk_collection не передаёт collection_id - папочная "
                            "цель уйдёт без папки всегда (runCollect app.js:1669-1671)")
    dart_code = _strip_comments(combined)
    for refused in ("Нужно явное подтверждение удаления",
                    "Группа содержит строки взрослой площадки"):
        if refused in gui and refused in dart_code:
            problems.append(f"production Dart дублирует серверный отказ {refused!r} - "
                            "текст приходит из ответа, клиент его не выдумывает")
    if "'adult_confirm'" not in bulk:
        problems.append("bulk_bar.dart не читает adult_confirm из отказа - "
                        "гейт 18+ пройдёт как успех (app/gui.py: _bulk_adult)")
    if '"adult_confirm": True' not in gui:
        problems.append("app/gui.py не возвращает adult_confirm - сверка гейта "
                        "потеряла источник")
    if "res.adult_confirm" not in js:
        problems.append("ui_src/app.js не читает adult_confirm - канон потерял "
                        "источник спрашивания 18+")
    if not re.search(r"'adult_confirm'\]\s*==\s*true[\s\S]{0,240}return null", bulk):
        problems.append("bulk_bar.dart после adult_confirm повторяет цель - "
                        "спрашивания «мне есть 18» не построено (волна W3)")
    gate_note = _dart_joined_constant(bulk, "kAdultGateNotBuilt", "bulk_bar.dart")
    if "не построен" not in gate_note or not re.search(r"волна W\d", gate_note):
        problems.append("kAdultGateNotBuilt не называет причину и волну - отказ "
                        "выглядит случайным")
    # Текст отказа живёт в bulk_bar.dart (там же, где сводка панели), а
    # потребляется library_view.dart: страж читает константу из источника и
    # сверяет, что экран её действительно показывает (факт прогона: поиск по
    # library_view.dart не находил константу).
    jump_note = _dart_joined_constant(bulk, "kNoSettingsJump", "bulk_bar.dart")
    if "kNoSettingsJump" not in lib:
        problems.append("library_view.dart не показывает kNoSettingsJump - отказ без "
                        "хранилища не называет переход на настройки")
    if "switchTab(\"settings\")" not in js:
        problems.append("канонный переход на настройки после отказа без хранилища "
                        "не найден в app.js - сверка потеряла источник")
    if "не построен" not in jump_note or not re.search(r"волны W\d", jump_note):
        problems.append("kNoSettingsJump не называет заглушку и волну W3")
    whitelist = set(re.findall(r'if "([a-z_]+)" in fields', repo_src))
    marks = _dart_named_list(bulk, "kMarkFieldKeys", "bulk_bar.dart")
    if sorted(marks) != sorted(whitelist):
        problems.append(f"ключи пометок Flutter {sorted(marks)} != whitelist "
                        f"repo.save_fields {sorted(whitelist)} - запись трогает поле, "
                        "которое сервер не принимает из окна")
    writes = set(re.findall(r"fields\['([a-z_]+)'\]", bulk))
    invented = sorted(writes - whitelist)
    if invented:
        problems.append(f"диалог пометок пишет поля {invented} вне whitelist - "
                        "статус или метаданные площадки недопустимы из окна")
    folder_keys = set(re.findall(r"folder\['([a-z_]+)'\]", bulk))
    col_block = re.search(r"def collection_list([\s\S]*?)\"\"\"([\s\S]*?)\"\"\"([\s\S]*?)return",
                          repo_src)
    aliases = (set(re.findall(r"c\.([a-z_]+)", repo_src[repo_src.find("def collection_list"):
                                            repo_src.find("def collection_list") + 600]))
               | {"total"})
    bad = sorted(folder_keys - aliases)
    if bad:
        problems.append(f"папки читаются с ключами {bad}, которых нет в SELECT "
                        "repo.collection_list - подпись папки выдумана")
    if "'collections_list'" not in bulk:
        problems.append("bulk_bar.dart не читает collections_list - папки диалога "
                        "не имеют пути к списку")
    if '"collections"' not in gui:
        problems.append("app/gui.py collections_list не возвращает collections - "
                        "сверка списка папок потеряла источник")
    for target in BULK_TARGETS + ("collections_list",):
        required = scopes.scope_for(target)
        if required is None:
            problems.append(f"цель {target} не экспортируется по HTTP (SCOPE_OF её "
                            "нет) - клиент шлёт цель, которой нет")
            continue
        pattern = (rf"'{target}'[\s\S]{{0,240}}'{required}'"
                   rf"|'{required}'[\s\S]{{0,320}}'{target}'")
        if not re.search(pattern, combined):
            problems.append(f"цель {target}: право {required} не подписано рядом с "
                            "вызовом - отказ 403 придёт молча (тот же applyAccess "
                            "канона требует права до действия)")
    return problems


def js_tile_sizes(js_src: str) -> dict[str, tuple[int, int, int]]:
    """Размерный ряд канона `TILE` (ui_src/app.js:28-32): w, row, gap px."""
    block = re.search(r"var TILE = \{([\s\S]*?)\};", js_src)
    if not block:
        raise AssertionError("в ui_src/app.js нет блока TILE - сверка плиток "
                             "потеряла источник")
    out = {name: (int(w), int(row), int(gap)) for name, w, row, gap in
           re.findall(r"([a-z]+):\s*\{\s*w:\s*(\d+),\s*row:\s*(\d+),\s*gap:\s*(\d+)\s*\}",
                      block.group(1))}
    if not out:
        raise AssertionError("TILE пустой - сверка плиток потеряла источник")
    return out


def dart_tile_sizes(tiles_src: str) -> dict[str, tuple[float, float, float]]:
    """Размерный ряд Flutter `kTileSizes` (tiles.dart) тем же разбором."""
    out = {name: (float(w), float(row), float(gap)) for name, w, row, gap in
           re.findall(r"'([a-z]+)':\s*\(w:\s*([\d.]+),\s*row:\s*([\d.]+),\s*gap:\s*([\d.]+)\)",
                      tiles_src)}
    if not out:
        raise AssertionError("в tiles.dart нет kTileSizes с числами канона - "
                             "размерный ряд написан на глаз")
    return out


def _js_int(js_src: str, name: str) -> int:
    m = re.search(rf"var {name} = (\d+);", js_src)
    if not m:
        raise AssertionError(f"в ui_src/app.js нет {name} - сверка потеряла источник")
    return int(m.group(1))


def _dart_int(tiles_src: str, name: str) -> int | None:
    m = re.search(rf"const int {name} = (\d+);", tiles_src)
    return int(m.group(1)) if m else None


def css_tile_rule(css_src: str, rule: str) -> str:
    block = re.search(rf"\.{rule} \{{([\s\S]*?)\}}", css_src)
    if not block:
        raise AssertionError(f"в ui_src/app.css нет .{rule} - сверка потеряла источник")
    return block.group(1)


def html_toggle_labels(html_src: str, group_id: str) -> list[str]:
    """aria-label ряда переключателей канона (ui_src/index.html:109-124):
    первой - подпись группы `role="group"`, далее - подписи целей по порядку."""
    block = re.search(rf'id="{group_id}"[\s\S]*?</span>', html_src)
    if not block:
        raise AssertionError(f"в ui_src/index.html нет ряда id={group_id} - "
                             "сверка потеряла источник подписей")
    return re.findall(r'aria-label="([^"]+)"', block.group(0))


def tiles_problems(tiles_text: str | None = None,
                   library_text: str | None = None) -> list[str]:
    """Плитки и вид библиотеки (W2, шаг 4б) против канона и бэка.

    Числа `TILE`, лимиты обложек (`THUMB_BATCH`/`THUMB_CACHE_MAX` и
    `THUMB_BATCH_LIMIT` бэка), коалесинг 120 мс, формула `tileCols`, 44 px
    play-кнопки и 16/9 обложки, подписи `tileHtml` и ряда
    `view-toggle`/`size-toggle`, ключи и `choices` `view_mode`/`tile_size`
    со схемы, проводка `get_thumbs` (ключ `ids`, ответ `thumbs`, право `read`
    из scopes, `has_thumb` из repo.py, LRU-кэш, снятие ids из очереди до
    вызова), типы `THUMB_MIME`, скрытие шапки/размера по `applyViewState`,
    guard `save_setting` «только при изменении» и тексты отказов бэка,
    которые клиент не дублирует.
    """
    tiles = (tiles_text if tiles_text is not None
             else (LIB / "tiles.dart").read_text(encoding="utf-8"))
    lib = (library_text if library_text is not None
           else (LIB / "library_view.dart").read_text(encoding="utf-8"))
    js = JS.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    gui = GUI.read_text(encoding="utf-8")
    repo_src = REPO.read_text(encoding="utf-8")
    combined = tiles + "\n" + lib
    problems: list[str] = []

    # -- размерный ряд и лимиты обложек --------------------------------------
    js_sizes = js_tile_sizes(js)
    dart_sizes = dart_tile_sizes(tiles)
    if sorted(js_sizes) != sorted(dart_sizes):
        problems.append(f"kTileSizes Flutter {sorted(dart_sizes)} != канон TILE "
                        f"{sorted(js_sizes)} - размерный ряд не совпадает по составу")
    for name, expected in js_sizes.items():
        got = dart_sizes.get(name)
        if got is not None and tuple(float(v) for v in expected) != got:
            problems.append(f"TILE {name}: Flutter {got} != канон app.js {expected} "
                            "- ширина/ряд/зазор плитки написаны на глаз")
    batch_js = _js_int(js, "THUMB_BATCH")
    cache_js = _js_int(js, "THUMB_CACHE_MAX")
    batch_dart = _dart_int(tiles, "kThumbBatchMax")
    cache_dart = _dart_int(tiles, "kThumbCacheMax")
    gui_batch = re.search(r"THUMB_BATCH_LIMIT = (\d+)", gui)
    if batch_dart is None or batch_dart != batch_js:
        problems.append(f"kThumbBatchMax = {batch_dart} != THUMB_BATCH канона "
                        f"{batch_js} (app.js:34) - батч обложек не канонный")
    if gui_batch is None or (batch_dart is not None and batch_dart != int(gui_batch.group(1))):
        problems.append(f"kThumbBatchMax = {batch_dart} != THUMB_BATCH_LIMIT бэка "
                        f"{gui_batch.group(1) if gui_batch else '?'} (app/gui.py:70) - "
                        "клиент шлёт id больше, чем сервер принимает")
    if cache_dart is None or cache_dart != cache_js:
        problems.append(f"kThumbCacheMax = {cache_dart} != THUMB_CACHE_MAX канона "
                        f"{cache_js} (app.js:33) - кэш обложек не канонный")
    debounce_js = re.search(r"function scheduleThumbLoad\([\s\S]*?\}, (\d+)\);", js)
    if not debounce_js:
        raise AssertionError("в app.js нет setTimeout в scheduleThumbLoad - "
                             "сверка коалесинга потеряла источник")
    debounce_dart = _dart_int(tiles, "kThumbDebounceMs")
    if debounce_dart is None or debounce_dart != int(debounce_js.group(1)):
        problems.append(f"kThumbDebounceMs = {debounce_dart} != коалесинг канона "
                        f"{debounce_js.group(1)} мс (app.js:3041) - плитки одного "
                        "кадре спрашивают обложки не одним батчем")

    # -- геометрия плитки из CSS -----------------------------------------------
    play_block = css_tile_rule(css, "tile-play")
    play_px = set(re.findall(r"(?:width|height): (\d+)px", play_block))
    if len(play_px) != 1:
        raise AssertionError(f".tile-play держит размеры {sorted(play_px)} - "
                             "сверка play-кнопки потеряла однозначный источник")
    play_dart = re.search(r"const double kTilePlayPx = (\d+);", tiles)
    if play_dart is None or int(play_dart.group(1)) != int(next(iter(play_px))):
        problems.append(f"kTilePlayPx = {play_dart.group(1) if play_dart else '?'} != "
                        f".tile-play {next(iter(play_px))} px (app.css:690-691) - "
                        "play-кнопка не канонного размера")
    thumb_block = css_tile_rule(css, "tile-thumb")
    aspect_js = re.search(r"aspect-ratio: (\d+) / (\d+)", thumb_block)
    if not aspect_js:
        raise AssertionError(".tile-thumb не объявляет aspect-ratio - сверка "
                             "обложки потеряла источник")
    aspect_dart = re.search(r"aspectRatio: (\d+) / (\d+)", tiles)
    if not aspect_dart or (aspect_dart.group(1), aspect_dart.group(2)) != (
            aspect_js.group(1), aspect_js.group(2)):
        dart_pair = (f"{aspect_dart.group(1)}/{aspect_dart.group(2)}"
                     if aspect_dart else "не объявлен")
        problems.append(f"обложка Flutter aspectRatio {dart_pair} != "
                        f"канон .tile-thumb {aspect_js.group(1)}/{aspect_js.group(2)} "
                        "(app.css:673) - форма превью не канонная")
    cols_js = re.search(r'clientWidth - (\d+)', js)
    cols_dart = re.search(r"width - (\d+) \+ cfg\.gap\) / \(cfg\.w \+ cfg\.gap", tiles)
    if not cols_js or int(cols_js.group(1)) != 2:
        raise AssertionError("tileCols канона не читает ширину с минусом рамки - "
                             "сверка формулы потеряла источник")
    if not cols_dart or int(cols_dart.group(1)) != int(cols_js.group(1)):
        problems.append("tileColsFor Flutter не повторяет формулу канона "
                        "(width - 2 + gap)/(w + gap) - число колонок решается не "
                        "шириной контейнера (app.js:2967-2972)")
    if "Math.max(1, Math.floor((width + cfg.gap) / (cfg.w + cfg.gap))" not in js:
        raise AssertionError("формула tileCols в app.js не найдена - источник потерен")
    if "cols < 1 ? 1 : cols" not in tiles:
        problems.append("tileColsFor не держит минимум 1 колонку - узкий экран "
                        "даёт ноль (канон Math.max(1, ...) app.js:2971)")
    if "cfg.w / cfg.row" not in lib:
        problems.append("_buildTiles не задаёт высоту ряда канонным cfg.row "
                        "(aspect w/row) - виртуализация канона считает по row "
                        "(app.css:642-646, paintGrid cfg.row)")

    # -- подписи плитки и ряда вида --------------------------------------------
    consts = _dart_string_consts(tiles)
    tile_start = js.find("function tileHtml")
    if tile_start < 0:
        raise AssertionError("в app.js нет tileHtml - сверка подписей плитки "
                             "потеряла источник")
    tile_block = js[tile_start:tile_start + 1200]
    for name, needle in (("kNoThumbLabel", "нет обложки"),
                         ("kTileCheckAria", "Выбрать плитку"),
                         ("kTilePlayAria", "Открыть в плеере")):
        if needle not in tile_block:
            raise AssertionError(f"подпись {needle!r} не в tileHtml - источник потерен")
        got = consts.get(name)
        if got is None:
            problems.append(f"tiles.dart не объявляет {name} - подпись канона "
                            f"{needle!r} не перенесена")
        elif got != needle:
            problems.append(f"подпись {name} = {got!r} != канон tileHtml {needle!r} "
                            "- клиент выдумывает слово")
    if "нет обложки" not in js:
        raise AssertionError("заглушка noThumbNode не найдена в app.js - источник потерен")
    view_labels = html_toggle_labels(html, "view-toggle")
    size_labels = html_toggle_labels(html, "size-toggle")
    expected_view = [consts.get("kViewToggleAria"), consts.get("kViewListLabel"),
                     consts.get("kViewGridLabel")]
    expected_size = [consts.get("kSizeToggleAria"), consts.get("kSizeSmallLabel"),
                     consts.get("kSizeMediumLabel"), consts.get("kSizeLargeLabel")]
    for labels, expected, gid in ((view_labels, expected_view, "view-toggle"),
                                  (size_labels, expected_size, "size-toggle")):
        if len(expected) != len(labels) or any(
                got is None or got != want for got, want in zip(expected, labels)):
            problems.append(f"подписи ряда {gid} Flutter {expected} != канон "
                            f"index.html {labels} - группа или цель названа не тем текстом")

    # -- настройки вида и размера ------------------------------------------------
    fields = {item["key"]: item for item in settings_schema.FIELDS}
    choice_ids = {item["key"]: {c[0] for c in item.get("choices") or []}
                  for item in settings_schema.FIELDS}
    for key in ("view_mode", "tile_size"):
        if key not in fields:
            problems.append(f"ключ {key} не в схеме настроек - save_setting "
                            "пишется настройке, которой нет")
    dart_values = set(re.findall(r"ButtonSegment<String>\(value: '([a-z]+)'", tiles))
    canon_values = choice_ids.get("view_mode", set()) | choice_ids.get("tile_size", set())
    if dart_values != canon_values:
        problems.append(f"значения ряда вида Flutter {sorted(dart_values)} != choices "
                        f"схемы {sorted(canon_values)} - выдуманный или потерянный variant")
    for key, default, declaration in (("view_mode", "list", "String _viewMode = 'list';"),
                                      ("tile_size", "medium", "String _tileSize = 'medium';")):
        item = fields.get(key)
        if item is not None and item.get("default") != default:
            problems.append(f"дефолт {key} в схеме {item.get('default')!r} != Flutter "
                            f"{default!r} - два клиента стартуют с разным видом")
        if declaration not in lib:
            problems.append(f"{declaration!r} не в library_view.dart - дефолт {key} "
                            "не объявлен как в схеме")
    if "_persistSetting('view_mode'" not in lib or "_persistSetting('tile_size'" not in lib:
        problems.append("library_view.dart не пишет через _persistSetting ключи "
                        "view_mode/tile_size - выбор вида не переживёт перезапуск "
                        "(saveSetting канона app.js:7210-7223)")
    if 'if (state.viewMode !== "list") saveSetting("view_mode", "list")' not in js \
            or 'if (state.viewMode !== "grid") saveSetting("view_mode", "grid")' not in js:
        raise AssertionError("guard записи вида в каноне не найден - источник потерен")
    if "if (value == _viewMode) return;" not in lib or "size == _tileSize" not in lib:
        problems.append("переключение вида/размера пишет save_setting даже без "
                        "изменения - канон app.js:7210-7223 пишет только при разнице")
    if '$("grid-head").hidden = grid' not in js or '$("size-toggle").hidden = !grid' not in js:
        raise AssertionError("скрытие шапки/размера в applyViewState канона не найдено - "
                             "источник потерен")
    if "if (_viewMode != 'grid')" not in lib:
        problems.append("шапка колонок не скрывается в режиме плитки - канон "
                        '"grid-head" hidden = grid (app.js:2974-2985)')
    if "if (!gridMode) return const SizedBox.shrink();" not in tiles:
        problems.append("ряд размера не скрывается в списке - канон "
                        '"size-toggle" hidden = !grid (app.js:2979) - '
                        "скрытый контрол обязан не быть в обходе")

    # -- проводка обложек: цель, ключи, кэш, очередь ---------------------------
    if "'get_thumbs'" not in lib:
        problems.append("get_thumbs не вызывается из production Dart - плитки не "
                        "имеют пути к обложке (канон scheduleThumbLoad app.js:3027)")
    required = scopes.scope_for("get_thumbs")
    if required != "read":
        problems.append(f"цель get_thumbs экспортируется с правом {required!r} != read "
                        "(app/http/scopes.py) - сверка права потеряла источник")
    server_keys = _gui_keys("get_thumbs", gui)
    if "ids" not in server_keys:
        raise AssertionError("app/gui.py get_thumbs не читает ids - источник потерен")
    if "'ids'" not in lib or "{'ids': ids}" not in lib:
        problems.append("get_thumbs вызывается без ключа ids - проводка не канонная "
                        "(app.js:3027 { ids: ids })")
    thumbs_block = gui[gui.find("def get_thumbs"):gui.find("def get_thumbs") + 900]
    if '"thumbs"' not in thumbs_block:
        raise AssertionError("app/gui.py get_thumbs не возвращает thumbs - источник потерен")
    if "res['thumbs']" not in lib:
        problems.append("Flutter не читает ответ thumbs - обложки ответа не доходят")
    if '"has_thumb"' not in repo_src:
        raise AssertionError("app/repo.py не ставит has_thumb (1453) - источник потерен")
    if "'has_thumb'" not in lib:
        problems.append("has_thumb не читается из строки выборки - канон tileHtml "
                        "рисует обложку только когда файл есть (app.js:3535)")
    if "_thumbInFlight" not in lib:
        problems.append("батч в полёте не блокирует повторную постановку в очередь - "
                        "один id спрашивается несколько раз в одном кадре, канон же "
                        "снимает ids из thumbQueue до вызова (app.js:3024-3025)")
    if "_thumbQueue.remove(id)" not in lib:
        problems.append("ids не снимаются из очереди до вызова get_thumbs - канон "
                        "thumbQueue.delete(id) до call (app.js:3025)")
    if "thumbQueue.delete(id)" not in js:
        raise AssertionError("снятие ids из очереди в каноне не найдено - источник потерен")
    if ".slice(0, THUMB_BATCH)" not in js:
        raise AssertionError("нарезка батча в каноне не найдена - источник потерен")
    if "take(kThumbBatchMax)" not in lib:
        problems.append("очередь не режется лимитом kThumbBatchMax - батч может "
                        "превысить лимит бэка и получить отказ текстом")
    if "thumbCache.delete(thumbCache.keys().next().value)" not in js:
        raise AssertionError("LRU-вытеснение канона не найдено (rememberThumb) - "
                             "источник потерен")
    if "_entries.keys.first" not in tiles:
        problems.append("LRU-кэш Flutter не снимает старейший - кэш растёт без "
                        "вытеснения (rememberThumb app.js:2996-2998)")
    if "capacity = kThumbCacheMax" not in tiles:
        problems.append("ThumbCache не держит capacity из kThumbCacheMax - лимит кэша "
                        "написан на глаз")
    if 'rememberThumb(id, "")' not in js:
        raise AssertionError("кэш пустышки в каноне не найден (ids.forEach) - источник потерен")
    if "_thumbs.remember(id, decodeThumbDataUri(uri))" not in lib:
        problems.append("ответ кэшируется не по всем ids батча - отсутствие обложки "
                        "не запоминается пустышкой и спрашивается повторно "
                        "(app.js:3034-3036)")
    if "_thumbError" not in lib:
        problems.append("отказ чтения обложек молчит - канон .catch снимает ids без "
                        "текста (app.js:3038-3040), здесь отказ обязан быть назван")

    # -- типы обложек и тексты отказов бэка ------------------------------------
    mime_js = set(re.findall(r': "([^"]+)"',
                             re.search(r"THUMB_MIME = \{([\s\S]*?)\}", gui).group(1)))
    dart_mime_match = re.search(r"kThumbMimes = <String>\{([\s\S]*?)\}", tiles)
    if not dart_mime_match:
        problems.append("kThumbMimes не объявлен - типы data-URI бэка не сверены")
    else:
        dart_mime = set(re.findall(r"'([^']+)'", dart_mime_match.group(1)))
        if dart_mime != mime_js:
            problems.append(f"kThumbMimes {sorted(dart_mime)} != THUMB_MIME бэка "
                            f"{sorted(mime_js)} (app/gui.py:71-73) - картинка иного "
                            "типа показана как обложка")
    dart_code = _strip_comments(combined)
    for refused in ("слишком много id за раз", "обложка не скачана",
                    "не прочиталась или слишком большая"):
        if refused in gui and refused in dart_code:
            problems.append(f"production Dart дублирует отказ бэка {refused!r} - "
                            "текст приходит из ответа, клиент его не выдумывает")

    # -- потребление токенов плиткой --------------------------------------------
    if "var(--scrim)" not in play_block or "var(--on-scrim)" not in play_block:
        raise AssertionError(".tile-play не читает --scrim/--on-scrim - источник потерен")
    if "'scrim'" not in tiles or "'on-scrim'" not in tiles:
        problems.append("tiles.dart не читает роли scrim/on-scrim - подложка "
                        "play-кнопки написана литералом (app.css:696-697)")
    if "var(--surface-2)" not in thumb_block:
        raise AssertionError(".tile-thumb не читает --surface-2 - источник потерен")
    if "surfaceContainerLow" not in tiles:
        problems.append("подложка обложки не читает --surface-2 темы (surfaceContainerLow) - "
                        "пустая превью-ячейка красится литералом (app.css:675)")
    return problems


def _gui_keys(name: str, gui_src: str) -> set[str]:
    """Ключи запроса, которые цель читает: `request.get("k")` и
    `(request or {}).get("k")` (тот же ключ в другой форме записи -
    bulk_remove_rows:931, факт прогона стража: прежний regex его не видел)."""
    m = re.search(rf"    def {name}\(self[^)]*\) -> dict:([\s\S]*?)\n    def ", gui_src)
    if not m:
        raise AssertionError(f"в app/gui.py нет цели {name} - проводка потеряла источник")
    body = m.group(1)
    keys = set(re.findall(r'request\.get\("([a-z_]+)"', body))
    keys |= set(re.findall(r'\(request or \{\}\)\.get\("([a-z_]+)"', body))
    return keys


def dialog_mechanism_problems(trap_text: str | None = None, bulk_text: str | None = None,
                              js_text: str | None = None, html_text: str | None = None) -> list[str]:
    """Механизм диалога §3.8 потребляется panel'ью так же, как канон: выход
    по «-decline»/«-close», Enter и Escape ведут выход, disabled пропускаются,
    имя выхода и заголовок - канонные подписи."""
    trap = (trap_text if trap_text is not None
            else (LIB / "dialog_trap.dart").read_text(encoding="utf-8"))
    bulk = (bulk_text if bulk_text is not None
            else (LIB / "bulk_bar.dart").read_text(encoding="utf-8"))
    js = js_text if js_text is not None else JS.read_text(encoding="utf-8")
    html = html_text if html_text is not None else HTML.read_text(encoding="utf-8")
    problems = []
    for needle, text, source in (("class DialogTrap", trap, "dialog_trap.dart"),
                                 ("class DialogScaffold", trap, "dialog_trap.dart"),
                                 ("LogicalKeyboardKey.escape", trap, "dialog_trap.dart"),
                                 # Точная форма фильтра: комментарий §3.8 упоминает
                                 # canRequestFocus отдельно, и общая подпись
                                 # проходила бы даже без фильтра (факт guard-теста).
                                 ("candidate.canRequestFocus", trap, "dialog_trap.dart"),
                                 # Обход ведётся по scope'у диалога: SegmentedButton
                                 # строит цели сам и не принимает focusNode, поэтому
                                 # именованный список не покрывает ряд рейтинга.
                                 ("scope.traversalDescendants", trap, "dialog_trap.dart"),
                                 ("FocusScopeNode scope", trap, "dialog_trap.dart")):
        if needle not in text:
            problems.append(f"{source}: {needle} не найден - ловушка Tab/Escape "
                            "или фильтр disabled потеряны (§3.8)")
    if '[id$="-decline"], [id$="-later"]' not in js or '[id$="-close"]' not in js:
        problems.append("ui_src/app.js не имеет канонного dialogExit («-decline»/«-later» "
                        "затем «-close», app.js:4796-4799) - сверка входа диалога "
                        "потеряла источник: безопасный дефолт §3.8 не сверен")
    if 'aria-label="Закрыть"' not in html:
        problems.append("в ui_src/index.html нет подписи выхода «Закрыть» - крестик "
                        "диалога потерял имя")
    if "label: 'Закрыть'" not in trap:
        problems.append("dialog_trap.dart не называет выход «Закрыть» - AT читает "
                        "крестик без имени")
    exit_names = re.findall(r"exitName:\s*'([a-z]+)'", bulk)
    if exit_names.count("exit") < 2:
        problems.append("диалоги «Пометить…»/«Операции…» не ведут вход на крестик "
                        "(-close) - не-разрушительный дефолт §3.8 потерян")
    if exit_names.count("decline") < 1:
        problems.append("разрушительный диалог не ведёт вход на «Отклонить» "
                        "(-decline) - §3.8 дефолт отказ, а не принятие")
    if "=> ConfirmDialog(" not in bulk:
        problems.append("bulk_bar.dart не строит ConfirmDialog - разрушительная "
                        "цель без галки подтверждения (§3.7)")
    for html_id in ("legal-decline", "legal-accept"):
        if html_id not in html:
            problems.append(f"в ui_src/index.html нет {html_id} - механизм "
                            "подтверждения правил потерял источник сверки")
    if "button:not([disabled])" not in js or "el.offsetParent !== null" not in js:
        problems.append("в ui_src/app.js нет канонного обхода диалога "
                        "(dialogFocusables :not([disabled]) + dialogTargets "
                        "offsetParent, app.js:4778-4786) - сверка потеряла "
                        "источник: скрытые и disabled цели в Tab не входят")
    if "onPressed: _checked ?" not in bulk:
        problems.append("ConfirmDialog: «Принять» не включается галкой - тот же "
                        "канон legal-accept disabled до подтверждения")
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
    # На шаге 4 барьеров стало четыре группы плюс карточка: каждый showDialog
    # обязан читать veilColor темы - иначе затемнение пишется литералом.
    veil_total = 0
    for name in ("library_view.dart", "bulk_bar.dart", "video_card.dart"):
        veil_total += (LIB / name).read_text(encoding="utf-8").count("veilColor(")
    if veil_total < 4:
        problems.append(f"барьеры диалогов читают veilColor {veil_total} раз "
                        "(ожидаем не менее четырёх: карточка, «Пометить…», "
                        "«Операции…», подтверждение удаления) - затемнение "
                        "--veil темы применён не везде")
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
    out += grid_problems()
    out += toolbar_problems()
    out += bulk_texts_problems()
    out += payload_problems()
    out += tiles_problems()
    out += dialog_mechanism_problems()
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
          "ui_src/app.js и app/http/server.py; грид: колонок ячеек "
          f"{len(js_all_list_cols())} с весами wide/narrow и breakpoint "
          "900px, панель целей: канонных "
          f"{len(js_sel_bar_targets())} (построенные и названные с волной), "
          "проводки bulk_* и whitelist пометок сверены с app/gui.py и "
          "app/repo.py, механизмы диалогов §3.8 сверены с dialogExit "
"ui_src/app.js; плитки: рядов TILE "
          f"{len(js_tile_sizes(JS.read_text(encoding='utf-8')))} с чисел app.js, "
          "лимиты батча/кэша и коалесинг сверены с app.js и THUMB_BATCH_LIMIT бэка, "
          "формула колонок и 44 px play - с app.css, подписи плитки и ряда вида - с "
          "tileHtml и index.html, ключи и choices view_mode/tile_size - со схемы "
          "настроек, проводка get_thumbs (ids/thumbs, has_thumb, LRU, право read) - "
          "с app.js, repo.py, gui.py и scopes, тексты отказов бэка в клиенте не "
          "дублируются")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
