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
/// названа целиком (имя, статус, канал, длительность, рейтинг,
/// «просмотрено»), выбор передаётся как `selected`, строка доступна с
/// клавиатуры (собственный `Focus` строки: обход по ↑↓ ведёт default
/// traversal, Enter открывает карточку, Ctrl+Enter открывает плеер, Space
/// переключает выбор - тот же канон, что `bindGridItems` в
/// ui_src/app.js:3400-3435 и строка клавиатуры в docs/DESIGN-UI.md §733).
/// Home/End (первая/последняя строка окна прокрутки) на этом шаге не
/// построены: названный разрыв, ложится на волну W3 вместе с roving-обходом
/// грид-ролей. Клик одинарный открывает карточку, двойной - плеер: Flutter
/// различает их жестом двойного нажатия (задержка по умолчанию 300 мс),
/// браузер держит 240 мс вручную (app.js:3410) - расхождение названо, а не
/// подменяется числом.
///
/// Активация карточки с AT (`Semantics(onTap:)`) повторяет одиночный клик:
/// карточка, не выбор; выбор - отдельное действие Space и чекбокс строки
/// (`row-check` канона гасит click строки - app.js:3408,3423).
///
/// Шаг 4: грид ячеек (`grid_cells.dart` - тот же состав колонок, что шапка:
/// `visibleCols`, гейты `col_*`, веса wide/narrow из канонных `LIST_COLS`),
/// мультивыбор (`Set<int>` - тот же `state.selected`, порядок вставки
/// канона `Array.from(Set)` в проводке bulk-целей), панель целей
/// (`SelectionBar` из `bulk_bar.dart`: «Выбрано: N» живой регион, селект
/// хранилища из `storages_list` под `read`, цели в канонном порядке),
/// диалоги «Пометить…» и «Операции…» (§3.8 через `dialog_trap.dart`: цели
/// `save_fields`/`bulk_*` под правами `mark`/`sources` подписываются до
/// вызова) и разрушительный диалог, называющий N (§3.7: галка подтверждения
/// до «Принять», вход - «Отклонить», отказ бэка без `confirm:true`
/// называется текстом сервера).
///
/// Сортировка колонок построена на шаге W2 (ниже): порядок строк приходит от
/// сервера через аргумент `order` - тот же, что `ensureRange` в ui_src/app.js, а
/// при пустой колонке сервер держит `uploaded_at DESC`, как браузерный
/// клиент с `order: null`. Табы - `tabs.dart` (шаг 2). Карточка строки -
/// `video_card.dart` (шаг 3), диалог §3.8 с ловушкой Tab на границе
/// focus-scope маршрута, Escape и возвратом фокуса на инициатора.

library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'access.dart';
import 'api_client.dart';
import 'bulk_bar.dart';
import 'grid_cells.dart';
import 'player.dart';
import 'theme.dart';
import 'video_card.dart';

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
    this.opener,
  });

  final Facade facade;

  /// Снимок настроек из `get_initial`: гейты колонок (`col_*` - колонка
  /// скрыта только при значении `false`, как `colVisible` в ui_src/app.js)
  /// и сохранённый порядок (`sort_col`, `sort_dir`).
  final Map<String, Object?> settings;

  /// Открытие адресов (страница плеера `/watch/<id>`, адрес площадки). По
  /// умолчанию - системный браузер; тест подменяет его и сверяет URL.
  final UrlOpener? opener;

  @override
  State<LibraryHome> createState() => _LibraryHomeState();
}

class _LibraryHomeState extends State<LibraryHome> {
  List<Map<String, Object?>> _rows = const <Map<String, Object?>>[];
  int _total = 0;
  /// Мультивыбор строк - тот же `state.selected` канона (`Set`, порядок
  /// вставки совпадает с `Array.from(state.selected)` в проводке целей -
  /// app.js:1467). Space на строке, чекбокс строки и галка «выбрать всё»
  /// ведут его вместе; выбор переживает перезагрузку страниц.
  final Set<int> _selected = <int>{};
  String? _error;
  String? _persistError;
  String _journal = 'журнал пуст';
  String? _notice;
  bool _loading = true;
  Timer? _poll;

