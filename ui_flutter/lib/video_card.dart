/// Карточка видео (W2, шаг 3): диалог поверх цели `get_video`.

/// Канон карточки - `openDetail` в ui_src/app.js:3571-3716: порядок подписей
/// kv, форма значений и действия по статусу. Данные читает та же цель фасада
/// (`app/gui.py: get_video` -> `app/repo.py: video_detail`), проводка та же
/// (`Facade.call`). tools/check_flutter_mirror.py сверяет список подписей,
/// списки статусов, права действий против `app/http/scopes.py` и путь
/// `/watch/<id>` против ui_src/app.js и app/http/server.py - расхождение
/// называется текстом, а не молчит.
///
/// Диалог строится по APG Dialog (Modal) и docs/DESIGN-UI.md §3.8:
///   * имя - `Semantics(label:)` контейнера (эквивалент `aria-labelledby`
///     в разметке браузера: узел имени в Flutter нет, имя кладётся на контейнер);
///   * `aria-modal` - барьер маршрута: `DialogRoute` с `barrierDismissible`,
///     затемнение --veil из реестра тем (theme.dart: veilColor);
///   * ловушка Tab - граница focus-scope маршрута: `traversalEdgeBehavior:
///     TraversalEdgeBehavior.closedLoop` (в этом SDK публичного `FocusTrap`
///     нет - `focus_trap.dart` не экспортируется, найдено чтением
///     packages/flutter/lib/widgets.dart), поэтому обход замыкается внутри
///     карточки и не уходит в затемнённый список;
///   * вход - дефолт не-разрушительный: фокус получает выход (`ui_src/app.js:
///     dialogEnter` ставит `[id$="-decline"]/[id$="-later"]`, иначе `-close`);
///     у карточки отказа нет, выход - крестик, это названо расхождением;
///   * Escape - закрывает карточку (в браузере `dialogExit` по Escape, здесь
///     `Focus.onKeyEvent` на границе содержимого: публичного обработчика
///     Escape в маршрутах этого SDK нет);
///   * возврат фокуса на инициатора - вызывающий код после `await showDialog`
///     просит фокус на узле строки (`ui_src/app.js: dialogLeave`), и узел
///     обязан быть ещё attached (строка могла исчезнуть из окна прокрутки).
///
/// Что здесь НЕ построено и названо причиной (не пустой экран и не молчание):
///   * поля `Mesh` и `Провенанс mesh` - состояние mesh-запроса этой строки и
///     подпись происхождения из `files.format_json` (волна W3, вместе с
///     панелью участников и SAS);
///   * действия `К очереди`, `Запросить у участников`, `Остановить запрос`,
///     `Файл оказался не тем?` - очередь и mesh-панель ложатся на W2/W3, а
///     проводник - нативная цель окна (`app/http/scopes.py: NATIVE` держит
///     open_folder/open_file/open_url), и путь файла показан строкой `Файлы`
///     вместо кнопки;
///   * блоки карточки канона: обложка строки (`detailThumbHtml`), редактор
///     пометок с правом mark (`detailMarksHtml`) и просмотр `raw_json` -
///     названы в `kCardBlocksNotBuilt`;
///   * стек диалогов с N вложенными карточками - здесь один диалог,
///     повторное открытие называется текстом в library_view.dart.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'access.dart';
import 'api_client.dart';
import 'player.dart';
import 'theme.dart';

/// Подписи kv карточки - тот же порядок, что `add(...)` в `openDetail`
/// (ui_src/app.js:3571-3656). Сверка ведёт tools/check_flutter_mirror.py:
/// список обязан совпасть с порядком JS за вычетом подписей из
/// `kCardFieldsNotBuilt` (они названы волной).
const List<String> kCardLabels = <String>[
  'Статус',
  'Тип',
  'Ключ',
  'Канал',
  'Опубликовано',
  'Длительность',
  'Просмотров',
  'Лайков',
  'Комментариев',
  'Синк данных',
  'Длительность файла',
  'Метки файла',
  'Битрейт',
  'Частота',
  'Каналы',
  'Кодекс',
  'Исполнитель',
  'Альбом',
  'Жанр',
  'Дата релиза',
  'Номер трека',
  'Тег читан из',
  'Обложка',
  'Происхождение',
  'URL',
  'Первый раз замечено',
  'Скачано',
  'Обновлено',
  'Плейлисты',
  'Файлы',
];

