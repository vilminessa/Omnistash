"""Темы и шрифт интерфейса (M15): палитра и стек-семейства одним источником.

Тема - набор CSS-переменных, который применяется через body[data-theme="<id>"]
в ui_src/app.css. Никаких новых зависимостей и скачивания шрифтов: только
системные стек-семейства и переменные, которые понимает WebView.

Один источник правды: палитра здесь, её же значения сверяет тест с
app.css (block body[data-theme="<id>") - иначе список тем в окне и
палитра в CSS не могут разойтись незаметно. Подписи тем/шрифтов для
карточки настроек берутся отсюда же (settings_schema).

Проверка читаемости - по относительной яркости (WCAG relative luminance):
тема считается годной, если контраст текста с подложкой, подложек и
акцентной кнопки выше порогов. «Своя» тема не должна оказаться нечитаемой
- тест ловит битый hex и низкий контраст, а UI на битом значении
возвращает дефолт.
"""

from __future__ import annotations

import math

DEFAULT_THEME = "dark"

# Обязательные переменные палитры: каждая тема обязана держать этот набор,
# иначе часть окна (кнопки, подложки, полосы хода) потеряет цвет.
PALETTE_VARS: tuple = (
    "--bg", "--surface", "--surface-2", "--widget", "--line",
    "--text", "--muted", "--accent", "--accent-2", "--on-accent",
    "--warn", "--ok", "--err", "--info",
)

# Контраст, который обязана выдержать тема (пары «подпись -> подложка»).
# Пороги - WCAG AA: 4.5 для основного текста, 3.0 для приглушённых подписей
# и цветных состояний (они не текст, но должны быть различимы).
# --line здесь НЕ проверяется: это рамка подложки, а не подпись - её
# низкий контраст сознательный, иначе границы перестали бы быть мягкими.
CONTRAST_PAIRS: tuple = (
    ("--text", "--bg", 4.5),
    ("--text", "--surface", 4.5),
    ("--muted", "--bg", 3.0),
    ("--muted", "--surface-2", 3.0),
    ("--accent", "--bg", 3.0),
    ("--accent-2", "--bg", 3.0),
    ("--on-accent", "--accent", 4.5),
    ("--warn", "--bg", 3.0),
    ("--ok", "--bg", 3.0),
    ("--err", "--bg", 3.0),
    ("--info", "--bg", 3.0),
)

THEMES: tuple = (
    {
        "id": "dark",
        "label": "Тёмная",
        "dark": True,
        "hint": "Палитра по умолчанию (тёмное дерево и приглушённый акцент).",
        "vars": {
            "--bg": "#0c1622",
            "--surface": "#111d2c",
            "--surface-2": "#16263a",
            "--widget": "#1b2f47",
            "--line": "#22344a",
            "--text": "#dcdedd",
            "--muted": "#8b97a3",
            "--accent": "#628d7c",
            "--accent-2": "#7fb3a0",
            "--on-accent": "#08150f",
            "--warn": "#ffb454",
            "--ok": "#3fbf6b",
            "--err": "#e0554f",
            "--info": "#5aa9e6",
        },
    },
    {
        "id": "light",
        "label": "Светлая",
        "dark": False,
        "hint": "Светлая палитра: та же структура подложек и тот же акцент.",
        "vars": {
            "--bg": "#eef1f5",
            "--surface": "#ffffff",
            "--surface-2": "#f6f8fa",
            "--widget": "#e6ebf1",
            "--line": "#d3dae2",
            "--text": "#1c2430",
            "--muted": "#66727f",
            "--accent": "#2f7d67",
            "--accent-2": "#1f6b58",
            "--on-accent": "#ffffff",
            "--warn": "#b87400",
            "--ok": "#157f47",
            "--err": "#c0392f",
            "--info": "#1f6fb2",
        },
    },
    {
        "id": "contrast",
        "label": "Контрастная",
        "dark": True,
        "hint": "Максимальная разница подписей и подложки: для чтения при "
                "ярком освещении и для слабого зрения.",
        "vars": {
            "--bg": "#05070a",
            "--surface": "#0b1017",
            "--surface-2": "#121a24",
            "--widget": "#1b2632",
            "--line": "#33455a",
            "--text": "#f6f8fa",
            "--muted": "#b6c2cd",
            "--accent": "#ffd166",
            "--accent-2": "#ffe3a3",
            "--on-accent": "#0a0603",
            "--warn": "#ff9f43",
            "--ok": "#2fd9a0",
            "--err": "#ff6b6b",
            "--info": "#6cb8ff",
        },
    },
    {
        "id": "custom",
        "label": "Своя",
        "dark": True,
        "hint": "Тёплая палитра (бурое дерево и золото) - те же переменные, "
                "которые правятся прямо в ui_src/app.css в этом блоке.",
        "vars": {
            "--bg": "#14100c",
            "--surface": "#1c1712",
            "--surface-2": "#241d16",
            "--widget": "#2c241c",
            "--line": "#3d3226",
            "--text": "#ece6dc",
            "--muted": "#a89b8c",
            "--accent": "#c9a24b",
            "--accent-2": "#d9b872",
            "--on-accent": "#14100c",
            "--warn": "#d99a4a",
            "--ok": "#35b07a",
            "--err": "#d4573f",
            "--info": "#6aa9d8",
        },
    },
)

