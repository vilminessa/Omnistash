/// Диалог-механизм §3.8 (APG Dialog (Modal)) для панелей волны W2 шаг 4.
///
/// Тот же канон, что `dialogEnter`/`dialogExit`/`dialogLeave` в
/// ui_src/app.js:4793-4816: имя диалога - заголовок карточки
/// (эквивалент `aria-labelledby` из ui_src/index.html:536-550); выход -
/// контрол по канону (`-decline`/`-later`, иначе `-close`), на него ведётся
/// вход (не-разрушительный дефолт §3.8) и Escape (браузер по Escape кликает
/// выход); ловушка Tab замыкается внутри диалога по DOM-порядку контролов
/// (`dialogTargets` app.js:4781 - скрытые и disabled цели не в обходе).
/// Граница focus-scope маршрута с `closedLoop` в этом SDK обход не замыкает
/// (факт прогона шага 3: после последнего контрола primaryFocus становится
/// null), поэтому круг ведётся явно в `Focus.onKeyEvent`; возврат фокуса на
/// инициатора делает вызывающий код после pop (post-frame - тот же факт).
///
/// Порядок обхода = порядок регистрации узла при build: карточка диалога
/// строит контролы в том же DOM-порядке, что канон (шапка с крестиком
/// первой, ряд действий последним), поэтому обход совпадает с
/// `dialogTargets` без отдельного списка.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'theme.dart';

/// Реестр узлов фокуса диалога: именованные узлы контролов (для входа на
/// выход и для сверки тестов) плюс обход по дереву цели scope'а диалога.
///
/// Порядок обхода ведётся по `scope.traversalDescendants`, а не по списку
/// регистрации: контролы, которые строит фокусные цели внутри себя
/// (`SegmentedButton` - ряд рейтинга), не принимают внешний `focusNode`, и
/// прежний список их не видел (факт шага 4: ряд из 5 целей оставался вне
/// круга Tab). disabled контролы (`canRequestFocus == false`) пропускаются -
/// тот же фильтр `dialogTargets` (`:not([disabled])`, app.js:4781-4786).
class DialogTrap {
  DialogTrap({required this.scope});

  /// Scope диалога: круг замыкается внутри него (§3.8).
  final FocusScopeNode scope;
  final Map<String, FocusNode> _named = <String, FocusNode>{};
  final List<FocusNode> _order = <FocusNode>[];
  bool _shiftDown = false;

  /// Узел контрола по имени (создаётся при первой регистрации).
  FocusNode node(String name) {
    final existing = _named[name];
    if (existing != null) return existing;
    final created = FocusNode();
    _named[name] = created;
    _order.add(created);
    return created;
  }

  List<FocusNode> get order => List<FocusNode>.unmodifiable(_order);

  /// Цели обхода внутри диалога в порядке обхода SDK.
  List<FocusNode> _targets() {
    final List<FocusNode> targets = <FocusNode>[];
    for (final FocusNode candidate in scope.traversalDescendants) {
      if (candidate.canRequestFocus) targets.add(candidate);
    }
    return targets;
  }

  void dispose() {
    for (final FocusNode node in _order) {
      node.dispose();
    }
  }

  /// Tab/Shift+Tab по кругу и Escape - выход. Shift ведётся локально по
  /// down/up (у `KeyEvent` в этом SDK нет модификаторов - найдено
  /// flutter analyze на шаге 3).
  KeyEventResult handle(KeyEvent event, {required VoidCallback escape}) {
    final LogicalKeyboardKey key = event.logicalKey;
    if (key == LogicalKeyboardKey.shiftLeft || key == LogicalKeyboardKey.shiftRight) {
      _shiftDown = event is KeyDownEvent;
      return KeyEventResult.ignored;
    }
    if (event is! KeyDownEvent) return KeyEventResult.ignored;
    if (key == LogicalKeyboardKey.escape) {
      escape();
      return KeyEventResult.handled;
    }
    if (key != LogicalKeyboardKey.tab) return KeyEventResult.ignored;
    final List<FocusNode> targets = _targets();
    if (targets.isEmpty) return KeyEventResult.ignored;
    final FocusNode? current = FocusManager.instance.primaryFocus;
    final int index = current == null ? -1 : targets.indexOf(current);
    final int step = _shiftDown ? -1 : 1;
    // `closedLoop` у границы focus-scope маршрута в этом SDK обход не замыкает
    // (факт шага 3: после последней цели primaryFocus становится null),
    // поэтому круг ведётся здесь: выход за scope - возврат на край ряда.
    final FocusNode next = index < 0
        ? (step < 0 ? targets.last : targets.first)
        : targets[(index + step + targets.length) % targets.length];
    next.requestFocus();
    return KeyEventResult.handled;
  }
}

