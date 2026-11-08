import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'fake_facade.dart';
import 'harness.dart';
import 'package:flutter/material.dart';
import 'package:omnistash_ui/access.dart';
import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/bulk_bar.dart';

void main() {
  testWidgets('«Пометить…»: пустой набор отказан без цели, заполненный пишет whitelist, '
      'фокус возвращается на инициатор', (tester) async {
    final facade = FakeFacade(total: 1, scopes: kFakeBulkScopes,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);
    await tester.tap(find.descendant(of: find.byType(ListView),
        matching: find.byType(Checkbox)).first);
    await pumpFrames(tester);
    await tester.tap(barButton('Пометить…'));
    await pumpFrames(tester);

    expect(find.text('Пометить: 1 строку'), findsOneWidget,
        reason: 'заголовок диалога не в канонной форме singular «1 строку» (app.js:1375)');
    final exitNode = tester.widget<IconButton>(
        find.descendant(of: find.byType(MarkDialog),
            matching: find.byType(IconButton))).focusNode;
    expect(FocusManager.instance.primaryFocus, exitNode,
        reason: 'вход диалога не ведён на выход - не-разрушительный дефолт §3.8 '
            '(dialogEnter app.js:4801-4806)');

    await tester.tap(find.text('Применить'));
    await pumpFrames(tester);
    expect(find.text('Ничего не заполнено'), findsOneWidget,
        reason: 'пустой набор вызвал цель молча (app.js:1440-1442)');
    expect(facade.bulkRequests, isEmpty);
    expect(find.byType(MarkDialog), findsOneWidget,
        reason: 'отказ должен оставаться в диалоге, не закрывать его');

    await tester.enterText(find.descendant(of: find.byType(MarkDialog),
        matching: find.byType(TextField)).first, 'музыка');
    await tester.tap(find.text('Применить'));
    await pumpFrames(tester);
    expect(find.byType(MarkDialog), findsNothing);
    expect(facade.bulkRequests, <Map<String, Object?>>[
      <String, Object?>{'name': 'save_fields',
        'request': <String, Object?>{'ids': <int>[0],
          'fields': <String, Object?>{'user_tags': 'музыка'}}},
    ], reason: 'проводка save_fields не форма {ids, fields} с whitelist-ключом '
        'канона (repo.py:976-995); непустое поле пишется, пустые - нет');
    expect(find.textContaining('Помечено строк: 1'), findsOneWidget,
        reason: 'сводка не канонный текст (app.js:1447)');
    expect(FocusManager.instance.primaryFocus,
        tester.widget<ButtonStyleButton>(barButton('Пометить…')).focusNode,
        reason: 'фокус не возвратился на цель панели (§3.8, dialogLeave app.js:4810-4816)');

    await tester.tap(barButton('Пометить…'));
    await pumpFrames(tester);
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await pumpFrames(tester);
    expect(find.byType(MarkDialog), findsNothing,
        reason: 'Escape не закрыл диалог (браузер по Escape кликает dialogExit)');
  });

  testWidgets('рейтинг пометки тронут -> user_rating; «сбросить» -> 0', (tester) async {
    final facade = FakeFacade(total: 1, scopes: kFakeBulkScopes,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);
    await tester.tap(find.descendant(of: find.byType(ListView),
        matching: find.byType(Checkbox)).first);
    await pumpFrames(tester);
    await tester.tap(barButton('Пометить…'));
    await pumpFrames(tester);

    // Звёздный ряд канона (5 целей + сброс) переложен SegmentedButton из пяти
    // целей с подписями 1..5 + «сбросить» - форма названа расхождением
    // (radiogroup ложится на W3). Причина формы - факт тест-рендерера этого
    // SDK: DropdownButton/ToggleButtons/Radio/DropdownMenu раздувают commit
    // flutter_tester до 8-9 GB на одном тесте, SegmentedButton - 660 MB.
    final segmented = find.descendant(of: find.byType(MarkDialog),
        matching: find.byType(SegmentedButton<int>));
    expect(segmented, findsOneWidget,
        reason: 'ряд рейтинга не построен как SegmentedButton из пяти целей');
    await tester.tap(find.descendant(of: segmented, matching: find.text('3')).first);
    await pumpFrames(tester);
    await tester.tap(find.text('Применить'));
    await pumpFrames(tester);
    expect(facade.bulkRequests.last['request'], <String, Object?>{
      'ids': <int>[0], 'fields': <String, Object?>{'user_rating': 3},
    }, reason: 'тронутый рейтинг не записан ключом whitelist user_rating');

    await tester.tap(barButton('Пометить…'));
    await pumpFrames(tester);
    await tester.tap(find.descendant(of: find.byType(MarkDialog),
        matching: find.text('сбросить')));
    await pumpFrames(tester);
    await tester.tap(find.text('Применить'));
    await pumpFrames(tester);
    expect(facade.bulkRequests.last['request'], <String, Object?>{
      'ids': <int>[0], 'fields': <String, Object?>{'user_rating': 0},
    }, reason: '«сбросить» не записан как rating 0 (канонная форма сбоста звёзд)');
  });

  testWidgets('без права mark пометка названа отказом i18n и не отправлена',
      (tester) async {
    final facade = FakeFacade(total: 1, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0)],
    ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);
    await tester.tap(find.descendant(of: find.byType(ListView),
        matching: find.byType(Checkbox)).first);
    await pumpFrames(tester);
    await tester.tap(barButton('Пометить…'));
    await pumpFrames(tester);
    await tester.enterText(find.descendant(of: find.byType(MarkDialog),
        matching: find.byType(TextField)).first, 'музыка');
    await tester.tap(find.text('Применить'));
    await pumpFrames(tester);

    expect(find.textContaining(kScopeDeniedText), findsOneWidget,
        reason: 'право mark не названо отказом внутри диалога');
    expect(find.byType(MarkDialog), findsOneWidget,
        reason: 'диалог закрыт на отказе по праву: человек теряет форму');
    expect(facade.bulkRequests, isEmpty);
  });

}
