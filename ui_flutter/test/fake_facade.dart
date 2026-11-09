/// Подмена фасада для тестов экрана: та же форма ответа, что app/repo.py
/// list_videos (`{"total","rows"}`), app/gui.py get_initial (`settings`),
/// save_setting, get_video (`video_detail`), storages_list, enqueue и GET
/// /whoami (`{"ok","paired","label","scopes"}`). Записывает проводку: аргумент
/// `order` каждого вызова list_videos, ключ/значение каждого save_setting и
/// аргумент каждого enqueue - тест сверяет запросы клиента с канонной формой,
/// а не «по ощущению».
///
/// Шаг 4 добавляет массовые цели в той же форме ответа бэкенда
/// (app/gui.py: bulk_download/bulk_tags/bulk_rating/bulk_collection/
/// bulk_remove_rows/save_fields/collections_list): сводки `total`/`queued`/
/// `skipped`/`reasons`/`updated`/`gone`/`added`/`removed`/`collection_name`,
/// отказ без `confirm` («Нужно явное подтверждение удаления» - app/gui.py:932)
/// и отказ с `adult_confirm` (текст _bulk_adult app/gui.py:772-775). Каждый
/// bulk-вызов пишется в `bulkRequests` именем и телом: тест сверяет тела
/// проводок, а не «по ощущению».
///
/// Шаг 4б добавляет обложки плиток в той же форме ответа бэкенда
/// (app/gui.py:3196-3241): `get_thumbs` -> `{"thumbs": {id: data-URI}}` с
/// лимитом 100 ид (отказ текстом бэка) и `get_thumb` ->
/// `{"ok":true,"data":...}` / `{"ok":false,"reason":...}`; вызовы `get_thumbs`
/// пишутся в `thumbRequests` списком id - тест сверяет батч и коалесинг.

library;

import 'dart:async';
import 'dart:convert';

import 'package:omnistash_ui/access.dart';
import 'package:omnistash_ui/api_client.dart';
import 'package:omnistash_ui/library_view.dart';
import 'package:omnistash_ui/player.dart';

/// Права ключа по умолчанию: read+watch+queue - карточка может и смотреть, и
/// ставить в очередь; тесты отказа выставляют `accessRefusal` или пустой
/// список явно.
const List<String> kFakeScopes = <String>['read', 'watch', 'queue'];

/// Права для тестов панели выделения: плюс `mark` и `sources` (цели
/// «Пометить…», папка и удаление строк под ними - тот же SCOPE_OF бэка).
const List<String> kFakeBulkScopes =
    <String>['read', 'watch', 'queue', 'mark', 'sources'];

/// Отказ гейта 18+ - тот же текст и поле, что app/gui.py: _bulk_adult
/// (772-775): бэкенд отказывает, окно (браузер) спрашивает и повторяет.
const String kAdultGateText =
    'Группа содержит строки взрослой площадки: подтвердите, что вам есть 18 '
    'лет и вы несёте ответственность за скачиваемое';

/// Отказ бэка на удаление без явной галки - тот же текст app/gui.py:932.
const String kConfirmRefusalText = 'Нужно явное подтверждение удаления';

class FakeFacade implements Facade {
  FakeFacade({
    this.theme = 'coolors',
    this.fontSize = 13.0,
    this.total = 0,
    this.pages = const <List<Map<String, Object?>>>[],
    this.refusal,
    this.logs = const <String>['мок: запись журнала'],
    this.sortCol = '',
    this.sortDir = 'desc',
    this.settingsExtra = const <String, Object?>{},
    this.persistError,
    this.persistThrow = false,
    this.scopes = kFakeScopes,
    this.accessRefusal,
    this.video,
    this.videoRefusal,
    this.videoAbsent = false,
    this.storages = const <Map<String, Object?>>[],
    this.defaultStorage = '',
    this.storagesRefusal,
    this.queued = 1,
    this.enqueueRefusal,
    this.bulkQueued,
    this.bulkSkipped = 0,
    this.bulkReasons = const <String, Object>{},
    this.bulkUpdated,
    this.bulkGone = 0,
    this.bulkAdded,
    this.bulkRemoved,
    this.bulkRemovedRows,
    this.bulkCollectionName = 'Папка мок',
    this.bulkRefusal,
    this.adultConfirm = false,
    this.collections = const <Map<String, Object?>>[],
    this.collectionsRefusal,
    this.thumbUris = const <String, String>{},
    this.thumbsRefusal,
    this.thumbReason,
    this.thumbPending = false,
  });

