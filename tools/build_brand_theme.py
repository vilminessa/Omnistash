r"""Тема «Бренд» (coolors): вывод из палитры человека по порогам WCAG.

    python tools/build_brand_theme.py           # печатает вывод и измеренные контрасты
    python tools/build_brand_theme.py --check   # сверяет вывод с реестром app/themes.py

Палитра - https://coolors.co/palette/780000-c1121f-fdf0d5-003049-669bbc.
Она же лежит в SVG-иконках бренда (tools/build_icon_svgs.py, docs/ICONS.md).

Правило вывода одно и то же для всех ролей: структурная роль берёт базовый
цвет палитры, когда он держит порог контраста против подложки; когда не держит
- берётся производный tint того же цвета (смешение с другим цветом палитры, с
белым или с чёрным), шаг подбирается по относительной яркости WCAG, а не
«красивый hex». Подпись на подложке выбирается из цветов палитры, которые держат 4.5;
если ни один не держит - сборка отказывает с названием подложки, а не молчит.

Почему тема светлая, а не тёмная: red на navy даёт 2.22 (измерено по
яркости WCAG), ниже порога 3.0 для значимого акцента (SC 1.4.11), поэтому на
тёмной navy подложке акцентом red быть не может; на cream подложке red держит
5.51. Причина названа в hint темы и в docs/FLUTTER-TRANSITION.md (§W1).

Сверки:
  * app/themes.py (CONTRAST_PAIRS) - подписи, границы контрола и состояния
    против подложек; тема отказывает, если пара ниже порога;
  * различимость ролей (ROLE_PAIRS) - коллизия это одинаковый hex ИЛИ один тон
    при яркости, различающейся меньше чем в 1.1 раза; порог подобран по
    действующим темам (в тёмной теме hover/selected различаются на 1.13,
    ok/info на 1.04), более строгое правило отвергло бы прежнюю палитру;
  * вывод вне палитры - каждая hex-переменная обязана быть цветом палитры,
    смешением двух цветов палитры (или цвета с белой/чёрной точкой смешения)
    или смешением такого tint-a с цветом палитры вторым шагом;
  * --check - реестр app/themes.py совпадает с выводом переменная-в-переменная.

Значения детерминированы (без случайностей и без дат): повторный вывод даёт
байт-в-байт те же hex, поэтому страж сверяет реестр с генерацией, а не
«какая-то тема есть».
"""

from __future__ import annotations

import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import themes  # noqa: E402 - путь добавлен выше

THEME_ID = "coolors"
LABEL = "Бренд"
HINT = ("Палитра человека: cream подложка, navy подпись, red акцент, blue "
        "границы. На тёмной navy подложке red не выдерживает 3:1 (измерено "
        "2.22), поэтому тема светлая.")

# Палитра в нижнем регистре: реестр и app.css хранят hex в нижнем регистре,
# сверка идёт строка-в-строка.
MAROON = "#780000"
RED = "#c1121f"
CREAM = "#fdf0d5"
NAVY = "#003049"
BLUE = "#669bbc"
PALETTE = (MAROON, RED, CREAM, NAVY, BLUE)
# Белая и чёрная точки смешения - механика затемнения/осветления tint,
# не цвета палитры: они не появляются в ассетах и в выводах ролей сами по себе.
SHADES = ("#ffffff", "#000000")

# Роли, которые обязаны различаться на экране: два состояния одним цветом -
# это враньё про состояние, глаз не отличит.
ROLE_PAIRS = (("--accent", "--err"), ("--accent", "--warn"),
              ("--accent", "--focus"), ("--ok", "--info"),
              ("--focus", "--warn"), ("--focus", "--err"),
              ("--hover", "--selected"), ("--line-strong", "--muted"))

HEX_ORDER = list(themes.PALETTE_VARS) + list(themes.COMPOSITE_VARS)


def rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def blend(value: str, toward: str, t: float) -> str:
    """Смешать value с toward на долю t (0..1)."""
    a, b = rgb(value), rgb(toward)
    return "#" + "".join(f"{round(a[i] + (b[i] - a[i]) * t):02x}"
                         for i in range(3))


def satisfy(base: str, backgrounds: list[str], floor: float,
            toward: str) -> str:
    """Первый tint базового цвета, выдерживающий порог против всех подложек.

    Худшая подложка решает: роль обязана читаться на каждой из них.
    """
    for step in range(0, 101):
        candidate = blend(base, toward, step / 100)
        if all(themes.contrast_ratio(candidate, bg) >= floor for bg in backgrounds):
            return candidate
    raise ValueError(f"{base} на {backgrounds}: порог {floor} не выдержан "
                     f"ни одним tint к {toward}")


def pick_on(background: str, floor: float) -> str:
    """Подпись на подложке: цвет палитры, выдерживающий порог; отказ - не молчание."""
    best, best_ratio = None, 0.0
    for candidate in (CREAM, NAVY, MAROON, BLUE):
        ratio = themes.contrast_ratio(candidate, background)
        if ratio > best_ratio:
            best_ratio, best = ratio, candidate
    if best_ratio < floor:
        raise ValueError(f"на {background}: ни один цвет палитры не выдержал "
                         f"{floor} (лучший {best} {best_ratio:.2f})")
    return best