/// Карточка диалога §3.8: шапка с выходом (крестик канона `-close`), тело из
/// колбэка, который регистрирует контролы в trap в DOM-порядке; имя узла -
/// заголовок (`aria-labelledby`). Вход - выход: `dialogEnter` ставит фокус
/// на отказ или крестик; у диалога без крестика (подтверждение правил по
/// канону `legalDialog`) выход - «Отклонить», `showExitIcon: false`.
///
/// `onExit` задаёт, что делает выход: по канону выход закрывает оверлей,
/// отказ (`decline`) называет причину отказом - колбэк caller решает,
/// что вернуть (pop(null)/pop(false)/pop(true)).
class DialogScaffold extends StatefulWidget {
  const DialogScaffold({
    super.key,
    required this.title,
    required this.exitName,
    required this.body,
    this.actions,
    this.showExitIcon = true,
    this.onExit,
  });

  final String title;
  /// Имя узла выхода (§3.8): «exit» - крестик, «decline» - отказ.
  final String exitName;
  final List<Widget> Function(BuildContext context, DialogTrap trap) body;
  /// Ряд целей диалога (§3.8, `.card-actions` канона) - рисуется ПОСЛЕ
  /// прокрутки тела, а не внутри неё: цели остаются видны, когда тело
  /// длиннее высоты окна (факт widget-теста шага 4: тап по «Применить»
  /// уходил за 600 px тест-поверхности и попадал мимо цели).
  final List<Widget> Function(BuildContext context, DialogTrap trap)? actions;
  final bool showExitIcon;
  final VoidCallback? onExit;

  @override
  State<DialogScaffold> createState() => _DialogScaffoldState();
}

class _DialogScaffoldState extends State<DialogScaffold> {
  final FocusScopeNode _scope = FocusScopeNode();
  late final DialogTrap _trap = DialogTrap(scope: _scope);