  final String theme;
  final double fontSize;
  final int total;
  final List<List<Map<String, Object?>>> pages;
  final String? refusal;
  final List<String> logs;
  final String sortCol;
  final String sortDir;
  final Map<String, Object?> settingsExtra;

  /// Отказ save_setting внутри 200 (поле `error`) и отказ транспортом
  /// (403 по скоупу) - оба названы, разные формы приходит сервер.
  final String? persistError;
  final bool persistThrow;

  /// Скоупы ключа (GET /whoami) и причина, по которой они не прочитаны.
  final List<String> scopes;
  final String? accessRefusal;

  /// Карточка видео (`get_video` -> `repo.video_detail`), отказ внутри 200 и
  /// отсутствие строки (`videoAbsent` - сервер отвечает null).
  Map<String, Object?>? video;
  final String? videoRefusal;
  final bool videoAbsent;

  /// Хранилища (`storages_list`) и выбранное по умолчанию; отказ - текст.
  final List<Map<String, Object?>> storages;
  final String defaultStorage;
  final String? storagesRefusal;

  /// Ответ `enqueue`: сколько строк поставлено и отказ (например хранилище
  /// отвязано - app/gui.py: «Хранилище не найдено или отвязано»).
  final int queued;
  final String? enqueueRefusal;

  /// Сводки массовых целей (ключи ответов app/gui.py: _bulk_apply):
  /// выкачка queued/skipped/reasons, пометки updated/gone, папка
  /// added/removed/gone с collection_name, удаление removed/gone.
  /// `bulkQueued`/`bulkUpdated`/`bulkAdded`/`bulkRemoved`/`bulkRemovedRows`
  /// null - серверная форма по умолчанию: ids минус skip / все ids.
  final int? bulkQueued;
  final int bulkSkipped;
  final Map<String, Object> bulkReasons;
  final int? bulkUpdated;
  final int bulkGone;
  final int? bulkAdded;
  final int? bulkRemoved;
  final int? bulkRemovedRows;
  final String bulkCollectionName;

  /// Отказ внутри 200 для любой bulk-цели (`error`) и общий гейт 18+:
  /// `adultConfirm` ставит отказ с `adult_confirm` (тот же _bulk_adult).
  final String? bulkRefusal;
  final bool adultConfirm;

  /// Папки (`collections_list` -> repo.collection_list: id, name,
  /// created_at, total) и отказ чтения.
  final List<Map<String, Object?>> collections;
  final String? collectionsRefusal;

  /// Обложки (`get_thumbs`/`get_thumb` -> app/gui.py:3196-3241): id ->
  /// data-URI локального файла обложки. Чего нет в этом наборе, бэкенд
  /// молча не кладёт в ответ (битый или пропавший файл), и плитка показывает
  /// заглушку; для одной обложки отказ называется текстом бэка
  /// (`thumbReason` - «обложка не скачана» app/gui.py:3207).
  final Map<String, String> thumbUris;
  final String? thumbsRefusal;
  final String? thumbReason;

  /// Батч обложек, который не отвечает (мок «ответ ещё не пришёл»): плитка
  /// остаётся в состоянии ожидания - пустая подложка `--surface-2` без
  /// подписи, как канон `img` без src (app.js:3003-3010, 3053-3060).
  final bool thumbPending;

  /// Проводка батчей обложек: список id каждого вызова `get_thumbs` - тест
  /// сверяет размер батча и дробление очереди, а не «по ощущению».
  final List<List<int>> thumbRequests = <List<int>>[];