  /// Права ключа (GET /whoami): цель «Открыть» подписывается до перехода,
  /// отсутствие `watch` называется текстом, а не молча уходит в 403.
  Access? _access;

  /// Хранилища для панели выделения: тот же `storages_list` под `read`,
  /// что карточка (app/gui.py); выбранное по умолчанию - тот же
  /// `defaultStorageId` канона (дефолт из ответа, иначе первое активное).
  List<Map<String, Object?>> _storages = const <Map<String, Object?>>[];
  String _storageId = '';

  /// Узлы фокуса целей панели выделения: возврат фокуса на инициатор
  /// диалога после pop (§3.8, dialogLeave в ui_src/app.js:4810).
  final Map<String, FocusNode> _barNodes = <String, FocusNode>{};

  // Один диалог карточки: повторное открытие называется, а не складывает
  // стек (стек диалогов с N вложенными карточками - волна W3).
  bool _cardOpen = false;
  int? _openRowId;

  /// Узлы фокуса строк: возврат фокуса на инициатора после закрытия
  /// карточки (§3.8, `dialogLeave` в ui_src/app.js). Узлы принадлежат
  /// состоянию и переживают перерисовку строки; узел исчезнувшей из окна
  /// прокрутки строки не получает фокус - это названо текстом.
  final Map<int, FocusNode> _rowNodes = <int, FocusNode>{};
  int? _hoverId;
  int? _focusedId;

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
    _loadAccess();
    _loadStorages();
    _poll = Timer.periodic(Duration(milliseconds: kPollIntervalMs), (_) => _pollJournal());
  }

  @override
  void dispose() {
    _poll?.cancel();
    for (final FocusNode node in _rowNodes.values) {
      node.dispose();
    }
    _rowNodes.clear();
    for (final FocusNode node in _barNodes.values) {
      node.dispose();
    }
    _barNodes.clear();
    super.dispose();
  }

  /// Права ключа: отказ читается текстом и остаётся в `refusal` - молчания
  /// нет, как в `fetchAccess` браузера (401 называет привязку).
  Future<void> _loadAccess() async {
    try {
      final Access access = await widget.facade.access();
      setState(() => _access = access);
    } catch (error) {
      setState(() => _access = Access(refusal: 'права не прочитаны: $error'));
    }
  }

  /// Хранилища панели «Скачать»: цель `storages_list` под `read` (та же,
  /// что карточка шага 3); отказ называется текстом и select остаётся с
  /// честной подписью канона («нет хранилищ - добавьте в настройках»).
  Future<void> _loadStorages() async {
    try {
      final Map<String, Object?> res = await widget.facade.call('storages_list', null);
      final String? refusal = _refusalOf(res);
      if (refusal != null) {
        setState(() => _notice = 'хранилища не прочитаны: $refusal');
        return;
      }
      final List<Map<String, Object?>> active = activeStorages(res);
      setState(() {
        _storages = active;
        _storageId = defaultStorageId(res, active);
      });
    } on ApiFailure catch (failure) {
      setState(() => _notice = 'хранилища не прочитаны: ${failure.reason}');
    } catch (error) {
      setState(() => _notice = 'хранилища не прочитаны: $error');
    }
  }

  /// Узел фокуса цели панели (создаётся один раз на подпись).
  FocusNode _barNode(String name) => _barNodes.putIfAbsent(name, () => FocusNode());

  /// Узел фокуса строки по её id (создаётся один раз на строку).
  FocusNode _rowNode(int id) => _rowNodes.putIfAbsent(id, () => FocusNode());

  /// Узлы строк, которых больше нет в списке: снимаются (кроме открытой
  /// карточки - её инициатор обязан дожить до закрытия).
  void _dropStaleNodes() {
    final live = _rows.map((row) => row['id'] is num
        ? (row['id'] as num).toInt()
        : null).toSet();
    for (final id in _rowNodes.keys.toList()) {
      if (live.contains(id) || id == _openRowId) continue;
      _rowNodes.remove(id)?.dispose();
    }
  }

  /// Вход в список с клавиатуры: первая строка получает фокус, как roving
  /// tabindex в `renderGridRows` (ui_src/app.js держит tabindex="0" на одной
  /// строке). Различие формы названо: в CSS обход строк ведёт ↑↓ и roving
  /// tabindex (Tab уходит из грида), здесь каждая строка в обходе Tab, а ↑↓
  /// работают default traversal-ом; грид ячеек построен на шаге 4, а roving
  /// обход и грид-роли (`role="gridcell"`) ложатся на волну W3.
  void _rovingFocus() {
    if (_cardOpen || _focusedId != null || _rows.isEmpty) return;
    final first = _rows.first;
    if (first['id'] is! num) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      // Узел первой строки стоит в focus-дереве только когда строка смонтирована
      // (ListView виртуализирует): `parent != null` - публичный признак,
      // `attached` в этом SDK нет (найдено flutter analyze).
      final node = _rowNodes[first['id'] as int];
      if (node != null && node.parent != null) node.requestFocus();
    });
  }

  /// Настройки -> состояние списка, как `syncSortAndCols` в
  /// ui_src/app.js:3273: снимок из `get_initial` приходит асинхронно, и
  /// сохранённый порядок применяется к первому запросу перезагрузкой
  /// страниц (страницы под новым порядком не имеют смысла).
  @override
  void didUpdateWidget(LibraryHome oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Новый фасад (тест подменяет транспорт; в production-окне фасад не
    // меняется): снимок настроек и права перечитываются - экран не должен
    // держать права и порядок от другого бэкенда.
    if (widget.facade != oldWidget.facade) {
      setState(() {
        _access = null;
        _notice = null;
        _selected.clear();
        _storages = const <Map<String, Object?>>[];
        _storageId = '';
      });
      _loadFirst();
      _loadAccess();
      _loadStorages();
      return;
    }
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
    try {      final res = await widget.facade.call('list_videos', <String, Object?>{
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
      _dropStaleNodes();
      _rovingFocus();
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
      _dropStaleNodes();
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
      // «просмотрено» - тот же badge канона (`watchBadge` app.js:3525),
      // знак строки: ячейка «✓» рисует его визуально, AT читает строку
      // одним узлом с этой подписью (§3.1, grid_cells.dart).
      if (watchedOf(row)) 'просмотрено',
      if (row['has_thumb'] == true) 'обложка есть',
      if (row['has_thumb'] == false) 'нет обложки',
    ];
    return parts.join(', ');
  }

  /// Состояние Ctrl: в этом SDK у `KeyEvent` нет `isControlPressed` и
  /// `modifiers` (найдено flutter analyze), поэтому модификатор ведётся
  /// локально по down/up событийам контрольных клавиш - тот же приём, что
  /// тесты: sendKeyDownEvent(controlLeft) … sendKeyUpEvent(controlLeft).
  bool _controlDown = false;

  /// Клавиатура строки - канон `bindGridItems` и docs/DESIGN-UI.md §733:
  /// Enter - карточка, Ctrl+Enter - плеер, Space - выбор. Стрелки и Tab
  /// возвращаются `ignored` и ведёт их default traversal по строкам
  /// (Home/End не построены - названный разрыв в шапке модуля).
  KeyEventResult _handleRowKey(Map<String, Object?> row, int id, KeyEvent event) {
    final key = event.logicalKey;
    if (key == LogicalKeyboardKey.controlLeft || key == LogicalKeyboardKey.controlRight) {
      _controlDown = event is KeyDownEvent;
      return KeyEventResult.ignored;
    }
    if (event is! KeyDownEvent) return KeyEventResult.ignored;
    if (key == LogicalKeyboardKey.space) {
      _toggleSelected(id);
      return KeyEventResult.handled;
    }
    if (key == LogicalKeyboardKey.enter || key == LogicalKeyboardKey.numpadEnter) {
      if (_controlDown) {
        _play(id);
      } else {
        _openCard(row, id, _rowNode(id));
      }
      return KeyEventResult.handled;
    }
    return KeyEventResult.ignored;
  }

  /// Space/чекбокс: выбор строки, карточку не открывает (тот же
  /// `stopPropagation` на `.row-check` в ui_src/app.js:3423). Выбор - Set
  /// канона `state.selected`: строка добавляется и снимается по id.
  void _toggleSelected(int id) {
    setState(() {
      if (_selected.contains(id)) {
        _selected.remove(id);
      } else {
        _selected.add(id);
      }
    });
  }

  /// Снять выбор (канон `clearSelection` app.js:7141-7147: галка «выбрать
  /// всё» снимается вместе со списком).
  void _clearSelection() {
    setState(() => _selected.clear());
  }

  /// Галка «выбрать всё» канона (`#sel-all` change app.js:7282-7289 ведёт
  /// все отрисованные строки окна прокрутки). Здесь - все загруженные
  /// строки: виртуальное окно браузера отмечает отрисованные, форма
  /// названа расхождением (roving грид-ролей ложится на W3).
  void _selectAll(bool on) {
    setState(() {
      // Все строки с числовым id; id 0 - честный id (фикстуры тестов и
      // первые строки), не «пустой»: прежний фильтр `where(id > 0)` терял
      // его (факт прогона: «выбрать всё» давало 2 из 3). Строки без
      // числового id пропускаются - их выбор не имеет смысла в проводке.
      final List<int> ids = <int>[];
      for (final Map<String, Object?> row in _rows) {
        if (row['id'] is num) ids.add((row['id'] as num).toInt());
      }
      if (on) {
        _selected.addAll(ids);
      } else {
        _selected.removeAll(ids);
      }
    });
  }

  bool get _allChecked => _rows.isNotEmpty && _rows.every(
      (Map<String, Object?> row) => row['id'] is num && _selected.contains((row['id'] as num).toInt()));

  /// Перечитывание загруженных страниц после операций (канон `renderGrid`
  /// после сводки: статусы и пометки строк приходят от сервера заново).
  /// Порядок запроса тот же (`_orderArg`), `total` обновляется из первой
  /// страницы (после удаления строк счётчик честный, а не прежний).
  Future<void> _reloadLoadedPages() async {
    final int loaded = _rows.length;
    final List<Map<String, Object?>> fresh = <Map<String, Object?>>[];
    int offset = 0;
    while (offset < loaded) {
      try {
        final Map<String, Object?> res = await widget.facade.call('list_videos',
            <String, Object?>{'offset': offset, 'limit': kPageLimit, 'order': _orderArg()});
        final String? refusal = _refusalOf(res);
        if (refusal != null) {
          setState(() => _error = refusal);
          return;
        }
        final List<Map<String, Object?>> page = _rowsOf(res);
        if (page.isEmpty) break;
        fresh.addAll(page);
        if (offset == 0 && res[kTotalKey] is num) {
          _total = (res[kTotalKey] as num).toInt();
        }
        offset += page.length;
      } catch (error) {
        setState(() => _error = 'список не перечитан: $error');
        return;
      }
    }
    setState(() => _rows = fresh);
    _dropStaleNodes();
  }

  /// «Скачать» панели: ОДНО действие на всю группу (канон
  /// `downloadSelection` app.js:1562-1577: бэкенд ставит queued списком id и
  /// запускает однопоточную очередь - параллельных запросов площадке не
  /// появляется). Хранилища нет - отказ текстом канона + названный переход
  /// к настройкам (браузер идёт на вкладку Настройки, здесь она заглушка
  /// W3). Сводка канона «В очередь: N из M · пропущено: причины» - причины
  /// приходят из бэка (`reasons` app/gui.py), UI их не угадывает. Выделение
  /// после выкачки снимается (`renderGrid(true)` app.js:1576) и статусы
  /// перечитываются; переход на вкладку очереди не построен (панель
  /// очереди - следующий шаг W2), сводка показана на месте - названо
  /// расхождением.
  Future<void> _downloadSelected() async {
    if (_selected.isEmpty) {
      setState(() => _notice = kNoSelectionText);
      return;
    }
    if (_storageId.isEmpty) {
      setState(() => _notice = '$kNoStorageText · $kNoSettingsJump');
      return;
    }
    final Map<String, Object?>? res = await bulkCall(
        widget.facade,
        _access ?? Access(refusal: 'права не прочитаны - цель не подписана'),
        'bulk_download',
        <String, Object?>{'ids': _selected.toList(), 'storage_id': _storageId},
        'queue',
        (String text) => setState(() => _notice = text));
    if (res == null) return;
    final Object? queued = res['queued'];
    setState(() {
      _notice = '$kQueuedPrefix${queued is num ? queued.toInt() : 0}$kFromPrefix'
          '${res['total'] ?? 0}${skipNoteOf(res)}';
      _selected.clear();
    });
    await _reloadLoadedPages();
  }

  /// Диалоги группы (§3.8 через `DialogScaffold`): барьер - --veil темы из
  /// реестра, вход решает диалог сам (выход по dialogEnter), возврат
  /// фокуса на инициатор панели - post-frame после pop (тот же факт шага
  /// 3: немедленный requestFocus терялся при disposal маршрута). Сводка
  /// приходит текстом результата и показывается живым регионом экрана.
  Future<void> _openMark(FocusNode opener) async {
    final String? result = await showDialog<String>(
      context: context,
      barrierColor: veilColor(themeIdOf(context)),
      barrierLabel: 'закрыть диалог пометок',
      traversalEdgeBehavior: TraversalEdgeBehavior.closedLoop,
      requestFocus: false,
      animationStyle: AnimationStyle(
        duration: durationExpand(),
        curve: kMetrics.easeExpressive,
        reverseCurve: kMetrics.easeExit,
      ),
      routeSettings: const RouteSettings(name: 'mark-dialog'),
      builder: (BuildContext context) => MarkDialog(
        facade: widget.facade,
        ids: _selected.toList(),
        access: _access ?? Access(refusal: 'права не прочитаны - цель не подписана'),
      ),
    );
    _afterDialog(opener, result);
  }

  /// «Операции…» - диалог со смешанными правами (цели внутри подписываются
  /// отдельно - тот же канон); результат удаления строк снимает выделение
  /// и перечитывает список (renderGrid(true) после сводки app.js:1729).
  Future<void> _openBulk(FocusNode opener) async {
    final String? result = await showDialog<String>(
      context: context,
      barrierColor: veilColor(themeIdOf(context)),
      barrierLabel: 'закрыть диалог операций',
      traversalEdgeBehavior: TraversalEdgeBehavior.closedLoop,
      requestFocus: false,
      animationStyle: AnimationStyle(
        duration: durationExpand(),
        curve: kMetrics.easeExpressive,
        reverseCurve: kMetrics.easeExit,
      ),
      routeSettings: const RouteSettings(name: 'bulk-dialog'),
      builder: (BuildContext context) => BulkDialog(
        facade: widget.facade,
        ids: _selected.toList(),
        access: _access ?? Access(refusal: 'права не прочитаны - цель не подписана'),
        onReload: _reloadLoadedPages,
      ),
    );
    if (result != null && result.startsWith(kRemoveSuccessPrefix)) {
      setState(() => _selected.clear());
      await _reloadLoadedPages();
    }
    _afterDialog(opener, result);
  }

  void _afterDialog(FocusNode opener, String? result) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final bool back = opener.parent != null;
      if (result != null) {
        // Сводка операции остаётся: отказ возвратить фокус (цель панели снята
        // с дерева после операции - выбор пуст или список перечитан) назван
        // appended-подписью, а не заменяет сводку (факт widget-теста шага 4:
        // сводка «Убраны строки из индекса» терялась).
        setState(() => _notice = result +
            (back ? '' : ' · фокус не возвратился на цель панели: '
                'выбор или список изменились'));
        if (back) opener.requestFocus();
      } else if (back) {
        opener.requestFocus();
      } else {
        setState(() => _notice = 'фокус не возвратился на цель панели: '
            'выбор или список изменились');
      }
      _dropStaleNodes();
    });
  }

  /// Одиночный клик/Enter: карточка строки (диалог §3.8). Двойной жест -
  /// плеер, одинарный не открывается, пока жест не различён (задержка
  /// двойного нажатия по умолчанию 300 мс против 240 мс в ui_src/app.js:3410 -
  /// расхождение названо).
  Future<void> _openCard(Map<String, Object?> row, int id, FocusNode opener) async {
    if (_cardOpen) {
      setState(() => _notice = 'карточка уже открыта: повторное открытие '
          'игнорируется (стек диалогов - волна W3)');
      return;
    }
    setState(() {
      _cardOpen = true;
      _openRowId = id;
    });
    await showDialog<void>(
      context: context,
      // Затемнение под карточкой - --veil темы из реестра; барьер гасит
      // список (эквивалент overlay + aria-modal в ui_src/index.html).
      barrierColor: veilColor(themeIdOf(context)),
      barrierLabel: 'закрыть карточку',
      // Ловушка Tab: обход замыкается внутри маршрута диалога и не уходит в
      // затемнённый список (APG Dialog (Modal), §3.8). Публичного `FocusTrap`
      // в этом SDK нет - граница focus-scope маршрута.
      traversalEdgeBehavior: TraversalEdgeBehavior.closedLoop,
      // Вход карточки решает сама: фокус на выход (§3.8), не на первый
      // контрол маршрута.
      requestFocus: false,
      // Карточка входит за --dur-slow-01, фон за --dur-slow-02 (CSS);
      // AnimationStyle один: взят медленный вход карточки с кривой
      // expressive, обратный ход - easeExit. Асимметрия названа.
      animationStyle: AnimationStyle(
        duration: durationExpand(),
        curve: kMetrics.easeExpressive,
        reverseCurve: kMetrics.easeExit,
      ),
      routeSettings: const RouteSettings(name: 'video-card'),
      builder: (context) => VideoCardDialog(
        facade: widget.facade,
        videoId: id,
        access: _access ?? Access(refusal: 'права не прочитаны - цель не '
            'подписана'),
        opener: widget.opener ?? const BrowserOpener(),
      ),
    );
    setState(() {
      _cardOpen = false;
      _openRowId = null;
    });
    // Возврат фокуса на инициатора (ui_src/app.js: dialogLeave): в браузере
    // возврат происходит, когда оверлей скрыт (MutationObserver по hidden),
    // поэтому и здесь он после кадра, когда маршрут диалога снят - немедленный
    // requestFocus под ещё живым барьером терялся при disposal маршрута
    // (найдено фактом прогона теста: isFocused оставался none).
    // `opener.parent != null` - публичный признак «узел ещё в focus-дереве»
    // (`attached` в этом SDK нет - найдено flutter analyze).
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (opener.parent != null) {
        opener.requestFocus();
      } else {
        setState(() => _notice = 'фокус не возвратился на строку: строка '
            'исчезла из окна прокрутки');
      }
      _dropStaleNodes();
    });
  }

  /// Двойной клик/Ctrl+Enter: страница сервера `/watch/<id>` в системном
  /// браузере. Право `watch` подписывается до перехода (тот же
  /// `routeReason("media")` в ui_src/app.js:3443), отказ называется.
  /// Системный плеер по файлу на диске (`open_file`) - нативная цель окна
  /// (app/http/scopes.py: NATIVE), Flutter-окно поверх HTTP её не вызывает.
  Future<void> _play(int id) async {
    final access = _access;
    if (access == null) {
      setState(() => _notice = 'права не прочитаны: плеер не открывается');
      return;
    }
    if (!access.has('watch')) {
      setState(() => _notice = access.denial('watch'));
      return;
    }
    final url = widget.facade.watchUrl(id);
    final failure = await (widget.opener ?? const BrowserOpener()).open(url);
    setState(() => _notice = failure ?? 'плеер открыт: $url');
  }

  Widget _buildRow(BuildContext context, int index, double availableWidth) {
    final theme = Theme.of(context);
    final themeId = themeIdOf(context);
    final row = _rows[index];
    final id = row['id'] is num ? (row['id'] as num).toInt() : index;
    final picked = _selected.contains(id);
    final node = _rowNode(id);
    final hovered = _hoverId == id;
    final focused = _focusedId == id;
    final ring = focusRing(themeId);
    // hover/selected - роли --hover и --selected из реестра (тот же порядок
    // различимости, что `.row:hover`/`.row.selected` в ui_src/app.css: выбор
    // виден и под курсором, поэтому выбор приоритетнее наведения).
    final background = picked
        ? roleColor(themeId, 'selected')
        : hovered
            ? roleColor(themeId, 'hover')
            : theme.colorScheme.surfaceContainerLowest;
    return Focus(
      focusNode: node,
      // Строка доступна с клавиатуры: узел строки, а не internal focus
      // контролов (внутренний узел не дал бы различить Enter и Space).
      canRequestFocus: true,
      // Чекбокс строки не отдельная точка Tab: канон обходит input
      // (`.row-check` без tabindex-точки в roving-обходе грида), выбор ведётся
      // Space на строке и кликом чекбокса - названо решением шага 4 и
      // расхождением формы (roving ложится на W3).
      descendantsAreFocusable: false,
      onKeyEvent: (FocusNode node, KeyEvent event) => _handleRowKey(row, id, event),
      onFocusChange: (bool hasFocus) => setState(() => _focusedId = hasFocus
          ? id
          : (_focusedId == id ? null : _focusedId)),
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        onEnter: (_) => setState(() => _hoverId = id),
        onExit: (_) => setState(() => _hoverId = _hoverId == id ? null : _hoverId),
        child: GestureDetector(
          onTap: () => _openCard(row, id, node),
          onDoubleTap: () => _play(id),
          child: Semantics(
            // Эквивалент aria из DESIGN-UI §3.1: строка названа целиком,
            // выбор передаётся отдельным свойством, активация - одиночный
            // клик (карточка), не выбор. Флаг `focused` кладётся явно:
            // Semantics-узел строки не получает состояние фокуса сам (Focus
            // не аннотирует семантику, это делают контролы Material), и без
            // него AT не объявил бы, на какой строке фокус.
            label: _rowSemantics(row, index),
            selected: picked,
            button: true,
            focused: focused,
            onTap: () => _openCard(row, id, node),
            child: Stack(
              children: <Widget>[
                Container(
                  color: background,
                  padding: EdgeInsets.symmetric(horizontal: kMetrics.s4),
                  child: Center(
                    // Ячейки канона `rowHtml` (grid_cells.dart): тот же
                    // состав видимых колонок, что шапка (гейты `col_*`), тот
                    // же порядок и веса wide/narrow по breakpoint канона.
                    child: rowCells(context, themeId, row, widget.settings,
                        index, picked, () => _toggleSelected(id), availableWidth),
                  ),
                ),
                if (focused)
                  // Кольцо фокуса рисуется поверх строки (Stack overlay), а не
                  // внутри её контейнера: границы внутри сдвигали содержимое
                  // строки за её slot, и клик попадал в соседнюю строку -
                  // найдено фактом прогона теста (hit test на «Клип 0» давал
                  // _RenderListTile соседней строки). В CSS это два box-shadow
                  // наружу (--focus-ring: 2px --bg, 4px --focus), здесь две
                  // границы поверх строки: форма названа расхождением, ширины и
                  // цвета читаются из токенов. IgnorePointer - кольцо не
                  // перехватывает клик по строке.
                  Positioned.fill(
                    child: IgnorePointer(
                      child: Container(
                        decoration: BoxDecoration(
                          border: Border.all(color: ring.ring, width: ring.ringWidth),
                        ),
                        child: Container(
                          decoration: BoxDecoration(
                            border: Border.all(color: ring.gap, width: ring.gapWidth),
                          ),
                        ),
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  /// Шапка колонок канона (`renderGridHead` из ui_src/app.js + `applyListColumns`):
  /// тот же состав видимых колонок и те же веса, что строки - `headerCells`
  /// из grid_cells.dart (скрытая гейтом `col_*` колонка не рисуется ни там,
  /// ни здесь). Сортируемые колонки - кнопки с направлением ▲/▼: отдельного
  /// флага `aria-sort` в семантике Flutter нет (`SemanticsSortKey` задаёт
  /// только порядок обхода), направление передаётся текстом подписи -
  /// названо расхождением. Форма канона - CSS grid с горизонтальным скроллом
  /// `--list-w`, здесь flex по доступной ширине с весами канонных px-ширин:
  /// расхождение названо, скролл и грид-роли ложатся на волну W3.
  Widget _buildHeader(BuildContext context, double availableWidth) {
    return Padding(
      padding: EdgeInsets.symmetric(horizontal: kMetrics.s4),
      child: headerCells(context, widget.settings, _sortCol, _sortDir,
          availableWidth, _allChecked,
          // Галка канона: click из unchecked ставит checked -> все строки,
          // из checked снимает -> снять всех (тот же change-делелегат
          // app.js:7282-7289); mixed в каноне не arises - input обычный,
          // частичный выбор называет счётчик «Выбрано: N» (grid_cells.dart).
          (bool? on) => _selectAll(on == true), _toggleSort),
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
          // Панель мультивыбора канона (`sel-bar` index.html:173): в DOM она
          // стоит перед шапкой колонок (173 < 218) - здесь тем же порядком,
          // появляется только при nonzero выборе. Узлы целей регистрируются
          // в канонном DOM-порядке (селект хранилища первым, затем цели):
          // возврат фокуса после диалога ведёт обход на инициатора (§3.8,
          // dialogLeave app.js:4810).
          if (_selected.isNotEmpty)
            SelectionBar(
              count: _selected.length,
              storages: _storages,
              storageId: _storageId,
              nodes: <String, FocusNode>{
                'storage': _barNode('storage'),
                'mark': _barNode('mark'),
                'bulk': _barNode('bulk'),
                'download': _barNode('download'),
                'clear': _barNode('clear'),
              },
              onStorage: (String? id) => setState(() => _storageId = id ?? _storageId),
              onMark: () => _openMark(_barNode('mark')),
              onBulk: () => _openBulk(_barNode('bulk')),
              onDownload: _downloadSelected,
              onClear: _clearSelection,
            ),
          LayoutBuilder(
            builder: (BuildContext context, BoxConstraints constraints) =>
                _buildHeader(context, constraints.maxWidth),
          ),
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
                : LayoutBuilder(
                    builder: (BuildContext context, BoxConstraints constraints) =>
                        ListView.builder(
                      itemCount: _rows.length + (more ? 1 : 0),
                      itemExtent: rowHeight(fontPxOf(context)),
                      itemBuilder: (BuildContext context, int index) {
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
                        // Ширина доступной строке - ширина списка: breakpoint
                        // узкого экрана канона (`max-width: 900px`) сверяется
                        // по ней, а не по ширине окна (расхождение формы
                        // названо в grid_cells.dart).
                        return _buildRow(context, index, constraints.maxWidth);
                      },
                    ),
                  ),
          ),
          // Живый регион: эквивалент aria-live у панели журнала - статус
          // меняется без фокуса и должен быть объявлен AT. Регион отдельным
          // узлом (`container`), а подпись внутри исключена из семантики,
          // иначе AT передаёт одно и то же дважды.
          Padding(
            padding: EdgeInsets.symmetric(horizontal: kMetrics.s4, vertical: kMetrics.s2),
            child: Semantics(
              liveRegion: true,
              container: true,
              label: _notice == null
                  ? 'ответ действия не назван'
                  : 'ответ действия: $_notice',
              child: Semantics(
                excludeSemantics: true,
                child: Text(_notice ?? 'действий пока не было',
                    style: theme.textTheme.bodySmall?.copyWith(
                        color: _notice == null
                            ? theme.colorScheme.onSurfaceVariant
                            : theme.colorScheme.error)),
              ),
            ),
          ),
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
