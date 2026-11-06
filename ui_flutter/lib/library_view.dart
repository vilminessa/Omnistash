/// Экран библиотеки (W2, шаг 1): список строк из живого фасада `--serve`.
///
/// Данные идут тем же контрактом, что браузерное окно: `POST /api/list_videos`
/// с телом `[аргумент]`, ответ - `{"total": N, "rows": [...]}`
/// (app/repo.py: list_videos -> {"total","rows"}; `tools/check_flutter_mirror.py`
/// сверяет, что Dart читает именно `rows`, а не выдуманный ключ). Цвета и
/// размеры читаются из токенов темы (theme.dart), литералов здесь нет; строка
/// списка растёт с размером текста формулой `rowHeight` - тот же расчёт, что
/// `--row-h` в CSS и `app/themes.py: row_height`.
///
/// Семантика строк - эквиваленты `aria` из docs/DESIGN-UI.md §3.1: строка
/// названа целиком (имя, статус, канал, длительность, рейтинг), выбор
/// передаётся как `selected`, строка доступна с клавиатуры (ListTile с
/// `onTap` фокусируется и активируется Enter/Space), полоса состояния -
/// живый регион (`liveRegion`) как `aria-live` у журнала. Сортировка
/// колонок построена на шаге W2 (ниже): порядок строк приходит от сервера
/// через аргумент `order` - тот же, что `ensureRange` в ui_src/app.js, а
/// при пустой колонке сервер держит `uploaded_at DESC`, как браузерный
/// клиент с `order: null`. Табы - `tabs.dart` (шаг 2).

library;

import 'dart:async';

import 'package:flutter/material.dart';

import 'api_client.dart';
import 'theme.dart';

/// Форма ответа списка строк - один источник правды: app/repo.py: list_videos.
const String kRowsKey = 'rows';
const String kTotalKey = 'total';

/// Страница списка: сколько строк фасада берётся за один вызов - тот же
/// размер, что `PAGE` в ui_src/app.js:35 (сверку ведёт
/// tools/check_flutter_mirror.py).
const int kPageLimit = 120;

/// Период опроса состояния: тот же `POLL_MS` из ui_src/app.js:36 (500 мс),
/// сверяется tools/check_flutter_mirror.py - молча не «по памяти клиента».
const int kPollIntervalMs = 500;

/// Сортируемые колонки списка - тот же порядок, подписи, ключи и гейты,
/// что `LIST_COLS` в ui_src/app.js:3170 (колонка `check` без sortable в
/// сверке исключена - она не сортируется, это названо в страже). Запись:
/// подпись, ключ `ORDER_COLS` из app/repo.py (белый список сервера), имя
/// настройки-гейта или '' без гейта. Именованная форма записи `({...})` -
/// позиционная с именами полей в этом SDK не компилируется (названо прогоном
/// flutter analyze). Сверку порядка, ключей и гейтов ведёт
/// tools/check_flutter_mirror.py против ui_src/app.js и app/repo.py.
const List<({String label, String col, String gate})> kSortableColumns =
    <({String label, String col, String gate})>[
  (label: 'Статус', col: 'status', gate: ''),
  (label: 'Тип', col: 'media_kind', gate: ''),
  (label: 'Название', col: 'title', gate: ''),
  (label: 'Канал', col: 'channel', gate: ''),
  (label: 'Время', col: 'duration', gate: ''),
  (label: 'Дата', col: 'date', gate: ''),
  (label: 'Размер', col: 'size', gate: ''),
  (label: 'Просмотры', col: 'views', gate: 'col_views'),
  (label: 'Лайки', col: 'likes', gate: 'col_likes'),
  (label: 'Комменты', col: 'comments', gate: 'col_comments'),
  (label: 'Теги', col: 'tag', gate: 'col_tags'),
  (label: 'Синк', col: 'sync', gate: 'col_sync'),
];