  final List<String> calls = <String>[];
  final List<Map<String, Object?>?> sortRequests = <Map<String, Object?>?>[];
  final List<Map<String, Object?>> savedSettings = <Map<String, Object?>>[];
  final List<Map<String, Object?>> enqueueRequests = <Map<String, Object?>>[];
  final List<Object?> videoRequests = <Object?>[];

  /// Проводки bulk-целей: имя цели + тело запроса (ids/fields/storage_id/
  /// add/remove/rating/action/collection_id/confirm) - порядок записи равен
  /// порядку вызовов (цепочка «Применить»: bulk_tags, потом bulk_rating).
  final List<Map<String, Object?>> bulkRequests = <Map<String, Object?>>[];

  @override
  Future<Access> access() async {
    calls.add('whoami');
    if (accessRefusal != null) return Access(refusal: accessRefusal!);
    return Access(scopes: scopes);
  }

  /// Тот же путь страницы плеера, что у ApiClient: тест сверяет адрес,
  /// который клиент отправил открывателю.
  @override
  String watchUrl(int videoId) => 'https://fake.test/watch/$videoId';

  /// Сколько ids в проводке группы (сервер считает total именно так -
  /// app/gui.py: len(ids)).
  int _groupSize(Map<String, Object?> request) =>
      (request['ids'] as List? ?? const <Object?>[]).length;

  Map<String, Object?>? _bulkGate(String name, Map<String, Object?> request) {
    bulkRequests.add(<String, Object?>{'name': name, 'request': request});
    if (adultConfirm) {
      return <String, Object?>{'adult_confirm': true, 'error': kAdultGateText};
    }
    if (bulkRefusal != null) return <String, Object?>{'error': bulkRefusal!};
    return null;
  }

