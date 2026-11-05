// Сгенерировано tools/build_flutter_theme.py из app/themes.py и ui_src/app.css.
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

  final double s1;
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

  s1: 2.0,
  s2: 4.0,
  s3: 8.0,
  s4: 12.0,
  s5: 16.0,
  s6: 24.0,
  s7: 32.0,
  s8: 48.0,
  r1: 4.0,
  r2: 6.0,
  r3: 8.0,
  r4: 12.0,
  rFull: 999.0,
  ctlH: 32.0,
  ctlHLg: 40.0,
  target: 44.0,
  targetMin: 24.0,
  gapCta: 8.0,
  tileMin: 240.0,
  fsCap: 11.5,
  fsSm: 12.5,
  fsBody: 13.0,
  fsLg: 14.0,
  fsTitle: 15.0,
  fsHead: 17.0,
  lhTight: 1.25,
  lhBody: 1.45,
  track: 0.02,
  fontSize: 13.0,
  durFast01: 70.0,
  durFast02: 110.0,
  durModerate01: 150.0,
  durModerate02: 240.0,
  durSlow01: 400.0,
  durSlow02: 700.0,
  easeProductive: Cubic(0.2, 0.0, 0.38, 0.9),
  easeExpressive: Cubic(0.4, 0.14, 0.3, 1.0),
  easeExit: Cubic(0.4, 0.14, 1.0, 1.0),
);

const List<String> kThemeIds = <String>['dark', 'light', 'coolors', 'contrast', 'custom'];
const String kDefaultThemeId = 'dark';
const Map<String, String> kThemeLabels = <String, String>{'dark': 'Тёмная', 'light': 'Светлая', 'coolors': 'Бренд', 'contrast': 'Контрастная', 'custom': 'Своя'};
const Map<String, bool> kThemeDark = <String, bool>{'dark': true, 'light': false, 'coolors': false, 'contrast': true, 'custom': true};
const List<ContrastPair> kContrastPairs = <ContrastPair>[
    ContrastPair('text', 'bg', 4.5),
    ContrastPair('text', 'surface', 4.5),
    ContrastPair('text', 'widget', 4.5),
    ContrastPair('text', 'surface-2', 4.5),
    ContrastPair('text', 'hover', 4.5),
    ContrastPair('text', 'selected', 4.5),
    ContrastPair('text', 'accent-dim', 4.5),
    ContrastPair('muted', 'bg', 3),
    ContrastPair('muted', 'surface-2', 3),
    ContrastPair('muted', 'widget', 3),
    ContrastPair('muted', 'selected', 3),
    ContrastPair('muted', 'accent-dim', 3),
    ContrastPair('accent', 'bg', 3),
    ContrastPair('accent', 'surface', 3),
    ContrastPair('accent-2', 'bg', 3),
    ContrastPair('accent-2', 'surface', 3),
    ContrastPair('on-accent', 'accent', 4.5),
    ContrastPair('on-scrim', 'scrim', 4.5),
    ContrastPair('on-warn', 'warn', 4.5),
    ContrastPair('focus', 'surface', 3),
    ContrastPair('focus', 'widget', 3),
    ContrastPair('line-strong', 'surface', 3),
    ContrastPair('line-strong', 'widget', 3),
    ContrastPair('line-strong', 'bg', 3),
    ContrastPair('warn', 'bg', 3),
    ContrastPair('warn', 'surface', 3),
    ContrastPair('ok', 'bg', 3),
    ContrastPair('ok', 'surface', 3),
    ContrastPair('err', 'bg', 3),
    ContrastPair('err', 'surface', 3),
    ContrastPair('info', 'bg', 3),
    ContrastPair('info', 'surface', 3),
    ContrastPair('log-text', 'log-bg', 4.5)
];
const FontBounds kFontBounds = FontBounds(
    sizeMin: 10,
    sizeMax: 22,
    sizeDefault: 13,
    scaleMin: 100,
    scaleMax: 200,
    scaleDefault: 100,
    rowMin: 34,
    rowFactor: 2.6,
);

