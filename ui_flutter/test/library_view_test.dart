import 'dart:ui' show CheckedState;
import 'package:flutter/gestures.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'fake_facade.dart';
import 'harness.dart';
import 'package:flutter/material.dart';
import 'package:omnistash_ui/access.dart';
import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/library_view.dart';
import 'package:omnistash_ui/theme.dart';
import 'package:omnistash_ui/video_card.dart';

void main() {
  testWidgets('строки из фасада показаны с подписью статуса', (tester) async {
    final facade = FakeFacade(total: 3, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0), row(1), row(2)],
    ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);

    expect(find.text('Клип 0'), findsOneWidget);
    expect(find.text('Клип 2'), findsOneWidget);
    expect(find.text('в индексе'), findsNWidgets(3),
        reason: 'подпись статуса из app/repo.py STATUS_LABELS не показана');
    expect(facade.calls, contains('list_videos'),
        reason: 'экран не вызвал list_videos - список пришёл не из фасада');
    expect(counterText(tester), '3 из 3',
        reason: 'счётчик показанных строк не равен total из ответа');
  });

  testWidgets('тема приходит из настройки бэкенда, а не из литерала',
      (tester) async {
    final facade = FakeFacade(theme: 'coolors', total: 1,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);

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
    await pumpFrames(tester);

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
    await pumpFrames(tester);

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
    await pumpFrames(tester);

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
    await pumpFrames(tester);

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
    await pumpFrames(tester);

    expect(counterText(tester), '$kPageLimit из 250',
        reason: 'первая страница не равна kPageLimit: PAGE из app.js не применён');
    // Кнопка следующей страницы строится только когда попадает на экран
    // (ListView.builder виртуализирует): тест прокручивает список drag-ом.
    await tester.drag(find.byType(ListView), const Offset(0.0, -2500.0));
    await pumpFrames(tester);
    await tester.drag(find.byType(ListView), const Offset(0.0, -2500.0));
    await pumpFrames(tester);
    expect(find.text('Ещё (${250 - kPageLimit})'), findsOneWidget,
        reason: 'total больше loaded: экрана без пути к следующей странице нет');
    await tester.tap(find.text('Ещё (${250 - kPageLimit})'));
    await pumpFrames(tester);

    expect(counterText(tester), '250 из 250',
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
    await pumpFrames(tester);

    expect(find.bySemanticsLabel('Клип 0, в индексе, Канал 0, 2:05, видео, '
        'обложка есть'), findsOneWidget,
        reason: 'строка не названа целиком - AT передаёт только имя');
    // Space - выбор (канон клавиатуры строк), карточку не открывает.
    await tester.sendKeyEvent(LogicalKeyboardKey.space);
    await pumpFrames(tester);

    // Чекбокс строки отмечен (эквивалент checked у `.row-check` канона);
    // ListTile больше не рисуется: строка собрана из ячеек грида
    // (grid_cells.dart), поэтому выбор читается через Semantics, а не
    // через виджет списка. Первый чекбокс - строка 0: подписи чекбоксов
    // одинаковы по канону (`Выбрать строку`), точный finder без .first
    // дал бы «Finder returned more than one element» (факт прогона теста).
    // isChecked в этом SDK возвращает CheckedState (dart:ui) - сверка
    // перечислением, notBool нет (факт компиляции теста: toBoolOrNull не
    // определён для CheckedState; isSelected остаётся TriState bool).
    expect(tester.getSemantics(find.bySemanticsLabel('Выбрать строку').first).flagsCollection.isChecked, CheckedState.isTrue,
        reason: 'чекбокс строки не отмечен - выбор не передан в Semantics.checked');
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'Space открыл карточку: выбор должен переключаться, а не '
            'показывать диалог (ui_src/app.js: чекбокс не открывает карточку)');
    final rowFlags = tester.getSemantics(find.bySemanticsLabel(
        'Клип 0, в индексе, Канал 0, 2:05, видео, обложка есть')).flagsCollection;
    // Tristate не экспортируется публично, поэтому выбор читается
    // преобразованием в bool - тот же приём, что в самом Flutter.
    expect(rowFlags.isSelected.toBoolOrNull(), true,
        reason: 'Semantics.selected не выставлен - AT не скажет «выбрано»');
    expect(rowFlags.isButton, true,
        reason: 'строка не активна как кнопка - с AT одиночное действие потеряно');
    final liveLabel = 'состояние: мок: запись журнала';
    expect(find.bySemanticsLabel(liveLabel), findsOneWidget,
        reason: 'регион состояния не назван текстом журнала - AT не объявит изменение');
    final liveFlags = tester.getSemantics(find.bySemanticsLabel(liveLabel)).flagsCollection;
    expect(liveFlags.isLiveRegion, true,
        reason: 'полоса состояния не живый регион (эквивалент aria-live у журнала)');
    handle.dispose();
  });

  testWidgets('одинарный клик открывает карточку, двойной - плеер',
      (tester) async {
    final facade = FakeFacade(total: 2, video: videoFixture(0),
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0), row(1)],
        ]);
    final opener = FakeOpener();
    await tester.pumpWidget(OmnistashApp(facade: facade, opener: opener));
    await pumpFrames(tester);

    // Одиночный жест: карточка (GestureDetector различает жесты, двойной
    // побеждает одинарный - канон app.js различает их таймером 240 мс).
    await tester.tap(find.text('Клип 0'));
    await tester.pump();
    await tester.pump(kDoubleTapTimeout);
    await pumpFrames(tester);
    expect(find.byType(VideoCardDialog), findsOneWidget,
        reason: 'одинарный клик не открыл карточку: список не имеет пути к деталям');
    expect(facade.videoRequests, <Object?>[0],
        reason: 'карточка не прочитана через get_video с id строки');

    await tester.tap(find.byType(IconButton));
    await pumpFrames(tester);
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'выход карточки не закрыл диалог');
    expect(opener.opened, isEmpty,
        reason: 'закрытие карточки не должно открывать адрес молча');

    // Двойной жест - плеер, карточка не открывается (dblclick в app.js:3415).
    await tester.tap(find.text('Клип 1'));
    await tester.pump(const Duration(milliseconds: 80));
    await tester.tap(find.text('Клип 1'));
    await tester.pump();
    await tester.pump(kDoubleTapTimeout);
    await pumpFrames(tester);
    expect(opener.opened, <String>['https://fake.test/watch/1'],
        reason: 'двойной клик не открыл страницу плеера /watch/<id>');
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'двойной клик открыл карточку вместо плеера');
  });

  testWidgets('Enter - карточка, Ctrl+Enter - плеер', (tester) async {
    final facade = FakeFacade(total: 2, video: videoFixture(0),
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0), row(1)],
        ]);
    final opener = FakeOpener();
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(OmnistashApp(facade: facade, opener: opener));
    await pumpFrames(tester);

    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await pumpFrames(tester);
    expect(find.byType(VideoCardDialog), findsOneWidget,
        reason: 'Enter не открыл карточку (канон: Enter - карточка)');
    // Enter не трогает выбор: строка названа через Semantics (ListTile
    // больше не рисуется - грид ячеек шага 4).
    final rowFlags = tester.getSemantics(find.bySemanticsLabel(
        'Клип 0, в индексе, Канал 0, 2:05, видео, обложка есть')).flagsCollection;
    expect(rowFlags.isSelected.toBoolOrNull(), false,
        reason: 'Enter не должен менять выбор строки');

    await tester.tap(find.byType(IconButton));
    await pumpFrames(tester);
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'карточка не закрылась выходом');

    await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
    await pumpFrames(tester);
    expect(opener.opened, <String>['https://fake.test/watch/0'],
        reason: 'Ctrl+Enter не открыл плеер (канон: Ctrl+Enter - плеер)');
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'Ctrl+Enter открыл карточку вместо плеера');
    handle.dispose();
  });

  testWidgets('карточка без права watch называет отказ и не открывает плеер',
      (tester) async {
    final facade = FakeFacade(total: 1, scopes: <String>['read'],
        video: videoFixture(0), pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0, status: 'downloaded', label: 'скачано')],
        ]);
    final opener = FakeOpener();
    await tester.pumpWidget(OmnistashApp(facade: facade, opener: opener));
    await pumpFrames(tester);

    await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
    await pumpFrames(tester);

    expect(opener.opened, isEmpty,
        reason: 'плеер открыт без права watch: сервер ответил бы 403 на /media '
            'молча, клиент обязан подписать отказ до перехода');
    expect(find.textContaining(kScopeDeniedText), findsOneWidget,
        reason: 'отказ по праву не назван текстом i18n (http.scope)');
  });

  testWidgets('права не прочитаны - отказ назван, плеер не открывается',
      (tester) async {
    final facade = FakeFacade(total: 1, accessRefusal: kUnpairedText,
        video: videoFixture(0), pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    final opener = FakeOpener();
    await tester.pumpWidget(OmnistashApp(facade: facade, opener: opener));
    await pumpFrames(tester);

    await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
    await pumpFrames(tester);

    expect(opener.opened, isEmpty,
        reason: 'цель подписана при непрочитанных правах: молчаливый переход '
            'выглядит как доступ, которого нет');
    expect(find.textContaining(kUnpairedText), findsOneWidget,
        reason: 'непрочитанные права названы причиной привязки (i18n http.pairing)');
    expect(find.textContaining('whoami'), findsNothing,
        reason: 'подмена фасада не должна печатать имя цели в интерфейсе');
  });

  testWidgets('без сервера список называет причину, а не пустую библиотеку',
      (tester) async {
    await tester.pumpWidget(const OmnistashApp(facade: null));
    await pumpFrames(tester);

    expect(find.textContaining('Сервер не запущен'), findsOneWidget,
        reason: 'отсутствие слушателя названо, а не показано как пустая библиотека');
    expect(find.text('Клип 0'), findsNothing,
        reason: 'без фасада строки не должны появляться из подменённых данных');
    expect(find.textContaining('тема:'), findsOneWidget,
        reason: 'экран не называет, какая палитра применена без сервера');
  });

}
