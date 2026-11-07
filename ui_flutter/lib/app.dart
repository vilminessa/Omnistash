/// OmnistashApp (W2): Flutter-окно поверх живого фасада `--serve`.
///
/// Тема и размер текста читаются из настройки бэкенда (`get_initial` ->
/// `settings.theme`, `settings.font_size`, app/gui.py: get_initial), поэтому
/// браузерное окно и Flutter-окно красятся одной выбранной палитрой - выбор
/// человека «две темы, выбор в настройках» (docs/FLUTTER-TRANSITION.md §W1).
/// Снимок настроек целиком передаётся вкладкам: состав колонок (гейты
/// `col_*`) и сохранённый порядок (`sort_col`, `sort_dir`) читаются из него,
/// как `syncSortAndCols` в ui_src/app.js:3273. Битое значение настройки
/// возвращается дефолтом (theme.dart: normalizeThemeId и границы
/// kFontBounds), молчания нет: отказ называет текст сервера.
///
/// Если фасад не передан (адрес, ключ и пин не в окружении), экран называет
/// причину и не показывает пустой список: `--serve` не поднят, а не «данных
/// нет».

library;

import 'package:flutter/material.dart';

import 'api_client.dart';
import 'player.dart';
import 'tabs.dart';
import 'theme.dart';

class OmnistashApp extends StatefulWidget {
  const OmnistashApp({super.key, required this.facade, this.opener});

  /// Реальный транспорт к `--serve` или подмена в тесте (Facade).
  final Facade? facade;

  /// Открытие адресов карточки (страница плеера, площадка). По умолчанию -
  /// системный браузер; тест подменяет его, чтобы прогон не запускал cmd.
  final UrlOpener? opener;

  @override
  State<OmnistashApp> createState() => _OmnistashAppState();
}

class _OmnistashAppState extends State<OmnistashApp> {
  String _themeId = kDefaultThemeId;
  double? _fontSizePx;
  String? _error;
  Map<String, Object?> _settings = const <String, Object?>{};

  @override
  void initState() {
    super.initState();
    if (widget.facade != null) _loadSettings();
  }

  /// Новый фасад (тест подменяет транспорт, в production-окне фасад не
  /// меняется): снимок настроек перечитывается - он обязан соответствовать
  /// тому бэкенду, к которому ходит экран.
  @override
  void didUpdateWidget(OmnistashApp oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.facade != oldWidget.facade) _loadSettings();
  }

  Future<void> _loadSettings() async {
    final facade = widget.facade;
    if (facade == null) return;
    try {
      final res = await facade.call('get_initial', null);
      final refusal = res['error'];
      if (refusal is String && refusal.isNotEmpty) {
        setState(() => _error = refusal);
        return;
      }
      final settings = res['settings'];
      final theme = settings is Map ? settings['theme'] : null;
      final size = settings is Map ? settings['font_size'] : null;
      setState(() {
        _themeId = normalizeThemeId(theme is String ? theme : null);
        _fontSizePx = size is num ? size.toDouble() : null;
        _settings = settings is Map<String, Object?>
            ? settings
            : const <String, Object?>{};
        _error = null;
      });
    } catch (error) {
      setState(() => _error = 'настройки не прочитаны: $error');
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = buildTheme(_themeId, fontSizePx: _fontSizePx);
    final facade = widget.facade;
    return MaterialApp(
      title: 'Omnistash',
      debugShowCheckedModeBanner: false,
      theme: theme,
      home: facade == null
          ? _NoServerHome(themeId: _themeId, error: _error)
          : OmnistashTabs(
              facade: facade,
              settings: _settings,
              // Карточка строки (W2, шаг 3) открывает адрес: системный
              // браузер поверх того же `--serve`, новых зависимостей нет.
              opener: widget.opener ?? const BrowserOpener(),
            ),
    );
  }
}

/// Честный экран без слушателя: называет, чего не хватает, а не молчит.
class _NoServerHome extends StatelessWidget {
  const _NoServerHome({required this.themeId, this.error});

  final String themeId;
  final String? error;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final text = error == null
        ? 'Сервер не запущен: `--serve` не поднят, адрес, ключ и пин не '
            'переданы в окружении (OMNISTASH_API_URL, OMNISTASH_API_TOKEN, '
            'OMNISTASH_API_CERT_SHA256). Данные библиотеки не показаны - это '
            'отсутствие сервера, а не пустая библиотека.'
        : 'Сервер отвечает отказом: $error';
    return Scaffold(
      backgroundColor: theme.colorScheme.surfaceContainerLowest,
      body: Center(
        child: Semantics(
          liveRegion: true,
          label: text,
          child: Padding(
            padding: EdgeInsets.all(kMetrics.s5),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('Omnistash', style: theme.textTheme.headlineSmall),
                SizedBox(height: kMetrics.s4),
                Text(text, style: theme.textTheme.bodyMedium),
                SizedBox(height: kMetrics.s3),
                Text('тема: ${themeLabel(themeId)}',
                    style: theme.textTheme.bodySmall),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
