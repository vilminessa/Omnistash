/// ThemeData Omnistash из токенов темы (theme_tokens.dart).
///
/// Числа и цвета приходят из генерации tools/build_flutter_theme.py, а не
/// литералами: roles - имена CSS-переменных темы, метрики - числа из :root
/// (docs/DESIGN-UI.md §2). tools/check_flutter_mirror.py сверяет, что здесь нет
/// ни одного hex-литерала и ни одного размера/длительности, написанных числом, и
/// что имена ролей существуют в реестре app/themes.py.
///
/// Соответствие ролей на Material названо явно (Material не «угадывает» роль):
///   surface                 <- --surface      подложка карточек и секций
///   surfaceContainerLowest  <- --bg           подложка окна
///   surfaceContainerLow     <- --surface-2    шапка колонок, плотные блоки
///   surfaceContainerHighest <- --widget       контролы и поля ввода
///   onSurface               <- --text         подпись
///   onSurfaceVariant        <- --muted        приглушённая подпись (AA по паре)
///   outline                 <- --line-strong  граница контрола (SC 1.4.11)
///   outlineVariant          <- --line         декоративный разделитель
///   primary / onPrimary     <- --accent / --on-accent
///   primaryContainer        <- --accent-dim   disabled primary-подложка
///   secondary               <- --info         цвет informational-статуса
///   error / onError         <- --err / --text
///   scrim                   <- --scrim        затемнение под диалогом (значки)
///   veil (не роль схемы)    <- --veil         подложка под диалогом: альфа и
///     цвет перенесены числом (шаг W2 3), форма барьера - объект Flutter
///   shadow                  <- --scrim        тон elevation: Flutter строит
///     тень своим объектом, строка box-shadow из CSS не переносится (волна W3)
///   focusRing               <- --focus/--bg   кольцо фокуса строки: две
///     границы вместо двух box-shadow (форма названа расхождением, волна W3
///     для теней контролов)
///
/// Ролей «on-ok», «on-warn», «on-info» в токен-системе нет и это названо
/// причиной, а не молчанием: в окне состояние несёт текст и подпись, а не
/// подложка с подписью (docs/DESIGN-UI.md §2.2), поэтому onError/onSecondary
/// берут --text. --nums, --mono и --font-family - не числа: их ложит шрифт
/// Raven-сабсетами после аудита лицензии (docs/LEGAL-REVIEW.md §5 «Отложить»),
/// до аудита Flutter использует системный стек и не качает ничего в рантайме.

library;

import 'package:flutter/material.dart';

import 'theme_tokens.dart';

// Токены доступны потребителям темы через этот модуль: экраны читают роли и
// метрики из theme.dart, а не импортируют сгенерированный файл напрямую.
export 'theme_tokens.dart';

/// Роль темы как Color по имени CSS-переменной ('bg', 'line-strong').
/// Отказ называет тему и имя - как app/themes.py: vars_problems.
Color roleColor(String themeId, String name) => Color(role(themeId, name));

/// Подложка под диалогом - --veil темы (альфа и цвет из реестра, шаг W2 3).
/// Flutter рисует барьер своим объектом: форма «строка rgba-в-строку» не
/// переносится, переносится цвет и альфа, и mirror-страж сверяет каждую тему.
Color veilColor(String themeId) => Color(kThemeVeils[normalizeThemeId(themeId)]!);

/// Кольцо фокуса: зазор к подложке (--bg) и само кольцо (--focus), ширины из
/// --focus-ring. В CSS это box-shadow, здесь - две границы: форма названа
/// расхождением (docs/DESIGN-UI.md §2.4), цвета читаются из палитры темы.
({Color gap, Color ring, double gapWidth, double ringWidth}) focusRing(String themeId) {
  final id = normalizeThemeId(themeId);
  return (
    gap: roleColor(id, 'bg'),
    ring: roleColor(id, 'focus'),
    gapWidth: kMetrics.focusRingGap,
    ringWidth: kMetrics.focusRingFocus,
  );
}

/// Светлая ли тема (по флагу реестра themes.themes()).
bool themeIsDark(String themeId) => kThemeDark[normalizeThemeId(themeId)] ?? false;

/// Подпись темы из реестра (для карточки настроек и заголовка окна).
String themeLabel(String themeId) =>
    kThemeLabels[normalizeThemeId(themeId)] ?? kThemeLabels[kDefaultThemeId]!;

/// Список тем для настройки: тот же порядок, что themes.ids().
List<String> themeIds() => List<String>.from(kThemeIds);

