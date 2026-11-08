import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omnistash_ui/library_view.dart';

/// Полоса состояния ходит к poll каждые kPollIntervalMs: pumpAndSettle не
/// завершится с активным периодическим таймером, поэтому тест снимает кадры
/// фиксированными длительностями (тот же приём, что в шаге 3).
Future<void> pumpFrames(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 120));
  await tester.pump(Duration(milliseconds: kPollIntervalMs));
}

/// Счётчик показанных строк в шапке: «N из total» (табличные цифры).
String counterText(WidgetTester tester) =>
    tester.widget<Text>(find.textContaining(' из ')).data ?? '';

/// Кнопка панели выделения по подписи (Material ButtonStyleButton с
/// текстом-ребёнком канона `sel-bar`); в диалогах те же подписи не
/// сталкиваются: «Применить»/«Закрыть» диалога - отдельный ряд.
Finder barButton(String label) => find.byWidgetPredicate(
    (Widget widget) => widget is ButtonStyleButton &&
        widget.child is Text &&
        (widget.child as Text).data == label);
