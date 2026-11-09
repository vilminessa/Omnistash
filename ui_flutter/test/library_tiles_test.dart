import 'dart:convert';
import 'dart:typed_data';
import 'dart:ui' as ui show CheckedState;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omnistash_ui/access.dart';
import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/tiles.dart';
import 'package:omnistash_ui/video_card.dart';

import 'fake_facade.dart';
import 'harness.dart';

/// PNG 1x1 в форме data-URI бэкенда (`app/gui.py: _thumb_data_uri`:
/// `data:image/png;base64,...`). Байты собраны из IHDR/IDAT/IEND и
/// проверены обратным декодом zlib (факт прогона скрипта
/// `build/scratch/png_uri.py`): обложка приходит с диска бэкенда, рантайм
/// ничего не качает снаружи. `createTestImage` в этом SDK возвращает
/// `ui.Image`, а не байты, поэтому константа здесь, а не генерация в тесте.
const String kThumbPngUri =
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1Pe'
    'AAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC';

String thumbUri() => kThumbPngUri;

int crossAxisCount(WidgetTester tester) {
  final GridView grid = tester.widget<GridView>(find.byType(GridView));
  final SliverGridDelegateWithFixedCrossAxisCount delegate =
      grid.gridDelegate as SliverGridDelegateWithFixedCrossAxisCount;
  return delegate.crossAxisCount;
}

double childAspectRatio(WidgetTester tester) {
  final GridView grid = tester.widget<GridView>(find.byType(GridView));
  final SliverGridDelegateWithFixedCrossAxisCount delegate =
      grid.gridDelegate as SliverGridDelegateWithFixedCrossAxisCount;
  return delegate.childAspectRatio;
}

