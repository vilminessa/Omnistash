import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omnistash_ui/main.dart';

/// Смоук W0: окно строится без сервера (режим mock-rows), виртуализированный
/// список показывает строки и статистика печатается в stdout теста.
void main() {
  testWidgets('spike окно строит виртуализированный список', (tester) async {
    runApp(SpikeApp(
      started: Stopwatch()..start(),
      api: null,
      rows: 60,
      scrollSeconds: 0.2,
    ));
    await tester.pump();
    expect(find.text('Библиотека'), findsOneWidget);
    expect(find.text('Строка 0: видео из библиотеки'), findsOneWidget);
    await tester.pump(const Duration(milliseconds: 300));
  });
}