/// Цветовая схема темы по ролям.
ColorScheme schemeFor(String themeId) {
  final id = normalizeThemeId(themeId);
  return ColorScheme(
    brightness: themeIsDark(id) ? Brightness.dark : Brightness.light,
    primary: roleColor(id, 'accent'),
    onPrimary: roleColor(id, 'on-accent'),
    primaryContainer: roleColor(id, 'accent-dim'),
    onPrimaryContainer: roleColor(id, 'text'),
    secondary: roleColor(id, 'info'),
    onSecondary: roleColor(id, 'text'),
    error: roleColor(id, 'err'),
    onError: roleColor(id, 'text'),
    surface: roleColor(id, 'surface'),
    onSurface: roleColor(id, 'text'),
    surfaceContainerLowest: roleColor(id, 'bg'),
    surfaceContainerLow: roleColor(id, 'surface-2'),
    surfaceContainerHighest: roleColor(id, 'widget'),
    onSurfaceVariant: roleColor(id, 'muted'),
    outline: roleColor(id, 'line-strong'),
    outlineVariant: roleColor(id, 'line'),
    scrim: roleColor(id, 'scrim'),
    shadow: roleColor(id, 'scrim'),
  );
}

/// Типографика: размеры из метрик (:root в app.css), высоты - множители
/// --lh-*, трекинг - множитель --track в пикселах (Flutter считает его
/// добавкой в пикселах, CSS - в em, поэтому умножение на размер текста).
TextStyle _type(String themeId, double size, double height, {double? spacing}) {
  return TextStyle(
    fontSize: size,
    height: height,
    color: roleColor(themeId, 'text'),
    letterSpacing: spacing == null ? null : spacing * size,
  );
}

TextTheme textThemeFor(String themeId) {
  final id = normalizeThemeId(themeId);
  final m = kMetrics;
  final muted = _type(id, m.fsSm, m.lhBody).copyWith(color: roleColor(id, 'muted'));
  final caption = _type(id, m.fsCap, m.lhTight)
      .copyWith(color: roleColor(id, 'muted'), letterSpacing: m.track * m.fsCap);
  return TextTheme(
    // Заголовок экрана (--fs-head) и заголовок секции/карточки (--fs-title):
    // обычный регистр, без ALL CAPS - русский в капсе теряет форму слова.
    headlineSmall: _type(id, m.fsHead, m.lhTight).copyWith(fontWeight: FontWeight.w600),
    titleLarge: _type(id, m.fsTitle, m.lhTight).copyWith(fontWeight: FontWeight.w600),
    titleMedium: _type(id, m.fsLg, m.lhTight).copyWith(fontWeight: FontWeight.w500),
    bodyLarge: _type(id, m.fsLg, m.lhBody),
    bodyMedium: _type(id, m.fsBody, m.lhBody),
    bodySmall: muted,
    labelLarge: _type(id, m.fsSm, m.lhTight).copyWith(fontWeight: FontWeight.w500),
    labelSmall: caption,
  );
}

/// Input-границы: OutlineInputBorder с --r-2 (как CSS для input/select/кнопок),
/// активная граница - --focus (2 px), покоящаяся - --line-strong (SC 1.4.11).
InputDecorationTheme decorationThemeFor(String themeId) {
  final id = normalizeThemeId(themeId);
  final resting = OutlineInputBorder(
    borderRadius: BorderRadius.circular(kMetrics.r2),
    borderSide: BorderSide(color: roleColor(id, 'line-strong')),
  );
  return InputDecorationTheme(
    filled: true,
    fillColor: roleColor(id, 'widget'),
    contentPadding: EdgeInsets.symmetric(
      horizontal: kMetrics.s4, vertical: kMetrics.s2),
    border: resting,
    enabledBorder: resting,
    focusedBorder: resting.copyWith(
      borderSide: BorderSide(color: roleColor(id, 'focus'), width: 2)),
    disabledBorder: resting.copyWith(
      borderSide: BorderSide(color: roleColor(id, 'line'))),
  );
}

/// Карточки и секции: --surface с границей --line и радиусом --r-3 (CSS держит
/// elevation строкой box-shadow; здесь elevation 0 - тень ложится на волне W3).
CardThemeData cardThemeFor(String themeId) {
  final id = normalizeThemeId(themeId);
  return CardThemeData(
    color: roleColor(id, 'surface'),
    elevation: 0,
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(kMetrics.r3),
      side: BorderSide(color: roleColor(id, 'line')),
    ),
  );
}

/// Кнопки: комфортная цель --target по минимальному размеру (WCAG 2.5.8
/// требует 24 px минимум, окно держит 44), compact-режим - --ctl-h.
ButtonThemeData buttonThemeFor(String themeId) {
  final id = normalizeThemeId(themeId);
  return ButtonThemeData(
    minWidth: kMetrics.target,
    height: kMetrics.ctlH,
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(kMetrics.r2)),
    colorScheme: schemeFor(id),
  );
}

