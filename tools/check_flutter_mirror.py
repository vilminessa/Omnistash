r"""Mirror-страж токенов темы: Dart-ассет сверяется с реестром и с CSS.

    python tools/check_flutter_mirror.py           # сверка, отказ называет причину
    python tools/check_flutter_mirror.py --check   # тот же ответ для CI-строки

GitHub-раннер Flutter не гоняет (docs/FLUTTER-TRANSITION.md §6: UI-тесты и
Impeller меряются на машине человека), поэтому расхождение двух клиентов
сверяется текстом ассета без тулчейна: ui_flutter/lib/theme_tokens.dart
парсится как текст и сравнивается с app/themes.py (роли, подписи, флаги dark,
пары контраста, границы шрифта, формула строки) и с ui_src/app.css (:root -
метрики DESIGN-UI §2). Плюс проверка, что production-код Flutter не держит
литералов: ни hex-цвета вне токенов, ни размера или длительности числом -
имена ролей читаются из реестра.

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

from app import settings_schema, themes  # noqa: E402 - путь добавлен выше
import build_flutter_theme as gen  # noqa: E402 - путь добавлен выше

TOKENS = ROOT / "ui_flutter" / "lib" / "theme_tokens.dart"
LIB = ROOT / "ui_flutter" / "lib"
CSS = ROOT / "ui_src" / "app.css"
JS = ROOT / "ui_src" / "app.js"
REPO = ROOT / "app" / "repo.py"

# Production-код Flutter: обязан читать токены, а не писать литералы.
# main.dart и spike-файлы здесь нет намеренно: стенд W0 держит фиксированную
# геометрию списка (высота ряда 44 px), и его числа описаны в
# docs/FLUTTER-TRANSITION.md §9 - это мерка, а не дизайн-токен.
PRODUCTION_FILES = ("theme.dart", "library_view.dart", "app.dart")

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
    return {
        "ids": ids,
        "default": default.group(1) if default else "",
        "labels": labels,
        "dark": darks,
        "colors": colors,
        "pairs": pairs,
        "fonts": fonts,
        "metrics": metrics,
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
    out += contract_problems()
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
          f"{len(parsed['metrics'])} метрик, {len(parsed['pairs'])} пар)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
