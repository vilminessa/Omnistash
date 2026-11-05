r"""Токены темы для Flutter: вывод из app/themes.py и ui_src/app.css.

    python tools/build_flutter_theme.py           # пишет ui_flutter/lib/theme_tokens.dart
    python tools/build_flutter_theme.py --check   # сверяет ассет с генерацией

Один источник чисел: роли цвета берутся из реестра тем (app/themes.py -
themes.ids(), themes.labels(), themes.css_vars()), метрики - из блока :root в
ui_src/app.css (docs/DESIGN-UI.md §2: шаг отступов, радиусы, цели и контролы,
типографика, веяния), границы шрифта и формула высоты строки - из констант
app/themes.py (FONT_SIZE_*, FONT_SCALE_*, ROW_MIN, ROW_FACTOR), пары контраста -
из themes.CONTRAST_PAIRS. Dart не читает Python в рантайме, поэтому вывод
кладётся ассетом, а mirror-страж tools/check_flutter_mirror.py сверяет его с
реестром и с CSS на каждый прогон набора проверок (GitHub-раннер Flutter не
гоняет, но этот страж работает без тулчейна).

Что НЕ переносится этим шагом и названо причиной:
  * строки box-shadow и кольцо фокуса (--shadow, --shadow-1/2, --veil,
    --focus-ring) - Flutter рисует elevation и фокус-кольцо своими объектами,
    перенос «строка-в-строку» невозможен; роли для них выводятся цветами, а
    форма тени ложится на волне W3;
  * шрифт: Raven-сабсеты (Roboto - Apache-2.0, Instrument Sans / Bricolage
    Grotesque / JetBrains Mono - SIL OFL 1.1) пинятся по sha256 и кладутся в
    бандл только после аудита лицензии (docs/LEGAL-REVIEW.md §5 «Отложить»);
    до аудита Dart держит системный стек - скачивания в рантайме нет;
  * стек-семейство (--mono, --font-family) и --nums: это не числа, в Dart им
    соответствует TextStyle (mono-фонт и tabular figures), ложится вместе со
    шрифтом.

Ассет детерминирован (без случайностей и без дат): повторный запуск даёт
байт-в-байт тот же файл, поэтому --check сверяет ассет с генерацией по образцу
tools/make_icon.py и tools/build_icon_svgs.py, а отказ называет расхождение.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import themes  # noqa: E402 - путь добавлен выше

OUT = ROOT / "ui_flutter" / "lib" / "theme_tokens.dart"
CSS = ROOT / "ui_src" / "app.css"

# Метрики из :root, которые являются числом (или тройкой cubic-bezier). Имя в
# Dart - то же, что CSS-переменная, без дефисов: --s-3 -> s3.
METRIC_VARS = (
    "--s-1", "--s-2", "--s-3", "--s-4", "--s-5", "--s-6", "--s-7", "--s-8",
    "--r-1", "--r-2", "--r-3", "--r-4", "--r-full",
    "--ctl-h", "--ctl-h-lg", "--target", "--target-min", "--gap-cta",
    "--tile-min",
    "--fs-cap", "--fs-sm", "--fs-body", "--fs-lg", "--fs-title", "--fs-head",
    "--lh-tight", "--lh-body", "--track", "--font-size",
    "--dur-fast-01", "--dur-fast-02", "--dur-moderate-01", "--dur-moderate-02",
    "--dur-slow-01", "--dur-slow-02",
    "--ease-productive", "--ease-expressive", "--ease-exit",
)

HEX = re.compile(r"^#([0-9A-Fa-f]{6})$")
Px = re.compile(r"^([\d.]+)px$")
Ms = re.compile(r"^([\d.]+)ms$")
Em = re.compile(r"^([\d.]+)em$")
Factor = re.compile(r"^([\d.]+)$")
Bezier = re.compile(r"^cubic-bezier\(\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\s*\)$")


def dart_name(css_name: str) -> str:
    """--dur-fast-01 -> durFast01, --r-full -> rFull."""
    parts = css_name.lstrip("-").split("-")
    return parts[0] + "".join(part[0].upper() + part[1:] for part in parts[1:])


def css_root_vars() -> dict[str, str]:
    """Значения блока :root (без комментариев)."""
    clean = re.sub(r"/\s*/\*.*?\*/\s*", "", CSS.read_text(encoding="utf-8"),
                   flags=re.S)
    match = re.search(r":root\s*\{([^}]*)\}", clean, flags=re.S)
    if not match:
        raise SystemExit("отказ: в ui_src/app.css нет блока :root - метрики "
                         "DESIGN-UI §2 потеряны")
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", match.group(1)))


def metric_values() -> dict[str, str]:
    """Числовые метрики :root в форме Dart-значений (имя -> строка литерала)."""
    vars_ = css_root_vars()
    out: dict[str, str] = {}
    for name in METRIC_VARS:
        value = vars_.get(name)
        if value is None:
            raise SystemExit(f"отказ: метрика {name} не объявлена в :root")
        value = value.strip()
        match = Px.match(value)
        if match:
            out[dart_name(name)] = _number(match.group(1))
            continue
        match = Ms.match(value)
        if match:
            out[dart_name(name)] = _number(match.group(1))
            continue
        match = Em.match(value)
        if match:
            out[dart_name(name)] = _number(match.group(1))
            continue
        match = Factor.match(value)
        if match:
            out[dart_name(name)] = _number(match.group(1))
            continue
        match = Bezier.match(value)
        if match:
            out[dart_name(name)] = ("Cubic(%s, %s, %s, %s)"
                                    % tuple(_number(part) for part in match.groups()))
            continue
        raise SystemExit(f"отказ: {name} = {value!r} - не px, не ms, не em, не "
                         f"множитель и не cubic-bezier: метрика не переносится "
                         f"числом")
    return out


def _number(text: str) -> str:
    """Дарт-литерал числа: 11.5 остаётся 11.5, 2 -> 2.0 (тип double)."""
    value = float(text)
    if value == int(value) and "." not in text:
        return f"{int(value)}.0"
    return f"{value:g}"


def theme_blocks() -> list[str]:
    """Записи реестра тем: id, подпись, тёмная/светлая, hex-роли."""
    lines = ["const Map<String, Map<String, int>> kThemeColors =\n"
             "    <String, Map<String, int>>{"]
    for item in themes.themes():
        vars_ = themes.css_vars(item["id"])
        roles = []
        for name in themes.PALETTE_VARS:
            value = vars_[name]
            match = HEX.match(value)
            if not match:
                raise SystemExit(f"отказ: тема {item['id']} {name} = "
                                 f"{value!r} - не hex: вывод прерван")
            roles.append(f"      '{name.lstrip('-')}': "
                         f"0xff{match.group(1).lower()}")
        lines.append(f"  '{item['id']}': <String, int>{{\n"
                     + ",\n".join(roles) + "\n  },")
    lines.append("};")
    return ["\n".join(lines)]


def meta_lines() -> list[str]:
    ids = ", ".join(f"'{item}'" for item in themes.ids())
    labels = ", ".join(f"'{item['id']}': '{item['label']}'" for item in themes.themes())
    darks = ", ".join(f"'{item['id']}': {'true' if item['dark'] else 'false'}"
                      for item in themes.themes())
    pairs = ",\n".join(
        f"    ContrastPair('{fg.lstrip('-')}', '{bg.lstrip('-')}', {_number(str(floor))})"
        for fg, bg, floor in themes.CONTRAST_PAIRS)
    return [
        f"const List<String> kThemeIds = <String>[{ids}];",
        f"const String kDefaultThemeId = '{themes.DEFAULT_THEME}';",
        f"const Map<String, String> kThemeLabels = <String, String>{{{labels}}};",
        f"const Map<String, bool> kThemeDark = <String, bool>{{{darks}}};",
        ("const List<ContrastPair> kContrastPairs = <ContrastPair>[\n" + pairs + "\n];"),
        ("const FontBounds kFontBounds = FontBounds(\n"
         f"    sizeMin: {float(themes.FONT_SIZE_MIN):g},\n"
         f"    sizeMax: {float(themes.FONT_SIZE_MAX):g},\n"
         f"    sizeDefault: {float(themes.FONT_SIZE_DEFAULT):g},\n"
         f"    scaleMin: {float(themes.FONT_SCALE_MIN):g},\n"
         f"    scaleMax: {float(themes.FONT_SCALE_MAX):g},\n"
         f"    scaleDefault: {float(themes.FONT_SCALE_DEFAULT):g},\n"
         f"    rowMin: {float(themes.ROW_MIN):g},\n"
         f"    rowFactor: {float(themes.ROW_FACTOR):g},\n);"),
    ]


HEADER = r"""// Сгенерировано tools/build_flutter_theme.py из app/themes.py и ui_src/app.css.
// Не править руками: tools/build_flutter_theme.py --check сверяет этот файл с
// генерацией байт-в-байт, а tools/check_flutter_mirror.py сверяет его с
// реестром тем, с :root в app.css и с контрастными парами themes.py.
//
// Роли цвета - те же имена, что CSS-переменные темы (themes.PALETTE_VARS),
// метрики - те же числа, что блок :root (docs/DESIGN-UI.md §2), границы
// шрифта и формула высоты строки - константы app/themes.py, пары контраста -
// themes.CONTRAST_PAIRS. Формула WCAG перенесена из app/themes.py
// (luminance/contrast_ratio): один и тот же расчёт на обеих сторонах клиента.
//
// Строки box-shadow и кольцо фокуса (--shadow, --shadow-1/2, --veil,
// --focus-ring) здесь нет намеренно: Flutter рисует elevation и фокус своими
// объектами, «строка-в-строку» не переносится, форма тени ложится на волне W3.
// Шрифт Raven-сабсетами не переносится до аудита лицензии (docs/LEGAL-REVIEW.md
// §5 «Отложить»): Dart держит системный стек, рантайм ничего не скачивает.
//
// Единицы метрик: px переносится в логические пикселы Flutter (dp), ms - в
// Duration, значения без единицы (--lh-tight, --lh-body) - в множитель высоты
// строки TextStyle, --track (.02em) - в множитель трекинга: Flutter считает
// letterSpacing в пикселах, поэтому theme.dart умножает его на размер текста.