/// Полоса хода и прогресс: подложка --widget, заполнение --accent.
ProgressIndicatorThemeData progressThemeFor(String themeId) {
  final id = normalizeThemeId(themeId);
  return ProgressIndicatorThemeData(
    color: roleColor(id, 'accent'),
    linearTrackColor: roleColor(id, 'widget'),
    circularTrackColor: roleColor(id, 'widget'),
  );
}

/// Веяния: длительности из --dur-* и кривые из --ease-* (DESIGN-UI §2.7).
/// Числа не дублируются: они читаются из kMetrics.
Duration durationOf(double milliseconds) => Duration(milliseconds: milliseconds.toInt());

Duration durationFast() => durationOf(kMetrics.durFast01);
Duration durationFade() => durationOf(kMetrics.durFast02);
Duration durationMove() => durationOf(kMetrics.durModerate01);
Duration durationToast() => durationOf(kMetrics.durModerate02);
Duration durationExpand() => durationOf(kMetrics.durSlow01);
Duration durationScrim() => durationOf(kMetrics.durSlow02);

/// Имя и размер текста текущей темы: Material не хранит id схемы, поэтому
/// клиент кладёт identity сам. Значения приходят из настройки (get_initial),
/// а не из литералов; границы чтения - kFontBounds (app/themes.py).
class ThemeIdentity extends ThemeExtension<ThemeIdentity> {
  const ThemeIdentity({required this.id, required this.fontSizePx});

  final String id;
  final double fontSizePx;

  @override
  ThemeIdentity copyWith({String? id, double? fontSizePx}) => ThemeIdentity(
      id: id ?? this.id, fontSizePx: fontSizePx ?? this.fontSizePx);

  @override
  ThemeIdentity lerp(ThemeIdentity? other, double t) {
    if (other == null) return this;
    return ThemeIdentity(
      id: t < 0.5 ? id : other.id,
      fontSizePx: fontSizePx + (other.fontSizePx - fontSizePx) * t,
    );
  }
}

/// id текущей темы из контекста (дефолт реестра, если identity не положен).
String themeIdOf(BuildContext context) =>
    Theme.of(context).extension<ThemeIdentity>()?.id ?? kDefaultThemeId;

/// Размер текста из настройки, уже в границах kFontBounds.
double fontPxOf(BuildContext context) =>
    Theme.of(context).extension<ThemeIdentity>()?.fontSizePx ?? kMetrics.fontSize;

/// Тема по значению настройки: битое значение -> дефолтная тема, как
/// app/themes.py: normalize (окно не «серый экран»). Размер текста
/// ограничивается границами реестра (FONT_SIZE_MIN..MAX) тем же правилом,
/// что на стороне бэкенда.
ThemeData buildTheme(String? themeId, {double? fontSizePx}) {
  final id = normalizeThemeId(themeId);
  final size = _clamped(fontSizePx ?? kMetrics.fontSize,
      kFontBounds.sizeMin, kFontBounds.sizeMax);
  final scheme = schemeFor(id);
  return ThemeData(
    useMaterial3: true,
    brightness: themeIsDark(id) ? Brightness.dark : Brightness.light,
    colorScheme: scheme,
    scaffoldBackgroundColor: roleColor(id, 'bg'),
    dividerColor: roleColor(id, 'line'),
    textTheme: textThemeFor(id).apply(bodyColor: roleColor(id, 'text')),
    cardTheme: cardThemeFor(id),
    inputDecorationTheme: decorationThemeFor(id),
    buttonTheme: buttonThemeFor(id),
    progressIndicatorTheme: progressThemeFor(id),
    splashFactory: NoSplash.splashFactory,
    extensions: <ThemeExtension<dynamic>>[
      ThemeIdentity(id: id, fontSizePx: size),
    ],
  );
}

/// Граница числа - тот же приём, что app/themes.py: _number (битое значение
/// возвращается дефолтом, а не падает).
double _clamped(double value, double min, double max) {
  if (value < min) return min;
  if (value > max) return max;
  return value;
}

/// Контраст роли к подложке по тем же парам, что бэкенд (themes.CONTRAST_PAIRS).
/// Считается перенесённой формулой WCAG, а не «на глаз».
List<String> contrastFailures(String themeId) {
  final id = normalizeThemeId(themeId);
  final problems = <String>[];
  for (final pair in kContrastPairs) {
    final ratio = contrastRatio(role(id, pair.fg), role(id, pair.bg));
    if (ratio < pair.floor) {
      problems.add('$id: ${pair.fg} на ${pair.bg} = ${ratio.toStringAsFixed(2)} '
          '< ${pair.floor}');
    }
  }
  return problems;
}
