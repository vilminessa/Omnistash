import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omnistash_ui/app.dart';
import 'package:omnistash_ui/library_view.dart';
import 'package:omnistash_ui/tabs.dart';

import 'fake_facade.dart';

Future<void> _pump(WidgetTester tester) async {
  // Библиотека опрашивает poll каждые kPollIntervalMs; переход табов
  // анимируется - кадры фиксированные, pumpAndSettle с таймером не завершится.
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 300));
  await tester.pump(Duration(milliseconds: kPollIntervalMs));
}

void main() {
  testWidgets('табы: четыре подписи в канонном порядке, активная вкладка выбрана',
      (tester) async {
    final facade = FakeFacade(total: 1, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0)],
    ]);
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    expect(kTabLabels, <String>['Библиотека', 'Синхронизация', 'Очередь', 'Настройки'],
        reason: 'порядок табов не тот, что значения TAB_RU в ui_src/app.js:5739');
    expect(find.byType(TabBar), findsOneWidget);
    expect(find.byType(Tab), findsNWidgets(kTabLabels.length));
    expect(find.byType(LibraryHome), findsOneWidget,
        reason: 'первая вкладка не построена: заглушка вместо фасада');

    // Эквивалент aria tablist/tab (TabBar строит роли сам): активная вкладка
    // названа выбранной, а не «подсвечена молча».
    final activeFlags =
        tester.getSemantics(find.byType(Tab).first).flagsCollection;
    expect(activeFlags.isSelected.toBoolOrNull(), true,
        reason: 'активная вкладка не названа selected - AT не скажет «выбран»');

    expect(find.textContaining('не построен'), findsNothing,
        reason: 'заглушки не построенных вкладок рисуются на активной вкладке');
    handle.dispose();
  });

  testWidgets('заглушки называют волну и не притворяются пустым экраном',
      (tester) async {
    final facade = FakeFacade(total: 1, pages: <List<Map<String, Object?>>>[
      <Map<String, Object?>>[row(0)],
    ]);
    await tester.pumpWidget(OmnistashApp(facade: facade));
    await _pump(tester);

    await tester.tap(find.text('Очередь'));
    await _pump(tester);
    expect(find.textContaining('не построен: волна W2'), findsOneWidget,
        reason: 'очередь не названа волной, где она ложится (W2 - очередь и журнал)');
    expect(find.textContaining('не построенный экран, а не пустая очередь'),
        findsOneWidget,
        reason: 'заглушка очереди выглядит как пустая очередь - это ложь');
    expect(find.text('Клип 0'), findsNothing,
        reason: 'панель библиотеки рисуется на вкладке очереди');

    await tester.tap(find.text('Синхронизация'));
    await _pump(tester);
    expect(find.textContaining('не построен: волна W3'), findsOneWidget,
        reason: 'синхронизация не названа волной W3 (mesh, участники, юр-гейты)');
    expect(find.textContaining('не построенный экран, а не пустой журнал'),
        findsOneWidget,
        reason: 'заглушка синхронизации выглядит как пустой журнал - это ложь');

    await tester.tap(find.text('Настройки'));
    await _pump(tester);
    expect(find.textContaining('не построен: волна W3'), findsOneWidget,
        reason: 'настройки не названы волной W3 (зеркало схемы в Dart)');
    expect(find.textContaining('не построен'), findsNWidgets(2),
        reason: 'панель настроек называет причину меньше одного раза');

    // Возврат на построенную вкладку: библиотека снова читает фасад.
    await tester.tap(find.text('Библиотека'));
    await _pump(tester);
    expect(find.text('Клип 0'), findsOneWidget,
        reason: 'возврат на вкладку библиотеки не показал список');
    expect(find.textContaining('не построен'), findsNothing,
        reason: 'заглушки рисуются поверх построенной библиотеки');
  });
}