import 'dart:math' as math;

import 'package:flutter/material.dart';

/// Пара подписи и подложки с порогом контраста - из themes.CONTRAST_PAIRS.
class ContrastPair {
  const ContrastPair(this.fg, this.bg, this.floor);
  final String fg;
  final String bg;
  final double floor;
}

/// Границы шрифта и формула высоты строки - из констант app/themes.py.
class FontBounds {
  const FontBounds({
    required this.sizeMin,
    required this.sizeMax,
    required this.sizeDefault,
    required this.scaleMin,
    required this.scaleMax,
    required this.scaleDefault,
    required this.rowMin,
    required this.rowFactor,
  });
  final double sizeMin;
  final double sizeMax;
  final double sizeDefault;
  final double scaleMin;
  final double scaleMax;
  final double scaleDefault;
  final double rowMin;
  final double rowFactor;
}

/// Метрики окна - числа из блока :root в ui_src/app.css (DESIGN-UI §2).
class Metrics {
  const Metrics({
    required this.s1,
    required this.s2,
    required this.s3,
    required this.s4,
    required this.s5,
    required this.s6,
    required this.s7,
    required this.s8,
    required this.r1,
    required this.r2,
    required this.r3,
    required this.r4,
    required this.rFull,
    required this.ctlH,
    required this.ctlHLg,
    required this.target,
    required this.targetMin,
    required this.gapCta,
    required this.tileMin,
    required this.fsCap,
    required this.fsSm,
    required this.fsBody,
    required this.fsLg,
    required this.fsTitle,
    required this.fsHead,
    required this.lhTight,
    required this.lhBody,
    required this.track,
    required this.fontSize,
    required this.durFast01,
    required this.durFast02,
    required this.durModerate01,
    required this.durModerate02,
    required this.durSlow01,
    required this.durSlow02,
    required this.easeProductive,
    required this.easeExpressive,
    required this.easeExit,
  });
"""

METRIC_FIELDS_TAIL = r"""  final double s1;
  final double s2;
  final double s3;
  final double s4;
  final double s5;
  final double s6;
  final double s7;
  final double s8;
  final double r1;
  final double r2;
  final double r3;
  final double r4;
  final double rFull;
  final double ctlH;
  final double ctlHLg;
  final double target;
  final double targetMin;
  final double gapCta;
  final double tileMin;
  final double fsCap;
  final double fsSm;
  final double fsBody;
  final double fsLg;
  final double fsTitle;
  final double fsHead;
  final double lhTight;
  final double lhBody;
  final double track;
  final double fontSize;
  final double durFast01;
  final double durFast02;
  final double durModerate01;
  final double durModerate02;
  final double durSlow01;
  final double durSlow02;
  final Cubic easeProductive;
  final Cubic easeExpressive;
  final Cubic easeExit;
}

