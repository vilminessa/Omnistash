import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/library_view.dart';
import 'package:omnistash_ui/theme.dart';

import 'fake_facade.dart';

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
    // (ListView.builder виртуализирует): тест прокручивает список drag-ом.
    await tester.drag(find.byType(ListView), const Offset(0.0, -2500.0));
    await _pump(tester);
    await tester.drag(find.byType(ListView), const Offset(0.0, -2500.0));
    await _pump(tester);
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

  testWidgets('сортировка: новая колонка - desc, та же - разворот, '
      'первая страница перезагружается', (tester) async {
    final facade = FakeFacade(total: 2, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0), row(1)],
    ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    // Порядок по умолчанию: order:null - сервер держит uploaded_at DESC,
    // как браузерный ensureRange (ui_src/app.js:3308).
    expect(facade.sortRequests, <Map<String, Object?>?>[null],
        reason: 'без выбранной колонки клиент должен послать order: null');

    await tester.tap(find.text('Название'));
    await _pump(tester);
    expect(facade.sortRequests.last, <String, Object?>{'col': 'title', 'dir': 'desc'},
        reason: 'новая колонка не началась с desc (toggleSort из app.js:3256)');
    expect(find.text('Название ▼'), findsOneWidget,
        reason: 'активная колонка не названа стрелкой - AT читает только подпись');
    expect(facade.calls.where((name) => name == 'list_videos').length, 2,
        reason: 'страницы под новым порядком не перезагружены');
    expect(facade.savedSettings.map((entry) => entry['key']),
        <String>['sort_col', 'sort_dir'],
        reason: 'порядок не сохранён теми же ключами настроек, что браузер');
    expect(facade.savedSettings.map((entry) => entry['value']),
        <String>['title', 'desc']);

    await tester.tap(find.text('Название ▼'));
    await _pump(tester);
    expect(facade.sortRequests.last, <String, Object?>{'col': 'title', 'dir': 'asc'},
        reason: 'та же колонка не развернула направление');
    expect(find.text('Название ▲'), findsOneWidget);

    await tester.tap(find.text('Статус'));
    await _pump(tester);
    expect(facade.sortRequests.last, <String, Object?>{'col': 'status', 'dir': 'desc'},
        reason: 'смена колонки должна начать с desc, а не наследовать asc');
    expect(find.text('Статус ▼'), findsOneWidget);
    expect(find.text('Название ▲'), findsNothing,
        reason: 'прежняя активная колонка осталась подсвечена');
  });

  testWidgets('начальный порядок приходит из настроек get_initial',
      (tester) async {
    final facade = FakeFacade(total: 1, sortCol: 'date', sortDir: 'asc',
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    expect(facade.calls.where((name) => name == 'list_videos').length, 2,
        reason: 'сохранённый порядок из настроек не перезагрузил первую страницу '
            '(syncSortAndCols в ui_src/app.js:3273)');
    expect(facade.sortRequests.last, <String, Object?>{'col': 'date', 'dir': 'asc'},
        reason: 'sort_col/sort_dir из settings не применены к запросу');
    expect(find.text('Дата ▲'), findsOneWidget,
        reason: 'колонка из сохранённой настройки не подсвечена');
    expect(facade.savedSettings, isEmpty,
        reason: 'экран не должен сохранять порядок без клика пользователя');
  });

  testWidgets('гейт настройки col_views=false скрывает колонку (colVisible)',
      (tester) async {
    final hidden = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'col_views': false},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: hidden));
    await _pump(tester);
    expect(find.text('Просмотры'), findsNothing,
        reason: 'col_views=false не скрыл колонку: браузер colVisible !== false');

    final shown = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'col_views': true},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: shown));
    await _pump(tester);
    expect(find.text('Просмотры'), findsOneWidget,
        reason: 'колонка со включённой настройкой исчезла молча');
  });

  testWidgets('отказ save_setting назван текстом, порядок остаётся применён',
      (tester) async {
    // Отказ внутри 200 (поле error) и отказ транспортом (403 по скоупу) -
    // обе формы называются, локальный порядок применён к запросу.
    final refused = FakeFacade(total: 2,
        persistError: 'не выдан скоуп server_config',
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0), row(1)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: refused));
    await _pump(tester);
    await tester.tap(find.text('Название'));
    await _pump(tester);

    expect(find.textContaining('порядок применён к списку, но не сохранён'),
        findsOneWidget,
        reason: 'отказ сохранения порядка скрыт: экран выглядел сохранённым');
    expect(find.textContaining('не выдан скоуп server_config'), findsOneWidget,
        reason: 'причина отказа не названа текстом сервера');
    expect(refused.sortRequests.last, <String, Object?>{'col': 'title', 'dir': 'desc'},
        reason: 'отказ сохранения не должен отменять применённый порядок запроса');
    expect(find.text('Название ▼'), findsOneWidget);

    final thrown = FakeFacade(total: 2, persistThrow: true,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0), row(1)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: thrown));
    await _pump(tester);
    await tester.tap(find.text('Название'));
    await _pump(tester);

    expect(find.textContaining('сервер ответил 403'), findsOneWidget,
        reason: 'транспортный отказ 403 не назван: молчание вместо причины');
    expect(thrown.sortRequests.last, <String, Object?>{'col': 'title', 'dir': 'desc'},
        reason: 'транспортный отказ сохранения не должен отменять порядок запроса');
  });

  testWidgets('заголовок колонки - роль header, направление в подписи',
      (tester) async {
    final facade = FakeFacade(total: 1, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0)],
    ]);
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);
    await tester.tap(find.text('Время'));
    await _pump(tester);

    // Эквивалент role="columnheader" + aria-sort из renderGridHead
    // (ui_src/app.js:3230): флаг header есть, отдельного флага aria-sort в
    // Flutter нет - направление читается текстом подписи со стрелкой.
    final flags = tester.getSemantics(find.bySemanticsLabel('Время ▼')).flagsCollection;
    expect(flags.isHeader, true,
        reason: 'колонка не названа заголовком - AT не объявит columnheader');
    expect(flags.isButton, true,
        reason: 'колонка не активна как кнопка - с клавиатуры не переключить');
    handle.dispose();
  });
}