def hue(value: str) -> float:
    """Тон цвета в градусах (HSL); ахроматический - 0."""
    red, green, blue = (channel / 255 for channel in rgb(value))
    hi, lo = max(red, green, blue), min(red, green, blue)
    if hi - lo < 0.02:
        return 0.0
    if hi == red:
        tone = (green - blue) / (hi - lo) * 60
    elif hi == green:
        tone = 120 + (blue - red) / (hi - lo) * 60
    else:
        tone = 240 + (red - green) / (hi - lo) * 60
    return tone % 360


def hue_gap(a: str, b: str) -> float:
    gap = abs(hue(a) - hue(b))
    return min(gap, 360 - gap)


def build() -> dict:
    """Вывод ролей темы «Бренд» из палитры (светлая подложка - cream)."""
    vars_ = {}
    bg = CREAM
    surfaces = [bg, blend(bg, "#ffffff", 0.4), blend(bg, BLUE, 0.18),
                blend(bg, BLUE, 0.30)]
    vars_["--bg"] = bg
    vars_["--surface"] = surfaces[1]
    vars_["--surface-2"] = surfaces[2]
    vars_["--widget"] = surfaces[3]
    vars_["--hover"] = surfaces[2]        # наведённая строка/плитка
    vars_["--selected"] = surfaces[3]     # выбранная строка - темнее hover
    vars_["--line"] = blend(bg, NAVY, 0.25)   # декоративный разделитель
    vars_["--line-strong"] = satisfy(BLUE, [vars_["--surface"],
                                            vars_["--widget"]], 3.0, NAVY)
    vars_["--accent-dim"] = blend(bg, NAVY, 0.22)
    vars_["--text"] = NAVY
    vars_["--muted"] = satisfy(BLUE, [bg, vars_["--surface-2"],
                                      vars_["--widget"], vars_["--selected"],
                                      vars_["--accent-dim"]], 3.0, NAVY)
    vars_["--accent"] = satisfy(RED, surfaces[:2], 3.0, MAROON)
    vars_["--accent-2"] = blend(vars_["--accent"], MAROON, 0.35)
    vars_["--on-accent"] = pick_on(vars_["--accent"], 4.5)
    vars_["--scrim"] = NAVY
    vars_["--on-scrim"] = pick_on(vars_["--scrim"], 4.5)
    vars_["--warn"] = satisfy(MAROON, surfaces[:2], 3.0, NAVY)
    vars_["--on-warn"] = pick_on(vars_["--warn"], 4.5)
    # ok и info из одного семейства: разные шаги затемнения, иначе два
    # состояния окна выглядят одним цветом
    vars_["--ok"] = blend(BLUE, NAVY, 0.55)
    vars_["--info"] = blend(BLUE, NAVY, 0.30)
    # err темнее акцента тем же шагом смешения: ошибка и акцент различимы
    vars_["--err"] = blend(RED, MAROON, 0.4)
    # фокус - свой шаг смешения red с navy, не совпадает с warn и err
    vars_["--focus"] = blend(RED, NAVY, 0.25)
    vars_["--ok-line"] = blend(vars_["--ok"], bg, 0.35)
    vars_["--warn-line"] = blend(vars_["--warn"], bg, 0.35)
    vars_["--err-line"] = blend(vars_["--err"], bg, 0.35)
    vars_["--info-line"] = blend(vars_["--info"], bg, 0.35)
    vars_["--log-bg"] = blend(NAVY, MAROON, 0.35)
    vars_["--log-text"] = satisfy(CREAM, [vars_["--log-bg"]], 4.5, CREAM)
    # Тени и кольцо фокуса - не цвета, а строки box-shadow; цвет в них - navy
    # палитры (0, 48, 73), hex внутри тени не прячется: тень остаётся тоном.
    vars_["--focus-ring"] = "0 0 0 2px var(--bg), 0 0 0 4px var(--focus)"
    vars_["--shadow"] = "0 18px 50px rgba(0, 48, 73, .35)"
    vars_["--shadow-1"] = "0 1px 2px rgba(0, 48, 73, .25)"
    vars_["--shadow-2"] = "0 2px 8px rgba(0, 48, 73, .25)"
    vars_["--veil"] = "rgba(0, 48, 73, .45)"
    return vars_


def theme() -> dict:
    return {"id": THEME_ID, "label": LABEL, "dark": False, "hint": HINT,
            "vars": {name: build()[name] for name in HEX_ORDER}}


def palette_problems(vars_: dict) -> list[str]:
    """Hex-переменная обязана быть палитрой, её tint-ом или tint-ом tint-a.

    Шаг 1 - смешение двух цветов палитры (или цвета палитры с белой/чёрной
    точкой смешения). Шаг 2 - смешение tint-a шага 1 с цветом палитры: так
    выводятся линии состояний (--ok-line и т.д.), потому что им нужен тон
    собственного состояния, а не базового цвета. Проверка идёт инверсией
    смешения (x такой, что blend(x, toward, t) == value), а не перебором всех
    пар - перебор двух шагов дал бы миллионы смешений на одну переменную.
    """
    one_step = _blend_family()
    anchors = tuple(PALETTE) + SHADES
    problems = []
    for name in themes.PALETTE_VARS:
        value = vars_.get(name)
        if value is None:
            continue
        value = value.lower()
        if value in one_step:
            continue
        if any(x in one_step for toward in anchors
               for step in range(101)
               for x in _one_step_back(value, toward, step)):
            continue
        problems.append(f"{name}: {value} - не палитра, не её tint и не "
                        f"tint её tint-a")
    return problems