_BY_ID = {item["id"]: item for item in THEMES}

# Шрифт интерфейса: только системные стек-семейства (скачивания нет).
# Стеки совпадают со строками FONT_STACKS в ui_src/app.js - их сверяет тест.
FONT_DEFAULT = "system"
FONTS: tuple = (
    {"id": "system", "label": "Системный (Segoe UI)",
     "stack": '"Segoe UI", system-ui, -apple-system, sans-serif'},
    {"id": "sans", "label": "Гравитический (Arial)",
     "stack": '"Helvetica Neue", Arial, system-ui, sans-serif'},
    {"id": "serif", "label": "Серабный (Georgia)",
     "stack": 'Georgia, "Times New Roman", serif'},
    {"id": "mono", "label": "Моноширинный (Consolas)",
     "stack": 'Consolas, "Cascadia Mono", "Segoe UI Mono", monospace'},
)

_BY_FONT = {item["id"]: item for item in FONTS}

FONT_SIZE_DEFAULT = 13
FONT_SIZE_MIN = 10
FONT_SIZE_MAX = 22
FONT_SCALE_DEFAULT = 100
FONT_SCALE_MIN = 100
FONT_SCALE_MAX = 200

# Высота строки списка: тот же расчёт держит --row-h в ui_src/app.css
# (значения сверяет тест: расхождение дало бы строки, лезущие на ряд вверх).
ROW_MIN = 34           # нижняя граница: строки не мельчают вместе с текстом
ROW_FACTOR = 2.6       # множитель высоты строки относительно размера текста


def themes() -> list[dict]:
    """Темы для карточки и окна: id, подпись, тёмная ли она, переменные."""
    return [{"id": item["id"], "label": item["label"], "dark": item["dark"],
             "hint": item["hint"], "vars": dict(item["vars"])}
            for item in THEMES]


def ids() -> tuple:
    """Идентификаторы тем (порядок - как в карточке настроек)."""
    return tuple(item["id"] for item in THEMES)


def labels() -> list:
    """[[id, подпись], ...] - choices поля «Тема» в settings_schema."""
    return [[item["id"], item["label"]] for item in THEMES]


def by_id(theme_id) -> dict | None:
    """Запись темы по идентификатору (или None)."""
    return _BY_ID.get(str(theme_id or ""))


def normalize(value) -> str:
    """Значение настройки темы: битая -> дефолт (окно не «серый экран»)."""
    item = by_id(value)
    return item["id"] if item else DEFAULT_THEME


def safe_theme(value) -> dict:
    """Годная тема по значению настройки; битое значение -> дефолтная тема."""
    return _BY_ID[normalize(value)]


def css_vars(theme_id) -> dict:
    """CSS-переменные темы (копия: вызывающий не правит реестр)."""
    item = by_id(theme_id) or _BY_ID[DEFAULT_THEME]
    return dict(item["vars"])


def font_labels() -> list:
    """[[id, подпись], ...] - choices поля «Семейство шрифта»."""
    return [[item["id"], item["label"]] for item in FONTS]


def font_ids() -> tuple:
    return tuple(item["id"] for item in FONTS)


