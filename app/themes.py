"""Темы и шрифт интерфейса (M15): палитра и стек-семейства одним источником.

Тема - набор CSS-переменных, который применяется через body[data-theme="<id>"]
в ui_src/app.css. Никаких новых зависимостей и скачивания шрифтов: только
системные стек-семейства и переменные, которые понимает WebView.

Один источник правды: палитра здесь, её же значения сверяет тест с
app.css (block body[data-theme="<id>") - иначе список тем в окне и
палитра в CSS не могут разойтись незаметно. Подписи тем/шрифтов для
карточки настроек берутся отсюда же (settings_schema).

Цвета палитры - hex-переменные (PALETTE_VARS), кольцо фокуса и тени -
строки для box-shadow (COMPOSITE_VARS): их hex-проверка бессмысленна, и
цвет внутри тени прячется от сверки палитры, поэтому проверка отдельная.

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
# иначе часть окна (кнопки, подложки, полосы хода, состояния) потеряет цвет.
# Порядок - как в блоке :root (docs/DESIGN-UI.md §2.2): поверхности, границы,
# подписи, акцент, фокус, состояния, границы состояний, служебные подложки.
PALETTE_VARS: tuple = (
    "--bg", "--surface", "--surface-2", "--widget", "--hover", "--selected",
    "--line", "--line-strong",
    "--text", "--muted", "--on-accent", "--on-scrim", "--on-warn",
    "--accent", "--accent-2", "--accent-dim",
    "--focus",
    "--warn", "--ok", "--err", "--info",
    "--ok-line", "--warn-line", "--err-line", "--info-line",
    "--log-bg", "--log-text", "--scrim",
)

# Токены, которые НЕ являются цветом: кольцо фокуса и тени (elevation).
# Значение - строка для box-shadow; она не проходит hex-проверку, поэтому
# проверяется отдельно: непустая, без hex-литерала (цвет палитры в тени
# прячется и тема тогда «красивая», но не правится) и с px/rgba внутри.
COMPOSITE_VARS: tuple = (
    "--focus-ring", "--shadow", "--shadow-1", "--shadow-2", "--veil",
)

# Контраст, который обязана выдержать тема (пары «подпись -> подложка»).
# Пороги - WCAG AA: 4.5 для основного текста, 3.0 для приглушённых подписей
# и цветов состояний (они не текст, но должны быть различимы), 3.0 для
# границы контрола и кольца фокуса (SC 1.4.11 - значимый компонент).
# --line здесь НЕ проверяется: это декоративный разделитель, а не подпись и
# не компонент - её низкий контраст сознательный, иначе границы перестали бы
# быть мягкими. --line-strong проверяется: это рамка input/select/кнопки,
# без 3:1 контрол на подложке не различим.
CONTRAST_PAIRS: tuple = (
    # подписи на подложках окна
    ("--text", "--bg", 4.5),
    ("--text", "--surface", 4.5),
    ("--text", "--widget", 4.5),
    ("--text", "--surface-2", 4.5),
    ("--text", "--hover", 4.5),          # наведённая строка/плитка
    ("--text", "--selected", 4.5),       # выбранная строка (отличается от hover)
    ("--text", "--accent-dim", 4.5),     # «акцент размытый» (disabled primary)
    # приглушённые подписи (хинты, метаданные - порог 3.0)
    ("--muted", "--bg", 3.0),
    ("--muted", "--surface-2", 3.0),
    ("--muted", "--widget", 3.0),
    ("--muted", "--selected", 3.0),
    ("--muted", "--accent-dim", 3.0),
    # акцент и подписи на акцентных подложках
    ("--accent", "--bg", 3.0),
    ("--accent", "--surface", 3.0),      # ссылка/граница акцентной кнопки
    ("--accent-2", "--bg", 3.0),
    ("--accent-2", "--surface", 3.0),
    ("--on-accent", "--accent", 4.5),
    ("--on-scrim", "--scrim", 4.5),      # значок/длительность поверх обложки
    ("--on-warn", "--warn", 4.5),        # подпись на предупреждающей подложке
    # фокус и граница контрола (компонент, не декорация)
    ("--focus", "--surface", 3.0),
    ("--focus", "--widget", 3.0),
    ("--line-strong", "--surface", 3.0),
    ("--line-strong", "--widget", 3.0),
    ("--line-strong", "--bg", 3.0),
    # цвета состояний: и на фоне окна, и на подложке карточки/строки
    ("--warn", "--bg", 3.0),
    ("--warn", "--surface", 3.0),
    ("--ok", "--bg", 3.0),
    ("--ok", "--surface", 3.0),
    ("--err", "--bg", 3.0),
    ("--err", "--surface", 3.0),
    ("--info", "--bg", 3.0),
    ("--info", "--surface", 3.0),
    # журнал: своя подложка и своя подпись (не --bg/--text окна)
    ("--log-text", "--log-bg", 4.5),
)

THEMES: tuple = (
    {
        "id": "dark",
        "label": "Тёмная",
        "dark": True,
        "hint": "Палитра по умолчанию (тёмное дерево и приглушённый акцент).",
        # Значения пересчитаны по формуле WCAG (docs/DESIGN-UI.md §2.2):
        # границы контрола 3:1, приглушённая подпись и состояния AA.
        "vars": {
            "--bg": "#0c1622",
            "--surface": "#111d2c",
            "--surface-2": "#16263a",
            "--widget": "#1b2f47",
            "--hover": "#16263a",        # наведённая строка/плитка
            "--selected": "#1b2f47",     # выбранная строка - темнее hover
            "--line": "#45617f",         # декоративный разделитель
            "--line-strong": "#5b799b",  # граница контрола (3.76 к surface)
            "--text": "#dcdedd",
            "--muted": "#96a3b0",        # 7.08 к bg, 5.29 к widget
            "--on-accent": "#04140d",    # подпись на акцентной подложке
            "--on-scrim": "#dcdedd",     # значок поверх затемнённой обложки
            "--on-warn": "#241500",      # подпись на подложке --warn
            "--accent": "#628d7c",
            "--accent-2": "#7fb3a0",     # hover акцентной кнопки
            "--accent-dim": "#33506c",   # «акцент размытый» (disabled)
            "--focus": "#ffc766",        # кольцо фокуса (11.01 к surface)
            "--warn": "#ffb454",
            "--ok": "#46c079",
            "--err": "#ea6a63",
            "--info": "#6bb6e8",
            "--ok-line": "#2f6b4a",      # рамки карточек состояния (декор)
            "--warn-line": "#7a5321",
            "--err-line": "#7c332e",
            "--info-line": "#2b5a7d",
            "--log-bg": "#070d15",
            "--log-text": "#a8b6c4",
            "--scrim": "#040910",        # solid подложка значков на обложке
            "--focus-ring": "0 0 0 2px var(--bg), 0 0 0 4px var(--focus)",
            "--shadow": "0 18px 50px rgba(0, 0, 0, .45)",
            "--shadow-1": "0 1px 2px rgba(0, 0, 0, .35)",
            "--shadow-2": "0 2px 8px rgba(0, 0, 0, .35)",
            "--veil": "rgba(4, 9, 16, .62)",
        },
    },
    {
        "id": "light",
        "label": "Светлая",
        "dark": False,
        "hint": "Светлая палитра: та же структура подложек и тот же акцент.",
        # §2.3: --muted, --warn, --ok и --accent-2 пересчитаны - прежде
        # не выдерживали AA (3.35..4.33), а hover акцентной кнопки был
        # того же цвета, что акцент (невидимое состояние).
        "vars": {
            "--bg": "#eef1f5",
            "--surface": "#ffffff",
            "--surface-2": "#f6f8fa",
            "--widget": "#e6ebf1",
            "--hover": "#f6f8fa",
            "--selected": "#d8e2ea",
            "--line": "#899fb5",
            "--line-strong": "#6f869d",  # 3.77 surface, 3.14 widget
            "--text": "#1c2430",
            "--muted": "#576472",        # 5.34 к bg, 5.05 к widget
            "--on-accent": "#ffffff",
            "--on-scrim": "#ffffff",     # значок на тёмной подложке обложки
            "--on-warn": "#ffffff",      # --warn тёмный: нужна светлая подпись
            "--accent": "#2f7d67",
            "--accent-2": "#24634f",     # темнее акцента: hover виден
            "--accent-dim": "#c7ded6",
            "--focus": "#8a5a00",        # 5.93 к surface
            "--warn": "#8a5a00",         # было #b87400 - 3.35 к bg
            "--ok": "#0f6b3d",           # было #157f47 - 4.46 к bg
            "--err": "#b02e24",
            "--info": "#155a94",
            "--ok-line": "#9fc7b3",
            "--warn-line": "#d3b585",
            "--err-line": "#d3a09a",
            "--info-line": "#9dc0dd",
            "--log-bg": "#141b24",       # журнал остаётся тёмным терминалом
            "--log-text": "#c9d4de",
            "--scrim": "#040910",
            "--focus-ring": "0 0 0 2px var(--bg), 0 0 0 4px var(--focus)",
            "--shadow": "0 18px 50px rgba(28, 36, 48, .22)",
            "--shadow-1": "0 1px 2px rgba(28, 36, 48, .12)",
            "--shadow-2": "0 2px 8px rgba(28, 36, 48, .14)",
            "--veil": "rgba(28, 36, 48, .45)",
        },
    },
    {
        "id": "coolors",
        "label": "Бренд",
        "dark": False,
        "hint": "Палитра человека: cream подложка, navy подпись, red акцент, blue границы. На тёмной navy подложке red не выдерживает 3:1 (измерено 2.22), поэтому тема светлая.",
        # Выведена из палитры coolors: базовые цвета там, где
        # они выдерживают порог контраста, и производный tint того
        # же цвета там, где не выдерживают (сверка app/themes.py).
        "vars": {
            "--bg": "#fdf0d5",
            "--surface": "#fef6e6",
            "--surface-2": "#e2e1d0",
            "--widget": "#d0d6ce",
            "--hover": "#e2e1d0",
            "--selected": "#d0d6ce",
            "--line": "#bec0b2",
            "--line-strong": "#497d9c",
            "--text": "#003049",
            "--muted": "#3f7290",
            "--on-accent": "#fdf0d5",
            "--on-scrim": "#fdf0d5",
            "--on-warn": "#fdf0d5",
            "--accent": "#c1121f",
            "--accent-2": "#a70c14",
            "--accent-dim": "#c5c6b6",
            "--focus": "#911a2a",
            "--warn": "#780000",
            "--ok": "#2e607d",
            "--err": "#a40b13",
            "--info": "#477b9a",
            "--ok-line": "#76929c",
            "--warn-line": "#a7544b",
            "--err-line": "#c35b57",
            "--info-line": "#87a4af",
            "--log-bg": "#2a1f2f",
            "--log-text": "#fdf0d5",
            "--scrim": "#003049",
            "--focus-ring": "0 0 0 2px var(--bg), 0 0 0 4px var(--focus)",
            "--shadow": "0 18px 50px rgba(0, 48, 73, .35)",
            "--shadow-1": "0 1px 2px rgba(0, 48, 73, .25)",
            "--shadow-2": "0 2px 8px rgba(0, 48, 73, .25)",
            "--veil": "rgba(0, 48, 73, .45)",
        },
    },
    {
        "id": "contrast",
        "label": "Контрастная",
        "dark": True,
        "hint": "Максимальная разница подписей и подложки: для чтения при "
                "ярком освещении и для слабого зрения.",
        # Границы контролов и разделители подняты: на почти чёрной подложке
        # мягкая граница почти не видна, а фокус должен отличаться от
        # акцента (акцент - золото, кольцо - светлая голубая).
        "vars": {
            "--bg": "#05070a",
            "--surface": "#0b1017",
            "--surface-2": "#121a24",
            "--widget": "#1b2632",
            "--hover": "#121a24",
            "--selected": "#24364a",
            "--line": "#5c7690",
            "--line-strong": "#7f9ab5",
            "--text": "#f6f8fa",
            "--muted": "#b6c2cd",
            "--on-accent": "#0a0603",
            "--on-scrim": "#ffffff",
            "--on-warn": "#241500",
            "--accent": "#ffd166",
            "--accent-2": "#ffe3a3",
            "--accent-dim": "#2b2318",
            "--focus": "#cfe6ff",        # отличается от золотого акцента
            "--warn": "#ff9f43",
            "--ok": "#2fd9a0",
            "--err": "#ff6b6b",
            "--info": "#6cb8ff",
            "--ok-line": "#4fae7f",
            "--warn-line": "#c08a3e",
            "--err-line": "#d07070",
            "--info-line": "#5f9fd6",
            "--log-bg": "#010305",
            "--log-text": "#c8d6e2",
            "--scrim": "#000000",
            "--focus-ring": "0 0 0 2px var(--bg), 0 0 0 4px var(--focus)",
            "--shadow": "0 18px 50px rgba(0, 0, 0, .6)",
            "--shadow-1": "0 1px 2px rgba(0, 0, 0, .45)",
            "--shadow-2": "0 2px 8px rgba(0, 0, 0, .45)",
            "--veil": "rgba(2, 4, 6, .75)",
        },
    },
    {
        "id": "custom",
        "label": "Своя",
        "dark": True,
        "hint": "Тёплая палитра (бурое дерево и золото) - те же переменные, "
                "которые правятся прямо в ui_src/app.css в этом блоке.",
        # Тот же набор, что у остальных тем: рамка контрола светлее поля
        # контрола (иначе 2.83:1 - контрол на подложке не различим).
        "vars": {
            "--bg": "#14100c",
            "--surface": "#1c1712",
            "--surface-2": "#241d16",
            "--widget": "#2c241c",
            "--hover": "#241d16",
            "--selected": "#3a2f24",
            "--line": "#5d4a36",
            "--line-strong": "#8d745c",
            "--text": "#ece6dc",
            "--muted": "#b4a692",
            "--on-accent": "#14100c",
            "--on-scrim": "#f6efe4",
            "--on-warn": "#241500",
            "--accent": "#c9a24b",
            "--accent-2": "#d9b872",
            "--accent-dim": "#4a3a22",
            "--focus": "#f0b35c",
            "--warn": "#d99a4a",
            "--ok": "#35b07a",
            "--err": "#d4573f",
            "--info": "#6aa9d8",
            "--ok-line": "#2e6b52",
            "--warn-line": "#74522b",
            "--err-line": "#7c3a2b",
            "--info-line": "#2b5a7d",
            "--log-bg": "#0b0806",
            "--log-text": "#c8bda9",
            "--scrim": "#0a0705",
            "--focus-ring": "0 0 0 2px var(--bg), 0 0 0 4px var(--focus)",
            "--shadow": "0 18px 50px rgba(0, 0, 0, .5)",
            "--shadow-1": "0 1px 2px rgba(0, 0, 0, .4)",
            "--shadow-2": "0 2px 8px rgba(0, 0, 0, .4)",
            "--veil": "rgba(12, 9, 6, .62)",
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


def _composite_ok(value) -> bool:
    """Значение кольца фокуса или тени: непустая строка для box-shadow.

    hex-литерал здесь запрещён: цвет должен идти из hex-переменной палитры
    (var(--focus), var(--bg)), иначе в тени прячется цвет, которого в теме
    нет, и сверка палитры с app.css его не ловит.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    if "#" in value:
        return False
    return "px" in value or "rgba(" in value


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
    """Битые значения палитры: отсутствие переменной и не hex.

    Кольцо фокуса и тени (--focus-ring/--shadow/--veil) проверяются отдельно:
    это не цвета, а строки для box-shadow, и hex-проверка для них бессмысленна.
    """
    problems = []
    vars_ = theme.get("vars") or {}
    for name in PALETTE_VARS:
        value = vars_.get(name)
        if value is None:
            problems.append(f"{name} отсутствует")
        elif not _hex_ok(value):
            problems.append(f"{name} = {value!r} - не hex-значение")
    for name in COMPOSITE_VARS:
        value = vars_.get(name)
        if value is None:
            problems.append(f"{name} отсутствует")
        elif not _composite_ok(value):
            problems.append(f"{name} = {value!r} - не значение для box-shadow")
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
