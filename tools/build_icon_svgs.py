r"""Иконки Omnistash и Stashroom: SVG в палитре человека, доступная разметка.

    python tools/build_icon_svgs.py            # пишет assets/icons/
    python tools/build_icon_svgs.py --check    # сверяет ассеты с генерацией

Геометрия задаётся ОДНИМ источником (GEOMETRY / GEOMETRY_16 в этом файле),
из неё выводятся все форматы: самостоятельная иконка, 16px-силуэт, мастер
1024 для .ico, спрайт <symbol> и демо вариантов использования. Ассеты
детерминированы (без случайностей и без дат): повторный запуск даёт
байт-в-байт те же файлы, поэтому тест сверяет asset с генерацией.

Правила доступности взяты из статьи «Доступные SVG-иконки с инлайновыми
спрайтами» (web-standards.ru, перевод 24a11y) и её источников:
  * самостоятельная иконка - role="img", <title id>, aria-labelledby по этому
    id, focusable="false" (IE/Edge фокусировали svg);
  * иконка рядом с текстом - aria-hidden="true" без title: текст первичный
    носитель смысла (WAI alt decision tree);
  * спрайт - <symbol> с сохранённым viewBox (при оптимизации viewBox удалять
    нельзя - Sara Soueidan про системы координат), подпись и скрытие решаются
    на месте <use>;
  * монохромный вариант - fill/stroke currentColor, масштаб от размера шрифта;
  * значимая графика держит контраст >= 3.0 (WCAG SC 1.4.11); слои ниже 3.0
    называются декоративными, они не носят смысл.

Палитра - https://coolors.co/palette/780000-c1121f-fdf0d5-003049-669bbc.
Контрасты считаются здесь же по относительной яркости WCAG и печатаются в
отчёте; сборка отказывает, если значимый слой ниже порога, а не молчит.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "icons"

MAROON = "#780000"
RED = "#C1121F"
CREAM = "#FDF0D5"
NAVY = "#003049"
BLUE = "#669BBC"
PALETTE = (MAROON, RED, CREAM, NAVY, BLUE)

# Порог значимой графики (WCAG SC 1.4.11). Роли: near/mid/accent/edge -
# значимые, far - декоративная глубина, обязана быть ниже или равна порогу и
# названа декоративной.
MEANING_MIN = 3.0
DECORATIVE_MAX = 3.0

# kind, role, координаты. Сетка 24x24, все числа кратны 0.25, значимые фигуры
# центрованы по оси канваса (x=12); `center` у path - центроид треугольника
# или середина bbox арки, `expect_cy` - заявленный центр по y.
GEOMETRY: dict = {
    "omnistash": {
        "title": "Omnistash",
        "tile": CREAM,
        "shapes": [
            # рамка плитки: navy на cream 4.59 - значимый край, чтобы плитка
            # читалась и на светлом, и на тёмном фоне рабочего стола
            {"kind": "rect", "role": "edge", "x": 0.75, "y": 0.75, "w": 22.5,
             "h": 22.5, "rx": 5.25, "stroke": NAVY, "sw": 0.5},
            # дальний слой глубины: blue на cream - декоративный
            {"kind": "circle", "role": "far", "cx": 12, "cy": 12, "r": 7.75,
             "stroke": BLUE, "sw": 0.75, "expect_cy": 12},
            # кольцо (omni): navy на cream - значимый ближний слой
            {"kind": "circle", "role": "near", "cx": 12, "cy": 12, "r": 6.75,
             "stroke": NAVY, "sw": 1.5, "expect_cy": 12},
            # полки-строки библиотеки (stash): блок 8.5..15.5 центрован по
            # y=12, каждая центрована по x=12
            {"kind": "rect", "role": "far", "x": 8, "y": 8.5, "w": 8, "h": 1.5,
             "rx": 0.75, "fill": BLUE, "expect_cy": 9.25},
            {"kind": "rect", "role": "mid", "x": 8.6, "y": 11.25, "w": 6.8,
             "h": 1.5, "rx": 0.75, "fill": MAROON, "expect_cy": 12},
            {"kind": "rect", "role": "near", "x": 9.2, "y": 14, "w": 5.6,
             "h": 1.5, "rx": 0.75, "fill": NAVY, "expect_cy": 14.75},
            # прорезь входа: перекрывает полосу штриха кольца (4.5..6.0);
            # red на cream - значимый акцент; вместе с нижним краем кольца
            # блок 4.5..19.5 центрован по y=12
            {"kind": "rect", "role": "accent", "x": 10.5, "y": 4.5, "w": 3,
             "h": 2.5, "rx": 1.25, "fill": RED, "expect_cy": 5.75},
        ],
    },
    "stashroom": {
        "title": "Stashroom",
        "tile": MAROON,
        "shapes": [
            # рамка плитки: cream на maroon 10.34 - значимый край
            {"kind": "rect", "role": "edge", "x": 0.75, "y": 0.75, "w": 22.5,
             "h": 22.5, "rx": 5.25, "stroke": CREAM, "sw": 0.5},
            # внутренность комнаты: navy на maroon - декоративная глубина
            {"kind": "path", "role": "far",
             "d": "M8 17 V11 A4 4 0 0 1 16 11 V17 Z", "fill": NAVY,
             "center": (12, 12), "expect_cy": 12},
            # арка-дверь (комната): cream на maroon - значимый; путь без Z,
            # нижний край рисует отдельная линия порога
            {"kind": "path", "role": "near",
             "d": "M8 17 V11 A4 4 0 0 1 16 11 V17", "stroke": CREAM,
             "sw": 1.5, "center": (12, 12), "expect_cy": 12},
            # внутренний hairline арки: blue на maroon 3.88 - значимый mid
            {"kind": "path", "role": "mid",
             "d": "M9.25 15.75 V11 A2.75 2.75 0 0 1 14.75 11 V15.75",
             "stroke": BLUE, "sw": 0.75, "center": (12, 13.375)},
            # порог комнаты: центрован по x=12 (6.5..17.5)
            {"kind": "path", "role": "near", "d": "M6.5 17 H17.5",
             "stroke": CREAM, "sw": 1.5, "cap": "round", "center": (12, 17)},
            # play-треугольник внутри hairline: центроид ровно (12, 13.75) -
            # центр видимой внутренней области комнаты (11.375..16.25)
            {"kind": "path", "role": "near",
             "d": "M11 11.5 L14 13.75 L11 16 Z", "fill": CREAM,
             "center": (12, 13.75), "expect_cy": 13.75},
        ],
    },
}

# 16px: тот же силуэт, но без декоративного дальнего слоя (на 16 пикселах он
# не читается) и со штрихами >= 1 пиксела. Оси симметрии - x=8, y=8.
GEOMETRY_16: dict = {
    "omnistash": {
        "title": "Omnistash",
        "tile": CREAM,
        "shapes": [
            {"kind": "rect", "role": "edge", "x": 0.5, "y": 0.5, "w": 15,
             "h": 15, "rx": 3.5, "stroke": NAVY, "sw": 1},
            {"kind": "circle", "role": "near", "cx": 8, "cy": 8, "r": 4.5,
             "stroke": NAVY, "sw": 1.4, "expect_cy": 8},
            {"kind": "rect", "role": "mid", "x": 4.6, "y": 6.05, "w": 6.8,
             "h": 1.3, "rx": 0.65, "fill": MAROON, "expect_cy": 6.7},
            {"kind": "rect", "role": "near", "x": 5.6, "y": 8.65, "w": 4.8,
             "h": 1.3, "rx": 0.65, "fill": NAVY, "expect_cy": 9.3},
            # прорезь точно на верхней полосе штриха кольца (2.8..4.2)
            {"kind": "rect", "role": "accent", "x": 7, "y": 2.8, "w": 2,
             "h": 1.4, "rx": 0.7, "fill": RED, "expect_cy": 3.5},
        ],
    },
    "stashroom": {
        "title": "Stashroom",
        "tile": MAROON,
        "shapes": [
            {"kind": "rect", "role": "edge", "x": 0.5, "y": 0.5, "w": 15,
             "h": 15, "rx": 3.5, "stroke": CREAM, "sw": 1},
            # на 16 пикселах декоративная внутренность (1.18 к maroon) не
            # читается - силуэт держится аркой, порогом и треугольником
            {"kind": "path", "role": "near",
             "d": "M5 12 V7 A3 3 0 0 1 11 7 V12", "stroke": CREAM,
             "sw": 1.2, "center": (8, 8), "expect_cy": 8},
            {"kind": "path", "role": "near", "d": "M3.8 12 H12.2",
             "stroke": CREAM, "sw": 1.2, "cap": "round", "center": (8, 12)},
            # центроид ровно (8, 8)
            {"kind": "path", "role": "near", "d": "M7 6.5 L10 8 L7 9.5 Z",
             "fill": CREAM, "center": (8, 8), "expect_cy": 8},
        ],
    },
}


def luminance(hex_color: str) -> float:
    """Относительная яркость по WCAG 2.x (sRGB, линейный канал)."""
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    hi, lo = max(luminance(a), luminance(b)), min(luminance(a), luminance(b))
    return (hi + 0.05) / (lo + 0.05)


def shape_center(shape: dict) -> tuple[float, float]:
    """Центр фигуры по её числам; path обязан называть `center`."""
    if shape["kind"] == "rect":
        return shape["x"] + shape["w"] / 2, shape["y"] + shape["h"] / 2
    if shape["kind"] == "circle":
        return shape["cx"], shape["cy"]
    if "center" in shape:
        return tuple(shape["center"])
    raise ValueError(f"path роль {shape['role']} не называет center: "
                     f"{shape['d'][:40]}")


def verify_geometry() -> list[str]:
    """Сверка симметрии по числам. Отказ называет фигуру и измеренный центр."""
    problems = []
    for canvas, table in (("24", GEOMETRY), ("16", GEOMETRY_16)):
        axis = 12 if canvas == "24" else 8
        for icon, geo in table.items():
            for shape in geo["shapes"]:
                cx, cy = shape_center(shape)
                if shape["role"] != "edge" and abs(cx - axis) > 0.001:
                    problems.append(f"{canvas} {icon} {shape['kind']} роль "
                                    f"{shape['role']}: центр x={cx} != {axis}")
                if (shape.get("expect_cy") is not None
                        and abs(cy - shape["expect_cy"]) > 0.001):
                    problems.append(f"{canvas} {icon} {shape['kind']} роль "
                                    f"{shape['role']}: центр y={cy} != "
                                    f"{shape['expect_cy']}")
    return problems


def verify_contrast() -> tuple[list[str], list[tuple[str, str, float, str]]]:
    """Сверка контрастов по ролям. Возвращает отказ и измеренную таблицу."""
    problems = []
    table = []
    for canvas, geo_table in (("24", GEOMETRY), ("16", GEOMETRY_16)):
        for icon, geo in geo_table.items():
            tile = geo["tile"]
            for shape in geo["shapes"]:
                color = shape.get("stroke") or shape.get("fill")
                if color is None or color not in PALETTE:
                    problems.append(f"{canvas} {icon} {shape['kind']}: цвет "
                                    f"{color!r} не из палитры")
                    continue
                value = contrast(color, tile)
                if shape["role"] == "far":
                    if value > DECORATIVE_MAX:
                        problems.append(f"{canvas} {icon} {shape['role']} "
                                        f"{color}: {value:.2f} > порога "
                                        f"декоративного слоя {DECORATIVE_MAX}")
                    verdict = "декоративный"
                else:
                    if value < MEANING_MIN:
                        problems.append(f"{canvas} {icon} {shape['role']} "
                                        f"{color} на {tile}: {value:.2f} < "
                                        f"{MEANING_MIN} - значимый слой")
                    verdict = "значимый"
                table.append((f"{canvas} {icon}", f"{color} на {tile}", value,
                              verdict))
    return problems, table


def verify_16_readability() -> list[str]:
    """На 16 пикселах значимый штрих обязан быть >= 1 пиксела."""
    problems = []
    for icon, geo in GEOMETRY_16.items():
        for shape in geo["shapes"]:
            if shape["role"] == "far":
                continue
            sw = shape.get("sw")
            if sw is not None and sw < 1.0:
                problems.append(f"16 {icon} {shape['kind']} роль "
                                f"{shape['role']}: штрих {sw} < 1 пиксела")
    return problems


def attrs_str(shape: dict, mono: bool = False) -> str:
    """Атрибуты фигуры; mono=True - один цвет currentColor вместо палитры."""
    if shape["kind"] == "rect":
        parts = [f'x="{shape["x"]}"', f'y="{shape["y"]}"',
                 f'width="{shape["w"]}"', f'height="{shape["h"]}"']
        if "rx" in shape:
            parts.append(f'rx="{shape["rx"]}"')
    elif shape["kind"] == "circle":
        parts = [f'cx="{shape["cx"]}"', f'cy="{shape["cy"]}"',
                 f'r="{shape["r"]}"']
    else:
        parts = [f'd="{shape["d"]}"']
    fill = shape.get("fill")
    stroke = shape.get("stroke")
    opacity = shape.get("opacity")
    if mono:
        fill = "currentColor" if fill else None
        stroke = "currentColor" if stroke else None
        if shape["role"] == "far":
            # глубина в монохроме остаётся слоем, а не цветом палитры
            opacity = 0.45
        elif shape["role"] == "edge":
            # рамка остаётся волосяной, чтобы не утолщать силуэт
            opacity = 0.5
    parts.append(f'fill="{fill or "none"}"')
    if stroke:
        parts.append(f'stroke="{stroke}" stroke-width="{shape["sw"]}"')
        if shape.get("cap"):
            parts.append(f'stroke-linecap="{shape["cap"]}"')
    if opacity:
        parts.append(f'opacity="{opacity}"')
    return " ".join(parts)


def body(shapes: list, mono: bool = False) -> str:
    return "\n".join(f'    <{shape["kind"]} {attrs_str(shape, mono)}/>'
                     for shape in shapes)


def standalone(name: str, geo: dict, size: int) -> str:
    """Самостоятельная иконка: role="img", title по id, aria-labelledby."""
    tid = f"{name}-title"
    rx = 5.25 * size / 24
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
            f'height="{size}" viewBox="0 0 {size} {size}" role="img" '
            f'aria-labelledby="{tid}" focusable="false">\n'
            f'  <title id="{tid}">{geo["title"]}</title>\n'
            f'  <rect x="0" y="0" width="{size}" height="{size}" '
            f'rx="{rx:.2f}" fill="{geo["tile"]}"/>\n'
            f'{body(geo["shapes"])}\n</svg>\n')


def sprite(names: list) -> str:
    """Спрайт: <symbol> с viewBox, подписи и скрытие решаются на <use>."""
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" '
             'style="display:none">']
    for name in names:
        geo = GEOMETRY[name]
        parts.append(f'  <symbol id="{name}" viewBox="0 0 24 24">')
        parts.append(f'    <rect x="0" y="0" width="24" height="24" rx="5.25" '
                     f'fill="{geo["tile"]}"/>')
        parts.append(body(geo["shapes"]))
        parts.append('  </symbol>')
        parts.append(f'  <symbol id="{name}-mono" viewBox="0 0 24 24">')
        parts.append(body(geo["shapes"], mono=True))
        parts.append('  </symbol>')
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def tile_master(name: str, geo: dict) -> str:
    """Мастер 1024 для растеризации в .ico: те же фигуры, масштаб 1024/24."""
    scale = 1024 / 24
    inner = "\n".join(f'    <{shape["kind"]} {attrs_str(shape)}/>'
                      for shape in geo["shapes"])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="1024" '
            f'height="1024" viewBox="0 0 1024 1024" role="img" '
            f'aria-labelledby="{name}-tile-title" focusable="false">\n'
            f'  <title id="{name}-tile-title">{geo["title"]}</title>\n'
            f'  <rect x="0" y="0" width="1024" height="1024" '
            f'rx="{5.25 * scale:.2f}" fill="{geo["tile"]}"/>\n'
            f'  <g transform="scale({scale:.6f})">\n{inner}\n  </g>\n</svg>\n')


def demo(names: list) -> str:
    """Демо вариантов использования из статьи: title, aria-hidden, sprite, 1em."""
    rows = []
    for name in names:
        title = GEOMETRY[name]["title"]
        rows.append(f"""
    <h2>{title}</h2>
    <div class="row">
      <button type="button">
        <svg class="svg-icon" role="img" aria-labelledby="t-{name}-stand" focusable="false">
          <title id="t-{name}-stand">{title}</title>
          <use href="#{name}"></use>
        </svg>
      </button>

      <button type="button">
        <svg class="svg-icon" aria-hidden="true" focusable="false">
          <use href="#{name}"></use>
        </svg>
        {title} - текст первичен, иконка декоративна
      </button>

      <button type="button">
        <svg class="svg-icon" aria-hidden="true" focusable="false">
          <use href="#{name}"></use>
        </svg>
        <span class="visually-hidden">Только иконка, текст скрыт визуально</span>
      </button>
    </div>

    <p>Масштаб от размера шрифта (монохром, currentColor): <svg class="svg-icon" aria-hidden="true" focusable="false"><use href="#{name}-mono"></use></svg> обычный,
      <span class="big">крупный <svg class="svg-icon" aria-hidden="true" focusable="false"><use href="#{name}-mono"></use></svg></span>,
      <span class="small">мелкий <svg class="svg-icon" aria-hidden="true" focusable="false"><use href="#{name}-mono"></use></svg></span></p>

    <div class="sizes">
      <svg style="width:16px;height:16px" role="img" aria-labelledby="t-{name}-16" focusable="false"><title id="t-{name}-16">{title}, 16 пиксела</title><use href="#{name}"></use></svg>
      <svg style="width:24px;height:24px" role="img" aria-labelledby="t-{name}-24" focusable="false"><title id="t-{name}-24">{title}, 24 пиксела</title><use href="#{name}"></use></svg>
      <svg style="width:48px;height:48px" role="img" aria-labelledby="t-{name}-48" focusable="false"><title id="t-{name}-48">{title}, 48 пикселов</title><use href="#{name}"></use></svg>
      <svg style="width:128px;height:128px" role="img" aria-labelledby="t-{name}-128" focusable="false"><title id="t-{name}-128">{title}, 128 пикселов</title><use href="#{name}"></use></svg>
    </div>
    <div class="sizes light">
      <svg style="width:24px;height:24px" aria-hidden="true" focusable="false"><use href="#{name}-mono"></use></svg>
      <svg style="width:48px;height:48px" aria-hidden="true" focusable="false"><use href="#{name}-mono"></use></svg>
    </div>