  @override
  void initState() {
    super.initState();
    // Вход диалога: фокус на выход - не-разрушительный дефолт (§3.8,
    // dialogEnter app.js:4801). post-frame: узел выходит из build этого
    // кадра, и `parent != null` - публичный признак «ещё в focus-дереве»
    // (шаг 3: `attached` в этом SDK нет).
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final FocusNode exit = _trap.node(widget.exitName);
      if (exit.parent != null) exit.requestFocus();
    });
  }

  @override
  void dispose() {
    _trap.dispose();
    _scope.dispose();
    super.dispose();
  }

  void _exit() {
    if (widget.onExit != null) {
      widget.onExit!();
      return;
    }
    Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    return Dialog(
      // Ширина канона `.card` - min(720px, 100%): 720px вне токен-шкалы,
      // здесь ширина по содержимому до доступной ширины маршрута (шаг 3).
      backgroundColor: theme.colorScheme.surface,
      insetPadding: EdgeInsets.all(kMetrics.s6),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(kMetrics.r4),
        side: BorderSide(color: theme.colorScheme.outlineVariant),
      ),
      child: Semantics(
        // Имя диалога - заголовок (эквивалент aria-labelledby карточки).
        container: true,
        label: widget.title,
        child: Focus(
          // Escape закрывает диалог (браузер кликает dialogExit по Escape),
          // Tab ведёт круг по trap.
          onKeyEvent: (FocusNode node, KeyEvent event) =>
              _trap.handle(event, escape: _exit),
          child: ConstrainedBox(
            // 86vh канона - доля высоты окна; здесь доступная высота маршрута
            // минус insetPadding (форма названа расхождением на шаге 3).
            constraints: BoxConstraints(
              maxHeight: MediaQuery.sizeOf(context).height - kMetrics.s6 * 2,
            ),
            child: FocusScope(
              // Scope диалога: круг Tab замыкается внутри (§3.8); ловушка
              // читает traversalDescendants этого scope, поэтому цели,
              // которые строит сам контрол (сегменты рейтинга), тоже в круге.
              node: _scope,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  Padding(
                    // .card-head 12px 14px: 12px = s4, 14px вне шкалы - s5.
                    padding: EdgeInsets.symmetric(
                        horizontal: kMetrics.s4, vertical: kMetrics.s5),
                    child: Row(
                      children: <Widget>[
                        Expanded(
                          child: Semantics(
                            header: true,
                            child: Text(widget.title,
                                style: theme.textTheme.titleMedium?.copyWith(
                                    fontWeight: FontWeight.w700),
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis),
                          ),
                        ),
                        if (widget.showExitIcon)
                          Semantics(
                            // Имя выхода: `aria-label="Закрыть"` у крестика
                            // канона (index.html:537,550) - то же имя здесь.
                            label: 'Закрыть',
                            button: true,
                            child: IconButton(
                              focusNode: _trap.node(widget.exitName),
                              onPressed: _exit,
                              icon: const Icon(Icons.close),
                              tooltip: 'Закрыть',
                            ),
                          ),
                      ],
                    ),
                  ),
                  Divider(color: theme.colorScheme.outlineVariant),
                  Flexible(
                    child: SingleChildScrollView(
                      padding: EdgeInsets.all(kMetrics.s5),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: widget.body(context, _trap),
                      ),
                    ),
                  ),
                  if (widget.actions != null)
                    Padding(
                      // Ряд целей вне прокрутки: тот же `.card-actions`
                      // канона (index.html), разрушительная цель последней
                      // (§3.5); подпись панели целей не дублируется - ряд
                      // внутри имени диалога.
                      padding: EdgeInsets.fromLTRB(kMetrics.s5, 0, kMetrics.s5, kMetrics.s5),
                      child: dialogActions(context, widget.actions!(context, _trap)),
                    ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Ряд полей диалога (`.bulk-row` канона: подпись + контрол). Подпись -
/// `field-label` канона (подписи полей из app.js), контрол - child.
Widget dialogField(BuildContext context, String label, Widget control) {
  final ThemeData theme = Theme.of(context);
  return Padding(
    padding: EdgeInsets.only(bottom: kMetrics.s3),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Expanded(
          flex: 1,
          child: Text(label, style: theme.textTheme.bodySmall?.copyWith(
              color: theme.colorScheme.onSurfaceVariant)),
        ),
        Expanded(flex: 2, child: control),
      ],
    ),
  );
}

/// Ряд целей диалога (`.card-actions` канона: --gap-cta между целями,
/// первичная - та, что кладёт первым app.js; разрушительная `.danger`
/// отдельной подписью и цветом err).
Widget dialogActions(BuildContext context, List<Widget> actions) {
  return Wrap(
    spacing: kMetrics.gapCta,
    runSpacing: kMetrics.s2,
    alignment: WrapAlignment.end,
    children: actions,
  );
}

/// Живый регион ответа внутри диалога (эквивалент тоста с `aria-live`
/// канона: отказ и сводка видны AT без фокуса).
Widget dialogNotice(BuildContext context, String notice) {
  final ThemeData theme = Theme.of(context);
  return Padding(
    padding: EdgeInsets.only(top: kMetrics.s3),
    child: Semantics(
      liveRegion: true,
      container: true,
      label: 'ответ действия: $notice',
      child: Semantics(
        excludeSemantics: true,
        child: Text(notice, style: theme.textTheme.bodySmall?.copyWith(
            color: theme.colorScheme.error)),
      ),
    ),
  );
}

/// muted-подсказка диалога (`.bulk-hint`/`.muted` канона: содержимое не
/// меняется само, живым регионом не помечено).
Widget dialogHint(BuildContext context, String text) {
  final ThemeData theme = Theme.of(context);
  return Padding(
    padding: EdgeInsets.only(bottom: kMetrics.s3),
    child: Text(text, style: theme.textTheme.bodySmall?.copyWith(
        color: theme.colorScheme.onSurfaceVariant)),
  );
}