void main() {
  Future<void> boot(WidgetTester tester, FakeFacade facade) async {
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await pumpFrames(tester);
  }

  // -- вид библиотеки из настройки -------------------------------------------

  testWidgets('вид приходит из настройки get_initial: плитка рисует состав '
      'канона, шапка колонок скрыта', (tester) async {
    final facade = FakeFacade(total: 2,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0, hasThumb: false), row(1, hasThumb: false)],
        ]);
    await boot(tester, facade);

    expect(find.byType(GridView), findsOneWidget,
        reason: 'настройка view_mode=grid не применена: экран остался списком');
    expect(find.text('Название'), findsNothing,
        reason: 'шапка колонок не скрыта в режиме плитки '
            '(канон grid-head hidden = grid, app.js:2974-2985)');
    expect(find.text(kNoThumbLabel), findsNWidgets(2),
        reason: 'заглушка обложки не названа подписью канона «нет обложки»');
    expect(crossAxisCount(tester), 3,
        reason: 'число колонок не по формуле канона tileCols '
            '(app.js:2967-2972): ширина окна 800px, medium w=240 gap=10');
    // Высота ряда задаётся канонным TILE.row (app.css:642-646), а не
    // содержимым ячейки: плитка выше ряда легла бы на следующий ряд.
    expect(childAspectRatio(tester), closeTo(240 / 218, 0.001),
        reason: 'aspect ячеки не канонный w/row ряда плиток');
    expect(find.text(kViewGridLabel), findsOneWidget,
        reason: 'ряд вида библиотеки не построен с подписью канона');
  });

  testWidgets('переключение вида пишет save_setting только при изменении '
      'и возвращает шапку колонок', (tester) async {
    final facade = FakeFacade(total: 2,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0), row(1)],
        ]);
    await boot(tester, facade);
    expect(find.byType(GridView), findsNothing,
        reason: 'дефолтный вид не список (канон default view_mode=list)');

    await tester.tap(find.text(kViewGridLabel));
    await pumpFrames(tester);
    expect(find.byType(GridView), findsOneWidget,
        reason: 'переключение вида не применило плитку');
    expect(facade.savedSettings, <Map<String, Object?>>[
      <String, Object?>{'key': 'view_mode', 'value': 'grid'},
    ], reason: 'проводка save_setting не канонный ключ view_mode');

    // Повторный тап по активной цели: канон writes nothing (`if !==`), вызова нет.
    await tester.tap(find.text(kViewGridLabel));
    await pumpFrames(tester);
    expect(facade.savedSettings.length, 1,
        reason: 'повторный выбор того же вида wrote save_setting: канон '
            'app.js:7210-7215 пишет только при изменении');

    await tester.tap(find.text(kViewListLabel));
    await pumpFrames(tester);
    expect(find.byType(GridView), findsNothing,
        reason: 'возврат к списку не применил разметку');
    expect(find.text('Название'), findsOneWidget,
        reason: 'шапка колонок не вернулась в списке');
    expect(facade.savedSettings, <Map<String, Object?>>[
      <String, Object?>{'key': 'view_mode', 'value': 'grid'},
      <String, Object?>{'key': 'view_mode', 'value': 'list'},
    ], reason: 'второй выбор не сохранён тем же ключом');
  });

  testWidgets('отказ save_setting назван текстом, вид остаётся применён к списку',
      (tester) async {
    final facade = FakeFacade(total: 1, persistError: 'не выдан скоуп server_config',
        pages: <List<Map<String, Object?>>>[<Map<String, Object?>>[row(0)]]);
    await boot(tester, facade);
    await tester.tap(find.text(kViewGridLabel));
    await pumpFrames(tester);
    expect(find.byType(GridView), findsOneWidget,
        reason: 'отказ записи снял применённый вид: канон держит локальный вид');
    expect(find.textContaining('вид применён к списку, но не сохранён в настройках'),
        findsOneWidget,
        reason: 'отказ записи вида молчит: человек не знает, что выбор не переживёт перезапуск');
  });

  testWidgets('размер плитки М/С/К пишет tile_size и меняет число колонок '
      'по формуле канона', (tester) async {
    final facade = FakeFacade(total: 4,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0), row(1), row(2), row(3)],
        ]);
    await boot(tester, facade);
    expect(crossAxisCount(tester), 3, reason: 'средняя плитка не medium по канону TILE');

    await tester.tap(find.text(kSizeLargeLabel));
    await pumpFrames(tester);
    expect(crossAxisCount(tester), 2,
        reason: 'крупная плитка не пересчитала колонки (TILE.large w=320 gap=12)');
    expect(facade.savedSettings.last, <String, Object?>{'key': 'tile_size', 'value': 'large'},
        reason: 'размер плитки сохранён не тем ключом, что канон (app.js:7216-7223)');

    await tester.tap(find.text(kSizeSmallLabel));
    await pumpFrames(tester);
    expect(crossAxisCount(tester), 4,
        reason: 'мелкая плитка не пересчитала колонки (TILE.small w=160 gap=8)');
    expect(facade.savedSettings.last, <String, Object?>{'key': 'tile_size', 'value': 'small'},
        reason: 'размер мелкой плитки не сохранён ключом tile_size');
  });

  testWidgets('ряд размера скрыт в списке (канон hidden = !grid)',
      (tester) async {
    final facade = FakeFacade(total: 1,
        pages: <List<Map<String, Object?>>>[<Map<String, Object?>>[row(0)]]);
    await boot(tester, facade);
    expect(find.text(kSizeSmallLabel), findsNothing,
        reason: 'размер плитки рисуется в списке: канон size-toggle hidden '
            '= !grid (app.js:2979) - скрытый контрол не в Tab-обходе');
    await tester.tap(find.text(kViewGridLabel));
    await pumpFrames(tester);
    expect(find.text(kSizeSmallLabel), findsOneWidget,
        reason: 'ряд размера не появился в режиме плитки');
  });

  // -- обложки: ленивое чтение с диска ---------------------------------------

  testWidgets('обложка читается из data-URI и кэшируется: повторный прогон '
      'не спрашивает повторно', (tester) async {
    final uri = thumbUri();
    final facade = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        thumbUris: <String, String>{'0': uri},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0, hasThumb: true)],
        ]);
    await boot(tester, facade);
    expect(facade.thumbRequests, <List<int>>[<int>[0]],
        reason: 'батч обложек не вызван целью get_thumbs с списком id');
    expect(find.byType(Image), findsOneWidget,
        reason: 'data-URI обложки не показан: плитка не прочитала превью с диска');
    expect(find.text(kNoThumbLabel), findsNothing,
        reason: 'прочитанная обложка подписана заглушкой «нет обложки»');

    // Возврат к списку и обратно: тот же id уже в кэше - нового вызова нет
    // (тот же канон paintCachedThumbs app.js:3001-3011).
    await tester.tap(find.text(kViewListLabel));
    await pumpFrames(tester);
    await tester.tap(find.text(kViewGridLabel));
    await pumpFrames(tester);
    expect(facade.thumbRequests.length, 1,
        reason: 'кэш обложек не пережил смену вида: один и тот же файл '
            'спрашивается повторно (канон LRU-кэш app.js:2993-2999)');
    expect(find.byType(Image), findsOneWidget,
        reason: 'после возврата к плитке обложка не показана из кэша');
  });

  testWidgets('чего нет в ответе -> заглушка «нет обложки» и повторного '
      'запроса нет', (tester) async {
    final facade = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        thumbUris: <String, String>{},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0, hasThumb: true)],
        ]);
    await boot(tester, facade);
    expect(find.text(kNoThumbLabel), findsOneWidget,
        reason: 'отсутствующая в ответе обложка молчит: канон кэширует '
            'пустышкой (app.js:3032-3036) и рисует tile-nothumb');
    await pumpFrames(tester);
    expect(facade.thumbRequests.length, 1,
        reason: 'пустышка не кэширована: тот же id спрашивается на каждом кадре');
  });

  testWidgets('has_thumb false -> заглушка без запроса обложки',
      (tester) async {
    final facade = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        thumbUris: <String, String>{'0': thumbUri()},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0, hasThumb: false)],
        ]);
    await boot(tester, facade);
    expect(facade.thumbRequests, isEmpty,
        reason: 'обложка спрашивается для строки без файла: канон tileHtml '
            'рисует заглушку и не кладёт id в очередь (app.js:3535-3538)');
    expect(find.text(kNoThumbLabel), findsOneWidget,
        reason: 'строка без обложки не названа заглушкой канона');
  });

  testWidgets('битый data-URI -> заглушка «нет обложки», не молчит',
      (tester) async {
    final facade = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        // Тип вне THUMB_MIME бэкенда (app/gui.py:71-73) - картинка не декодируется.
        thumbUris: <String, String>{'0': 'data:application/octet-stream;base64,AAA='},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0, hasThumb: true)],
        ]);
    await boot(tester, facade);
    expect(find.text(kNoThumbLabel), findsOneWidget,
        reason: 'битая обложка молчит: форма названа расхождением (браузер '
            'рисует сломанный img без подписи, здесь заглушка названа)');
    expect(find.byType(Image), findsNothing,
        reason: 'битый data-URI попытки показать как картинку');
  });

  testWidgets('пока батч не answered - подложка ожидания без подписи',
      (tester) async {
    final facade = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        thumbPending: true,
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0, hasThumb: true)],
        ]);
    await boot(tester, facade);
    expect(facade.thumbRequests, <List<int>>[<int>[0]],
        reason: 'ожидание ответа не вызвало батч get_thumbs');
    expect(find.text(kNoThumbLabel), findsNothing,
        reason: 'ожидание ответа подписано «нет обложки»: канон держит img '
            'без src без подписи (app.js:3003-3010), заглушка arises только '
            'когда ответа нет');
    expect(find.byType(Image), findsNothing,
        reason: 'ожидание ответа показало картинку до ответа бэкенда');
  });

  testWidgets('обложки коалесятся одним батчем на кадр и не превышают лимит канона',
      (tester) async {
    final Map<String, String> uris = <String, String>{};
    final List<Map<String, Object?>> rows = <Map<String, Object?>>[];
    final String uri = thumbUri();
    for (int id = 0; id < 12; id++) {
      uris['$id'] = uri;
      rows.add(row(id, hasThumb: true));
    }
    final facade = FakeFacade(total: 12,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        thumbUris: uris,
        pages: <List<Map<String, Object?>>>[rows]);
    await boot(tester, facade);
    expect(facade.thumbRequests.length, 1,
        reason: 'плитки одного кадре спрашивают обложки несколькими вызовами: '
            'канон коалесятся таймером 120 мс (app.js:3020-3042)');
    expect(facade.thumbRequests.first.length, lessThanOrEqualTo(kThumbBatchMax),
        reason: 'батч превысил канонный лимит THUMB_BATCH (app.js:34, '
            'app/gui.py:70 - 100 ид за вызов)');
    // Окно виртуализации: спрашиваются только построенные (видимые) ячейки.
    expect(find.byType(Image), findsAtLeastNWidgets(1),
        reason: 'видимые плитки не прочитали обложки');
    final int shown = find.byType(Image).evaluate().length;
    expect(shown, lessThanOrEqualTo(facade.thumbRequests.first.length),
        reason: 'обложек показано больше, чем прочитано ид в батче: '
            'запрос не соответствует окну канона (app.js:3044-3055)');
  });

  testWidgets('отказ обложек назван текстом, ids не зависают в очереди',
      (tester) async {
    final facade = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        thumbsRefusal: 'мок: обложки не читаются',
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0, hasThumb: true)],
        ]);
    await boot(tester, facade);
    expect(find.textContaining('обложки не прочитаны: мок: обложки не читаются'),
        findsOneWidget,
        reason: 'отказ чтения обложек молчит: канон .catch снимает ids без '
            'текста (app.js:3038-3040) - здесь отказ назван, молчания нет');
    expect(facade.thumbRequests.length, 1,
        reason: 'ids остались в очереди после отказа: тот же id спрашивается '
            'несколько раз в одном кадре');
    expect(find.text(kNoThumbLabel), findsOneWidget,
        reason: 'отказ не показал заглушку: человек видит плитку без причины');
  });

  // -- цели плитки: выбор, плеер, карточка -----------------------------------

  testWidgets('чекбокс «Выбрать плитку» отмечает строку и не открывает карточку',
      (tester) async {
    final facade = FakeFacade(total: 2,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0), row(1)],
        ]);
    final handle = tester.ensureSemantics();
    await boot(tester, facade);

    await tester.tap(find.byType(Checkbox).first);
    await pumpFrames(tester);
    expect(find.textContaining('Выбрано: 1'), findsOneWidget,
        reason: 'выбор из плитки не ведёт панель целей канона');
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'чекбокс плитки открыл карточку: канон гасит click строки '
            '(`.row-check` stopPropagation, app.js:3423-3427)');
    // Состояние галки передается AT: flagsCollection.isChecked в этом SDK -
    // ui.CheckedState (факт flutter_test: controller.dart:362), не bool.
    // После тапа строка выбрана - галка отмечена (тот же канон: checkbox
    // плитки отражает state.selected, app.js:3542).
    expect(tester.getSemantics(find.bySemanticsLabel(kTileCheckAria).first)
        .flagsCollection.isChecked, ui.CheckedState.isTrue,
        reason: 'выбор из плитки не передан AT как отмеченная галка');
    handle.dispose();
  });

  testWidgets('play-кнопка плитки ведёт /watch/<id>; без права watch называет отказ',
      (tester) async {
    final opener = FakeOpener();
    final facade = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        scopes: <String>['read', 'watch'],
        pages: <List<Map<String, Object?>>>[<Map<String, Object?>>[row(0)]]);
    await tester.pumpWidget(OmnistashApp(facade: facade, opener: opener));
    await pumpFrames(tester);

    await tester.tap(find.text('▶').first);
    await pumpFrames(tester);
    expect(opener.opened, <String>['https://fake.test/watch/0'],
        reason: 'play из плитки не открыл страницу плеера тем же адресом, что список');

    final denied = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        scopes: <String>['read'],
        pages: <List<Map<String, Object?>>>[<Map<String, Object?>>[row(1)]]);
    await tester.pumpWidget(OmnistashApp(facade: denied, opener: opener));
    await pumpFrames(tester);
    await tester.tap(find.text('▶').first);
    await pumpFrames(tester);
    expect(find.textContaining(kScopeDeniedText), findsOneWidget,
        reason: 'play без права watch молчит: право подписывается до перехода '
            '(тот же канон data-need="watch", app.js:3545)');
    expect(opener.opened.length, 1,
        reason: 'без права watch адрес всё равно открылся');
  });

  testWidgets('одиночный клик плитки открывает карточку, двойной - плеер',
      (tester) async {
    final opener = FakeOpener();
    final facade = FakeFacade(total: 2, video: videoFixture(0),
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0), row(1)],
        ]);
    await tester.pumpWidget(OmnistashApp(facade: facade, opener: opener));
    await pumpFrames(tester);

    await tester.tap(find.text('Клип 0'));
    await tester.pump();
    await tester.pump(kDoubleTapTimeout);
    await pumpFrames(tester);
    expect(find.byType(VideoCardDialog), findsOneWidget,
        reason: 'одиночный клик по плитке не открыл карточку '
            '(тот же канон bindGridItems app.js:3400-3414 для .tile)');

    await tester.tap(find.byType(IconButton).first);
    await pumpFrames(tester);
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'выход карточки не закрыл диалог');

    await tester.tap(find.text('Клип 1'));
    await tester.pump(const Duration(milliseconds: 80));
    await tester.tap(find.text('Клип 1'));
    await tester.pump();
    await tester.pump(kDoubleTapTimeout);
    await pumpFrames(tester);
    expect(opener.opened, <String>['https://fake.test/watch/1'],
        reason: 'двойной клик по плитке не открыл плеер (dblclick app.js:3415-3419)');
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'двойной клик открыл карточку вместо плеера');
  });

  testWidgets('семантика плитки называет строку целиком и подписи канона целей',
      (tester) async {
    final facade = FakeFacade(total: 1,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[row(0, userRating: 3, watchedAt: '2026-10-27T09:00:00')],
        ]);
    final handle = tester.ensureSemantics();
    await boot(tester, facade);

    expect(find.bySemanticsLabel(RegExp('Клип 0')), findsWidgets,
        reason: 'плитка не названа строкой целиком для AT (§3.1)');
    final node = tester.getSemantics(
        find.byWidgetPredicate((Widget widget) => widget is Semantics &&
            (widget.properties.label ?? '').startsWith('Клип 0')).first);
    expect(node.label, contains('рейтинг 3'),
        reason: 'пометка рейтинга не передана AT (канон starBadge «рейтинг N»)');
    expect(node.label, contains('просмотрено'),
        reason: 'отметка «просмотрено» не передана AT (канон watchBadge)');
    expect(node.label, contains('в индексе'),
        reason: 'статус строки не передан подписью плитки');
    expect(find.bySemanticsLabel(kTileCheckAria), findsOneWidget,
        reason: 'галка плитки потеряла канонную подпись «Выбрать плитку» '
            '(app.js:3543)');
    expect(find.bySemanticsLabel(kTilePlayAria), findsOneWidget,
        reason: 'play-кнопка потеряла канонную подпись «Открыть в плеере» '
            '(app.js:3546)');
    expect(find.bySemanticsLabel(kViewToggleAria), findsOneWidget,
        reason: 'группа вида потеряла канонный aria-label «Вид библиотеки»');
    expect(find.bySemanticsLabel(kSizeToggleAria), findsOneWidget,
        reason: 'группа размера потеряла канонный aria-label «Размер плитки»');
    handle.dispose();
  });

  testWidgets('подпись плитки: канал · длительность · «аудио» для аудио-дорожки',
      (tester) async {
    final facade = FakeFacade(total: 2,
        settingsExtra: <String, Object?>{'view_mode': 'grid'},
        pages: <List<Map<String, Object?>>>[
          <Map<String, Object?>>[
            row(0, mediaKind: 'audio', mediaLabel: 'аудио'),
            row(1),
          ],
        ]);
    await boot(tester, facade);
    expect(find.text('Канал 0 · 2:05 · аудио'), findsOneWidget,
        reason: 'tile-sub не в канонной форме (app.js:3553-3558: канал, '
            '« · » длительность, « · аудио» для аудио-дорожки)');
    expect(find.text('Канал 1 · 2:06'), findsOneWidget,
        reason: 'видео-плитка подписана лишенним « · аудио»');
  });

  // -- чистые сверки формы (без виджетов) ------------------------------------

  test('формула колонок канона: размеры ряда и узкий экран не дают ноль', () {
    expect(tileColsFor(800, 'small'), 4);
    expect(tileColsFor(800, 'medium'), 3);
    expect(tileColsFor(800, 'large'), 2);
    expect(tileColsFor(300, 'large'), 1,
        reason: 'узкий экран дал 0 колонок: канон держит max(1, ...) (app.js:2971)');
    expect(tileColsFor(800, 'weird'), 3,
        reason: 'неизвестный размер не фолбэк medium (канон TILE[size] || TILE.medium)');
  });

  test('высота ячейки плиток - канонный ряд w/row, а не содержимое', () {
    // Формула виртуализации канона: высота ряда задаётся по TILE.row
    // (app.css:642-646), иначе неточность накрывает следующий ряд.
    expect(kTileSizes['medium']!.w / kTileSizes['medium']!.row, closeTo(240 / 218, 0.001));
  });

  test('data-URI обложки декодируется по типам бэкенда, прочее - заглушка', () {
    final Uint8List? png = decodeThumbDataUri(thumbUri());
    expect(png, isNotNull, reason: 'png data-URI бэкенда не декодировался');
    if (png == null) return;
    expect(decodeThumbDataUri('data:image/jpeg;base64,${base64Encode(png)}'),
        isNotNull, reason: 'jpeg (THUMB_MIME бэкенда) не декодировался');
    expect(decodeThumbDataUri('data:application/octet-stream;base64,AAA='), isNull,
        reason: 'тип вне THUMB_MIME показан картинкой');
    expect(decodeThumbDataUri('not-a-uri'), isNull,
        reason: 'строка без data-URI показана картинкой');
    expect(decodeThumbDataUri('data:image/png;base64,broken!'), isNull,
        reason: 'битый base64 показан картинкой вместо заглушки');
  });

  test('LRU-кэш обложек снимает старейший при превышении и держит пустышку', () {
    final ThumbCache cache = ThumbCache(capacity: 2);
    final Uint8List bytes = Uint8List.fromList(<int>[1, 2, 3]);
    cache.remember(1, bytes);
    cache.remember(2, bytes);
    cache.remember(3, bytes);
    expect(cache.has(1), false,
        reason: 'старейший id не снят: канон LRU снимает keys().next() '
            '(app.js:2996-2998)');
    expect(cache.has(3), true);
    cache.remember(4, null);
    expect(cache.has(4), true,
        reason: 'пустышка не удержана: absence в ответе кэшируется (app.js:3034-3036)');
    expect(cache.of(4), isNull, reason: 'пустышка читается как ожидание ответа');
    expect(cache.size, 2, reason: 'кэш превысил capacity канона');
  });
}