  @override
  Future<Map<String, Object?>> call(String name, Object? arg) async {
    calls.add(name);
    switch (name) {
      case 'get_initial':
        return <String, Object?>{
          'settings': <String, Object?>{
            'theme': theme,
            'font_size': fontSize,
            'sort_col': sortCol,
            'sort_dir': sortDir,
            ...settingsExtra,
          },
        };
      case 'list_videos':
        if (refusal != null) {
          return <String, Object?>{'error': refusal!};
        }
        final request = arg as Map<String, Object?>;
        final order = request['order'];
        sortRequests.add(order is Map<String, Object?> ? order : null);
        final offset = request['offset'] as int;
        final index = offset ~/ kPageLimit;
        final rows = index < pages.length
            ? pages[index]
            : const <Map<String, Object?>>[];
        return <String, Object?>{kTotalKey: total, kRowsKey: rows};
      case 'save_setting':
        if (persistThrow) {
          throw ApiFailure('save_setting',
              'сервер ответил 403: не выдан скоуп server_config');
        }
        if (persistError != null) {
          return <String, Object?>{'error': persistError!};
        }
        savedSettings.add(arg as Map<String, Object?>);
        return <String, Object?>{'ok': true};
      case 'poll':
        return <String, Object?>{'logs': logs, 'rev': logs.length};
      case 'get_video':
        videoRequests.add(arg);
        if (videoAbsent) {
          // app/gui.py: get_video -> repo.video_detail -> None: тело null.
          throw ApiFailure('get_video',
              'ответ null - данных нет (цель не вернула объект)');
        }
        if (videoRefusal != null) {
          return <String, Object?>{'error': videoRefusal!};
        }
        if (video == null) {
          return <String, Object?>{'error': 'цель не подменена в тесте: get_video'};
        }
        return video!;
      case 'storages_list':
        if (storagesRefusal != null) {
          return <String, Object?>{'error': storagesRefusal!};
        }
        return <String, Object?>{
          'storages': storages,
          'default_storage_id': defaultStorage,
        };
      case 'enqueue':
        final request = arg as Map<String, Object?>;
        enqueueRequests.add(request);
        if (enqueueRefusal != null) {
          return <String, Object?>{'error': enqueueRefusal!};
        }
        return <String, Object?>{
          'queued': queued,
          'storage_id': request['storage_id'],
        };
      case 'collections_list':
        if (collectionsRefusal != null) {
          return <String, Object?>{'error': collectionsRefusal!};
        }
        return <String, Object?>{'ok': true, 'collections': collections};
      case 'save_fields':
        // Ответ тот же, что app/gui.py:3169-3213: {"ok","updated"}.
        final request = arg as Map<String, Object?>;
        bulkRequests.add(<String, Object?>{'name': name, 'request': request});
        if (bulkRefusal != null) return <String, Object?>{'error': bulkRefusal!};
        final count = _groupSize(request);
        return <String, Object?>{
          'ok': true,
          'updated': bulkUpdated ?? count,
          'gone': bulkGone,
        };
      case 'bulk_download':
        // Форма ответа app/gui.py:825-826: ok/total + сводка queued/skipped/
        // reasons + storage_id.
        final request = arg as Map<String, Object?>;
        final gate = _bulkGate(name, request);
        if (gate != null) return gate;
        final count = _groupSize(request);
        return <String, Object?>{
          'ok': true,
          'total': count,
          'queued': bulkQueued ?? count - bulkSkipped,
          'skipped': bulkSkipped,
          'reasons': bulkReasons,
          'storage_id': request['storage_id'],
        };
      case 'bulk_tags':
        // app/gui.py:855: ok/total + updated/gone.
        final request = arg as Map<String, Object?>;
        final gate = _bulkGate(name, request);
        if (gate != null) return gate;
        final count = _groupSize(request);
        return <String, Object?>{
          'ok': true,
          'total': count,
          'updated': bulkUpdated ?? count,
          'gone': bulkGone,
        };
      case 'bulk_rating':
        // app/gui.py:884: ok/total/rating + updated/gone.
        final request = arg as Map<String, Object?>;
        final gate = _bulkGate(name, request);
        if (gate != null) return gate;
        final count = _groupSize(request);
        return <String, Object?>{
          'ok': true,
          'total': count,
          'rating': request['rating'],
          'updated': bulkUpdated ?? count,
          'gone': bulkGone,
        };
      case 'bulk_collection':
        // app/gui.py:898-906: отказ по действию, по отсутствующей и по
        // несуществующей папке - тексты сервера, клиент их не выдумывает.
        final request = arg as Map<String, Object?>;
        final gate = _bulkGate(name, request);
        if (gate != null) return gate;
        final action = request['action'];
        if (action != 'add' && action != 'remove') {
          return <String, Object?>{'error': 'Нужно действие: add или remove'};
        }
        final cid = request['collection_id'] is num
            ? (request['collection_id'] as num).toInt() : 0;
        if (cid == 0) return <String, Object?>{'error': 'Не указана коллекция'};
        if (!collections.any((c) => (c['id'] as num).toInt() == cid)) {
          return <String, Object?>{'error': 'Коллекция не найдена'};
        }
        final count = _groupSize(request);
        return <String, Object?>{
          'ok': true,
          'total': count,
          'action': action,
          'collection_id': cid,
          'collection_name': bulkCollectionName,
          'added': bulkAdded ?? count,
          'removed': bulkRemoved ?? count,
          'gone': bulkGone,
        };
      case 'bulk_remove_rows':
        // Отказ бэка без явной галки - тот же путь app/gui.py:931-932:
        // UI без подтверждения не отправляет цель, но проводка записана и
        // отказ назван текстом сервера (клиент не угадывает причину).
        final request = arg as Map<String, Object?>;
        bulkRequests.add(<String, Object?>{'name': name, 'request': request});
        if (request['confirm'] != true) {
          return <String, Object?>{'error': kConfirmRefusalText};
        }
        if (adultConfirm) {
          return <String, Object?>{'adult_confirm': true, 'error': kAdultGateText};
        }
        if (bulkRefusal != null) return <String, Object?>{'error': bulkRefusal!};
        final count = _groupSize(request);
        return <String, Object?>{
          'ok': true,
          'total': count,
          'files_kept': true,
          'removed': bulkRemovedRows ?? count,
          'gone': bulkGone,
        };
      case 'get_thumbs':
        // Форма ответа app/gui.py:3214-3241: {"thumbs": {id: data-URI}};
        // сверх лимита бэкенд отказывает своим текстом (3232-3234), битые и
        // пропавшие файлы молча не попадают в ответ - заглушка называется
        // подписью канона, а не выдуманным текстом.
        final request = arg as Map<String, Object?>;
        final List<int> ids = (request['ids'] as List? ?? const <Object?>[])
            .map((Object? v) => v is num ? v.toInt() : 0).toList();
        thumbRequests.add(ids);
        if (ids.length > 100) {
          // Тот же текст лимита бэкенда (app/gui.py:3233-3234).
          return <String, Object?>{'error': 'слишком много id за раз: '
              '${ids.length} (максимум 100)'};
        }
        if (thumbsRefusal != null) return <String, Object?>{'error': thumbsRefusal!};
        if (thumbPending) {
          // Мок «ответ ещё не пришёл»: батч висит, плитка остаётся в
          // ожидании (канон img без src - пустая подложка без подписи).
          return Completer<Map<String, Object?>>().future;
        }
        final Map<String, Object?> thumbs = <String, Object?>{};
        for (final int id in ids) {
          final String? uri = thumbUris['$id'];
          if (uri != null) thumbs['$id'] = uri;
        }
        return <String, Object?>{'thumbs': thumbs};
      case 'get_thumb':
        // app/gui.py:3196-3212: {"ok":true,"data":uri} либо {"ok":false,
        // "reason":...} - текст бэкенда, клиент его не выдумывает.
        final request = arg as Map<String, Object?>;
        final int id = request['id'] is num ? (request['id'] as num).toInt() : 0;
        final String? uri = thumbUris['$id'];
        if (uri == null) {
          return <String, Object?>{'ok': false,
            'reason': thumbReason ?? 'обложка не скачана'};
        }
        return <String, Object?>{'ok': true, 'data': uri};
      default:
        return <String, Object?>{'error': 'цель не подменена в тесте: $name'};
    }
  }
}

