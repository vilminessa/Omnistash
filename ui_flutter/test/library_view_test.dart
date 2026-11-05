import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omnistash_ui/api_client.dart';
import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/library_view.dart';
import 'package:omnistash_ui/theme.dart';

/// Подмена фасада для теста экрана: та же форма ответа, что app/repo.py
/// list_videos ({"total","rows"}) и app/gui.py get_initial (settings).
class FakeFacade implements Facade {
  FakeFacade({
    this.theme = 'coolors',
    this.fontSize = 13.0,
    this.total = 0,
    this.pages = const <List<Map<String, Object?>>>[],
    this.refusal,
    this.logs = const <String>['мок: запись журнала'],
  });

  final String theme;
  final double fontSize;
  final int total;
  final List<List<Map<String, Object?>>> pages;
  final String? refusal;
  final List<String> logs;
  final List<String> calls = <String>[];

  @override
  Future<Map<String, Object?>> call(String name, Object? arg) async {
    calls.add(name);
    switch (name) {
      case 'get_initial':
        return <String, Object?>{
          'settings': <String, Object?>{'theme': theme, 'font_size': fontSize},
        };
      case 'list_videos':
        if (refusal != null) {
          return <String, Object?>{'error': refusal!};
        }
        final offset = (arg as Map<String, Object?>)['offset'] as int;
        final index = offset ~/ kPageLimit;
        final rows = index < pages.length
            ? pages[index]
            : const <Map<String, Object?>>[];
        return <String, Object?>{kTotalKey: total, kRowsKey: rows};
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

Future<void> _pump(WidgetTester tester) async {
  // Полоса состояния ходит к poll каждые kPollIntervalMs: pumpAndSettle не
  // завершится с активным периодическим таймером, поэтому тест снимает кадры
  // фиксированными длительностями.
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 120));
  await tester.pump(Duration(milliseconds: kPollIntervalMs));
}

/// Счётчик показанных строк в шапке: «N из total» (табличные цифры).
String _counter(WidgetTester tester) =>
    tester.widget<Text>(find.textContaining(' из ')).data ?? '';

void main() {
  testWidgets('строки из фасада показаны с подписью статуса', (tester) async {
    final facade = FakeFacade(total: 3, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0), row(1), row(2)],
    ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    expect(find.text('Клип 0'), findsOneWidget);
    expect(find.text('Клип 2'), findsOneWidget);
    expect(find.text('в индексе'), findsNWidgets(3),
        reason: 'подпись статуса из app/repo.py STATUS_LABELS не показана');
    expect(facade.calls, contains('list_videos'),
        reason: 'экран не вызвал list_videos - список пришёл не из фасада');
    expect(_counter(tester), '3 из 3',
        reason: 'счётчик показанных строк не равен total из ответа');
  });