""")
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Иконки Omnistash и Stashroom - варианты использования</title>
<style>
  body {{ background: {NAVY}; color: {CREAM}; font-family: system-ui, sans-serif;
          padding: 24px; }}
  h2 {{ font-size: 1rem; margin: 24px 0 8px; }}
  button {{ background: {NAVY}; color: {CREAM}; border: 1px solid {BLUE};
            padding: 8px 10px; margin-right: 8px; font: inherit;
            display: inline-flex; align-items: center; gap: 8px; }}
  .svg-icon {{ height: 1em; width: 1em; flex-shrink: 0; fill: currentColor; }}
  .visually-hidden {{ position: absolute; height: 1px; width: 1px;
                      overflow: hidden; clip: rect(0, 0, 0, 0);
                      white-space: nowrap; }}
  .big {{ font-size: 2rem; }}
  .small {{ font-size: 0.75rem; }}
  .sizes {{ display: flex; align-items: flex-end; gap: 16px; margin: 12px 0; }}
  .sizes.light {{ color: {NAVY}; background: {CREAM}; padding: 12px; }}
</style>
</head>
<body>
  <h1>Иконки Omnistash и Stashroom</h1>
  <p>Спрайт ниже тот же <code>&lt;symbol&gt;</code>, что в
     <code>assets/icons/sprite.svg</code>: геометрия одного источника,
     подпись и скрытие решаются на месте <code>&lt;use&gt;</code>.</p>
{sprite(names)}
{"".join(rows)}
</body>
</html>
"""