/// Строка списка - форма `list_videos` (app/repo.py SELECT ...): ячейки
/// грида читают size (сумма целых дорожек video), счётчики площадки на
/// момент синка, даты iso, теги площадки и отметку «просмотрено».
Map<String, Object?> row(int id, {
  String status = 'known',
  String label = 'в индексе',
  Object? size = 1048576,
  Object? viewCount = 1234,
  Object? likeCount = 5,
  Object? commentCount = 2,
  String? uploadedAt = '2026-10-25T10:00:00+00:00',
  String? syncedAt = '2026-10-26T08:12:00',
  List<String> platformTags = const <String>['lofi', 'live'],
  Object userRating = 0,
  String? watchedAt,
  String mediaKind = 'video',
  String? mediaLabel,
  bool? hasThumb,
}) =>
    <String, Object?>{
      'id': id,
      'title': 'Клип $id',
      'status': status,
      'status_label': label,
      'channel': 'Канал ${id % 3}',
      'duration_s': 125 + id,
      'media_kind': mediaKind,
      'media_label': mediaLabel ?? (mediaKind == 'audio' ? 'аудио' : 'видео'),
      'user_rating': userRating,
      // Отметка «обложка есть» - тот же источник, что app/repo.py:1453
      // (`row["has_thumb"] = row["id"] in thumbs`); по умолчанию чередование
      // из прежних фиктур шага 3 оставлено как есть.
      'has_thumb': hasThumb ?? (id % 2 == 0),
      'uploaded_at': uploadedAt,
      'size': size,
      'view_count': viewCount,
      'like_count': likeCount,
      'comment_count': commentCount,
      'synced_at': syncedAt,
      'platform_tags': platformTags,
      'watched_at': watchedAt,
    };

/// Папка из `collections_list` (app/repo.py: collection_list SELECT id, name,
/// created_at, COUNT(... ) AS total).
Map<String, Object?> collection(String id, String name, {int total = 7}) =>
    <String, Object?>{
      'id': int.parse(id),
      'name': name,
      'created_at': '2026-10-20T07:00:00',
      'total': total,
    };

/// Подмена открывателя адресов: пишет URL, который клиент собрал, и может
/// назвать отказ (браузер не запущен) - тест сверяет адрес `/watch/<id>` и
/// текст причины, а не «по ощущению».
class FakeOpener implements UrlOpener {
  FakeOpener({this.failure});

