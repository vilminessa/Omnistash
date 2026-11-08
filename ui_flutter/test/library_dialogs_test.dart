import 'package:flutter_test/flutter_test.dart';
import 'fake_facade.dart';
import 'harness.dart';
import 'package:flutter/material.dart';
import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/bulk_bar.dart';

void main() {
  testWidgets('«Операции…»: теги и рейтинг по очереди, сводка « · »; пустой набор отказан',
      (tester) async {
    final facade = FakeFacade(total: 2, scopes: kFakeBulkScopes,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0), row(1)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);
    final boxes = find.descendant(of: find.byType(ListView),
        matching: find.byType(Checkbox));
    await tester.tap(boxes.first);
    await tester.tap(boxes.last);
    await pumpFrames(tester);
    await tester.tap(barButton('Операции…'));
    await pumpFrames(tester);
    expect(find.text('Операции: 2 строки'), findsOneWidget,
        reason: 'заголовок диалога не плюрализован канонно (pluralRows app.js:1553)');

    await tester.tap(find.text('Применить'));
    await pumpFrames(tester);
    expect(find.text('Ничего не заполнено'), findsOneWidget,
        reason: 'пустая цепочка вызвала цели молча (app.js:1642)');
    expect(facade.bulkRequests, isEmpty);

    await tester.enterText(find.descendant(of: find.byType(BulkDialog),
        matching: find.byType(TextField)).first, 'новое');
    // Ряд рейтинга - SegmentedButton (факт SDK: Dropdown/Toggle/Radio
    // раздувают тест-процесс, см. ratingRow в bulk_bar.dart); тап ведёт
    // сегмент внутри диалога.
    final segmented = find.descendant(of: find.byType(BulkDialog),
        matching: find.byType(SegmentedButton<int>));
    expect(segmented, findsOneWidget,
        reason: 'ряд рейтинга не построен как SegmentedButton из пяти целей');
    await tester.tap(find.descendant(of: segmented, matching: find.text('3')).first);
    await pumpFrames(tester);
    await tester.tap(find.text('Применить'));
    await pumpFrames(tester);

    expect(find.byType(BulkDialog), findsNothing);
    expect(facade.bulkRequests.map((Map<String, Object?> entry) => entry['name']),
        <String>['bulk_tags', 'bulk_rating'],
        reason: 'цепочка не по очереди (app.js:1643-1649: сводка каждой цели видна по порядку)');
    expect(facade.bulkRequests.first['request'], <String, Object?>{
      'ids': <int>[0, 1], 'add': 'новое', 'remove': '',
    }, reason: 'проводка bulk_tags не форма канона {ids, add, remove}');
    expect(facade.bulkRequests.last['request'], <String, Object?>{
      'ids': <int>[0, 1], 'rating': 3,
    }, reason: 'проводка bulk_rating не форма канона {ids, rating}');
    expect(find.textContaining('Теги: 2 строки · Рейтинг 3: 2 строки'), findsOneWidget,
        reason: 'сводка apply не в канонной форме (app.js:1654-1657, строки « · »)');
    expect(facade.calls.where((String name) => name == 'list_videos').length, 2,
        reason: 'строки не перечитаны после сводки (renderGrid после apply канона)');
  });

  testWidgets('папка диалога: collections_list под read; «в папку» - проводка и сводка канона',
      (tester) async {
    final facade = FakeFacade(total: 2, scopes: kFakeBulkScopes,
        collections: <Map<String, Object?>>[collection('5', 'Папка мок', total: 7)],
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0), row(1)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);
    final boxes = find.descendant(of: find.byType(ListView),
        matching: find.byType(Checkbox));
    await tester.tap(boxes.first);
    await tester.tap(boxes.last);
    await pumpFrames(tester);
    await tester.tap(barButton('Операции…'));
    await pumpFrames(tester);

    expect(facade.calls, contains('collections_list'),
        reason: 'папки не прочитаны целью под read (дерево из get_initial не построено - W3)');
    expect(find.text('Папка мок (7)'), findsOneWidget,
        reason: 'папка со счётчиком не показана в селекте (bulkCollectionOptions app.js:1580)');
    await tester.tap(find.text('в папку'));
    await pumpFrames(tester);

    expect(facade.bulkRequests.last['request'], <String, Object?>{
      'ids': <int>[0, 1], 'action': 'add', 'collection_id': 5,
    }, reason: 'проводка bulk_collection не форма канона {ids, action, collection_id}');
    expect(find.textContaining('В папку «Папка мок»: 2 из 2'), findsOneWidget,
        reason: 'сводка папки не канонный текст (app.js:1676)');
    expect(find.byType(BulkDialog), findsOneWidget,
        reason: 'диалог закрылся на папочной цели (канон: renderGrid, диалог остаётся)');
    expect(facade.calls.where((String name) => name == 'list_videos').length, 2,
        reason: 'строки не перечитаны после связи с папкой');
  });

  testWidgets('«папок пока нет» названо; цель папки без выбора папки не отправляется',
      (tester) async {
    final facade = FakeFacade(total: 1, scopes: kFakeBulkScopes,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);
    await tester.tap(find.descendant(of: find.byType(ListView),
        matching: find.byType(Checkbox)).first);
    await pumpFrames(tester);
    await tester.tap(barButton('Операции…'));
    await pumpFrames(tester);

    expect(find.text('папок пока нет'), findsOneWidget,
        reason: 'пустой список папок не назван канонной подписью селекта (bulkCollectionOptions app.js:1582)');
    await tester.tap(find.text('в папку'));
    await pumpFrames(tester);
    expect(find.textContaining('Не указана коллекция'), findsOneWidget,
        reason: 'отказ без collection_id не назван текстом бэкенда (app/gui.py:902-903) - '
        'клиент не выдумывает свою подпись');
    expect(facade.bulkRequests.where((Map<String, Object?> entry) =>
        entry['name'] == 'bulk_collection'),
        <Map<String, Object?>>[<String, Object?>{'name': 'bulk_collection',
          'request': <String, Object?>{'ids': <int>[0], 'action': 'add'}}],
        reason: 'папочная цель без папок отправляется с action и без collection_id '
        '(runCollect app.js:1669-1671 кладёт cid только когда он есть)');
  });

}