def _blend_family() -> set[str]:
    """Все цвета в один шаг смешения: палитра x палитра и палитра x тени."""
    out = set(PALETTE)
    for first in PALETTE:
        for second in PALETTE:
            for step in range(101):
                out.add(blend(first, second, step / 100))
        for shade in SHADES:
            for step in range(101):
                out.add(blend(first, shade, step / 100))
    return out


def _one_step_back(value: str, toward: str, step: int) -> list[str]:
    """Кандидаты x, для которых blend(x, toward, step/100) == value.

    Смешение округляет канал, поэтому точное решение не единственно: берём
    floor/ceil по каждому каналу и проверяем прямое смешение.
    """
    if step == 100:
        return [toward] if value == toward else []
    t = step / 100
    channels = []
    for v, c in zip(rgb(value), rgb(toward)):
        exact = (v - c * t) / (1 - t)
        lo = max(0, min(255, math.floor(exact)))
        hi = max(0, min(255, math.ceil(exact)))
        channels.append([lo, hi] if lo != hi else [lo])
    out = []
    for red in channels[0]:
        for green in channels[1]:
            for blue in channels[2]:
                x = f"#{red:02x}{green:02x}{blue:02x}"
                if blend(x, toward, t) == value:
                    out.append(x)
    return out


def role_problems(vars_: dict) -> list[str]:
    """Коллизии ролей: одинаковый hex или один тон при близкой яркости."""
    problems = []
    for a, b in ROLE_PAIRS:
        first, second = vars_[a], vars_[b]
        if first.lower() == second.lower():
            problems.append(f"{a} и {b}: один цвет {first}")
            continue
        ratio = themes.contrast_ratio(first, second)
        if hue_gap(first, second) < 12 and ratio < 1.1:
            problems.append(f"{a} и {b}: тон {hue(first):.0f} и "
                            f"{hue(second):.0f}, яркость различается на "
                            f"{ratio:.2f} - состояния неразличимы")
    return problems


def registry_problems() -> list[str]:
    """Реестр app/themes.py против вывода: переменная-в-переменная."""
    derived = build()
    registered = themes.css_vars(THEME_ID)
    problems = []
    if themes.by_id(THEME_ID) is None:
        problems.append(f"тема {THEME_ID} отсутствует в реестре app/themes.py")
        return problems
    for name in HEX_ORDER:
        if name not in registered:
            problems.append(f"{name}: нет в реестре темы {THEME_ID}")
            continue
        if registered[name] != derived[name]:
            problems.append(f"{name}: реестр {registered[name]} != вывод "
                            f"{derived[name]}")
    if themes.normalize(THEME_ID) != THEME_ID:
        problems.append(f"normalize({THEME_ID}) вернул {themes.normalize(THEME_ID)}")
    return problems


def problems() -> list[str]:
    """Все сверки темы одним списком; отказ называет причину построчно."""
    vars_ = build()
    out = list(themes.check(theme()))
    out += role_problems(vars_)
    out += palette_problems(vars_)
    out += registry_problems()
    return out


def contrast_table(vars_: dict) -> list[tuple[str, str, float, float]]:
    """Измеренные контрасты пар из CONTRAST_PAIRS (порог - часть пары)."""
    return [(fg, bg, themes.contrast_ratio(vars_[fg], vars_[bg]), floor)
            for fg, bg, floor in themes.CONTRAST_PAIRS]


def main(argv: list[str]) -> int:
    found = problems()
    if found:
        print("отказ темы «Бренд»:", file=sys.stderr)
        for line in found:
            print("  ", line, file=sys.stderr)
        return 1
    if "--check" in argv:
        print("реестр app/themes.py совпадает с выводом из палитры "
              "(переменная-в-переменная, hex в нижнем регистре)")
        return 0
    vars_ = build()
    print(f"тема «{LABEL}» (id {THEME_ID}), светлая: dark=False")
    print("вывод ролей:")
    for name in HEX_ORDER:
        print(f"  {name}: {vars_[name]}")
    print("измеренные контрасты пар из app/themes.py CONTRAST_PAIRS:")
    for fg, bg, ratio, floor in contrast_table(vars_):
        print(f"  {fg} на {bg}: {ratio:.2f} (порог {floor})")
    print("различимость ролей (порог коллизии: тон < 12 и яркость < 1.1):")
    for a, b in ROLE_PAIRS:
        print(f"  {a} против {b}: тон {hue(vars_[a]):.0f} и {hue(vars_[b]):.0f}, "
              f"яркость различается на "
              f"{themes.contrast_ratio(vars_[a], vars_[b]):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
