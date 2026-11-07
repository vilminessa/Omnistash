/// Подмена фасада для тестов экрана: та же форма ответа, что app/repo.py
/// list_videos (`{"total","rows"}`), app/gui.py get_initial (`settings`),
/// save_setting, get_video (`video_detail`), storages_list, enqueue и GET
/// /whoami (`{"ok","paired","label","scopes"}`). Записывает проводку: аргумент
/// `order` каждого вызова list_videos, ключ/значение каждого save_setting и
/// аргумент каждого enqueue - тест сверяет запросы клиента с канонной формой,
/// а не «по ощущению».

library;

import 'dart:convert';

import 'package:omnistash_ui/access.dart';
import 'package:omnistash_ui/api_client.dart';
import 'package:omnistash_ui/library_view.dart';
import 'package:omnistash_ui/player.dart';

/// Права ключа по умолчанию: read+watch+queue - карточка может и смотреть, и
/// ставить в очередь; тесты отказа выставляют `accessRefusal` или пустой
/// список явно.
const List<String> kFakeScopes = <String>['read', 'watch', 'queue'];

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

  final List<String> calls = <String>[];
  final List<Map<String, Object?>?> sortRequests = <Map<String, Object?>?>[];
  final List<Map<String, Object?>> savedSettings = <Map<String, Object?>>[];
  final List<Map<String, Object?>> enqueueRequests = <Map<String, Object?>>[];
  final List<Object?> videoRequests = <Object?>[];

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
      default:
        return <String, Object?>{'error': 'цель не подменена в тесте: $name'};
    }
  }
}

Map<String, Object?> row(int id, {
  String status = 'known',
  String label = 'в индексе',
}) =>
    <String, Object?>{
      'id': id,
      'title': 'Клип $id',
      'status': status,
      'status_label': label,
      'channel': 'Канал ${id % 3}',
      'duration_s': 125 + id,
      'media_label': 'видео',
      'user_rating': 0,
      'has_thumb': id % 2 == 0,
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
