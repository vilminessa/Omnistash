/// Табы окна (W2, шаг 2): Библиотека + честные заглушки остальных экранов.
///
/// Порядок и подписи - тот же `TAB_RU` в ui_src/app.js:5739 (library, sync,
/// queue, settings). `platform` из того словаря - динамические вкладки
/// площадок (app.js:5955), не часть основной панели; сверку порядка ведёт
/// `tools/check_flutter_mirror.py`, который читает и Dart, и JS.
///
/// Заглушки называют, какой экран не построен и на какой волне он ложится,
/// а не показывают пустой контент: «журнал пуст» или «очередь пуста» без
/// сервера было бы ложью, и здесь их нет. Библиотека построена на шаге W2,
/// поэтому её заглушки нет - она получает `LibraryHome` поверх фасада.
///
/// Семантика: `TabBar` сам строит tab-роли (каждая вкладка - `Semantics`
/// с `selected` и `button`, активная читается тестом через flagsCollection),
/// `TabBarView` - панели: эквиваленты `aria` tablist/tab/tabpanel из
/// `ui_src/app.js` (§3.1 DESIGN-UI). Отдельного флага для «не построен»
/// в семантике нет - текст панели называет это словами.

library;

import 'package:flutter/material.dart';

import 'api_client.dart';
import 'library_view.dart';
import 'player.dart';
import 'theme.dart';

/// Порядок подписей табов - тот же, что значения `TAB_RU` в ui_src/app.js
/// (без динамических вкладок площадок). Сверку ведёт
/// tools/check_flutter_mirror.py.
const List<String> kTabLabels = <String>[
  'Библиотека', 'Синхронизация', 'Очередь', 'Настройки',
];

/// Честные заглушки: каждая называет, какой экран не построен, на какой
/// волне ложится и что должно в него войти. Первая вкладка (Библиотека)
/// построена на W2 - для неё заглушки нет.
const Map<String, String> kTabStubs = <String, String>{
  'Синхронизация':
      'Экран синхронизации не построен: волна W3 (участники, mesh, SAS и '
      'юр-гейты AUP/adult/first_send). Журнал запусков здесь не показан - '
      'это не построенный экран, а не пустой журнал.',
  'Очередь':
      'Экран очереди не построен: волна W2 (очередь загрузки и журнал как '
      'панели). Строк очереди здесь нет - это не построенный экран, а не '
      'пустая очередь.',
  'Настройки':
      'Экран настроек не построен: волна W3 (зеркало схемы настроек в Dart '
      'и страж по образцу tests/test_ui_settings_mirror.py). Тема и размер '
      'текста уже применяются из get_initial, остальное - не построено.',
};

/// Таб-панель окна поверх фасада: первая вкладка - библиотека (W2 шаг 1),
/// остальные - заглушки, называющие волну.
class OmnistashTabs extends StatefulWidget {
  const OmnistashTabs({
    super.key,
    required this.facade,
    this.settings = const <String, Object?>{},
    this.opener,
  });

  final Facade facade;

  /// Снимок настроек из `get_initial`: состав колонок (гейты `col_*`) и
  /// порядок сортировки (`sort_col`, `sort_dir`) читаются из него.
  final Map<String, Object?> settings;

  /// Открытие адресов (страница плеера, площадка) - системный браузер по
  /// умолчанию; тест подменяет его и сверяет URL.
  final UrlOpener? opener;

  @override
  State<OmnistashTabs> createState() => _OmnistashTabsState();
}

class _OmnistashTabsState extends State<OmnistashTabs>
    with SingleTickerProviderStateMixin {
  late final TabController _tabs;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: kTabLabels.length, vsync: this);
  }

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final panels = <Widget>[
      for (final label in kTabLabels)
        label == kTabLabels.first
            ? LibraryHome(
                facade: widget.facade,
                settings: widget.settings,
                opener: widget.opener,
              )
            : StubPanel(label: label, text: kTabStubs[label]!),
    ];
    return Scaffold(
      backgroundColor: theme.colorScheme.surfaceContainerLowest,
      body: Column(
        children: [
          TabBar(
            controller: _tabs,
            tabs: <Widget>[
              for (final label in kTabLabels) Tab(text: label),
            ],
          ),
          Expanded(
            child: TabBarView(controller: _tabs, children: panels),
          ),
        ],
      ),
    );
  }
}

/// Панель unbuilt экрана: текст называет причину и волну, а не молчит.
/// Живым регионом не помечается: содержимое панели не меняется само.
class StubPanel extends StatelessWidget {
  const StubPanel({super.key, required this.label, required this.text});

  final String label;
  final String text;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SingleChildScrollView(
      child: Padding(
        padding: EdgeInsets.all(kMetrics.s5),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(label, style: theme.textTheme.headlineSmall),
            SizedBox(height: kMetrics.s4),
            Text(text, style: theme.textTheme.bodyMedium),
            SizedBox(height: kMetrics.s3),
            Text('данные этого экрана не показаны: экран не построен',
                style: theme.textTheme.bodySmall),
          ],
        ),
      ),
    );
  }
}
