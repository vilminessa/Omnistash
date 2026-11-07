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
/// передаётся как `selected`, строка доступна с клавиатуры (собственный
/// `Focus` строки: обход по ↑↓ ведёт default traversal, Enter открывает
/// карточку, Ctrl+Enter открывает плеер, Space переключает выбор - тот же
/// канон, что `bindGridItems` в ui_src/app.js:3400-3435 и строка клавиатуры
/// в docs/DESIGN-UI.md §733). Home/End (первая/последняя строка окна
/// прокрутки) на этом шаге не построены: названный разрыв, ложится вместе с
/// гридом ячеек на волне W2. Клик одинарный открывает карточку, двойной -
/// плеер: Flutter различает их жестом двойного нажатия (задержка по умолчанию
/// 300 мс), браузер держит 240 мс вручную (app.js:3410) - расхождение
/// названо, а не подменяется числом.
///
/// Активация карточки с AT (`Semantics(onTap:)`) повторяет одиночный клик:
/// карточка, не выбор; выбор - отдельное действие Space, как в окне.
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
  int? _selectedId;
  String? _error;
  String? _persistError;
  String _journal = 'журнал пуст';
  String? _notice;
  bool _loading = true;
  Timer? _poll;

  /// Права ключа (GET /whoami): цель «Открыть» подписывается до перехода,
  /// отсутствие `watch` называется текстом, а не молча уходит в 403.
  Access? _access;

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
    _poll = Timer.periodic(Duration(milliseconds: kPollIntervalMs), (_) => _pollJournal());
  }

  @override
  void dispose() {
    _poll?.cancel();
    for (final node in _rowNodes.values) {
      node.dispose();
    }
    _rowNodes.clear();
    super.dispose();
  }

  /// Права ключа: отказ читается текстом и остаётся в `refusal` - молчания
  /// нет, как в `fetchAccess` браузера (401 называет привязку).
  Future<void> _loadAccess() async {
    try {
      final access = await widget.facade.access();
      setState(() => _access = access);
    } catch (error) {
      setState(() => _access = Access(refusal: 'права не прочитаны: $error'));
    }
  }

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
  /// работают default traversal-ом; грид ячеек с roving ложится на волне W2.
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
      });
      _loadFirst();
      _loadAccess();
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
  /// `stopPropagation` на `.row-check` в ui_src/app.js:3423).
  void _toggleSelected(int id) {
    setState(() => _selectedId = _selectedId == id ? null : id);
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

  Widget _buildRow(BuildContext context, int index) {
    final theme = Theme.of(context);
    final themeId = themeIdOf(context);
    final row = _rows[index];
    final id = row['id'] is num ? (row['id'] as num).toInt() : index;
    final selected = _selectedId == id;
    final role = roleOfStatus(row['status'] is String ? row['status'] as String : null);
    final color = roleColor(themeId, role);
    final node = _rowNode(id);
    final hovered = _hoverId == id;
    final focused = _focusedId == id;
    final ring = focusRing(themeId);
    return Focus(
      focusNode: node,
      // Строка доступна с клавиатуры: узел строки, а не internal focus
      // ListTile (внутренний узел не дал бы различить Enter и Space).
      canRequestFocus: true,
      descendantsAreFocusable: false,
      onKeyEvent: (node, event) => _handleRowKey(row, id, event),
      onFocusChange: (hasFocus) => setState(() => _focusedId = hasFocus
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
            selected: selected,
            button: true,
            focused: focused,
            onTap: () => _openCard(row, id, node),
            child: Stack(
              children: <Widget>[
                ListTile(
                  // onTap null: pointer-жест ведёт GestureDetector выше, а
                  // Semantics(onTap) даёт активацию AT. Внутренний focus
                  // ListTile не используется (Enter и Space нужно различать).
                  onTap: null,
                  selected: selected,
                  // hover/selected - роли --hover и --selected из реестра
                  // (тот же порядок различимости, что `.row:hover`/`.row.selected`).
                  tileColor: hovered
                      ? roleColor(themeId, 'hover')
                      : theme.colorScheme.surfaceContainerLowest,
                  selectedTileColor: roleColor(themeId, 'selected'),
                  leading: Semantics(
                    // Маркер статуса: 7 px в CSS лежит вне токен-шкалы, здесь
                    // ближайший шаг шкалы (s3) - названо расхождением, форма
                    // маркера переносится вместе с веяниями на волне W3.
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