/// Подписи канона, которые Flutter не рисует на этом шаге: каждая называет
/// волну и причину (страж сверяет, что волна названа).
const Map<String, String> kCardFieldsNotBuilt = <String, String>{
  'Mesh': 'состояние mesh-запроса этой строки не построено: волна W3 (mesh-панель '
      'участников вместе с SAS и юр-гейтами)',
  'Провенанс mesh': 'подпись происхождения файла из `files.format_json` '
      '(уровень приёма, подтверждения, синк узла) не построена: волна W3',
};

/// Статусы по действиям - те же списки, что `indexOf(data.status)` в
/// `openDetail` (ui_src/app.js:3663-3692). Сверку ведёт mirror-страж.
const List<String> kDownloadStatuses =
    <String>['known', 'failed', 'missing', 'detached'];
const List<String> kQueueStatuses = <String>['queued', 'downloading'];
const List<String> kPlayableStatuses = <String>['downloaded', 'unavailable'];
const List<String> kMeshStatuses = <String>[
  'known', 'failed', 'missing', 'detached', 'unavailable',
];

/// Действия карточки, построенные сейчас: подпись -> право ключа и цель
/// фасада (`''` - цель не вызывается: адрес открывает системный браузер).
/// Право сверяется с `app/http/scopes.py` стражем: клиент не выдумывает,
/// какое право требует цель, а читает то же, что подписывает браузер
/// (`data-need="queue"`, `data-need="watch"`).
const List<({String label, String scope, String target})> kCardActions =
    <({String label, String scope, String target})>[
  (label: 'Скачать', scope: 'queue', target: 'enqueue'),
  (label: 'Открыть', scope: 'watch', target: ''),
  (label: 'Открыть на площадке', scope: '', target: ''),
];

/// Действия канона, не построенные на этом шаге: подпись -> причина и волна.
const Map<String, String> kCardActionsNotBuilt = <String, String>{
  'К очереди': 'переход на экран очереди не построен: волна W2 (шаг 4 - '
      'очередь и журнал как панели)',
  'Запросить у участников': 'mesh-запрос файлу у участников не построен: '
      'волна W3 (вместе с mesh-панелью и доверием)',
  'Остановить запрос': 'остановка mesh-запроса не построена: волна W3',
  'Файл оказался не тем?': 'отзыв mesh-копии (стоп-лист хеша + удаление '
      'файла) не построен: волна W3',
  'Открыть папку': 'проводник с выделенным файлом - нативная цель окна '
      '(app/http/scopes.py: NATIVE держит open_folder), по HTTP не '
      'выполняется: путь файла показан строкой «Файлы»',
};

/// Блоки карточки канона (кроме ряда действий и kv), не построенные на этом
/// шаге: имя источника из ui_src/app.js -> причина и волна.
const Map<String, String> kCardBlocksNotBuilt = <String, String>{
  'detailThumbHtml': 'обложка строки (плитка карточки, get_thumbs) не построена: '
      'волна W2 (шаг 4 - грид ячеек и плитки с захватом обложек)',
  'detailMarksHtml': 'редактор пометок строки (рейтинг, просмотрено, теги, '
      'заметка, «Сохранить пометки» с правом mark) не построен: волна W3',
  'raw_json': 'полные метаданные площадки (raw_json) не показаны: волна W3 '
      '(вместе с просмотром raw и сверкой поля площадки)',
};

/// Строка хранилищ, когда их нет - тот же текст, что `storageOptionsHtml`
/// (ui_src/app.js:3091). Сверяется стражем с JS.
const String kNoStoragesText = 'нет хранилищ - добавьте в настройках';
/// Метка недоступного носителя в списке хранилищ - тот же текст, что
/// `storageOptionsHtml` (ui_src/app.js:3095).
const String kNoMediaText = ' (нет носителя)';
/// Ответ на «Скачать» - тот же текст, что обработчик `data-detail="download"`
/// (ui_src/app.js:3886).
const String kEnqueuedText = 'В очередь поставлено: ';

/// Тип дорожки - та же пара подписей, что `openDetail` (audio -> «аудио»).
String mediaKindLabel(Object? kind) =>
    kind == 'audio' ? 'аудио' : 'видео';

/// Дата выхода: `shortDate` в ui_src/app.js:945 - первые 10 символов iso,
/// пустое значение - «—» (не выдумываем дату).
String shortDateOf(Object? iso) {
  if (iso is! String || iso.isEmpty) return '—';
  return iso.length > 10 ? iso.substring(0, 10) : iso;
}