/// Метрики окна: те же числа, что :root в ui_src/app.css.
const Metrics kMetrics = Metrics(
"""


def metrics_literal(metrics: dict[str, str]) -> str:
    lines = [f"  {name}: {value}," for name, value in metrics.items()]
    return "\n".join(lines) + "\n);"


WCAG = r"""/// Относительная яркость по WCAG 2.x - перенос app/themes.py: luminance.
double luminance(int hex) {
  final red = ((hex >> 16) & 0xff) / 255.0;
  final green = ((hex >> 8) & 0xff) / 255.0;
  final blue = (hex & 0xff) / 255.0;
  double channel(double unit) =>
      unit <= 0.03928 ? unit / 12.92 : math.pow((unit + 0.055) / 1.055, 2.4).toDouble();
  return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue);
}

/// Контраст пары (1..21) - перенос app/themes.py: contrast_ratio.
double contrastRatio(int a, int b) {
  final first = luminance(a);
  final second = luminance(b);
  final hi = first > second ? first : second;
  final lo = first > second ? second : first;
  return (hi + 0.05) / (lo + 0.05);
}

/// Роль темы по имени CSS-переменной ('bg', 'line-strong'). Отказ называет
/// тему и имя: молчания нет, как в app/themes.py: vars_problems.
int role(String themeId, String name) {
  final colors = kThemeColors[themeId];
  if (colors == null) {
    throw ArgumentError('тема $themeId не в списке kThemeIds: палитра потеряна');
  }
  final value = colors[name];
  if (value == null) {
    throw ArgumentError('тема $themeId: роль $name не выведена - часть окна '
        'останется без цвета');
  }
  return value;
}