const Map<String, Map<String, int>> kThemeColors =
    <String, Map<String, int>>{
  'dark': <String, int>{
      'bg': 0xff0c1622,
      'surface': 0xff111d2c,
      'surface-2': 0xff16263a,
      'widget': 0xff1b2f47,
      'hover': 0xff16263a,
      'selected': 0xff1b2f47,
      'line': 0xff45617f,
      'line-strong': 0xff5b799b,
      'text': 0xffdcdedd,
      'muted': 0xff96a3b0,
      'on-accent': 0xff04140d,
      'on-scrim': 0xffdcdedd,
      'on-warn': 0xff241500,
      'accent': 0xff628d7c,
      'accent-2': 0xff7fb3a0,
      'accent-dim': 0xff33506c,
      'focus': 0xffffc766,
      'warn': 0xffffb454,
      'ok': 0xff46c079,
      'err': 0xffea6a63,
      'info': 0xff6bb6e8,
      'ok-line': 0xff2f6b4a,
      'warn-line': 0xff7a5321,
      'err-line': 0xff7c332e,
      'info-line': 0xff2b5a7d,
      'log-bg': 0xff070d15,
      'log-text': 0xffa8b6c4,
      'scrim': 0xff040910
  },
  'light': <String, int>{
      'bg': 0xffeef1f5,
      'surface': 0xffffffff,
      'surface-2': 0xfff6f8fa,
      'widget': 0xffe6ebf1,
      'hover': 0xfff6f8fa,
      'selected': 0xffd8e2ea,
      'line': 0xff899fb5,
      'line-strong': 0xff6f869d,
      'text': 0xff1c2430,
      'muted': 0xff576472,
      'on-accent': 0xffffffff,
      'on-scrim': 0xffffffff,
      'on-warn': 0xffffffff,
      'accent': 0xff2f7d67,
      'accent-2': 0xff24634f,
      'accent-dim': 0xffc7ded6,
      'focus': 0xff8a5a00,
      'warn': 0xff8a5a00,
      'ok': 0xff0f6b3d,
      'err': 0xffb02e24,
      'info': 0xff155a94,
      'ok-line': 0xff9fc7b3,
      'warn-line': 0xffd3b585,
      'err-line': 0xffd3a09a,
      'info-line': 0xff9dc0dd,
      'log-bg': 0xff141b24,
      'log-text': 0xffc9d4de,
      'scrim': 0xff040910
  },
  'coolors': <String, int>{
      'bg': 0xfffdf0d5,
      'surface': 0xfffef6e6,
      'surface-2': 0xffe2e1d0,
      'widget': 0xffd0d6ce,
      'hover': 0xffe2e1d0,
      'selected': 0xffd0d6ce,
      'line': 0xffbec0b2,
      'line-strong': 0xff497d9c,
      'text': 0xff003049,
      'muted': 0xff3f7290,
      'on-accent': 0xfffdf0d5,
      'on-scrim': 0xfffdf0d5,
      'on-warn': 0xfffdf0d5,
      'accent': 0xffc1121f,
      'accent-2': 0xffa70c14,
      'accent-dim': 0xffc5c6b6,
      'focus': 0xff911a2a,
      'warn': 0xff780000,
      'ok': 0xff2e607d,
      'err': 0xffa40b13,
      'info': 0xff477b9a,
      'ok-line': 0xff76929c,
      'warn-line': 0xffa7544b,
      'err-line': 0xffc35b57,
      'info-line': 0xff87a4af,
      'log-bg': 0xff2a1f2f,
      'log-text': 0xfffdf0d5,
      'scrim': 0xff003049
  },
  'contrast': <String, int>{
      'bg': 0xff05070a,
      'surface': 0xff0b1017,
      'surface-2': 0xff121a24,
      'widget': 0xff1b2632,
      'hover': 0xff121a24,
      'selected': 0xff24364a,
      'line': 0xff5c7690,
      'line-strong': 0xff7f9ab5,
      'text': 0xfff6f8fa,
      'muted': 0xffb6c2cd,
      'on-accent': 0xff0a0603,
      'on-scrim': 0xffffffff,
      'on-warn': 0xff241500,
      'accent': 0xffffd166,
      'accent-2': 0xffffe3a3,
      'accent-dim': 0xff2b2318,
      'focus': 0xffcfe6ff,
      'warn': 0xffff9f43,
      'ok': 0xff2fd9a0,
      'err': 0xffff6b6b,
      'info': 0xff6cb8ff,
      'ok-line': 0xff4fae7f,
      'warn-line': 0xffc08a3e,
      'err-line': 0xffd07070,
      'info-line': 0xff5f9fd6,
      'log-bg': 0xff010305,
      'log-text': 0xffc8d6e2,
      'scrim': 0xff000000
  },
  'custom': <String, int>{
      'bg': 0xff14100c,
      'surface': 0xff1c1712,
      'surface-2': 0xff241d16,
      'widget': 0xff2c241c,
      'hover': 0xff241d16,
      'selected': 0xff3a2f24,
      'line': 0xff5d4a36,
      'line-strong': 0xff8d745c,
      'text': 0xffece6dc,
      'muted': 0xffb4a692,
      'on-accent': 0xff14100c,
      'on-scrim': 0xfff6efe4,
      'on-warn': 0xff241500,
      'accent': 0xffc9a24b,
      'accent-2': 0xffd9b872,
      'accent-dim': 0xff4a3a22,
      'focus': 0xfff0b35c,
      'warn': 0xffd99a4a,
      'ok': 0xff35b07a,
      'err': 0xffd4573f,
      'info': 0xff6aa9d8,
      'ok-line': 0xff2e6b52,
      'warn-line': 0xff74522b,
      'err-line': 0xff7c3a2b,
      'info-line': 0xff2b5a7d,
      'log-bg': 0xff0b0806,
      'log-text': 0xffc8bda9,
      'scrim': 0xff0a0705
  },
};

/// Относительная яркость по WCAG 2.x - перенос app/themes.py: luminance.
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
