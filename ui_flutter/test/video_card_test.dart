/// Тесты карточки видео (W2, шаг 3): поля из `get_video`, действия по
/// статусу и диалог §3.8 (APG Dialog (Modal)): имя, ловушка Tab на границе
/// focus-scope маршрута, Escape и возврат фокуса на инициатора.
///
/// Фокус читается семантикой (`flagsCollection.isFocused`) - тот же уровень,
/// на котором Narrator объявляет фокус, а не «по внутреннему узлу виджета».

library;

import 'dart:convert';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omnistash_ui/access.dart';
import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/library_view.dart';
import 'package:omnistash_ui/video_card.dart';

import 'fake_facade.dart';

Future<void> _pump(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 120));
  await tester.pump(Duration(milliseconds: kPollIntervalMs));
}

/// Поиск внутри карточки: экран списка под модальным барьером остаётся в
/// дереве и имеет свои подписи колонок («Статус», «Канал»), поэтому сверка
/// полей карточки ведётся внутри диалога, а не по всему дереву.
Finder _card(Finder finder) =>
    find.descendant(of: find.byType(VideoCardDialog), matching: finder);

/// Карточка скачанного видео с локальным файлом, плейлистом и пропадающим
/// файлом обложки - форма `repo.video_detail`.
Map<String, Object?> downloadedVideo() => videoFixture(
      7,
      files: <Map<String, Object?>>[
        videoFile('D:\\media\\clip.mp4'),
        videoFile('D:\\media\\cover.jpg', kind: 'thumbnail', missing: true),
      ],
      playlists: <Map<String, Object?>>[
        <String, Object?>{'title': 'Избранное', 'kind': 'manual', 'position': 3,
          'removed_at': null},
        <String, Object?>{'title': 'Часовой', 'kind': 'uploads', 'position': 1,
          'removed_at': '2026-11-02T00:00:00'},
      ],
    );

Future<void> _openCard(WidgetTester tester, FakeFacade facade, FakeOpener opener) async {
  await tester.pumpWidget(OmnistashApp(facade: facade, opener: opener));
  await _pump(tester);
  await tester.tap(find.text('Клип 7'));
  await tester.pump();
  await tester.pump(kDoubleTapTimeout);
  await _pump(tester);
}

/// Подпись строки-инициатора из `_rowSemantics` (row 7: канал 7 % 3 = 1,
/// длительность списка 125+7 = 132 с, обложка id%2 - нет).
const String kRowLabel = 'Клип 7, скачано, Канал 1, 2:12, видео, нет обложки';

/// Закрытие карточки по Escape (§3.8): тест открывает карточку повторно и
/// обязан сначала снять прежнюю - барьер диалога гасит pointer-события списка.
Future<void> _closeCard(WidgetTester tester) async {
  await tester.sendKeyEvent(LogicalKeyboardKey.escape);
  await _pump(tester);
}