/// Роль цвета статуса строки - тот же источник, что `.status[data-s=...]`
/// в ui_src/app.css (downloaded -> ok, queued -> info, downloading -> warn,
/// failed -> err, missing -> warn; known и unavailable -> muted). Сверку
/// карты с CSS ведёт tools/check_flutter_mirror.py.
const Map<String, String> kStatusRoles = <String, String>{
  'known': 'muted',
  'queued': 'info',
  'downloading': 'warn',
  'downloaded': 'ok',
  'failed': 'err',
  'missing': 'warn',
  'unavailable': 'muted',
};

/// Статус без подписи реестра (app/repo.py STATUS_LABELS) читается текстом
/// статуса как есть - молчания нет: строка называет, что не распознано.
String statusLabelOf(Map<String, Object?> row) {
  final label = row['status_label'];
  if (label is String && label.isNotEmpty) return label;
  final status = row['status'];
  if (status is String && status.isNotEmpty) return 'статус: $status';
  return 'статус не назван';
}

String roleOfStatus(String? status) => kStatusRoles[status ?? ''] ?? 'muted';

/// Длительность в минуты:секунды - та же форма, что колонка списка окна.
String durationOf(Object? seconds) {
  final total = seconds is num ? seconds.toInt() : 0;
  if (total <= 0) return '—';
  final minutes = total ~/ 60;
  final rest = total % 60;
  return '$minutes:${rest.toString().padLeft(2, '0')}';
}

/// Строка состояния (полоса под шапкой): журнал дельтой от `poll`, последняя
/// запись; пустой журнал называется, а не прячется.
class LibraryHome extends StatefulWidget {
  const LibraryHome({
    super.key,
    required this.facade,
    this.settings = const <String, Object?>{},
  });

  final Facade facade;

  /// Снимок настроек из `get_initial`: гейты колонок (`col_*` - колонка
  /// скрыта только при значении `false`, как `colVisible` в ui_src/app.js)
  /// и сохранённый порядок (`sort_col`, `sort_dir`).
  final Map<String, Object?> settings;

  @override
  State<LibraryHome> createState() => _LibraryHomeState();
}

class _LibraryHomeState extends State<LibraryHome> {
  List<Map<String, Object?>> _rows = const <Map<String, Object?>>[];
  int _total = 0;
  int? _selectedId;
  String? _error;
  String? _persistError;
  String _journal = 'журнал пуст';
  bool _loading = true;
  Timer? _poll;

  /// Порядок сортировки: тот же тогл, что `toggleSort` в ui_src/app.js:3256 -
  /// та же колонка разворачивает направление, новая колонка начинает с
  /// `desc`; при пустой колонке сервер держит default order (uploaded_at
  /// DESC) - как браузерный клиент с `order: null`.
  String _sortCol = '';
  String _sortDir = 'desc';