  /// Текст отказа, который возвращает open (null - адрес открыт).
  final String? failure;

  final List<String> opened = <String>[];

  @override
  Future<String?> open(String url) async {
    opened.add(url);
    return failure;
  }
}

/// Строка хранилища из `storages_list` (app/gui.py: all_storages): id,
/// подпись, активность, включённость, доступность носителя.
Map<String, Object?> storage(String id, {
  String label = 'Основное',
  bool active = true,
  bool enabled = true,
  bool available = true,
}) =>
    <String, Object?>{
      'id': id,
      'label': label,
      'status': active ? 'active' : 'detached',
      'enabled': enabled,
      'available': available,
    };

/// Карточка видео - форма `app/repo.py: video_detail`: колонки строки,
/// `files[]` (kind, path, missing, format_json строкой) и `playlists[]`.
Map<String, Object?> videoFixture(
  int id, {
  String status = 'downloaded',
  String label = 'скачано',
  String mediaKind = 'video',
  String? uploadedAt = '2026-10-25T10:00:00+00:00',
  Object? duration = 3720,
  Object? viewCount = 1234567,
  Object? likeCount = 42,
  Object? commentCount = 7,
  String? webpageUrl = 'https://www.youtube.com/watch?v=abc',
  String? origin = 'site',
  List<Map<String, Object?>> files = const <Map<String, Object?>>[],
  List<Map<String, Object?>> playlists = const <Map<String, Object?>>[],
}) =>
    <String, Object?>{
      'id': id,
      'key': 'youtube|UCaaaa|abc',
      'title': 'Клип $id',
      'status': status,
      'status_label': label,
      'media_kind': mediaKind,
      'channel': 'Канал $id',
      'uploaded_at': uploadedAt,
      'duration_s': duration,
      'view_count': viewCount,
      'like_count': likeCount,
      'comment_count': commentCount,
      'synced_at': '2026-10-26T08:12:00',
      'origin': origin,
      'webpage_url': webpageUrl,
      'first_seen_at': '2026-10-20T07:00:00',
      'downloaded_at': status == 'downloaded' ? '2026-10-25T12:00:00' : null,
      'updated_at': '2026-11-01T09:30:00',
      'files': files,
      'playlists': playlists,
      'raw': <String, Object?>{},
    };

/// Файл дорожки как его отдаёт `files` (format_json - строка JSON, не объект:
/// app/repo.py SELECT ... format_json).
Map<String, Object?> videoFile(String path, {
  bool missing = false,
  String? formatJson,
  String kind = 'video',
}) =>
    <String, Object?>{
      'id': 1,
      'kind': kind,
      'path': path,
      'size': 1048576,
      'hash': 'abc123',
      'mtime': 1760000000,
      'missing': missing,
      'format_json': formatJson,
    };

/// Метки аудио-файла из ffprobe (ключи app/audio.py: meta["probe"], ...).
/// Текст - `jsonEncode`: рантайм бэкенда пишет format_json той же проводкой
/// (app/repo.py хранит строку JSON), тест не выдумывает свой сериализатор.
String audioFormatJson({
  String probe = 'ffprobe',
  Object? duration = 254.3,
  Object? bitrate = 320000,
  Object? sampleRate = 44100,
  Object? channels = 2,
  Object? codec = 'mp3',
  String? artist = 'Меломан',
  String? album = 'Ромашка',
  String? genre = 'Электронная',
  String? date = '2026',
  String? track = '3',
  String? tagsScheme = 'ID3v2',
  String? probeNote,
}) =>
    jsonEncode(<String, Object?>{
      'media': 'audio',
      'format': '.mp3',
      'probe': probe,
      'probe_note': probeNote,
      'duration_s': duration,
      'bitrate_bps': bitrate,
      'sample_rate_hz': sampleRate,
      'channels': channels,
      'codec': codec,
      'artist': artist,
      'album': album,
      'genre': genre,
      'date': date,
      'track': track,
      'tags_scheme': tagsScheme,
    });
