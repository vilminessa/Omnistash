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
/// живый регион (`live`) как `aria-live` у журнала. Табы, сортировка колонок
/// и `aria-sort` - следующий шаг W2: здесь порядок строк приходит от сервера
/// (`v.uploaded_at DESC, v.id DESC`), а не из клиента.

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
  const LibraryHome({super.key, required this.facade});

  final Facade facade;

  @override
  State<LibraryHome> createState() => _LibraryHomeState();
}

class _LibraryHomeState extends State<LibraryHome> {
  List<Map<String, Object?>> _rows = const <Map<String, Object?>>[];
  int _total = 0;
  int? _selectedId;
  String? _error;
  String _journal = 'журнал пуст';
  bool _loading = true;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    _loadFirst();
    _poll = Timer.periodic(Duration(milliseconds: kPollIntervalMs), (_) => _pollJournal());
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _loadFirst() async {
    setState(() => _loading = true);
    try {
      final res = await widget.facade.call('list_videos',
          <String, Object?>{'offset': 0, 'limit': kPageLimit});
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
      final res = await widget.facade.call('list_videos',
          <String, Object?>{'offset': offset, 'limit': kPageLimit});
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
          if (_error != null)
            Padding(
              padding: EdgeInsets.symmetric(horizontal: kMetrics.s4, vertical: kMetrics.s2),
              child: Text('Отказ: $_error',
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