  @override
  void initState() {
    super.initState();
    final col = widget.settings['sort_col'];
    _sortCol = col is String ? col : '';
    _sortDir = widget.settings['sort_dir'] == 'asc' ? 'asc' : 'desc';
    _loadFirst();
    _poll = Timer.periodic(Duration(milliseconds: kPollIntervalMs), (_) => _pollJournal());
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  /// Настройки -> состояние списка, как `syncSortAndCols` в
  /// ui_src/app.js:3273: снимок из `get_initial` приходит асинхронно, и
  /// сохранённый порядок применяется к первому запросу перезагрузкой
  /// страниц (страницы под новым порядком не имеют смысла).
  @override
  void didUpdateWidget(LibraryHome oldWidget) {
    super.didUpdateWidget(oldWidget);
    final col = widget.settings['sort_col'];
    final newCol = col is String ? col : '';
    final newDir = widget.settings['sort_dir'] == 'asc' ? 'asc' : 'desc';
    if (newCol != _sortCol || newDir != _sortDir) {
      setState(() {
        _sortCol = newCol;
        _sortDir = newDir;
      });
      _loadFirst();
    }
  }

  /// Аргумент `order` для `list_videos` - та же форма, что `ensureRange` в
  /// ui_src/app.js:3308: ключ присутствует всегда, `null` при пустой
  /// колонке (сервер тогда держит `uploaded_at DESC NULLS LAST`).
  Map<String, Object?>? _orderArg() => _sortCol.isEmpty
      ? null
      : <String, Object?>{'col': _sortCol, 'dir': _sortDir};

  Future<void> _loadFirst() async {
    setState(() => _loading = true);
    try {
      final res = await widget.facade.call('list_videos', <String, Object?>{
        'offset': 0, 'limit': kPageLimit, 'order': _orderArg(),
      });
      final refusal = _refusalOf(res);
      if (refusal != null) {
        setState(() {
          _error = refusal;
          _loading = false;
        });
        return;
      }
      setState(() {
        _rows = _rowsOf(res);
        _total = res[kTotalKey] is num ? (res[kTotalKey] as num).toInt() : _rows.length;
        _error = null;
        _loading = false;
      });
    } catch (error) {
      setState(() {
        _error = 'список не загружен: $error';
        _loading = false;
      });
    }
  }

  Future<void> _loadMore() async {
    final offset = _rows.length;
    if (offset >= _total) return;
    try {
      final res = await widget.facade.call('list_videos', <String, Object?>{
        'offset': offset, 'limit': kPageLimit, 'order': _orderArg(),
      });
      final refusal = _refusalOf(res);
      if (refusal != null) {
        setState(() => _error = refusal);
        return;
      }
      setState(() {
        _rows = <Map<String, Object?>>[..._rows, ..._rowsOf(res)];
        _error = null;
      });
    } catch (error) {
      setState(() => _error = 'следующая страница не загружена: $error');
    }
  }

  /// Тогл сортировки - поведение `toggleSort` из ui_src/app.js:3256: та же
  /// колонка разворачивает направление, новая - начинает с `desc`. Страницы
  /// под новым порядком не имеют смысла, поэтому первая страница
  /// перезагружается; выбор живёт в настройках и переживает перезапуск -
  /// `save_setting` `sort_col`/`sort_dir` тем же ключом, что браузер.
  Future<void> _toggleSort(String col) async {
    final same = _sortCol == col;
    final dir = (same && _sortDir == 'desc') ? 'asc' : 'desc';
    setState(() {
      _sortCol = col;
      _sortDir = dir;
    });
    await _loadFirst();
    await _persistSort(col, dir);
  }

  /// Сохранение порядка. Читателю ключ `save_setting` сервер отказывает
  /// (403 «не выдан скоуп server_config» или отказ внутри 200): локальный
  /// порядок остаётся применённым к запросу, а отказ называется текстом -
  /// молчания нет.
  Future<void> _persistSort(String col, String dir) async {
    final refusals = <String>[];
    for (final entry in <Map<String, Object?>>[
      <String, Object?>{'key': 'sort_col', 'value': col},
      <String, Object?>{'key': 'sort_dir', 'value': dir},
    ]) {
      try {
        final res = await widget.facade.call('save_setting', entry);
        final refusal = _refusalOf(res);
        if (refusal != null) refusals.add(refusal);
      } catch (error) {
        refusals.add('$error');
      }
    }
    setState(() => _persistError = refusals.isEmpty
        ? null
        : 'порядок применён к списку, но не сохранён в настройках: '
            '${refusals.join('; ')}');
  }

  Future<void> _pollJournal() async {
    try {
      final res = await widget.facade.call('poll', <String, Object?>{'since': 0});
      final logs = (res['logs'] as List?)?.cast<String>() ?? const <String>[];
      setState(() => _journal = logs.isEmpty ? 'журнал пуст' : logs.last);
    } catch (error) {
      setState(() => _journal = 'журнал не прочитан: $error');
    }
  }

  /// Отказ фасада приходит внутри ответа 200 с полем `error` (гейт 18+,
  /// «ничего не выбрано», битый запрос): называем его текстом, а не молчим.
  String? _refusalOf(Map<String, Object?> res) {
    final error = res['error'];
    return error is String && error.isNotEmpty ? error : null;
  }

  List<Map<String, Object?>> _rowsOf(Map<String, Object?> res) {
    final rows = res[kRowsKey] as List?;
    return rows == null
        ? const <Map<String, Object?>>[]
        : rows.cast<Map<String, Object?>>();
  }

  String _rowSemantics(Map<String, Object?> row, int index) {
    final title = row['title'] is String ? row['title'] as String : 'строка $index';
    final channel = row['channel'] is String ? row['channel'] as String : 'без канала';
    final parts = <String>[
      title,
      statusLabelOf(row),
      channel,
      durationOf(row['duration_s']),
      if (row['media_label'] is String) row['media_label'] as String,
      if (row['user_rating'] is num && (row['user_rating'] as num) > 0)
        'рейтинг ${(row['user_rating'] as num).toInt()}',
      if (row['has_thumb'] == true) 'обложка есть',
      if (row['has_thumb'] == false) 'нет обложки',
    ];
    return parts.join(', ');
  }

  Widget _buildRow(BuildContext context, int index) {
    final theme = Theme.of(context);
    final row = _rows[index];
    final id = row['id'] is num ? (row['id'] as num).toInt() : index;
    final selected = _selectedId == id;
    final role = roleOfStatus(row['status'] is String ? row['status'] as String : null);
    final color = roleColor(themeIdOf(context), role);
    return Semantics(
      // Эквивалент aria из DESIGN-UI §3.1: строка названа целиком, выбор
      // передаётся отдельным свойством, строка доступна с клавиатуры.
      label: _rowSemantics(row, index),
      selected: selected,
      button: true,
      child: ListTile(
        // Фокус и активация с клавиатуры - ListTile с onTap: Tab ходит по
        // строкам, Enter/Space выбирает строку (возврат фокуса - шаг с
        // диалогами на W2).
        onTap: () => setState(() => _selectedId = selected ? null : id),
        selected: selected,
        leading: Semantics(
          // Маркер статуса: 7 px в CSS лежит вне токен-шкалы, здесь
          // ближайший шаг шкалы (s3) - названо расхождением, форма маркера
          // переносится вместе с веяниями на волне W3.
          excludeSemantics: true,
          child: Container(
            width: kMetrics.s3,
            height: kMetrics.s3,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: color,
              border: Border.all(color: color),
            ),
          ),
        ),
        title: Text(row['title'] is String ? row['title'] as String : 'строка $index',
            style: theme.textTheme.bodyMedium,
            maxLines: 1,
            overflow: TextOverflow.ellipsis),
        subtitle: Text(
          '${row['channel'] is String ? row['channel'] as String : 'без канала'} · '
          '${durationOf(row['duration_s'])}',
          style: theme.textTheme.bodySmall,
          maxLines: 1,
          overflow: TextOverflow.ellipsis),
        trailing: Text(statusLabelOf(row), style: theme.textTheme.labelSmall?.copyWith(color: color)),
      ),
    );
  }