/// Тема по значению настройки: битое значение -> дефолт, как themes.normalize.
String normalizeThemeId(String? value) {
  final id = value ?? '';
  return kThemeIds.contains(id) ? id : kDefaultThemeId;
}

/// Высота строки списка: max(rowMin, fontSize * rowFactor) - тот же расчёт,
/// что app/themes.py: row_height и --row-h в CSS.
double rowHeight(double fontSize) {
  final grown = fontSize * kFontBounds.rowFactor;
  return grown > kFontBounds.rowMin ? grown : kFontBounds.rowMin;
}
"""


def render() -> str:
    """Вывод ассета токенов: классы, метрики, списки тем, роли и перенос WCAG."""
    metrics = metric_values()
    blocks = [
        HEADER.rstrip(),
        METRIC_FIELDS_TAIL.rstrip(),
        metrics_literal(metrics).rstrip(),
        "\n".join(meta_lines()),
        "\n".join(theme_blocks()),
        WCAG.rstrip(),
    ]
    # Одна пустая строка между блоками и перевод в конце файла: ассет сверяется
    # байт-в-байт, поэтому форматирование здесь фиксировано, а не «по форматтеру».
    return "\n\n".join(blocks) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        if not OUT.is_file():
            print(f"отказ --check: {OUT} не построен", file=sys.stderr)
            return 1
        if OUT.read_text(encoding="utf-8") != text:
            print(f"отказ --check: {OUT} не совпадает с генерацией "
                  "tools/build_flutter_theme.py", file=sys.stderr)
            return 1
        print("токены Flutter совпадают с генерацией из app/themes.py и "
              "ui_src/app.css")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"записано: {OUT} ({len(text)} символов, тем {len(themes.ids())}, "
          f"метрик {len(metric_values())}, контрастных пар "
          f"{len(themes.CONTRAST_PAIRS)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
