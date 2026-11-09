import 'package:flutter_test/flutter_test.dart';
import 'fake_facade.dart';
import 'harness.dart';
import 'package:flutter/material.dart';
import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/video_card.dart';

void main() {
  testWidgets('сортировка: новая колонка - desc, та же - разворот, '
      'первая страница перезагружается', (tester) async {
    final facade = FakeFacade(total: 2, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0), row(1)],
    ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);

    // Порядок по умолчанию: order:null - сервер держит uploaded_at DESC,
    // как браузерный ensureRange (ui_src/app.js:3308).
    expect(facade.sortRequests, <Map<String, Object?>?>[null],
        reason: 'без выбранной колонки клиент должен послать order: null');

    await tester.tap(find.text('Название'));
    await pumpFrames(tester);
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
    await pumpFrames(tester);
    expect(facade.sortRequests.last, <String, Object?>{'col': 'title', 'dir': 'asc'},
        reason: 'та же колонка не развернула направление');
    expect(find.text('Название ▲'), findsOneWidget);

    await tester.tap(find.text('Статус'));
    await pumpFrames(tester);
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
    await pumpFrames(tester);

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
    await pumpFrames(tester);
    expect(find.text('Просмотры'), findsNothing,
        reason: 'col_views=false не скрыл колонку: браузер colVisible !== false');

    final shown = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'col_views': true},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: shown));
    await pumpFrames(tester);
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
    await pumpFrames(tester);
    await tester.tap(find.text('Название'));
    await pumpFrames(tester);

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
    await pumpFrames(tester);
    await tester.tap(find.text('Название'));
    await pumpFrames(tester);

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
    await pumpFrames(tester);
    await tester.tap(find.text('Время'));
    await pumpFrames(tester);

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

  /* ------------------------------------------------------------------ *
   *  Шаг 4: грид ячеек, мультивыбор, панель целей и диалоги группы
   * ------------------------------------------------------------------ */

  testWidgets('ячейки строки - канонные формы значений (rowHtml)', (tester) async {
    final facade = FakeFacade(total: 2, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[
        row(3, size: 1572864, viewCount: 1234567, likeCount: 42, commentCount: 7,
            platformTags: <String>['lofi', 'live', 'remix', 'extra'],
            userRating: 4, watchedAt: '2026-11-01T09:30:00'),
        row(4, size: null, viewCount: null, likeCount: null, commentCount: null,
            uploadedAt: null, syncedAt: null, platformTags: const <String>[]),
      ],
    ]);
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);

    expect(find.text('Клип 3'), findsOneWidget);
    expect(find.text('1.5 МиБ'), findsOneWidget,
        reason: 'ячейка размера не в канонной форме humanSize (app.js:909)');
    expect(find.text('1 234 567'), findsOneWidget,
        reason: 'счётчик площадки не в канонной форме bigCount (разряды)');
    expect(find.text('42'), findsOneWidget);
    expect(find.text('lofi, live, remix'), findsOneWidget,
        reason: 'колонка «Теги» не берёт первые три тега площадки (app.js:3495)');
    expect(find.text('★4'), findsOneWidget,
        reason: 'значок рейтинга не показан рядом с названием (starBadge app.js:3518)');
    expect(find.text('✓'), findsOneWidget,
        reason: 'значок «просмотрено» не показан (watchBadge app.js:3525)');
    expect(find.text('2026-10-25'), findsOneWidget,
        reason: 'дата публикации не в форме shortDate');
    expect(find.text('2026-10-26'), findsOneWidget,
        reason: 'дата синка не в форме shortDate');
    expect(find.text('Синк'), findsOneWidget);
    expect(find.text('Теги'), findsOneWidget);
    expect(find.text('Тип'), findsOneWidget);
    // Строка названа целиком, включая «просмотрено» (§3.1); пустые ячейки
    // второй строки читаются как «—», а не как выдуманный ноль.
    expect(find.bySemanticsLabel('Клип 3, в индексе, Канал 0, 2:08, видео, '
        'рейтинг 4, просмотрено, нет обложки'), findsOneWidget,
        reason: 'строка не называет «просмотрено» - AT теряет отметку');
    expect(find.text('—'), findsNWidgets(7),
        reason: 'пустые ячейки не стали «—» (строка без данных: дата, размер, '
            'счётчики, теги, синк)');
    handle.dispose();
  });

  testWidgets('галка «выбрать всё» ведёт все загруженные строки и снимает их',
      (tester) async {
    final facade = FakeFacade(total: 3, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0), row(1), row(2)],
    ]);
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);

    final allBox = find.descendant(of: find.byType(Scaffold),
        matching: find.byType(Checkbox)).first;
    await tester.tap(allBox);
    await pumpFrames(tester);
    expect(find.text('Выбрано: 3'), findsOneWidget,
        reason: 'галка «выбрать всё» не повела все загруженные строки (#sel-all)');
    // Ряд вида (шаг 4б) и панель мультивыбора сдвигают список вниз: последняя
    // строка уходит за нижний край вьюпорта и остаётся смонтированной в
    // cacheExtent - её узел семантики в дереве есть, но finder со
    // skipOffstage его пропускает (факт дампа прогона: «Клип 2» найден 0 при
    // узле в merged-дереве и skipOffstage=false даёт 1). Выбор строки вне
    // экрана ведётся канонным DOM (#sel-all ставит checked всем строкам
    // страницы), поэтому selected сверяется со skipOffstage: false.
    for (final String label in <String>[
      'Клип 0, в индексе, Канал 0, 2:05, видео, обложка есть',
      'Клип 1, в индексе, Канал 1, 2:06, видео, нет обложки',
      'Клип 2, в индексе, Канал 2, 2:07, видео, обложка есть',
    ]) {
      expect(tester.getSemantics(find.bySemanticsLabel(label, skipOffstage: false))
          .flagsCollection.isSelected.toBoolOrNull(), true,
          reason: 'строка "$label" не выбрана - AT не объявит selected');
    }
    final counterFlags =
        tester.getSemantics(find.bySemanticsLabel('Выбрано: 3')).flagsCollection;
    expect(counterFlags.isLiveRegion, true,
        reason: 'счётчик выбора не живой регион (aria-live у sel-count канона)');

    await tester.tap(allBox);
    await pumpFrames(tester);
    expect(find.textContaining('Выбрано:'), findsNothing,
        reason: 'панель не исчезла после снятия выбора (hidden при нуле канона)');

    // Чекбокс строки: выбор, не открытие карточки (канон гасит click строки
    // на `.row-check`, app.js:3408,3423).
    await tester.tap(find.descendant(of: find.byType(ListView),
        matching: find.byType(Checkbox)).first);
    await pumpFrames(tester);
    expect(find.text('Выбрано: 1'), findsOneWidget);
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'чекбокс строки открыл карточку вместо выбора');
    await tester.tap(barButton('Снять выбор'));
    await pumpFrames(tester);
    expect(find.textContaining('Выбрано:'), findsNothing,
        reason: '«Снять выбор» не снял выбор (clearSelection app.js:7141)');
    handle.dispose();
  });

}