void main() {
  test('audioMetaOf читает media=audio из format_json строки', () {
    final files = <Map<String, Object?>>[
      videoFile('D:\\media\\cover.jpg', kind: 'thumbnail'),
      videoFile('D:\\media\\song.mp3', formatJson: audioFormatJson()),
    ];
    final decoded = jsonDecode(files[1]['format_json'] as String);
    expect(decoded, isA<Map<String, Object?>>(),
        reason: 'format_json не разобран: текст ${files[1]['format_json']}');
    expect((decoded as Map<String, Object?>)['media'], 'audio',
        reason: 'ключ media не в разобранном тексте');
    final meta = audioMetaOf(files);
    expect(meta, isNotNull,
        reason: 'метки аудио не прочитаны из files.format_json (канон '
            'audioFileInfo в app.js:3750: kind=video и media == "audio")');
    expect(meta?['media'], 'audio', reason: 'ключ media прочитан неверно');
    expect(meta?['probe'], 'ffprobe', reason: 'ключ probe прочитан неверно');
    expect(cardFieldsOf(videoFixture(7, files: files))
        .where((field) => field.label == 'Длительность файла').length, 1,
        reason: 'поле «Длительность файла» не собрано из меток аудио');
  });

  testWidgets('карточка показывает поля из get_video 1-в-1 с каноном',
      (tester) async {
    final facade = FakeFacade(
      total: 1,
      video: downloadedVideo(),
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано')],
      ],
    );
    await _openCard(tester, facade, FakeOpener());

    expect(find.byType(VideoCardDialog), findsOneWidget,
        reason: 'карточка не открылась: список не имеет пути к деталям строки');
    expect(facade.calls, contains('get_video'),
        reason: 'карточка не прочитана через цель фасада get_video');

    // Подписи канона openDetail (ui_src/app.js:3581-3656).
    for (final label in <String>['Статус', 'Тип', 'Ключ', 'Канал', 'Опубликовано',
        'Длительность', 'Просмотров', 'Лайков', 'Комментариев', 'Синк данных',
        'Происхождение', 'URL', 'Первый раз замечено', 'Скачано', 'Обновлено',
        'Плейлисты', 'Файлы']) {
      expect(_card(find.text(label)), findsOneWidget,
          reason: 'подпись поля "$label" из канона не показана внутри карточки');
    }
    expect(_card(find.text('1:02:00')), findsOneWidget,
        reason: 'длительность 3720 с не в форме humanDuration (часы + pad секунд)');
    expect(_card(find.text('1 234 567')), findsOneWidget,
        reason: 'просмотры не разбиты разрядами (toLocaleString ru-RU)');
    expect(_card(find.text('youtube|UCaaaa|abc')), findsOneWidget,
        reason: 'ключ строки (data.key) не показан');
    expect(_card(find.textContaining('video: D:\\media\\clip.mp4')), findsOneWidget,
        reason: 'файл дорожки не назван строкой kind: path');
    expect(_card(find.textContaining('thumbnail: D:\\media\\cover.jpg [пропал]')),
        findsOneWidget,
        reason: 'пропавший файл не помечен [пропал] (канон app.js:3655)');
    expect(_card(find.textContaining('Избранное #3')), findsOneWidget,
        reason: 'плейлист не назван позицией (канон: title #position)');
    expect(_card(find.textContaining('Часовой (убран)')), findsOneWidget,
        reason: 'убранный плейлист не помечен (канон: title (убран))');
    expect(_card(find.text('2026-10-25')), findsOneWidget,
        reason: 'дата публикации не в форме shortDate (первые 10 символов iso)');
    expect(_card(find.text('2026-10-25 12:00')), findsOneWidget,
        reason: 'момент скачивания не в форме shortTime (T на пробел, 16 символов)');
    expect(_card(find.text('2026-10-26 08:12')), findsOneWidget,
        reason: 'синк данных не в форме shortTime');
  });

  testWidgets('карточка аудио показывает метки файла и обложку из тега',
      (tester) async {
    final facade = FakeFacade(
      total: 1,
      video: videoFixture(
        7,
        status: 'downloaded',
        mediaKind: 'audio',
        duration: null,
        files: <Map<String, Object?>>[
          videoFile('D:\\media\\song.mp3',
              formatJson: audioFormatJson(duration: 254.3)),
          videoFile('D:\\media\\cover.jpg', kind: 'thumbnail'),
        ],
      ),
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано')],
      ],
    );
    await _openCard(tester, facade, FakeOpener());

    for (final label in <String>['Длительность файла', 'Битрейт', 'Частота',
        'Каналы', 'Кодекс', 'Исполнитель', 'Альбом', 'Жанр', 'Дата релиза',
        'Номер трека', 'Тег читан из', 'Обложка']) {
      expect(_card(find.text(label)), findsOneWidget,
          reason: 'подпись блока меток аудио "$label" из канона не показана');
    }
    expect(_card(find.text('4:14')), findsOneWidget,
        reason: 'дробная длительность из ffprobe не округлена до целых '
            '(иначе выглядела бы 4:14.300000000000011)');
    expect(_card(find.text('320 кбит/с')), findsOneWidget,
        reason: 'битрейт не в форме кбит/с (app.js:3625)');
    expect(_card(find.text('44.1 кГц')), findsOneWidget,
        reason: 'частота не в форме кГц (app.js:3628)');
    expect(_card(find.text('из тега файла')), findsOneWidget,
        reason: 'обложка в теге файла не названа (канон app.js:3642)');
    // Значения меток читаются из format_json (app/audio.py: meta), а не
    // выдумываются: подпись и значение различаются текстом.
    expect(_card(find.text('Меломан')), findsOneWidget,
        reason: 'исполнитель из тега не показан значением поля');
    expect(_card(find.text('Ромашка')), findsOneWidget,
        reason: 'альбом из тега не показан значением поля');
    expect(_card(find.text('Электронная')), findsOneWidget,
        reason: 'жанр из тега не показан значением поля');
    expect(_card(find.text('ID3v2')), findsOneWidget,
        reason: 'схема тегов (tags_scheme) не показана значением поля');
  });

  testWidgets('без ffprobe метки названы «не получены» с причиной, а не молчат',
      (tester) async {
    final facade = FakeFacade(
      total: 1,
      video: videoFixture(
        7,
        status: 'downloaded',
        mediaKind: 'audio',
        files: <Map<String, Object?>>[
          videoFile('D:\\media\\song.mp3',
              formatJson: audioFormatJson(probe: 'no probe',
                  probeNote: 'ffprobe не установлен: длительность и битрейт '
                      'не прочитаны', duration: null, bitrate: null,
                  sampleRate: null, channels: null, codec: null)),
        ],
      ),
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано')],
      ],
    );
    await _openCard(tester, facade, FakeOpener());

    expect(_card(find.textContaining('ffprobe не установлен')), findsOneWidget,
        reason: 'метки аудио без ffprobe не названы причиной из probe_note '
            '(канон app.js:3622)');
    expect(_card(find.text('не получена')), findsOneWidget,
        reason: 'длительность файла без ffprobe должна быть названа '
            '«не получена», а не молча пропущена');
    expect(_card(find.text('не получена (в теге файла её нет)')), findsOneWidget,
        reason: 'обложка без тега не названа причиной отсутствия');
    expect(_card(find.text('Битрейт')), findsNothing,
        reason: 'битрейт без значения выдуман: пустое поле не рисуется');
  });

  testWidgets('отказ get_video назван текстом, карточка не выглядит пустой',
      (tester) async {
    final facade = FakeFacade(
      total: 1,
      videoRefusal: 'Нет видео по id 7',
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано')],
      ],
    );
    await _openCard(tester, facade, FakeOpener());
    expect(_card(find.textContaining('Отказ: Нет видео по id 7')), findsOneWidget,
        reason: 'отказ фасада внутри 200 скрыт: карточка выглядела бы пустой');
    expect(_card(find.text('Плейлисты')), findsNothing,
        reason: 'при отказе поля канона не должны рисоваться из подменённых данных');
    await _closeCard(tester);

    final absent = FakeFacade(
      total: 1,
      videoAbsent: true,
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано')],
      ],
    );
    await _openCard(tester, absent, FakeOpener());
    expect(_card(find.textContaining('карточка не прочитана')), findsOneWidget,
        reason: 'ответ null (app/gui.py get_video -> None) назван как '
            '«не найдено»: браузер молча выходил, Flutter называет причину');
    await _closeCard(tester);
  });

  testWidgets('«Открыть» шлёт /watch/<id>, проводник папки назван не построенным',
      (tester) async {
    final facade = FakeFacade(
      total: 1,
      video: downloadedVideo(),
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано')],
      ],
    );
    final opener = FakeOpener();
    await _openCard(tester, facade, opener);

    expect(_card(find.text('Открыть')), findsOneWidget,
        reason: 'скачанное с локальным файлом не имеет кнопки «Открыть» '
            '(канон app.js:3683)');
    await tester.tap(_card(find.text('Открыть')));
    await _pump(tester);
    expect(opener.opened, <String>['https://fake.test/watch/7'],
        reason: '«Открыть» не открыл страницу сервера /watch/<id>');
    expect(_card(find.textContaining('плеер открыт')), findsOneWidget,
        reason: 'открытие плеера не названо ответом: человек не видит, что произошло');
    expect(_card(find.textContaining('проводник с выделенным файлом')), findsOneWidget,
        reason: '«Открыть папку» не названо нативной целью: путь вместо кнопки - '
            'расхождение должно быть видно, а не молчать');
    expect(_card(find.textContaining('путь файла показан строкой')), findsOneWidget,
        reason: 'карточка не говорит, чем заменён проводник папки');
    expect(_card(find.textContaining('волна W3')), findsWidgets,
        reason: 'mesh-запрос и провенанс не названы построенными на волне W3');
    expect(_card(find.textContaining('Открыть на площадке')), findsOneWidget,
        reason: 'адрес площадки (webpage_url) не имеет пути открытия');
    await tester.tap(_card(find.text('Открыть на площадке')));
    await _pump(tester);
    expect(opener.opened, <String>['https://fake.test/watch/7',
      'https://www.youtube.com/watch?v=abc'],
        reason: '«Открыть на площадке» не открыл webpage_url строки');
    await _closeCard(tester);
  });

  testWidgets('«Открыть» нет у строки без локального файла и у known',
      (tester) async {
    final missing = FakeFacade(
      total: 1,
      video: videoFixture(7, files: <Map<String, Object?>>[
        videoFile('D:\\media\\clip.mp4', missing: true),
      ]),
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано')],
      ],
    );
    await _openCard(tester, missing, FakeOpener());
    expect(_card(find.text('Открыть')), findsNothing,
        reason: '«Открыть» показана для файла, который пропал: плеер получил бы '
            'отказ на пустом файле');
    await _closeCard(tester);

    final known = FakeFacade(
      total: 1,
      video: videoFixture(7, status: 'known', label: 'в индексе'),
      storages: <Map<String, Object?>>[storage('main')],
      defaultStorage: 'main',
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7)],
      ],
    );
    await _openCard(tester, known, FakeOpener());
    expect(_card(find.text('Открыть')), findsNothing,
        reason: 'у строки «в индексе» нет локального файла - кнопки плеера нет');
    expect(_card(find.text('Скачать')), findsOneWidget,
        reason: 'у known нет пути скачать (канон: known/failed/missing/detached)');
    expect(known.calls, contains('storages_list'),
        reason: 'выбор хранилища не прочитан: фоновая очередь не может спрашивать');
  });

  testWidgets('«Скачать» ставит строку в очередь тем же аргументом',
      (tester) async {
    final facade = FakeFacade(
      total: 1,
      queued: 1,
      video: videoFixture(7, status: 'known', label: 'в индексе'),
      storages: <Map<String, Object?>>[
        storage('main'),
        storage('disk2', label: 'Второй диск', available: false),
      ],
      defaultStorage: 'disk2',
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7)],
      ],
    );
    await _openCard(tester, facade, FakeOpener());

    expect(_card(find.textContaining('Второй диск (нет носителя)')), findsOneWidget,
        reason: 'хранилище без носителя не помечено (канон storageOptionsHtml)');
    await tester.tap(_card(find.text('Скачать')));
    await _pump(tester);

    expect(facade.enqueueRequests, <Map<String, Object?>>[
      <String, Object?>{'ids': <int>[7], 'storage_id': 'disk2'},
    ],
        reason: 'enqueue послан не той проводкой, что браузер (ids + storage_id)');
    expect(_card(find.textContaining('В очередь поставлено: 1')), findsOneWidget,
        reason: 'ответ очереди не назван текстом канона (app.js:3886)');
    await _closeCard(tester);
  });

  testWidgets('отказ enqueue и отказ хранилищ названы текстом', (tester) async {
    final refused = FakeFacade(
      total: 1,
      enqueueRefusal: 'Хранилище не найдено или отвязано',
      video: videoFixture(7, status: 'known', label: 'в индексе'),
      storages: <Map<String, Object?>>[storage('main')],
      defaultStorage: 'main',
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7)],
      ],
    );
    await _openCard(tester, refused, FakeOpener());
    await tester.tap(_card(find.text('Скачать')));
    await _pump(tester);
    expect(_card(find.textContaining('Хранилище не найдено или отвязано')),
        findsOneWidget,
        reason: 'отказ очереди скрыт: экран выглядел бы поставленным в очередь');
    await _closeCard(tester);

    final noStorages = FakeFacade(
      total: 1,
      video: videoFixture(7, status: 'known', label: 'в индексе'),
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7)],
      ],
    );
    await _openCard(tester, noStorages, FakeOpener());
    expect(_card(find.textContaining(kNoStoragesText)), findsOneWidget,
        reason: 'пустой список хранилищ назван текстом канона, а не молчит '
            '(app.js:3091)');
    await _closeCard(tester);

    final denied = FakeFacade(
      total: 1,
      scopes: <String>['read'],
      video: videoFixture(7, status: 'known', label: 'в индексе'),
      storages: <Map<String, Object?>>[storage('main')],
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7)],
      ],
    );
    await _openCard(tester, denied, FakeOpener());
    await tester.tap(_card(find.text('Скачать')));
    await _pump(tester);
    expect(denied.enqueueRequests, isEmpty,
        reason: 'enqueue вызван без права queue: сервер ответил бы 403 молча');
    expect(_card(find.textContaining(kScopeDeniedText)), findsOneWidget,
        reason: 'отказ по праву не назван текстом i18n');
    await _closeCard(tester);
  });

  testWidgets('диалог: вход на выход, Tab замыкается внутри, Escape возвращает фокус',
      (tester) async {
    final facade = FakeFacade(
      total: 1,
      video: downloadedVideo(),
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано')],
      ],
    );
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(OmnistashApp(facade: facade, opener: FakeOpener()));
    await _pump(tester);

    // `isFocused` в этом SDK - Tristate (тот же приём, что isSelected:
    // преобразование в bool, найдено фактом прогона).
    bool rowFocused() => tester.getSemantics(find.bySemanticsLabel(kRowLabel))
        .flagsCollection.isFocused.toBoolOrNull() ?? false;

    expect(rowFocused(), true,
        reason: 'список не имеет входа с клавиатуры: roving tabindex из app.js не перенесён');
    await tester.tap(find.text('Клип 7'));
    await tester.pump();
    await tester.pump(kDoubleTapTimeout);
    await _pump(tester);

    expect(find.byType(VideoCardDialog), findsOneWidget);
    expect(rowFocused(), false,
        reason: 'фокус остался на строке: модальный диалог не принял фокус');
    // Имя диалога - заголовок карточки (aria-labelledby="detail-title" в
    // ui_src/index.html:509). Узел семантики контейнера сливает подписи
    // потомков, поэтому сверка ведётся подстрокой, а не точным текстом.
    expect(find.bySemanticsLabel(RegExp('Клип 7')), findsWidgets,
        reason: 'карточка не названа своим заголовком: AT объявит безымянное окно');

    // Дефолт входа - выход (не-разрушительный): §3.8 и dialogEnter. Узел
    // выхода сливает подпись иконки, поэтому сверка ведётся подстрокой.
    expect(_card(find.bySemanticsLabel(RegExp('Закрыть'))), findsOneWidget);
    expect(tester.getSemantics(_card(find.bySemanticsLabel(RegExp('Закрыть'))))
        .flagsCollection.isFocused.toBoolOrNull(), true,
        reason: 'вход диалога не на выход: фокус на разрушительном действии');

    // Ловушка Tab: обход замыкается внутри карточки (closedLoop на маршруте),
    // и не уходит в затемнённый список (APG Dialog (Modal)).
    final state = tester.state<VideoCardDialogState>(find.byType(VideoCardDialog));
    final order = state.focusOrder();
    expect(order.length, 3,
        reason: 'карточка скачанного видео должна иметь выход, «Открыть» и '
            'площадку в порядке обхода');
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.pump();
    expect(order[1].hasFocus, true, reason: 'Tab не перешёл к следующему контролу');
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.pump();
    expect(order[2].hasFocus, true, reason: 'Tab не перешёл к площадке');
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.pump();
    expect(order[0].hasFocus, true,
        reason: 'Tab после последнего контрола вышел из диалога: ловушка '
            '(замкнутый обход) потеряна - AT читает затемнённый фон');
    // Shift+Tab - обратный ход по кругу (APG Dialog (Modal)).
    await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
    await tester.pump();
    expect(order[2].hasFocus, true,
        reason: 'Shift+Tab не развернул обход назад: ловушка работает только вперёд');
    expect(rowFocused(), false,
        reason: 'фокус ушёл в список под диалогом: ловушка не работает');
    expect(find.byType(VideoCardDialog), findsOneWidget,
        reason: 'обход закрыл диалог вместо замыкания');

    // Escape закрывает и возвращает фокус на инициатора.
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await _pump(tester);
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'Escape не закрыл карточку (§3.8: Escape закрывает диалог)');
    expect(rowFocused(), true,
        reason: 'фокус не вернулся на строку-инициатор (dialogLeave в app.js)');
    handle.dispose();
  });

  testWidgets('выход карточки закрывает диалог и возвращает фокус строке',
      (tester) async {
    final facade = FakeFacade(
      total: 1,
      video: downloadedVideo(),
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано')],
      ],
    );
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(OmnistashApp(facade: facade, opener: FakeOpener()));
    await _pump(tester);
    await tester.tap(find.text('Клип 7'));
    await tester.pump();
    await tester.pump(kDoubleTapTimeout);
    await _pump(tester);

    expect(tester.getSemantics(find.bySemanticsLabel(kRowLabel))
        .flagsCollection.isFocused.toBoolOrNull(), false,
        reason: 'фокус остался на списке под модальным диалогом');
    await tester.tap(_card(find.byType(IconButton)));
    await _pump(tester);
    expect(find.byType(VideoCardDialog), findsNothing,
        reason: 'выход не закрыл диалог: карточка остаётся модальной');
    expect(tester.getSemantics(find.bySemanticsLabel(kRowLabel))
        .flagsCollection.isFocused.toBoolOrNull(), true,
        reason: 'закрытие выходом не вернуло фокус на инициатора');
    handle.dispose();
  });

  testWidgets('карточка одна за раз: закрытая открывает новую, не стек',
      (tester) async {
    // Guard повторного открытия (`_cardOpen`) в тексте карточки назван
    // «карточка уже открыта». Его путь с барьером в этом SDK не симулируется:
    // flutter_test не имеет API семантических действий (нет performSemanticsAction
    // - найдено чтением packages/flutter_test), а pointer-барьер гасит список,
    // поэтому тест сверяет инвариант: диалог ровно один и после закрытия
    // открывается следующий, а не два поверх.
    final facade = FakeFacade(
      total: 2,
      video: downloadedVideo(),
      pages: <List<Map<String, Object?>>>[
        <Map<String, Object?>>[row(7, status: 'downloaded', label: 'скачано'), row(8)],
      ],
    );
    await _openCard(tester, facade, FakeOpener());
    expect(find.byType(VideoCardDialog), findsOneWidget,
        reason: 'первая карточка не открылась ровно одним диалогом');
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await _pump(tester);
    await tester.tap(find.text('Клип 8'));
    await tester.pump();
    await tester.pump(kDoubleTapTimeout);
    await _pump(tester);
    expect(find.byType(VideoCardDialog), findsOneWidget,
        reason: 'вторая карточка открылась вторым диалогом поверх: стек '
            'диалогов не построен, и их должно быть ровно N=1');
    expect(facade.calls.where((name) => name == 'get_video').length, 2,
        reason: 'обе карточки не прочитаны: вторая строка не имеет пути к деталям');
  });
}