/// Момент события: `shortTime` в ui_src/app.js:949 - «T» на пробел, первые
/// 16 символов; пустое - «—».
String shortTimeOf(Object? iso) {
  if (iso is! String || iso.isEmpty) return '—';
  final text = iso.replaceFirst('T', ' ');
  return text.length > 16 ? text.substring(0, 16) : text;
}

/// Длительность: `humanDuration` в ui_src/app.js:917 - часы добавляются
/// только когда они есть, минуты пишутся без дополнения, секунды
/// дополняются до двух знаков; дробные секунды округляются до целых (иначе
/// строка выглядела бы «4:14.300000000000011»).
String humanDurationOf(Object? seconds) {
  if (seconds is! num || seconds.isNaN || seconds < 0) return '—';
  final total = seconds.floor();
  final hours = total ~/ 3600;
  final minutes = (total % 3600) ~/ 60;
  final rest = total % 60;
  // Дополнение до двух знаков - та же `pad` из ui_src/app.js:925.
  String pad(int value) => value.toString().padLeft(2, '0');
  return hours > 0
      ? '$hours:${pad(minutes)}:${pad(rest)}'
      : '$minutes:${pad(rest)}';
}

/// Счётчик: `Number(x).toLocaleString("ru-RU")` (ui_src/app.js:3588-3592) -
/// разряды через разделитель. Здесь обычный пробел вместо неразрывного NBSP:
/// названо расхождением (intl не в зависимостях, рантайм ничего не грузит).
String countOf(Object? value) {
  if (value == null) return '';
  final number = value is num ? value.toInt() : int.tryParse(value.toString());
  if (number == null) return value.toString();
  final digits = number.abs().toString();
  final grouped = <String>[];
  for (int start = digits.length; start > 0; start -= 3) {
    grouped.insert(0, digits.substring(start > 3 ? start - 3 : 0, start));
  }
  return '${number < 0 ? '-' : ''}${grouped.join(' ')}';
}

/// Запись `files` из `video_detail` (app/repo.py: SELECT id, kind, path, ...).
String fileLine(Map<String, Object?> file) {
  final kind = file['kind'] is String ? file['kind'] as String : 'файл';
  final path = file['path'] is String ? file['path'] as String : 'путь не назван';
  return '$kind: $path${file['missing'] == true ? ' [пропал]' : ''}';
}

/// Запись плейлиста-источника: `title (убран)` или `title #position`
/// (ui_src/app.js:3651-3653).
String playlistLine(Map<String, Object?> playlist) {
  final title = playlist['title'] is String ? playlist['title'] as String : 'без названия';
  return playlist['removed_at'] != null
      ? '$title (убран)'
      : '$title #${playlist['position'] ?? ''}';
}

/// Метки аудио-файла из `files[].format_json` - тот же путь, что
/// `audioFileInfo` в ui_src/app.js:3750: берётся первый файл kind=video с
/// `media == "audio"`, чужой JSON молча пропускается (не наш случай),
/// ключи читает app/audio.py (`meta["probe"]`, `tags_scheme`, ...).
Map<String, Object?>? audioMetaOf(List<Map<String, Object?>> files) {
  for (final file in files) {
    if (file['kind'] != 'video') continue;
    final raw = file['format_json'];
    if (raw is! String || raw.isEmpty) continue;
    try {
      final meta = jsonDecode(raw);
      if (meta is Map<String, Object?> && meta['media'] == 'audio') return meta;
    } on Object {
      // Чужой формат: не наш случай - молча идём к следующему файлу, как JS.
      continue;
    }
  }
  return null;
}

/// Пара подпись/значение карточки; пустое значение не кладётся - тот же
/// `add(label, value)` из ui_src/app.js:3577 (null/undefined/'' пропускаются).
typedef CardField = ({String label, String value});

