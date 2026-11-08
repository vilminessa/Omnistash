import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'fake_facade.dart';
import 'harness.dart';
import 'package:flutter/material.dart';
import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/bulk_bar.dart';

void main() {
  testWidgets('разрушительная цель: диалог называет N; вход - «Отклонить»; '
      '«Принять» до галки неактивна; Escape - отказ; галка -> confirm:true',
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

    await tester.tap(find.text('Убрать строки из индекса'));
    await pumpFrames(tester);
    expect(find.byType(ConfirmDialog), findsOneWidget,
        reason: 'разрушительная цель выполнила удаление без диалога (§3.7)');
    expect(find.descendant(of: find.byType(ConfirmDialog),
        matching: find.textContaining('Строки (2 строки)')), findsOneWidget,
        reason: 'диалог не называет размер группы (app.js:1714-1720)');
    expect(find.descendant(of: find.byType(ConfirmDialog),
        matching: find.textContaining('ФАЙЛЫ НА ДИСКЕ НЕ удаляются')), findsOneWidget,
        reason: 'диалог не называет, что файлы на диске остаются (канонный текст)');

    final declineNode = tester.widget<OutlinedButton>(
        find.descendant(of: find.byType(ConfirmDialog),
            matching: find.byType(OutlinedButton))).focusNode;
    expect(FocusManager.instance.primaryFocus, declineNode,
        reason: 'вход разрушительного диалога не ведён на отказ: '
            'dialogExit по «-decline» - дефолт §3.8 (app.js:4796-4806)');

    final accept = tester.widget<FilledButton>(
        find.descendant(of: find.byType(ConfirmDialog),
            matching: find.byType(FilledButton)));
    expect(accept.onPressed, null,
        reason: '«Принять» активна до галки подтверждения '
            '(тот же legal-accept disabled, index.html:624)');
    await tester.tap(find.text('Принять'));
    await pumpFrames(tester);
    expect(find.byType(ConfirmDialog), findsOneWidget,
        reason: 'неактивная цель сработала как клик');
    expect(facade.bulkRequests, isEmpty);

    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await pumpFrames(tester);
    expect(find.byType(ConfirmDialog), findsNothing,
        reason: 'Escape не отклонил удаление (браузер по Escape кликает выход-отказ)');
    expect(find.text('Удаление отменено'), findsOneWidget,
        reason: 'отказ не назван канонным текстом (app.js:1722)');
    expect(facade.bulkRequests, isEmpty,
        reason: 'цель без подтверждения отправлена: бэк отказывает сам (app/gui.py:931-932)');

    await tester.tap(find.text('Убрать строки из индекса'));
    await pumpFrames(tester);
    await tester.tap(find.descendant(of: find.byType(ConfirmDialog),
        matching: find.byType(Checkbox)));
    await pumpFrames(tester);
    expect(tester.widget<FilledButton>(
        find.descendant(of: find.byType(ConfirmDialog),
            matching: find.byType(FilledButton))).onPressed, isNotNull,
        reason: 'галка подтверждения не включила «Принять»');
    await tester.tap(find.text('Принять'));
    await pumpFrames(tester);

    expect(find.byType(ConfirmDialog), findsNothing);
    expect(find.byType(BulkDialog), findsNothing,
        reason: 'диалог операций не закрылся после сводки удаления');
    expect(facade.bulkRequests.last['request'], <String, Object?>{
      'ids': <int>[0, 1], 'confirm': true,
    }, reason: 'confirm:true ставит только галка человека (проводка канона)');
    expect(find.textContaining('Убраны строки из индекса: 2 из 2 '
        '(файлы на диске не тронуты)'), findsOneWidget,
        reason: 'сводка удаления не канонный текст (app.js:1726-1727)');
    expect(find.textContaining('Выбрано:'), findsNothing,
        reason: 'выбор не снят после убранных строк (renderGrid(true) app.js:1729)');
    expect(facade.calls.where((String name) => name == 'list_videos').length, 2,
        reason: 'строки не перечитаны после удаления');
  });

  testWidgets('отказ бэка на удаление назван в диалоге, выбор и диалог остаются',
      (tester) async {
    final facade = FakeFacade(total: 1, scopes: kFakeBulkScopes,
        bulkRefusal: 'Не удалось выполнить: мок-отказ бэка',
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
    await tester.tap(find.text('Убрать строки из индекса'));
    await pumpFrames(tester);
    await tester.tap(find.descendant(of: find.byType(ConfirmDialog),
        matching: find.byType(Checkbox)));
    await pumpFrames(tester);
    await tester.tap(find.text('Принять'));
    await pumpFrames(tester);

    expect(find.textContaining('Не удалось выполнить: мок-отказ бэка'), findsOneWidget,
        reason: 'отказ бэка скрыт: операция выглядела выполненной');
    expect(find.byType(BulkDialog), findsOneWidget,
        reason: 'диалог закрыт на отказе: сводка потеряна');
    expect(find.text('Выбрано: 1'), findsOneWidget,
        reason: 'выбор снят на отказе бэка: канон держит выделение (bulkRemoveRows '
            'app.js:1723-1725 - toast об отказе, renderGrid не вызывается)');
  });

  testWidgets('гейт 18+: отказ назван текстом сервера, диалог назван волной W3, '
      'повтор не отправлен', (tester) async {
    final facade = FakeFacade(total: 1, scopes: kFakeBulkScopes, adultConfirm: true,
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

    expect(find.textContaining(kAdultGateText), findsOneWidget,
        reason: 'отказ adult_confirm не назван текстом бэкенда (app/gui.py:772-775)');
    expect(find.textContaining('диалог 18+ не построен'), findsOneWidget,
        reason: 'диалог askAdult не назван как не построенный');
    expect(find.textContaining('диалог 18+ не построен: волна W3'), findsOneWidget,
        reason: 'отказ adult не назван волной в той же сводке: цель панели с '
            'не построенными целями тоже пишет «волна W3», поэтому сверка '
            'читает полный текст гейта, а не подстроку');
    expect(facade.bulkRequests.length, 1,
        reason: 'повтор цели отправлен без подтверждения: спрашивать нечем '
            '(askAdult app.js:1483 не построен)');
  });

  testWidgets('обход Tab в диалогах замыкается внутри; disabled контролы пропускаются',
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

    final exitNode = tester.widget<IconButton>(
        find.descendant(of: find.byType(BulkDialog),
            matching: find.byType(IconButton))).focusNode;
    expect(FocusManager.instance.primaryFocus, exitNode);

    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await pumpFrames(tester);
    expect(FocusManager.instance.primaryFocus,
        tester.widget<TextField>(find.descendant(of: find.byType(BulkDialog),
            matching: find.byType(TextField)).first).focusNode,
        reason: 'Tab не шагнул вперёд по порядку обхода scope (DOM-порядок канона)');

    // Обход ведётся по traversalDescendants scope'а диалога: цели, которые
    // контрол строит сам (сегменты рейтинга), тоже в круге - именованный
    // список регистрации их не видел (факт шага 4).
    await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
    await pumpFrames(tester);
    expect(FocusManager.instance.primaryFocus,
        tester.widget<IconButton>(find.descendant(of: find.byType(BulkDialog),
            matching: find.byType(IconButton))).focusNode,
        reason: 'Shift+Tab от поля тегов не вернулся на выход - первую цель круга');

    await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
    await pumpFrames(tester);
    expect(FocusManager.instance.primaryFocus,
        tester.widget<FilledButton>(find.descendant(of: find.byType(BulkDialog),
            matching: find.byType(FilledButton))).focusNode,
        reason: 'Shift+Tab от первой цели не замкнул круг на последнюю цель ряда '
            '(граница closedLoop в этом SDK не замыкает - ведётся явно)');

    await tester.tap(find.text('Убрать строки из индекса'));
    await pumpFrames(tester);
    final declineNode = tester.widget<OutlinedButton>(
        find.descendant(of: find.byType(ConfirmDialog),
            matching: find.byType(OutlinedButton))).focusNode;
    expect(FocusManager.instance.primaryFocus, declineNode);
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await pumpFrames(tester);
    final checkNode = tester.widget<Checkbox>(
        find.descendant(of: find.byType(ConfirmDialog),
            matching: find.byType(Checkbox))).focusNode;
    expect(FocusManager.instance.primaryFocus, checkNode,
        reason: 'обход не пропустил disabled «Принять» (тот же фильтр dialogTargets - :not([disabled]))');
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await pumpFrames(tester);
    expect(FocusManager.instance.primaryFocus, declineNode,
        reason: 'обход из галки снова не пропустил неактивную цель');
  });
}