  testWidgets('тема приходит из настройки бэкенда, а не из литерала',
      (tester) async {
    final facade = FakeFacade(theme: 'coolors', total: 1,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    expect(facade.calls, contains('get_initial'),
        reason: 'экран не прочитал get_initial: тема настройки не применена');
    expect(tester.widget<Scaffold>(find.byType(Scaffold).first).backgroundColor,
        Color(role('coolors', 'bg')),
        reason: 'подложка окна не из роли bg выбранной темы');
  });

  testWidgets('цвет статуса - роль из токенов темы', (tester) async {
    final facade = FakeFacade(theme: 'coolors', total: 1,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано')],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    final style = tester.widget<Text>(find.text('скачано')).style;
    expect(style?.color, Color(role('coolors', 'ok')),
        reason: 'статус downloaded не красится ролью ok из токенов темы');
  });

  testWidgets('высота строки списка - формула токена, не литерал',
      (tester) async {
    final facade = FakeFacade(fontSize: 22.0, total: 1,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    final list = tester.widget<ListView>(find.byType(ListView).first);
    expect(list.itemExtent, rowHeight(22.0),
        reason: 'ряд не равен формуле max(rowMin, fontSize * rowFactor)');
    expect(list.itemExtent, greaterThan(rowHeight(13.0)),
        reason: 'крупный текст не поднял высоту ряда: подписи обрежутся');
  });

  testWidgets('размер текста из настройки ограничивается границами реестра',
      (tester) async {
    final facade = FakeFacade(fontSize: 90.0, total: 1,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    final theme = tester.widget<MaterialApp>(find.byType(MaterialApp)).theme!;
    final identity = theme.extension<ThemeIdentity>()!;
    expect(identity.fontSizePx, kFontBounds.sizeMax,
        reason: 'битый размер текста из настройки не ограничен kFontBounds');
    expect(identity.id, 'coolors',
        reason: 'id темы не положен в контекст: роли читаются не из той палитры');
  });

  testWidgets('отказ фасада внутри 200 назван текстом, а не молчит',
      (tester) async {
    final facade = FakeFacade(refusal: 'Ничего не выбрано', total: 0);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    expect(find.textContaining('Отказ: Ничего не выбрано'), findsOneWidget,
        reason: 'экран скрыл отказ фасада: список выглядел пустым успехом');
    expect(find.text('Клип 0'), findsNothing,
        reason: 'при отказе строки не должны рисоваться из подменённых данных');
  });

  testWidgets('вторая страница грузится вызовом с offset', (tester) async {
    final facade = FakeFacade(total: 250, pages: <List<Map<String, Object?>>>[
      List<Map<String, Object?>>.generate(kPageLimit, (i) => row(i)),
      List<Map<String, Object?>>.generate(250 - kPageLimit,
          (i) => row(kPageLimit + i)),
    ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    expect(_counter(tester), '$kPageLimit из 250',
        reason: 'первая страница не равна kPageLimit: PAGE из app.js не применён');
    // Кнопка следующей страницы строится только когда попадает на экран
    // (ListView.builder виртуализирует): тест докручивает список до неё.
    await tester.scrollUntilVisible(find.textContaining('Ещё ('), 200.0,
        scrollable: find.byType(Scrollable).first);
    expect(find.text('Ещё (${250 - kPageLimit})'), findsOneWidget,
        reason: 'total больше loaded: экрана без пути к следующей странице нет');
    await tester.tap(find.text('Ещё (${250 - kPageLimit})'));
    await _pump(tester);

    expect(_counter(tester), '250 из 250',
        reason: 'вторая страница не добавлена - offset не передан');
    expect(facade.calls.where((name) => name == 'list_videos').length, 2,
        reason: 'пагинация не сделала второй вызов list_videos');
  });

  testWidgets('семантика строки называет её целиком и выбор', (tester) async {
    final facade = FakeFacade(total: 2, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0), row(1)],
    ]);
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    expect(find.bySemanticsLabel('Клип 0, в индексе, Канал 0, 2:05, видео, '
        'обложка есть'), findsOneWidget,
        reason: 'строка не названа целиком - AT передаёт только имя');
    await tester.tap(find.text('Клип 0'));
    await _pump(tester);

    expect(tester.widget<ListTile>(find.byType(ListTile).first).selected, true,
        reason: 'выбор строки не передан в ListTile.selected');
    final rowFlags = tester.getSemantics(find.bySemanticsLabel(
        'Клип 0, в индексе, Канал 0, 2:05, видео, обложка есть')).flagsCollection;
    // Tristate не экспортируется публично, поэтому выбор читается
    // преобразованием в bool - тот же приём, что в самом Flutter.
    expect(rowFlags.isSelected.toBoolOrNull(), true,
        reason: 'Semantics.selected не выставлен - AT не скажет «выбрано»');
    final liveLabel = 'состояние: мок: запись журнала';
    expect(find.bySemanticsLabel(liveLabel), findsOneWidget,
        reason: 'регион состояния не назван текстом журнала - AT не объявит изменение');
    final liveFlags = tester.getSemantics(find.bySemanticsLabel(liveLabel)).flagsCollection;
    expect(liveFlags.isLiveRegion, true,
        reason: 'полоса состояния не живый регион (эквивалент aria-live у журнала)');
    handle.dispose();
  });

  testWidgets('без сервера экран называет причину, а не пустой список',
      (tester) async {
    await tester.pumpWidget(const OmnistashApp(facade: null));
    await _pump(tester);

    expect(find.textContaining('Сервер не запущен'), findsOneWidget,
        reason: 'отсутствие слушателя названо, а не показано как пустая библиотека');
    expect(find.text('Клип 0'), findsNothing,
        reason: 'без фасада строки не должны появляться из подменённых данных');
    expect(find.textContaining('тема:'), findsOneWidget,
        reason: 'экран не называет, какая палитра применена без сервера');
  });
}