/// Поля карточки по канону `openDetail`, в том же порядке.
List<CardField> cardFieldsOf(Map<String, Object?> data) {
  final files = (data['files'] as List?)?.cast<Map<String, Object?>>()
      ?? const <Map<String, Object?>>[];
  final playlists = (data['playlists'] as List?)?.cast<Map<String, Object?>>()
      ?? const <Map<String, Object?>>[];
  final fields = <CardField>[
    (label: 'Статус', value: data['status_label'] is String
        ? data['status_label'] as String
        : (data['status'] is String ? data['status'] as String : '')),
    (label: 'Тип', value: mediaKindLabel(data['media_kind'])),
    (label: 'Ключ', value: data['key'] is String ? data['key'] as String : ''),
    (label: 'Канал', value: data['channel'] is String ? data['channel'] as String : ''),
    (label: 'Опубликовано', value: shortDateOf(data['uploaded_at'])),
    (label: 'Длительность', value: data['duration_s'] == null
        ? '' : humanDurationOf(data['duration_s'])),
    (label: 'Просмотров', value: countOf(data['view_count'])),
    (label: 'Лайков', value: countOf(data['like_count'])),
    (label: 'Комментариев', value: countOf(data['comment_count'])),
    (label: 'Синк данных', value: _timeOrNull(data['synced_at'])),
  ];
  final audio = audioMetaOf(files);
  if (audio != null) {
    fields.add((label: 'Длительность файла',
        value: audio['duration_s'] == null
            ? 'не получена' : humanDurationOf(audio['duration_s'])));
    if (audio['probe'] != 'ffprobe') {
      fields.add((label: 'Метки файла',
          value: audio['probe_note'] is String
              ? audio['probe_note'] as String : 'не получены'));
    } else {
      final bitrate = audio['bitrate_bps'];
      if (bitrate is num && bitrate > 0) {
        fields.add((label: 'Битрейт', value: '${(bitrate / 1000).round()} кбит/с'));
      }
      final rate = audio['sample_rate_hz'];
      if (rate is num && rate > 0) {
        // Та же форма, что ui_src/app.js:3628: Гц -> кГц с одним знаком.
        fields.add((label: 'Частота',
            value: '${((rate / 100).round() / 10).toStringAsFixed(1)} кГц'));
      }
      if (audio['channels'] != null) {
        fields.add((label: 'Каналы', value: '${audio['channels']}'));
      }
      if (audio['codec'] is String) {
        fields.add((label: 'Кодекс', value: audio['codec'] as String));
      }
    }
    for (final entry in <String>[
      'artist', 'album', 'genre', 'date', 'track', 'tags_scheme',
    ]) {
      final value = audio[entry];
      if (value == null) continue;
      fields.add((label: _audioLabel(entry), value: '$value'));
    }
    final cover = files.firstWhere(
        (file) => file['kind'] == 'thumbnail' && file['missing'] != true,
        orElse: () => <String, Object?>{});
    fields.add((label: 'Обложка', value: cover.isEmpty
        ? 'не получена (в теге файла её нет)' : 'из тега файла'));
  }
  fields.add((label: 'Происхождение',
      value: data['origin'] is String ? data['origin'] as String : ''));
  fields.add((label: 'URL',
      value: data['webpage_url'] is String ? data['webpage_url'] as String : ''));
  fields.add((label: 'Первый раз замечено', value: _timeOrNull(data['first_seen_at'])));
  fields.add((label: 'Скачано', value: _timeOrNull(data['downloaded_at'])));
  fields.add((label: 'Обновлено', value: _timeOrNull(data['updated_at'])));
  fields.add((label: 'Плейлисты',
      value: playlists.map(playlistLine).join(', ')));
  fields.add((label: 'Файлы', value: files.map(fileLine).join('\n')));
  return fields.where((field) => field.value.isNotEmpty).toList();
}

/// Подпись поля аудио-файла - те же имена, что `add(...)` в JS (порядок
/// artist/album/genre/date/track/tags_scheme тот же).
String _audioLabel(String key) {
  switch (key) {
    case 'artist':
      return 'Исполнитель';
    case 'album':
      return 'Альбом';
    case 'genre':
      return 'Жанр';
    case 'date':
      return 'Дата релиза';
    case 'track':
      return 'Номер трека';
    default:
      return 'Тег читан из';
  }
}

/// Момент события для kv: пустое значение не кладётся (пустая строка `add`
/// пропускает, «—» кладётся только когда дата есть).
String _timeOrNull(Object? iso) =>
    iso == null ? '' : shortTimeOf(iso);

/// Есть ли локальная дорожка, которую можно смотреть - тот же фильтр, что
/// `openDetail` (ui_src/app.js:3677): kind=video и не пропал.
bool localVideoFile(Map<String, Object?> data) {
  final files = (data['files'] as List?)?.cast<Map<String, Object?>>()
      ?? const <Map<String, Object?>>[];
  return files.any((file) => file['kind'] == 'video' && file['missing'] != true);
}

/// Хранилища из `storages_list` (app/gui.py: storages_list): активные и
/// включённые - тот же `activeStorages` (ui_src/app.js:3070).
List<Map<String, Object?>> activeStorages(Map<String, Object?> res) {
  final storages = (res['storages'] as List?)?.cast<Map<String, Object?>>()
      ?? const <Map<String, Object?>>[];
  return storages.where((storage) =>
      storage['status'] == 'active' && storage['enabled'] == true).toList();
}