  /// Строка сортируемых колонок - эквивалент `renderGridHead` из
  /// ui_src/app.js:3230: `role="columnheader"` + `aria-sort` на ячейке
  /// заголовка. Отдельного флага `aria-sort` в семантике Flutter нет
  /// (`SemanticsSortKey` задаёт только порядок обхода), поэтому направление
  /// передаётся текстом подписи со стрелкой ▲/▼ - названо расхождением.
  /// Скрытые гейтом `col_*` колонки не рисуются - тот же `colVisible`.
  Widget _buildSortHead(BuildContext context) {
    final theme = Theme.of(context);
    final visible = kSortableColumns.where(
        (c) => c.gate.isEmpty || widget.settings[c.gate] != false);
    return Padding(
      padding: EdgeInsets.symmetric(horizontal: kMetrics.s4),
      child: Wrap(
        spacing: kMetrics.s2,
        runSpacing: kMetrics.s2,
        children: <Widget>[
          for (final c in visible)
            TextButton(
              // Клавиатура: TextButton фокусируется и активируется
              // Enter/Space - тот же доступ, что `tabindex="0"` у ячейки.
              onPressed: () => _toggleSort(c.col),
              child: Semantics(
                // Эквивалент role="columnheader" + aria-sort из
                // renderGridHead (ui_src/app.js:3230): флаг header внутри
                // child сливается с узлом кнопки в один узел (проверено
                // widget-тестом: [isButton, isHeader] на узле с подписью;
                // обёртка снаружи давала бы отдельный узел без кнопки).
                // Отдельного флага aria-sort в семантике Flutter нет
                // (SemanticsSortKey задаёт только порядок обхода), поэтому
                // направление передаётся текстом подписи со стрелкой ▲/▼ -
                // названо расхождением.
                header: true,
                child: Text(
                  '${c.label}${_sortCol == c.col ? (_sortDir == 'asc' ? ' ▲' : ' ▼') : ''}',
                  style: theme.textTheme.bodySmall?.copyWith(
                      fontWeight: _sortCol == c.col ? FontWeight.w600 : null),
                ),
              ),
            ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final more = _rows.length < _total;
    return Scaffold(
      backgroundColor: theme.colorScheme.surfaceContainerLowest,
      body: Column(
        children: [
          Padding(
            padding: EdgeInsets.symmetric(horizontal: kMetrics.s4, vertical: kMetrics.s3),
            child: Row(
              children: [
                Text('Библиотека', style: theme.textTheme.headlineSmall),
                const Spacer(),
                // Цифры табличные (эквивалент --nums из :root): счётчик не
                // «пляшет» при смене порядка и при выборе строк.
                Semantics(
                  label: 'строк в индексе: $_total',
                  child: Text('${_rows.length} из $_total',
                      style: theme.textTheme.bodySmall?.copyWith(
                          fontFeatures: const <FontFeature>[
                            FontFeature.tabularFigures(),
                          ])),
                ),
              ],
            ),
          ),
          _buildSortHead(context),
          if (_error != null)
            Padding(
              padding: EdgeInsets.symmetric(horizontal: kMetrics.s4, vertical: kMetrics.s2),
              child: Text('Отказ: $_error',
                  style: theme.textTheme.bodySmall?.copyWith(
                      color: theme.colorScheme.error)),
            ),
          if (_persistError != null)
            Padding(
              padding: EdgeInsets.symmetric(horizontal: kMetrics.s4, vertical: kMetrics.s2),
              child: Text(_persistError!,
                  style: theme.textTheme.bodySmall?.copyWith(
                      color: theme.colorScheme.error)),
            ),
          Expanded(
            child: _loading
                ? Center(child: Text('загрузка списка…', style: theme.textTheme.bodySmall))
                : ListView.builder(
                    itemCount: _rows.length + (more ? 1 : 0),
                    itemExtent: rowHeight(fontPxOf(context)),
                    itemBuilder: (context, index) {
                      if (index >= _rows.length) {
                        return Semantics(
                          label: 'загрузить ещё строки: осталось $_total из '
                              '${_total - _rows.length}',
                          child: FilledButton(
                            onPressed: _loadMore,
                            child: Text('Ещё (${_total - _rows.length})'),
                          ),
                        );
                      }
                      return _buildRow(context, index);
                    },
                  ),
          ),
          // Живый регион: эквивалент aria-live у панели журнала - статус
          // меняется без фокуса и должен быть объявлен AT. Регион отдельным
          // узлом (`container`), а подпись внутри исключена из семантики,
          // иначе AT передаёт одно и то же дважды.
          Padding(
            padding: EdgeInsets.symmetric(horizontal: kMetrics.s4, vertical: kMetrics.s3),
            child: Semantics(
              liveRegion: true,
              container: true,
              label: 'состояние: $_journal',
              child: Semantics(
                excludeSemantics: true,
                child: Text(_journal, style: theme.textTheme.bodySmall),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Тема и размер текста текущего экрана приходят из настройки (get_initial),
/// а не из литералов; границы чтения - kFontBounds из app/themes.py.
