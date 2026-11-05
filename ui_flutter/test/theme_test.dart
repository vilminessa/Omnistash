import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omnistash_ui/theme.dart';

/// сверка токенов темы на стороне Flutter: числа и контрасты считаются по
/// перенесённой формуле WCAG из app/themes.py, а не «на глаз».
void main() {
  test('контрастные пары выдержаны для каждой темы реестра', () {
    for (final id in themeIds()) {
      expect(contrastFailures(id), isEmpty,
          reason: 'тема $id: контраст ниже порога из themes.CONTRAST_PAIRS');
    }
  });

  test('подложка окна - роль bg, не seed-цвет', () {
    for (final id in themeIds()) {
      final theme = buildTheme(id);
      expect(theme.scaffoldBackgroundColor, Color(role(id, 'bg')),
          reason: 'тема $id: scaffoldBackgroundColor не из роли bg');
      expect(theme.dividerColor, Color(role(id, 'line')),
          reason: 'тема $id: разделитель не из роли line');
      expect(theme.colorScheme.outline, Color(role(id, 'line-strong')),
          reason: 'тема $id: граница контрола не из роли line-strong');
    }
  });

  test('яркость темы совпадает с флагом реестра', () {
    for (final id in themeIds()) {
      final expected = kThemeDark[id] == true ? Brightness.dark : Brightness.light;
      expect(buildTheme(id).brightness, expected,
          reason: 'тема $id: brightness не совпадает с dark из реестра тем');
    }
  });

  test('битое значение настройки возвращает дефолтную тему', () {
    expect(normalizeThemeId('coolors-plus'), kDefaultThemeId,
        reason: 'битое значение не вернуло дефолтный id');
    expect(normalizeThemeId(null), kDefaultThemeId,
        reason: 'пустое значение не вернуло дефолтный id');
    expect(normalizeThemeId(''), kDefaultThemeId,
        reason: 'пустая строка не вернула дефолтный id');
    expect(buildTheme('coolors-plus').scaffoldBackgroundColor,
        buildTheme(kDefaultThemeId).scaffoldBackgroundColor,
        reason: 'битое значение не вернуло дефолтную палитру: окно «серый экран»');
  });

  test('высота строки списка растёт с текстом и не мельчает', () {
    // Тот же расчёт, что app/themes.py: row_height и --row-h в CSS.
    expect(rowHeight(kFontBounds.sizeDefault), kFontBounds.rowMin,
        reason: 'при дефолтном размере текста строка крупнее нижней границы');
    expect(rowHeight(kFontBounds.sizeMax),
        kFontBounds.sizeMax * kFontBounds.rowFactor,
        reason: 'при максимальном размере строка не равна размеру текста * множителю');
    expect(rowHeight(kFontBounds.sizeMin), kFontBounds.rowMin,
        reason: 'на мелком тексте строка обязана держать нижнюю границу');
  });

  test('типографика берёт размеры из метрик, а не литералы', () {
    final theme = buildTheme(kDefaultThemeId);
    expect(theme.textTheme.bodyMedium?.fontSize, kMetrics.fsBody,
        reason: 'bodyMedium не равен --fs-body из :root');
    expect(theme.textTheme.headlineSmall?.fontSize, kMetrics.fsHead,
        reason: 'headlineSmall не равен --fs-head из :root');
    expect(theme.textTheme.bodyMedium?.height, kMetrics.lhBody,
        reason: 'высота строки текста не равна --lh-body');
    expect(theme.textTheme.labelSmall?.letterSpacing,
        kMetrics.track * kMetrics.fsCap,
        reason: 'трекинг подписи не равен --track, умноженному на размер текста');
    expect(theme.textTheme.bodyMedium?.color,
        Color(role(kDefaultThemeId, 'text')),
        reason: 'подпись не из роли text');
  });

  test('радиусы контролов и карточек из токенов радиуса', () {
    final theme = buildTheme(kDefaultThemeId);
    final card = theme.cardTheme.shape! as RoundedRectangleBorder;
    expect(card.borderRadius, BorderRadius.circular(kMetrics.r3),
        reason: 'карточка не держит --r-3 из :root');
    final border = theme.inputDecorationTheme.border as OutlineInputBorder;
    expect(border.borderRadius, BorderRadius.circular(kMetrics.r2),
        reason: 'контрол не держит --r-2 из :root');
    final focused = theme.inputDecorationTheme.focusedBorder! as OutlineInputBorder;
    expect(focused.borderSide.color, Color(role(kDefaultThemeId, 'focus')),
        reason: 'кольцо фокуса не из роли focus');
    expect(theme.buttonTheme.minWidth, kMetrics.target,
        reason: 'минимальная цель кнопки не равна --target');
    expect(theme.buttonTheme.height, kMetrics.ctlH,
        reason: 'высота контрола не равна --ctl-h');
  });

  test('бренд-тема светлая: причина названа числами, а не выбором на глаз', () {
    expect(kThemeDark['coolors'], false,
        reason: 'тема «Бренд» объявлена тёмной: на navy подложке red не читается');
    expect(contrastRatio(role('coolors', 'accent'), role('coolors', 'bg')),
        greaterThanOrEqualTo(3.0),
        reason: 'red на cream не держит порог значимой графики (SC 1.4.11)');
    expect(contrastRatio(role('coolors', 'accent'), role('dark', 'bg')),
        lessThan(3.0),
        reason: 'red на navy выдерживает 3.0 - причина светлой темы названа неверно');
    expect(themeLabel('coolors'), kThemeLabels['coolors'],
        reason: 'подпись темы не совпадает с реестром');
  });
}