/// Выбранное хранилище: default из ответа, иначе первое активное - тот же
/// `defaultStorageId` (ui_src/app.js:3076).
String defaultStorageId(Map<String, Object?> res, List<Map<String, Object?>> active) {
  final chosen = res['default_storage_id'];
  if (chosen is String && active.any((storage) => storage['id'] == chosen)) {
    return chosen;
  }
  return active.isEmpty ? '' : '${active.first['id']}';
}

/// Подпись хранилища в селекте - `storageOptionsHtml` (ui_src/app.js:3095).
String storageLabel(Map<String, Object?> storage) {
  final label = storage['label'] is String ? storage['label'] as String : 'без названия';
  return storage['available'] == true ? label : '$label$kNoMediaText';
}

/// Карточка видео: поля из `get_video`, действия по статусу, диалог §3.8.
class VideoCardDialog extends StatefulWidget {
  const VideoCardDialog({
    super.key,
    required this.facade,
    required this.videoId,
    required this.access,
    required this.opener,
  });

  final Facade facade;
  final int videoId;

  /// Права ключа (GET /whoami): цель подписывается до запроса, отсутствие
  /// права называется текстом.
  final Access access;

  /// Системный браузер для страницы плеера и адреса площадки.
  final UrlOpener opener;

  @override
  State<VideoCardDialog> createState() => VideoCardDialogState();
}

class VideoCardDialogState extends State<VideoCardDialog> {
  Map<String, Object?>? _data;
  bool _loading = true;
  String? _error;
  String? _notice;
  List<Map<String, Object?>> _storages = const <Map<String, Object?>>[];
  String _storageId = '';

  // Узлы фокуса контролов карточки: вход - на выход (§3.8), обход - внутри
  // маршрута (closedLoop), возврат - на инициатора (делает вызывающий код).
  final FocusNode _exitNode = FocusNode();
  final FocusNode _storageNode = FocusNode();
  final FocusNode _downloadNode = FocusNode();
  final FocusNode _playNode = FocusNode();
  final FocusNode _sourceNode = FocusNode();

  /// Узлы, которые реально смонтированы в текущем состоянии карточки, в
  /// порядке обхода: выход, хранилище, «Скачать», «Открыть», площадка.
  List<FocusNode> focusOrder() {
    final order = <FocusNode>[_exitNode];
    if (_downloadVisible) order.add(_storageNode);
    if (_downloadVisible) order.add(_downloadNode);
    if (_playVisible) order.add(_playNode);
    if (_sourceVisible) order.add(_sourceNode);
    return order;
  }

  @visibleForTesting
  String? get noticeForTest => _notice;

  bool get _downloadVisible {
    final status = _data?['status'];
    return status is String && kDownloadStatuses.contains(status);
  }

  bool get _playVisible {
    final status = _data?['status'];
    return status is String && kPlayableStatuses.contains(status)
        && localVideoFile(_data!);
  }

  bool get _sourceVisible => _data?['webpage_url'] is String;

  /// Состояние фокуса выхода: кладётся в семантику явно (кнопка Material не
  /// аннотирует `focused` на узле с нашей подписью - найдено фактом прогона),
  /// чтобы AT объявил, что вход диалога - не-разрушительный выход (§3.8).
  bool _exitFocused = false;

  /// Состояние Shift для обратного обхода: у `KeyEvent` в этом SDK нет
  /// `isShiftPressed`/`modifiers` (найдено flutter analyze), поэтому
  /// модификатор ведётся локально по down/up событийам контрольных клавиш.
  bool _shiftDown = false;

  /// Ловушка Tab (§3.8, APG Dialog (Modal)): обход замыкается внутри
  /// карточки. Граница focus-scope маршрута с `closedLoop` в этом SDK
  /// фокус не возвращает: после последнего контрола primaryFocus становится
  /// null (найдено фактом прогона теста), поэтому порядок ведётся явно по
  /// списку `focusOrder()`. Если фокус ушёл в открытое меню селекта (его
  /// узла нет в списке), событие отдаётся framework: меню листается само.
  KeyEventResult _handleDialogKey(FocusNode node, KeyEvent event) {
    final key = event.logicalKey;
    if (key == LogicalKeyboardKey.shiftLeft || key == LogicalKeyboardKey.shiftRight) {
      _shiftDown = event is KeyDownEvent;
      return KeyEventResult.ignored;
    }
    if (event is! KeyDownEvent) return KeyEventResult.ignored;
    if (key == LogicalKeyboardKey.escape) {
      _close();
      return KeyEventResult.handled;
    }
    if (key != LogicalKeyboardKey.tab) return KeyEventResult.ignored;
    final order = focusOrder();
    final current = FocusManager.instance.primaryFocus;
    if (order.isEmpty || current == null || !order.contains(current)) {
      return KeyEventResult.ignored;
    }
    final index = order.indexOf(current);
    final next = _shiftDown
        ? (index - 1 + order.length) % order.length
        : (index + 1) % order.length;
    order[next].requestFocus();
    return KeyEventResult.handled;
  }