def render_all(names: list[str]) -> dict[str, str]:
    """Все ассеты одним вызовом - детерминированный вывод."""
    files = {"sprite.svg": sprite(names), "demo.html": demo(names)}
    for name in names:
        geo = GEOMETRY[name]
        files[f"{name}.svg"] = standalone(name, geo, 24)
        files[f"{name}-16.svg"] = standalone(name, GEOMETRY_16[name], 16)
        files[f"{name}-tile.svg"] = tile_master(name, geo)
    return files


def main(argv: list[str]) -> int:
    names = list(GEOMETRY)
    geometry_problems = verify_geometry()
    contrast_problems, table = verify_contrast()
    read_problems = verify_16_readability()
    problems = geometry_problems + contrast_problems + read_problems
    if problems:
        print("отказ сверки:", file=sys.stderr)
        for line in problems:
            print("  ", line, file=sys.stderr)
        return 1

    files = render_all(names)
    check = "--check" in argv
    if check:
        stale = []
        for name, text in files.items():
            path = OUT / name
            if not path.is_file():
                stale.append(f"{name}: не построен")
                continue
            if path.read_text(encoding="utf-8") != text:
                stale.append(f"{name}: ассет не совпадает с генерацией")
        if stale:
            print("отказ --check:", file=sys.stderr)
            for line in stale:
                print("  ", line, file=sys.stderr)
            return 1
        print(f"ассеты совпадают с генерацией: файлов {len(files)}")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (OUT / name).write_text(text, encoding="utf-8")
    print(f"записано файлов: {len(files)} в {OUT}")
    for label, pair, value, verdict in table:
        print(f"  {label} {pair}: {value:.2f} ({verdict})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
