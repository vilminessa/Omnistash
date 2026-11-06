/// Подмена фасада для тестов экрана: та же форма ответа, что app/repo.py
/// list_videos (`{"total","rows"}`), app/gui.py get_initial (`settings`) и
/// save_setting. Записывает проводку: аргумент `order` каждого вызова
/// list_videos и ключ/значение каждого save_setting - тест сверяет запросы
/// клиента с канонной формой, а не «по ощущению».

library;

import 'package:omnistash_ui/api_client.dart';
import 'package:omnistash_ui/library_view.dart';

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

  final List<String> calls = <String>[];
  final List<Map<String, Object?>?> sortRequests = <Map<String, Object?>?>[];
  final List<Map<String, Object?>> savedSettings = <Map<String, Object?>>[];

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
