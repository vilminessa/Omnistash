import 'package:flutter_test/flutter_test.dart';
import 'fake_facade.dart';
import 'harness.dart';
import 'package:flutter/material.dart';
import 'package:omnistash_ui/access.dart';
import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/bulk_bar.dart';

void main() {
  testWidgets('панель целей: канонный порядок sel-bar; не построенные названы с волной',
      (tester) async {
    final facade = FakeFacade(total: 2, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0), row(1)],
    ]);
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);
    await tester.tap(find.descendant(of: find.byType(ListView),
        matching: find.byType(Checkbox)).first);
    await pumpFrames(tester);

    // find.byType матчит точный runtimeType, не подклассы (факт прогона):
    // ряд целей читается предикатом по ButtonStyleButton (Outlined/Filled).
    final labels = tester.widgetList<ButtonStyleButton>(
        find.descendant(of: find.byType(SelectionBar),
            matching: find.byWidgetPredicate(
                (Widget widget) => widget is ButtonStyleButton)))
        .map((ButtonStyleButton button) => (button.child as Text).data)
        .toList();
    expect(labels, <String>['Пометить…', 'Операции…', 'Скачать', 'Снять выбор'],
        reason: 'порядок целей не равен канонному sel-bar (index.html:173-201) без пропущенных');
    expect(find.text('В коллекцию…'), findsNothing,
        reason: 'не построенная цель не должна молча существовать кнопкой');
    expect(find.textContaining('В коллекцию…: '), findsOneWidget,
        reason: 'не построенная цель не названа в панели');
    expect(find.textContaining('Перенести…: '), findsOneWidget);
    expect(find.textContaining('Убрать из папки: '), findsOneWidget);
    expect(find.textContaining('Переименовать…: '), findsOneWidget);
    expect(find.textContaining('Удалить папку: '), findsOneWidget);
    expect(find.textContaining('волна W3'), findsNWidgets(5),
        reason: 'не каждая не построенная цель названа волной');
    // Подпись панели: в этом SDK узел Semantics merge'ит подписи plain-Text
    // детей (ряд целей и названные не-построенные цели) в одну label -
    // точный канонный `aria-label="Действия над выбранными"` отдельным узлом
    // не читается (факт прогона теста: bySemanticsLabel точной строки дал 0).
    // Сверка partial-regex: AT объявляет панель по подписи плюс содержимое -
    // расхождение формы названо (грид-роли и отдельные toolbar-узлы - W3).
    expect(find.bySemanticsLabel(RegExp('Действия над выбранными')),
        findsAtLeastNWidgets(1),
        reason: 'панель не названа - AT не объявит toolbar (aria-label канона sel-bar)');
    handle.dispose();
  });

  testWidgets('«Скачать»: одна цель bulk_download {ids, storage_id}, сводка канона, '
      'выбор снят и страницы перечитаны', (tester) async {
    final facade = FakeFacade(total: 2, scopes: kFakeBulkScopes,
        storages: <Map<String, Object?>>[storage('main')], defaultStorage: 'main',
        bulkSkipped: 1, bulkReasons: <String, Object>{'уже на диске': 1},
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
    expect(find.text('Выбрано: 2'), findsOneWidget);

    await tester.tap(barButton('Скачать'));
    await pumpFrames(tester);
    expect(facade.bulkRequests, <Map<String, Object?>>[
      <String, Object?>{'name': 'bulk_download',
        'request': <String, Object?>{'ids': <int>[0, 1], 'storage_id': 'main'}},
    ], reason: 'выкачка группы не одна цель со списком id и хранилищем '
        '(downloadSelection app.js:1571: бэк ставит queued разом, без параллельных запросов)');
    expect(find.textContaining('В очередь: 1 из 2 · пропущено: уже на диске: 1'),
        findsOneWidget,
        reason: 'сводка выкачки не в канонной форме с причинами из бэка '
            '(app.js:1574 + skipNote: причины угадываются не');
    expect(find.textContaining('Выбрано:'), findsNothing,
        reason: 'выбор не снят после выкачки (renderGrid(true) app.js:1576)');
    expect(facade.calls.where((String name) => name == 'list_videos').length, 2,
        reason: 'загруженные страницы не перечитаны после выкачки (statuses from server again)');
  });

  testWidgets('«Скачать» без хранилища назван текстом канона, переход к настройкам назван',
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
    await tester.tap(barButton('Скачать'));
    await pumpFrames(tester);

    expect(find.textContaining('Не выбрано хранилище - добавьте папку в настройках'),
        findsOneWidget, reason: 'отказ без хранилища не назван канонным текстом (app.js:1565)');
    expect(find.textContaining('переход к настройкам не построен'), findsOneWidget,
        reason: 'канонный switchTab("settings") (app.js:1566) не построен и не назван');
    expect(facade.bulkRequests, isEmpty, reason: 'цель отправлена без хранилища');
  });

  testWidgets('без права queue выкачка названа отказом i18n и не отправлена',
      (tester) async {
    final facade = FakeFacade(total: 1, scopes: <String>['read', 'watch'],
        storages: <Map<String, Object?>>[storage('main')], defaultStorage: 'main',
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);
    await tester.tap(find.descendant(of: find.byType(ListView),
        matching: find.byType(Checkbox)).first);
    await pumpFrames(tester);
    await tester.tap(barButton('Скачать'));
    await pumpFrames(tester);

    expect(find.textContaining(kScopeDeniedText), findsOneWidget,
        reason: 'отказ по праву не назван текстом i18n (http.scope из app/i18n.py)');
    expect(facade.bulkRequests, isEmpty,
        reason: 'цель подписана без права: сервер ответил бы 403 молча');
  });

}