  @override
  void initState() {
    super.initState();
    _load();
    // Состояние фокуса выхода: IconButton в этом SDK не принимает
    // onFocusChange (найдено flutter analyze), поэтому слушатель на узле -
    // тот же приём, что FocusNode.addListener в самом Flutter.
    _exitNode.addListener(() => setState(() => _exitFocused = _exitNode.hasFocus));
  }

  @override
  void dispose() {
    _exitNode.dispose();
    _storageNode.dispose();
    _downloadNode.dispose();
    _playNode.dispose();
    _sourceNode.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final res = await widget.facade.call('get_video', widget.videoId);
      final refusal = res['error'];
      if (refusal is String && refusal.isNotEmpty) {
        setState(() {
          _error = refusal;
          _loading = false;
        });
        return;
      }
      setState(() {
        _data = res;
        _loading = false;
        _error = null;
      });
      await _loadStorages();
    } on ApiFailure catch (failure) {
      // get_video на отсутствующее видео отдаёт null (app/gui.py ->
      // repo.video_detail -> None): это «не найдено», и оно называется.
      setState(() {
        _error = 'карточка не прочитана: ${failure.reason}';
        _loading = false;
      });
    } catch (error) {
      setState(() => _error = 'карточка не прочитана: $error');
    }
  }

  /// Хранилища для «Скачать»: цель под скоупом read; отказ называется и
  /// select остаётся с честной подписью, а не пустым списком.
  Future<void> _loadStorages() async {
    if (!_downloadVisible) return;
    try {
      final res = await widget.facade.call('storages_list', null);
      final refusal = res['error'];
      if (refusal is String && refusal.isNotEmpty) {
        setState(() => _notice = 'хранилища не прочитаны: $refusal');
        return;
      }
      setState(() {
        _storages = activeStorages(res);
        _storageId = defaultStorageId(res, _storages);
      });
    } on ApiFailure catch (failure) {
      setState(() => _notice = 'хранилища не прочитаны: ${failure.reason}');
    } catch (error) {
      setState(() => _notice = 'хранилища не прочитаны: $error');
    }
  }

  /// «Скачать» карточки: ОДНА строка в очередь тем же `enqueue`, что панель
  /// выделения. Канон после успеха скрывает оверлей и идёт на вкладку
  /// очереди (`switchTab("queue")`, ui_src/app.js:3887-3888): панель очереди
  /// как экран Flutter ещё не построена (шаг W2 дальше - очередь/журнал
  /// панелями), поэтому карточка остаётся открытой, а сводка называется на
  /// месте - расхождение формы названо, молчания нет.
  Future<void> _download() async {
    if (!widget.access.has('queue')) {
      setState(() => _notice = widget.access.denial('queue'));
      return;
    }
    try {
      final res = await widget.facade.call('enqueue', <String, Object?>{
        'ids': <int>[widget.videoId],
        'storage_id': _storageId,
      });
      final refusal = res['error'];
      if (refusal is String && refusal.isNotEmpty) {
        setState(() => _notice = refusal);
        return;
      }
      setState(() => _notice = '$kEnqueuedText${res['queued'] ?? 0}');
    } on ApiFailure catch (failure) {
      setState(() => _notice = failure.reason);
    } catch (error) {
      setState(() => _notice = 'очередь не приняла строку: $error');
    }
  }

  /// «Открыть» - страница сервера `/watch/<id>` в системном браузере. Право
  /// `watch` подписывается до перехода (браузер делает то же по
  /// `routeReason("media")`), отсутствие права называется текстом.
  Future<void> _play() async {
    if (!widget.access.has('watch')) {
      setState(() => _notice = widget.access.denial('watch'));
      return;
    }
    final url = widget.facade.watchUrl(widget.videoId);
    final failure = await widget.opener.open(url);
    setState(() => _notice = failure ?? 'плеер открыт: $url');
  }

  Future<void> _openSource() async {
    final url = _data?['webpage_url'];
    if (url is! String || url.isEmpty) {
      setState(() => _notice = 'адрес площадки не назван - открывать нечего');
      return;
    }
    final failure = await widget.opener.open(url);
    setState(() => _notice = failure ?? 'площадка открыта: $url');
  }

  void _close() => Navigator.of(context).pop();

  Widget _actions(BuildContext context) {
    final buttons = <Widget>[
      if (_downloadVisible) _storageSelect(context),
      if (_downloadVisible) FilledButton(
        focusNode: _downloadNode,
        onPressed: _download,
        child: Text(kCardActions.first.label),
      ),
      if (_playVisible) TextButton(
        focusNode: _playNode,
        onPressed: _play,
        child: const Text('Открыть'),
      ),
      if (_sourceVisible) OutlinedButton(
        focusNode: _sourceNode,
        onPressed: _openSource,
        child: const Text('Открыть на площадке'),
      ),
    ];
    if (buttons.isEmpty) {
      return Text('действий для этого статуса нет: качать нечего, '
          'файла на диске нет', style: Theme.of(context).textTheme.bodySmall);
    }
    return Wrap(
      spacing: kMetrics.gapCta,
      runSpacing: kMetrics.s2,
      alignment: WrapAlignment.end,
      children: buttons,
    );
  }

  Widget _storageSelect(BuildContext context) {
    if (_storages.isEmpty) {
      return Semantics(
        label: kNoStoragesText,
        child: Text(kNoStoragesText, style: Theme.of(context).textTheme.bodySmall),
      );
    }
    return DropdownButton<String>(
      focusNode: _storageNode,
      // Значение по умолчанию то же, что `defaultStorageId`: выбранный id из
      // ответа или первое активное хранилище.
      value: _storages.any((storage) => '${storage['id']}' == _storageId)
          ? _storageId
          : '${_storages.first['id']}',
      items: <DropdownMenuItem<String>>[
        for (final storage in _storages)
          DropdownMenuItem<String>(
            value: '${storage['id']}',
            child: Text(storageLabel(storage)),
          ),
      ],
      onChanged: (value) => setState(() => _storageId = value ?? ''),
    );
  }

  Widget _fields(BuildContext context) {
    final theme = Theme.of(context);
    final fields = cardFieldsOf(_data!);
    // Ряды kv: доля подписи к значению (1:3). В CSS это 170px + 1fr, и
    // 170px лежит вне токен-шкалы - форма meta-grid ложится на волне W3,
    // здесь доля, названная расхождением.
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        for (final field in fields)
          Padding(
            padding: EdgeInsets.only(bottom: kMetrics.s3),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Expanded(
                  // Подпись - muted, как `.kv dt`; значение - с tabular
                  // цифрами, как `.kv dd { font-variant-numeric: var(--nums) }`.
                  flex: 1,
                  child: Text(field.label,
                      style: theme.textTheme.bodySmall?.copyWith(
                          fontFeatures: const <FontFeature>[
                            FontFeature.tabularFigures(),
                          ])),
                ),
                Expanded(
                  flex: 3,
                  child: Text(field.value,
                      style: theme.textTheme.bodySmall?.copyWith(
                          fontFeatures: const <FontFeature>[
                            FontFeature.tabularFigures(),
                          ]),
                      // Длинный путь файла и URL не должны вылезать за
                      // карточку: перенос, как `.kv dd { word-break }`.
                      softWrap: true),
                ),
              ],
            ),
          ),
      ],
    );
  }

  Widget _notBuilt(BuildContext context) {
    final theme = Theme.of(context);
    final lines = <String>[
      ...kCardFieldsNotBuilt.values,
      ...kCardActionsNotBuilt.values,
      ...kCardBlocksNotBuilt.values,
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text('не построено на этом шаге',
            style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.onSurfaceVariant)),
        SizedBox(height: kMetrics.s2),
        for (final line in lines)
          Padding(
            // Живым регионом не помечено: содержимое не меняется само.
            padding: EdgeInsets.only(bottom: kMetrics.s2),
            child: Text(line, style: theme.textTheme.bodySmall),
          ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final title = _data?['title'] is String ? _data!['title'] as String : '';
    final meshStatus = _data?['status'];
    return Dialog(
      // Ширина карточки в CSS - min(720px, 100%): 720px лежит вне
      // токен-шкалы, поэтому здесь ширина по содержимому до доступной
      // ширины маршрута минус insetPadding (--s-6 = padding оверлея).
      // Форма названа расхождением (фиксированная ширина ложится на волне W3).
      backgroundColor: theme.colorScheme.surface,
      insetPadding: EdgeInsets.all(kMetrics.s6),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(kMetrics.r4),
        side: BorderSide(color: theme.colorScheme.outlineVariant),
      ),
      child: Semantics(
        // Имя диалога: в браузере `aria-labelledby="detail-title"` (ui_src/
        // index.html:509-513) - имя карточки это её заголовок, поэтому здесь
        // label контейнера = title, а не «Карточка видео: ...» (иначе AT
        // объявлял бы имя, которого в окне нет).
        container: true,
        label: title.isEmpty ? 'Без названия' : title,
        child: Focus(
          // Escape закрывает карточку - выход по §3.8; публичного
          // обработчика Escape в маршрутах этого SDK нет (найдено чтением
          // widgets.dart), поэтому handler на границе содержимого. Тот же
          // handler ведёт Tab по кругу (ловушка фокуса).
          onKeyEvent: _handleDialogKey,
          child: ConstrainedBox(
            // 86vh в CSS - доля высоты окна; здесь доступная высота маршрута
            // минус insetPadding (форма названа расхождением).
            constraints: BoxConstraints(
              maxHeight: MediaQuery.sizeOf(context).height - kMetrics.s6 * 2,
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Padding(
                  // .card-head 12px 14px: 12px = s4, 14px вне шкалы - s5.
                  padding: EdgeInsets.symmetric(
                      horizontal: kMetrics.s4, vertical: kMetrics.s5),
                  child: Row(
                    children: <Widget>[
                      Expanded(
                        child: Semantics(
                          header: true,
                          child: Text(title.isEmpty ? 'Без названия' : title,
                              style: theme.textTheme.titleMedium?.copyWith(
                                  fontWeight: FontWeight.w700),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis),
                        ),
                      ),
                      Semantics(
                        // Имя выхода: `aria-label="Закрыть"` у `#detail-close`
                        // (ui_src/index.html:513) - то же имя здесь.
                        label: 'Закрыть',
                        button: true,
                        focused: _exitFocused,
                        child: IconButton(
                          focusNode: _exitNode,
                          // Вход диалога - на выход: не-разрушительный дефолт
                          // §3.8 (браузер делает то же в dialogEnter, выбирая
                          // -decline/-later, иначе -close). `autofocus` ведёт
                          // framework: showDialog выставлен requestFocus: false,
                          // чтобы маршрут не фокусировал первый контрол сам.
                          autofocus: true,
                          onPressed: _close,
                          icon: const Icon(Icons.close),
                          tooltip: 'Закрыть',
                        ),
                      ),
                    ],
                  ),
                ),
                Divider(color: theme.colorScheme.outlineVariant),
                Flexible(
                  child: SingleChildScrollView(
                    padding: EdgeInsets.all(kMetrics.s5),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: <Widget>[
                        if (_loading)
                          Text('карточка загружается…',
                              style: theme.textTheme.bodySmall),
                        if (_error != null)
                          Text('Отказ: $_error',
                              style: theme.textTheme.bodySmall?.copyWith(
                                  color: theme.colorScheme.error)),
                        if (!_loading && _error == null && _data != null) ...[
                          Padding(
                            padding: EdgeInsets.only(bottom: kMetrics.s3),
                            child: _actions(context),
                          ),
                          _fields(context),
                          SizedBox(height: kMetrics.s3),
                          _notBuilt(context),
                          if (meshStatus is String &&
                              kMeshStatuses.contains(meshStatus))
                            Padding(
                              padding: EdgeInsets.only(top: kMetrics.s2),
                              child: Text('участие mesh в настройках: экран '
                                  'участников не построен (волна W3), запрос '
                                  'файлу отсюда не запускается',
                                  style: theme.textTheme.bodySmall),
                            ),
                        ],
                        if (_notice != null)
                          Padding(
                            padding: EdgeInsets.only(top: kMetrics.s3),
                            child: Semantics(
                              // Живый регион: ответ на действие читается
                              // без фокуса (эквивалент aria-live у тоста).
                              liveRegion: true,
                              container: true,
                              label: 'ответ действия: $_notice',
                              child: Semantics(
                                excludeSemantics: true,
                                child: Text(_notice!,
                                    style: theme.textTheme.bodySmall?.copyWith(
                                        color: theme.colorScheme.error)),
                              ),
                            ),
                          ),
                      ],
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
}