def normalize_font(value) -> str:
    """Семейство шрифта: битое значение -> дефолтное стек."""
    return value if value in _BY_FONT else FONT_DEFAULT


def font_stack(value) -> str:
    """Стек-семейство по id (битое значение -> системный стек)."""
    return _BY_FONT[normalize_font(value)]["stack"]


def _number(value, default: int, low: int, high: int) -> int:
    """Целочисленное значение с границами: битое/не то -> дефолт."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default
    return max(low, min(high, number))


def normalize_size(value) -> int:
    """Размер шрифта интерфейса (px): битое -> дефолт, в границах поля."""
    return _number(value, FONT_SIZE_DEFAULT, FONT_SIZE_MIN, FONT_SIZE_MAX)


def normalize_scale(value) -> int:
    """Множитель размера (проценты): битое -> 100, в границах поля."""
    return _number(value, FONT_SCALE_DEFAULT, FONT_SCALE_MIN, FONT_SCALE_MAX)


def font_px(size, scale) -> int:
    """Фактический базовый размер: множитель (проценты) применяем к размеру."""
    return _number(round(normalize_size(size) * normalize_scale(scale) / 100.0),
                   FONT_SIZE_DEFAULT, FONT_SIZE_MIN, FONT_SIZE_MAX)


def row_px(size, scale) -> float:
    """Высота строки списка: ровно то, что считает CSS --row-h.

    max(34px, базовый_размер * 2.6) - строки растут вместе с текстом, и
    подписи при крупном шрифте не обрезаются (виртуализация считает по ней).
    Значения границ/множителя сверяет тест с ui_src/app.css.
    """
    return float(max(ROW_MIN, font_px(size, scale) * ROW_FACTOR))


def _hex_ok(value) -> bool:
    """hex-значение (#rgb или #rrggbb) - то, что годно для CSS-переменной."""
    if not isinstance(value, str) or not value.startswith("#"):
        return False
    body = value[1:]
    if len(body) not in (3, 6):
        return False
    return all(char in "0123456789abcdefABCDEF" for char in body)


def _rgb(value: str) -> tuple:
    body = value[1:]
    if len(body) == 3:
        body = "".join(char * 2 for char in body)
    return (int(body[0:2], 16), int(body[2:4], 16), int(body[4:6], 16))


def luminance(value: str) -> float:
    """Относительная яркость цвета (WCAG), 0..1."""
    red, green, blue = _rgb(value)
    parts = []
    for channel in (red, green, blue):
        unit = channel / 255.0
        parts.append(unit / 12.92 if unit <= 0.03928
                     else math.pow((unit + 0.055) / 1.055, 2.4))
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast_ratio(fg: str, bg: str) -> float:
    """Контраст пары подписей (1..21), как требует WCAG."""
    first = luminance(fg)
    second = luminance(bg)
    light = max(first, second)
    dark = min(first, second)
    return (light + 0.05) / (dark + 0.05)


def vars_problems(theme: dict) -> list[str]:
    """Битые значения палитры: отсутствие переменной и не hex."""
    problems = []
    vars_ = theme.get("vars") or {}
    for name in PALETTE_VARS:
        value = vars_.get(name)
        if value is None:
            problems.append(f"{name} отсутствует")
        elif not _hex_ok(value):
            problems.append(f"{name} = {value!r} - не hex-значение")
    return problems


def contrast_problems(theme: dict) -> list[str]:
    """Нечитаемые пары темы (контраст ниже порога)."""
    vars_ = theme.get("vars") or {}
    problems = []
    for fg, bg, floor in CONTRAST_PAIRS:
        first = vars_.get(fg)
        second = vars_.get(bg)
        if not _hex_ok(first) or not _hex_ok(second):
            continue        # битые значения уже ловит vars_problems
        ratio = contrast_ratio(first, second)
        if ratio < floor:
            problems.append(f"{fg} на {bg}: контраст {ratio:.1f} ниже {floor}")
    return problems


def check(theme: dict) -> list[str]:
    """Все проблемы темы (пусто - тема годна и читаема)."""
    return vars_problems(theme) + contrast_problems(theme)


def readable(theme_id) -> bool:
    """Годна ли тема из реестра (значения + контраст)."""
    item = by_id(theme_id)
    return bool(item) and not check(item)
