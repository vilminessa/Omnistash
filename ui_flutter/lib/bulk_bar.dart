/// Панель мультивыбора и диалоги операций (W2, шаг 4).
///
/// Канон: `sel-bar` из ui_src/index.html:173-201 (`role="toolbar"`,
/// `aria-label="Действия над выбранными"`, счётчик «Выбрано: N» с
/// `aria-live`, селект хранилища, ряд целей с разрушительной `.danger`),
/// диалог «Пометить…» (app.js:1370-1450, право `mark` -> `save_fields`),
/// диалог «Операции…» (app.js:1590-1759: теги и рейтинг под `mark`,
/// папка под `sources`, разрушительная цель `bulk_remove_rows` под
/// `sources` только после подтверждения диалога, называющего N - §3.7) и
/// разрушительный диалог по механизму `legalDialog` (app.js:1711-1732:
/// галка подтверждения, «Принять» до галки неактивна, вход - «Отклонить»
/// по §3.8). Проводки целей - те же формы, что `app/gui.py`
/// (bulk_download/bulk_tags/bulk_rating/bulk_collection/bulk_remove_rows/
/// save_fields/collections_list); сводки - строки канона из app.js, сверяемые
/// `tools/check_flutter_mirror.py` (тексты не выдумываются на клиенте).
///
/// Права подписываются до вызова (тот же `fetchAccess`+`applyAccess`: браузер
/// гасит цели без права, Flutter называет отказ текстом i18n `http.scope` на
/// шаге цели - расхождение формы названо в `access.dart`). «Операции…»
/// смешивает права (`mark` + `sources`): цели внутри диалога подписываются
/// отдельно - тот же канон (комментарий index.html:190-191), полоса не гасит
/// диалог целиком.
///
/// Гейт 18+ массовых операций (`adult_confirm` из app/gui.py: _bulk_adult)
/// назван волной W3: диалог «мне есть 18» не построен, повтор без
/// подтверждения бэкенд отвергнет - отказ показан текстом сервера.
///
/// Цели канона `sel-bar`, не построенные на этом шаге, названы в
/// `kToolbarTargetsNotBuilt` с волной: «В коллекцию…» и «Перенести…» вместе
/// с деревом папок и хранилищами ложатся на волну W3; папочные цели канона
/// («Убрать из папки», «Переименовать…», «Удалить папку») в каноне скрыты
/// для выбора пула (hidden до scope коллекции) - здесь их нет, каждая
/// названа.
library;

import 'package:flutter/material.dart';

import 'access.dart';
import 'api_client.dart';
import 'dialog_trap.dart';
import 'theme.dart';
import 'video_card.dart';

/// Подписи панели и диалогов - канонные строки (их сверяет страж против
/// ui_src/app.js и ui_src/index.html; клиент не выдумывает слова).
const String kSelCountPrefix = 'Выбрано: ';
const String kToolbarAria = 'Действия над выбранными';
const String kStorageLabel = 'Хранилище:';
const String kMarkLabel = 'Пометить…';
const String kBulkLabel = 'Операции…';
const String kDownloadLabel = 'Скачать';
const String kClearLabel = 'Снять выбор';
const String kCancelLabel = 'Отмена';
const String kCloseLabel = 'Закрыть';
const String kApplyLabel = 'Применить';
const String kResetLabel = 'сбросить';
const String kUnmarkLabel = 'снять отметку';
const String kFolderAddLabel = 'в папку';
const String kFolderRemoveLabel = 'из папки';
const String kTagsAddLabel = 'Теги добавить';
const String kTagsRemoveLabel = 'Теги убрать';
const String kRatingLabel = 'Рейтинг';
const String kFolderLabel = 'Папка';
const String kTagsLabel = 'Теги';
const String kNotesLabel = 'Заметка';
const String kWatchedLabel = 'просмотрено';
const String kTagsAddPlaceholder = 'через запятую, несколько';
const String kTagsRemovePlaceholder = 'теги, которые надо снять';
const String kTagsPlaceholder = 'через запятую';

/// Заголовки диалога пометок канона: «Пометить: 1 строку» / «N строк»
/// (особая форма app.js:1375-1376) и «Операции: N строка/строки/строк»
/// (плюрализация app.js:1473-1476).
const String kMarkPrefix = 'Пометить: ';
const String kBulkPrefix = 'Операции: ';
const List<String> kRowPlural = <String>['строка', 'строки', 'строк'];

/// Плюрализация канона `plural` (app.js:929-937): 1 запись / 2 записи /
/// 5 записей, 11..14 по mod 100 - третья форма.
String pluralRu(int n, List<String> forms) {
  final int mod100 = n % 100;
  final int mod10 = n % 10;
  if (mod100 >= 11 && mod100 <= 14) return forms[2];
  if (mod10 == 1) return forms[0];
  if (mod10 >= 2 && mod10 <= 4) return forms[1];
  return forms[2];
}

String pluralRows(int n) => '$n ${pluralRu(n, kRowPlural)}';

/// Заголовок диалога пометок - канонная особая форма («1 строку»).
String markTitleFor(int n) => '$kMarkPrefix$n${n == 1 ? ' строку' : ' строк'}';

/// Подпись группы в диалоге операций: пул - «N строка/строки/строк»;
/// папка целиком (bulkLabel app.js:1478-1480 «папка «имя»») не построена:
/// дерево папок - волна W3, названо в `kToolbarTargetsNotBuilt`.
String bulkLabelFor(int n) => pluralRows(n);

String bulkTitleFor(int n) => '$kBulkPrefix${bulkLabelFor(n)}';

/// Сводки канона (app.js): пропуск с причинами из бэка (`skipNote:1538`),
/// «строк в индексе нет» (`goneNote:1547`), ответ выкачки («В очередь: N из
/// M», downloadSelection app.js:1574), пометок («Помечено строк: N»,
/// app.js:1447), сводка apply по целям («Теги: ...», «Рейтинг N: ...»,
/// app.js:1654-1657, строки соединяются « · » - app.js:1663), папки
/// («В папку «имя»: x из y», app.js:1676), удаления («Убраны строки из
/// индекса: x из y (файлы на диске не тронуты)» - app.js:1726-1727).
const String kQueuedPrefix = 'В очередь: ';
const String kSkipPrefix = ' · пропущено: ';
const String kSkipJoin = ', ';
const String kGonePrefix = ' · строк в индексе нет: ';
const String kSummaryJoin = ' · ';
const String kTagsLine = 'Теги: ';
const String kRatingLine = 'Рейтинг ';
const String kMarkedPrefix = 'Помечено строк: ';
const String kCollectAddPrefix = 'В папку «';
const String kCollectRemovePrefix = 'Из папки «';
const String kCollectSuffix = '»: ';
const String kFromPrefix = ' из ';
const String kRemoveSuccessPrefix = 'Убраны строки из индекса: ';
const String kFilesKeptNote = ' (файлы на диске не тронуты)';
const String kDeleteCancelled = 'Удаление отменено';
const String kNothingFilled = 'Ничего не заполнено';
const String kNoFoldersOption = 'папок пока нет';
const String kNoStorageText = 'Не выбрано хранилище - добавьте папку в настройках';
const String kNoSelectionText = 'Сначала выберите строки или откройте папку';
/// Канон после отказа «Скачать» без хранилища идёт `switchTab("settings")`
/// (app.js:1566); вкладка Настройки - заглушка, переход назван расхождением.
const String kNoSettingsJump =
    'переход к настройкам не построен: вкладка Настройки - заглушка волны W3';
/// Гейт 18+ массовых операций: диалог «мне есть 18» (askAdult app.js:1483)
/// не построен - волна W3 вместе с юр-гейтами; бэкенд без подтверждения
/// повтор отвергнет, отказ показан текстом сервера (`adult_confirm`).
const String kAdultGateNotBuilt =
    'диалог 18+ не построен: волна W3 (гейт adult/legal вместе с юр-пунктами) '
    '- бэкенд без подтверждения повтор отвергнет';

/// Подсказки диалогов - канонные тексты (app.js:1391-1392 и 1612-1618).
const String kMarkHint =
    'Пустые поля не меняются: отмечается только то, что вы заполнили. '
    'По тегу ищется поиском в строке поиска.';
const String kBulkHint =
    'Теги - ваши локальные метки (по ним ищется поиск), теги площадки не '
    'меняются. «Из папки» снимает связи: строки видео остаются. «Скачать» '
    'не берёт строки, где файл уже на месте - они считаются пропуском и '
    'видны в сводке с причиной. Группа ограничена по размеру (страховка '
    'бэка: одна операция не должна переписывать всю библиотеку), слишком '
    'большую он откажет - разбейте на части. Кнопка удаления строк работает '
    'только после подтверждения диалога.';

/// Разрушительный диалог «Убрать строки из индекса» (app.js:1714-1720):
/// называет N группы, галка подтверждения, «Принять» до галки неактивна
/// (тот же `legal-accept disabled` index.html:624), вход - отказ (§3.8:
/// dialogExit по «-decline», app.js:4796-4799), Escape - отказ (браузер по
/// Escape кликает выход); без подтверждения бэк отказывает сам
/// («Нужно явное подтверждение удаления», app/gui.py bulk_remove_rows).
const String kRemoveConfirmTitle = 'Убрать строки из индекса';
const String kRemoveConfirmHead = 'Строки (';
const String kRemoveConfirmTail =
    ') уйдут из индекса: название, метаданные, пометки и связи с '
    'плейлистами/папками. ФАЙЛЫ НА ДИСКЕ НЕ удаляются - они остаются в '
    'хранилище, и переиндексация найдёт их заново.\n\nОтменить операцию '
    'можно только переиндексацией папок.';
const String kRemoveConfirmCheck = 'Я понимаю и подтверждаю';
const String kAcceptLabel = 'Принять';
const String kDeclineLabel = 'Отклонить';

/// Построенные цели панели в канонном порядке (`sel-bar` index.html:173-201:
/// «В коллекцию…», «Пометить…», «Перенести…», «Операции…», «Скачать»,
/// «Снять выбор», скрытая папочная цель). Право цели - тот же `data-need`
/// канона; «Операции…» права смешивает внутри диалога (`''` - полоса её не
/// гасит, канонный комментарий index.html:190-191).
const List<String> kToolbarBuilt =
    <String>[kMarkLabel, kBulkLabel, kDownloadLabel, kClearLabel];
const Map<String, String> kToolbarScopes = <String, String>{
  kMarkLabel: 'mark',
  kBulkLabel: '',
  kDownloadLabel: 'queue',
  kClearLabel: '',
};

/// Цели канона, не построенные на этом шаге: подпись -> причина и волна
/// (страж сверяет, что каждая названа и указывает волну).
const Map<String, String> kToolbarTargetsNotBuilt = <String, String>{
  'В коллекцию…': 'выбор папки для выбранных строк не построен: волна W3 '
      '(вместе с деревом папок и sources-панелью)',
  'Перенести…': 'перенос файлов выбранных строк в другое хранилище не '
      'построен: волна W3 (хранилища и перенос файлов)',
  'Убрать из папки': 'снятие связей выбранных строк с текущей папкой не '
      'построено: волна W3 (папка-коллекция как панель)',
  'Переименовать…': 'переименование папки не построено: волна W3',
  'Удалить папку': 'удаление папки-коллекции с диалогом, называющим N '
      'связей, не построено: волна W3',
};

/// Ключи пометок канона `save_fields` (whitelist app/repo.py:976-995 -
/// локальные поля человека; статус, хранилище и метаданные площадки отсюда
/// менять нельзя). Страж сверяет список с whitelist бэкенда.
const List<String> kMarkFieldKeys = <String>['user_rating', 'user_tags', 'notes', 'watched'];

String goneNoteOf(Map<String, Object?> res) {
  final Object? gone = res['gone'];
  return gone is num && gone > 0 ? '$kGonePrefix${gone.toInt()}' : '';
}

/// Ряд рейтинга - канонные звёздные цели (`stars` app.js:1378-1386: пять
/// целей + сброс). Форма названа расхождением: `SegmentedButton` из пяти
/// целей с подписями 1..5 плюс отдельная цель `kResetLabel`; radiogroup
/// ложится на AT только с волной W3. Семантика цели - `Semantics(selected:)`
/// у сегментов (М3 так и делает), выбранное значение читается AT; повторный
/// тап по выбранной цели рейтинг не сбрасывает (канон: звёздный ряд
/// остаётся), сброс - только `kResetLabel`.
///
/// Выбор формы - факт прогонов тест-рендерера этого SDK: `DropdownButton`
/// раздувает commit `flutter_tester` до 8.1-9.3 GB даже статично,
/// `ToggleButtons` с тапом и перестройкой - 8.35 GB, `Radio`-ряд - 7.8-8.4 GB,
/// `DropdownMenu` с выбором - 9.0 GB; `SegmentedButton` с перестройкой по
/// selection - 660 MB в том же `DialogScaffold` (факт probe'ов шага 4).
Widget ratingRow(BuildContext context, DialogTrap trap, int? rating,
    void Function(int? value) onChange) {
  return Wrap(
    spacing: kMetrics.s2,
    crossAxisAlignment: WrapCrossAlignment.center,
    children: <Widget>[
      SegmentedButton<int>(
        segments: const <ButtonSegment<int>>[
          ButtonSegment<int>(value: 1, label: Text('1')),
          ButtonSegment<int>(value: 2, label: Text('2')),
          ButtonSegment<int>(value: 3, label: Text('3')),
          ButtonSegment<int>(value: 4, label: Text('4')),
          ButtonSegment<int>(value: 5, label: Text('5')),
        ],
        selected: rating == null ? const <int>{} : <int>{rating},
        emptySelectionAllowed: true,
        showSelectedIcon: false,
        onSelectionChanged: (Set<int> next) {
          final int? value = next.isEmpty ? null : next.first;
          if (value == null) return;
          onChange(value);
        },
      ),
      TextButton(
        focusNode: trap.node('ratingReset'),
        onPressed: () => onChange(null),
        child: const Text(kResetLabel),
      ),
    ],
  );
}

/// Сводка пропуска выкачки: причины приходят из бэка (`reasons`
/// app/gui.py: _bulk_apply), UI их угадывает не - тот же `skipNote` канона
/// (app.js:1538-1544: « · пропущено: причина: n, ...»).
String skipNoteOf(Map<String, Object?> res) {
  final Object? reasons = res['reasons'];
  if (reasons is! Map) return '';
  final List<String> parts = <String>[];
  for (final MapEntry<Object?, Object?> entry in reasons.entries) {
    parts.add('${entry.key}: ${entry.value}');
  }
  return parts.isEmpty ? '' : '$kSkipPrefix${parts.join(kSkipJoin)}';
}

/// Массовый вызов с гейтом 18+: отказ сервера с полем `adult_confirm`
/// назван текстом сервера + волной гейта (браузер спрашивал «мне есть 18» и
/// повторял цель - askAdult app.js:1483-1497; диалог не построен, повтор
/// здесь отправлять нечем). Возвращает сводку или null (отказ уже назван
/// в notice).
Future<Map<String, Object?>?> bulkCall(Facade facade, Access access, String name,
    Map<String, Object?> payload, String scope, void Function(String) notice) async {
  if (!access.has(scope)) {
    notice(access.denial(scope));
    return null;
  }
  try {
    final Map<String, Object?> res = await facade.call(name, payload);
    if (res['adult_confirm'] == true) {
      final Object? error = res['error'];
      notice('${error is String ? error : 'отказ 18+ не назван'} · $kAdultGateNotBuilt');
      return null;
    }
    final Object? refusal = res['error'];
    if (refusal is String && refusal.isNotEmpty) {
      notice(refusal);
      return null;
    }
    return res;
  } on ApiFailure catch (failure) {
    notice(failure.reason);
    return null;
  } catch (error) {
    notice('$name не выполнен: $error');
    return null;
  }
}

/// Панель мультивыбора (`sel-bar` канона): появляется при nonzero выборе;
/// счётчик «Выбрано: N» - живой регион (эквивалент `aria-live="polite"`
/// index.html:175); селект хранилища - тот же `storageOptionsHtml`
/// (пустой список назван текстом канона); ряд целей в канонном порядке,
/// первичная «Скачать»; цели канона, не построенные сейчас, названы
/// строками ниже - полоса неполная, и это видно.
class SelectionBar extends StatelessWidget {
  const SelectionBar({
    super.key,
    required this.count,
    required this.storages,
    required this.storageId,
    required this.nodes,
    required this.onStorage,
    required this.onMark,
    required this.onBulk,
    required this.onDownload,
    required this.onClear,
  });

  final int count;
  final List<Map<String, Object?>> storages;
  final String storageId;
  /// Узлы фокуса целей: возврат фокуса на инициатор диалога (§3.8,
  /// dialogLeave app.js:4810) после pop делает caller post-frame.
  final Map<String, FocusNode> nodes;
  final ValueChanged<String?> onStorage;
  final VoidCallback onMark;
  final VoidCallback onBulk;
  final VoidCallback onDownload;
  final VoidCallback onClear;

  @override
  Widget build(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    final List<DropdownMenuItem<String>> items = <DropdownMenuItem<String>>[
      if (storages.isEmpty)
        const DropdownMenuItem<String>(value: '', child: Text(kNoStoragesText))
      else
        for (final Map<String, Object?> storage in storages)
          DropdownMenuItem<String>(value: '${storage['id']}', child: Text(storageLabel(storage))),
    ];
    // defaultStorageId канона (app.js:3076): выбранный id из ответа,
    // иначе первое активное хранилище.
    final String chosen = storages.any((Map<String, Object?> s) => '${s['id']}' == storageId)
        ? storageId
        : (storages.isEmpty ? '' : '${storages.first['id']}');
    return Semantics(
      // Эквивалент role="toolbar" + aria-label канона: панель названа;
      // отдельных toolbar-ролей в семантике Flutter нет - названо
      // расхождением (AT читает ряд кнопок внутри подписи панели).
      // container: true - подпись панели отдельным узлом: без неё label
      // merge'ится в ancestors и точный bySemanticsLabel теряет панель
      // (факт прогона widget-теста).
      container: true,
      label: kToolbarAria,
      child: Padding(
        padding: EdgeInsets.symmetric(horizontal: kMetrics.s4, vertical: kMetrics.s2),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Wrap(
              spacing: kMetrics.gapCta,
              runSpacing: kMetrics.s2,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: <Widget>[
                Semantics(
                  // Живый регион счётчика (aria-live у sel-count канона).
                  liveRegion: true,
                  container: true,
                  label: '$kSelCountPrefix$count',
                  child: Semantics(
                    excludeSemantics: true,
                    child: Text('$kSelCountPrefix$count',
                        style: theme.textTheme.bodySmall?.copyWith(
                            fontFeatures: const <FontFeature>[FontFeature.tabularFigures()])),
                  ),
                ),
                Text(kStorageLabel, style: theme.textTheme.bodySmall?.copyWith(
                    color: theme.colorScheme.onSurfaceVariant)),
                DropdownButton<String>(
                  focusNode: nodes['storage'],
                  value: chosen,
                  items: items,
                  onChanged: onStorage,
                ),
                OutlinedButton(focusNode: nodes['mark'], onPressed: onMark, child: const Text(kMarkLabel)),
                OutlinedButton(focusNode: nodes['bulk'], onPressed: onBulk, child: const Text(kBulkLabel)),
                FilledButton(focusNode: nodes['download'], onPressed: onDownload, child: const Text(kDownloadLabel)),
                OutlinedButton(focusNode: nodes['clear'], onPressed: onClear, child: const Text(kClearLabel)),
              ],
            ),
            // Цели канона, не построенные сейчас: названы подписью и
            // причиной с волной, а не молчат - человек видит, что полоса
            // неполная и что появится позже.
            for (final MapEntry<String, String> entry in kToolbarTargetsNotBuilt.entries)
              Padding(
                padding: EdgeInsets.only(top: kMetrics.s2),
                child: Text('${entry.key}: ${entry.value}',
                    style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant)),
              ),
          ],
        ),
      ),
    );
  }
}

/// Диалог «Пометить…» (канон openMark app.js:1370-1450): поля пишутся
/// только если заполнены или тронуты (`touched` канона: звёзды и галка
/// считаются изменёнными после взаимодействия); пустой набор - отказ
/// «Ничего не заполнено» без вызова цели; право `mark` подписывается до
/// `save_fields`; ответ сводки канона - «Помечено строк: N».
///
/// Звёздный ряд канона (5 целей с «сбросить») переложен dropdown-ом 1..5 +
/// «сбросить» (0) - форма названа расхождением, radiogroup ложится на W3.
class MarkDialog extends StatefulWidget {
  const MarkDialog({super.key, required this.facade, required this.ids, required this.access});

  final Facade facade;
  final List<int> ids;
  final Access access;

  @override
  State<MarkDialog> createState() => _MarkDialogState();
}

class _MarkDialogState extends State<MarkDialog> {
  final TextEditingController _tags = TextEditingController();
  final TextEditingController _notes = TextEditingController();
  int? _rating;
  bool _ratingTouched = false;
  bool _watched = false;
  bool _watchedTouched = false;
  String? _notice;

  @override
  void dispose() {
    _tags.dispose();
    _notes.dispose();
    super.dispose();
  }

  /// Поля по канону bindMark: touched и непустые строки только.
  Map<String, Object?> _fields() {
    final Map<String, Object?> fields = <String, Object?>{};
    if (_ratingTouched) fields['user_rating'] = _rating ?? 0;
    if (_tags.text.trim().isNotEmpty) fields['user_tags'] = _tags.text.trim();
    if (_notes.text.trim().isNotEmpty) fields['notes'] = _notes.text.trim();
    if (_watchedTouched) fields['watched'] = _watched;
    return fields;
  }

  Future<void> _apply() async {
    final Map<String, Object?> fields = _fields();
    if (fields.isEmpty) {
      setState(() => _notice = kNothingFilled);
      return;
    }
    if (!widget.access.has('mark')) {
      setState(() => _notice = widget.access.denial('mark'));
      return;
    }
    try {
      final Map<String, Object?> res = await widget.facade.call(
          'save_fields', <String, Object?>{'ids': widget.ids, 'fields': fields});
      final Object? refusal = res['error'];
      if (refusal is String && refusal.isNotEmpty) {
        setState(() => _notice = refusal);
        return;
      }
      if (!mounted) return;
      Navigator.of(context).pop('$kMarkedPrefix${res['updated'] ?? 0}');
    } on ApiFailure catch (failure) {
      setState(() => _notice = failure.reason);
    } catch (error) {
      setState(() => _notice = 'пометка не записалась: $error');
    }
  }

  @override
  Widget build(BuildContext context) {
    return DialogScaffold(
      title: markTitleFor(widget.ids.length),
      exitName: 'exit',
      body: (BuildContext context, DialogTrap trap) => <Widget>[
        dialogField(context, kRatingLabel, ratingRow(
          context, trap, _rating,
          (int? value) => setState(() {
            _rating = value;
            _ratingTouched = true;
          }),
        )),
        dialogField(context, kTagsLabel, TextField(
          focusNode: trap.node('tags'),
          controller: _tags,
          // Подсказка канона «через запятую» - через InputDecoration:
          // `placeholder` и `hintText` как shortcut-параметры TextField
          // в этом SDK (3.47) не определены (факт flutter analyze),
          // форма названа расхождением, текст - канонный.
          decoration: InputDecoration(hintText: kTagsPlaceholder),
        )),
        dialogField(context, kNotesLabel, TextField(
          focusNode: trap.node('notes'),
          controller: _notes,
          maxLines: 2,
        )),
        dialogField(context, kWatchedLabel, Wrap(
          spacing: kMetrics.s2,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: <Widget>[
            Semantics(
              label: kWatchedLabel,
              checked: _watched,
              child: Checkbox(
                focusNode: trap.node('watched'),
                value: _watched,
                onChanged: (bool? value) => setState(() {
                  _watched = value ?? false;
                  _watchedTouched = true;
                }),
              ),
            ),
            TextButton(
              focusNode: trap.node('watchedReset'),
              onPressed: () => setState(() {
                _watched = false;
                _watchedTouched = true;
              }),
              child: const Text(kUnmarkLabel),
            ),
          ],
        )),
        dialogHint(context, kMarkHint),
        if (_notice != null) dialogNotice(context, _notice!),
      ],
      actions: (BuildContext context, DialogTrap trap) => <Widget>[
        OutlinedButton(
          focusNode: trap.node('cancel'),
          // Отмена = выход канона (hidden overlay без записи;
          // не-разрушительный дефолт - §3.8).
          onPressed: () => Navigator.of(context).pop(),
          child: const Text(kCancelLabel),
        ),
        FilledButton(
          focusNode: trap.node('apply'),
          onPressed: _apply,
          child: const Text(kApplyLabel),
        ),
      ],
    );
  }
}

/// Диалог «Операции…» (канон openBulk app.js:1590-1759). Ряд целей - тот же
/// DOM-порядок канона (app.js:1620-1625): разрушительная «Убрать строки из
/// индекса», «Закрыть», первичная «Применить». Разрушительная цель работает
/// только после диалога, называющего N (§3.7; бэкенд без `confirm:true`
/// отказывает сам - app/gui.py bulk_remove_rows). `Применить` ведёт цели по
/// очереди (тот же канон: сводка каждой цели видна по порядку -
/// app.js:1643-1666). Папки читает `collections_list` под `read`.
class BulkDialog extends StatefulWidget {
  const BulkDialog({
    super.key,
    required this.facade,
    required this.ids,
    required this.access,
    required this.onReload,
  });

  final Facade facade;
  final List<int> ids;
  final Access access;
  /// Перерисовка строк после операции (канон renderGrid после сводки):
  /// caller перечитывает загруженные страницы.
  final VoidCallback onReload;

  @override
  State<BulkDialog> createState() => _BulkDialogState();
}

class _BulkDialogState extends State<BulkDialog> {
  final TextEditingController _tagsAdd = TextEditingController();
  final TextEditingController _tagsRemove = TextEditingController();
  int? _rating;
  bool _ratingTouched = false;
  List<Map<String, Object?>> _collections = const <Map<String, Object?>>[];
  int? _folderId;
  String? _notice;
  /// Узел разрушительной цели: на него возвращается фокус после диалога
  /// подтверждения (dialogLeave канона - инициатор диалога).
  FocusNode? _dangerNode;

  @override
  void initState() {
    super.initState();
    _loadFolders();
  }

  @override
  void dispose() {
    _tagsAdd.dispose();
    _tagsRemove.dispose();
    super.dispose();
  }

  /// Папки читает цель `collections_list` под `read` (дерево канона из
  /// get_initial не построено: W3) - отказ называется, молчания нет.
  Future<void> _loadFolders() async {
    if (!widget.access.has('read')) {
      setState(() => _notice = widget.access.denial('read'));
      return;
    }
    try {
      final Map<String, Object?> res = await widget.facade.call('collections_list', null);
      final Object? refusal = res['error'];
      if (refusal is String && refusal.isNotEmpty) {
        setState(() => _notice = 'папки не прочитаны: $refusal');
        return;
      }
      final List<Map<String, Object?>> list =
          (res['collections'] as List?)?.cast<Map<String, Object?>>() ?? const <Map<String, Object?>>[];
      setState(() {
        _collections = list;
        // default select канона (bulkCollectionOptions app.js:1580): без
        // текущей папки выбран первый список; пустой список назван текстом.
        _folderId = list.isEmpty ? null : (list.first['id'] as num).toInt();
      });
    } on ApiFailure catch (failure) {
      setState(() => _notice = 'папки не прочитаны: ${failure.reason}');
    } catch (error) {
      setState(() => _notice = 'папки не прочитаны: $error');
    }
  }

  Future<void> _collect(String action) async {
    // Тот же канон (runCollect app.js:1669-1671): collection_id кладётся
    // только когда папка выбрана; без неё бэкенд отказывает сам
    // («Не указана коллекция», app/gui.py:902-903) - текст отказа приходит
    // от сервера, клиент не выдумывает свою подпись.
    final int? cid = _folderId;
    final Map<String, Object?> payload = <String, Object?>{
      'ids': widget.ids, 'action': action,
    };
    if (cid != null && cid != 0) payload['collection_id'] = cid;
    final Map<String, Object?>? res = await bulkCall(widget.facade, widget.access,
        'bulk_collection', payload, 'sources', (String text) => setState(() => _notice = text));
    if (res == null) return;
    final Object? movedRaw = action == 'add' ? res['added'] : res['removed'];
    final int moved = movedRaw is num ? movedRaw.toInt() : 0;
    final String prefix = action == 'add' ? kCollectAddPrefix : kCollectRemovePrefix;
    setState(() {
      _notice = '$prefix${res['collection_name'] ?? ''}$kCollectSuffix$moved$kFromPrefix'
          '${res['total'] ?? 0}${goneNoteOf(res)}';
    });
    // Канон renderGrid после сводки: диалог остаётся открытым, строки
    // перерисовываются сразу.
    widget.onReload();
  }

  /// Применить - цепочка по очереди (канон app.js:1637-1666): сначала теги,
  /// потом рейтинг; отказ останавливает цепочку; сводки соединяются « · ».
  Future<void> _apply() async {
    final List<String> steps = <String>[];
    final List<String> lines = <String>[];
    final String add = _tagsAdd.text.trim();
    final String remove = _tagsRemove.text.trim();
    if (add.isNotEmpty || remove.isNotEmpty) steps.add('tags');
    if (_ratingTouched) steps.add('rating');
    if (steps.isEmpty) {
      setState(() => _notice = kNothingFilled);
      return;
    }
    for (final String step in steps) {
      if (step == 'tags') {
        final Map<String, Object?>? res = await bulkCall(widget.facade, widget.access,
            'bulk_tags', <String, Object?>{'ids': widget.ids, 'add': add, 'remove': remove},
            'mark', (String text) => setState(() => _notice = text));
        if (res == null) return;
        final Object? updated = res['updated'];
        lines.add('$kTagsLine${pluralRows(updated is num ? updated.toInt() : 0)}${goneNoteOf(res)}');
      } else {
        final int rating = _rating ?? 0;
        final Map<String, Object?>? res = await bulkCall(widget.facade, widget.access,
            'bulk_rating', <String, Object?>{'ids': widget.ids, 'rating': rating},
            'mark', (String text) => setState(() => _notice = text));
        if (res == null) return;
        final Object? updated = res['updated'];
        lines.add('$kRatingLine$rating: ${pluralRows(updated is num ? updated.toInt() : 0)}${goneNoteOf(res)}');
      }
    }
    widget.onReload();
    if (!mounted) return;
    Navigator.of(context).pop(lines.join(kSummaryJoin));
  }

  /// Разрушительная цель: право `sources` подписывается до диалога, диалог
  /// называет N группы, `confirm:true` ставит только галка человека (тот же
  /// канон; бэкенд без подтверждения отказывает сам).
  Future<void> _removeRows() async {
    if (!widget.access.has('sources')) {
      setState(() => _notice = widget.access.denial('sources'));
      return;
    }
    final bool? accepted = await showDialog<bool>(
      context: context,
      barrierColor: veilColor(themeIdOf(context)),
      barrierLabel: 'отклонить удаление',
      traversalEdgeBehavior: TraversalEdgeBehavior.closedLoop,
      requestFocus: false,
      animationStyle: AnimationStyle(
        duration: durationExpand(),
        curve: kMetrics.easeExpressive,
        reverseCurve: kMetrics.easeExit,
      ),
      routeSettings: const RouteSettings(name: 'confirm-remove'),
      builder: (BuildContext context) => ConfirmDialog(
        title: kRemoveConfirmTitle,
        label: bulkLabelFor(widget.ids.length),
        checkLabel: kRemoveConfirmCheck,
      ),
    );
    // Возврат фокуса на инициатор (dialogLeave): отказ/Escape возвращают
    // обход на разрушительную кнопку - post-frame после снятия маршрута.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final FocusNode? danger = _dangerNode;
      if (danger != null && danger.parent != null) danger.requestFocus();
    });
    if (accepted != true) {
      setState(() => _notice = kDeleteCancelled);
      return;
    }
    final Map<String, Object?>? res = await bulkCall(widget.facade, widget.access,
        'bulk_remove_rows', <String, Object?>{'ids': widget.ids, 'confirm': true},
        'sources', (String text) => setState(() => _notice = text));
    if (res == null) return;
    if (!mounted) return;
    final Object? removed = res['removed'];
    Navigator.of(context).pop('$kRemoveSuccessPrefix${removed is num ? removed.toInt() : 0}'
        '$kFromPrefix${res['total'] ?? 0}$kFilesKeptNote${goneNoteOf(res)}');
  }

  @override
  Widget build(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    final List<DropdownMenuItem<String>> folderItems = <DropdownMenuItem<String>>[
      if (_collections.isEmpty)
        const DropdownMenuItem<String>(value: '', child: Text(kNoFoldersOption))
      else
        for (final Map<String, Object?> folder in _collections)
          DropdownMenuItem<String>(
            value: '${folder['id']}',
            child: Text('${folder['name'] ?? ''} (${folder['total'] ?? 0})'),
          ),
    ];
    return DialogScaffold(
      title: bulkTitleFor(widget.ids.length),
      exitName: 'exit',
      body: (BuildContext context, DialogTrap trap) {
        _dangerNode = trap.node('danger');
        return <Widget>[
          dialogField(context, kTagsAddLabel, TextField(
            focusNode: trap.node('tagsAdd'),
            controller: _tagsAdd,
            decoration: InputDecoration(hintText: kTagsAddPlaceholder),
          )),
          dialogField(context, kTagsRemoveLabel, TextField(
            focusNode: trap.node('tagsRemove'),
            controller: _tagsRemove,
            decoration: InputDecoration(hintText: kTagsRemovePlaceholder),
          )),
          dialogField(context, kRatingLabel, ratingRow(
            context, trap, _rating,
            (int? value) => setState(() {
              _rating = value;
              _ratingTouched = true;
            }),
          )),
          dialogField(context, kFolderLabel, Wrap(
            spacing: kMetrics.s2,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: <Widget>[
              DropdownButton<String>(
                focusNode: trap.node('folder'),
                // Значение - выбранная папка; без неё канонный select держит
                // '' и сервер отказывает своим текстом («Не указана коллекция»,
                // app/gui.py:902-903), клиент не выдумывает подпись. Браузер при
                // пустом значении рисует первую подпись списка, Flutter - hint
                // '—': форма названа расхождением, поведение то же (цель уходит
                // без collection_id).
                value: _folderId == null ? '' : '$_folderId',
                hint: const Text('—'),
                items: folderItems,
                onChanged: (String? value) => setState(() => _folderId = int.tryParse(value ?? '')),
              ),
              OutlinedButton(
                focusNode: trap.node('collectAdd'),
                onPressed: () => _collect('add'),
                child: const Text(kFolderAddLabel),
              ),
              OutlinedButton(
                focusNode: trap.node('collectRemove'),
                onPressed: () => _collect('remove'),
                child: const Text(kFolderRemoveLabel),
              ),
            ],
          )),
          dialogHint(context, kBulkHint),
          if (_notice != null) dialogNotice(context, _notice!),
        ];
      },
      actions: (BuildContext context, DialogTrap trap) => <Widget>[
          OutlinedButton(
            // Разрушительная цель `.danger` канона (app.js:1621): цвет err
            // из реестра, работает только после подтверждения диалога с N.
            focusNode: _dangerNode!,
            style: OutlinedButton.styleFrom(
              foregroundColor: theme.colorScheme.error,
              side: BorderSide(color: theme.colorScheme.error),
            ),
            onPressed: _removeRows,
            child: const Text(kRemoveConfirmTitle),
          ),
          OutlinedButton(
            focusNode: trap.node('close'),
            onPressed: () => Navigator.of(context).pop(),
            child: const Text(kCloseLabel),
          ),
          FilledButton(
            focusNode: trap.node('apply'),
            onPressed: _apply,
            child: const Text(kApplyLabel),
          ),
        ],
      );
  }
}

/// Разрушительный диалог по механизму `legalDialog` (канон правил и 18+,
/// index.html:613-626): галка подтверждения, «Принять» до галки неактивна
/// (`legal-accept disabled`), вход - «Отклонить» (§3.8 дефолт
/// не-разрушительный, dialogExit по `-decline`), Escape - отказ. Возвращает
/// true только после явной галки и «Принять»; отказ и закрытие - false/null,
/// caller называет «Удаление отменено» (канон app.js:1722).
class ConfirmDialog extends StatefulWidget {
  const ConfirmDialog({
    super.key,
    required this.title,
    required this.label,
    required this.checkLabel,
  });

  final String title;
  /// N группы в тексте диалога (канон называет сколько строк сгинет).
  final String label;
  final String checkLabel;

  @override
  State<ConfirmDialog> createState() => _ConfirmDialogState();
}

class _ConfirmDialogState extends State<ConfirmDialog> {
  bool _checked = false;

  @override
  Widget build(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    return DialogScaffold(
      title: widget.title,
      exitName: 'decline',
      showExitIcon: false,
      onExit: () => Navigator.of(context).pop(false),
      body: (BuildContext context, DialogTrap trap) => <Widget>[
        Padding(
          padding: EdgeInsets.only(bottom: kMetrics.s3),
          child: Text('$kRemoveConfirmHead${widget.label}$kRemoveConfirmTail',
              style: theme.textTheme.bodySmall),
        ),
        Wrap(
          spacing: kMetrics.s2,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: <Widget>[
            Semantics(
              label: widget.checkLabel,
              checked: _checked,
              child: Checkbox(
                focusNode: trap.node('check'),
                value: _checked,
                onChanged: (bool? value) => setState(() => _checked = value ?? false),
              ),
            ),
            Semantics(
              excludeSemantics: true,
              child: Text(widget.checkLabel, style: theme.textTheme.bodySmall),
            ),
          ],
        ),
      ],
      actions: (BuildContext context, DialogTrap trap) => <Widget>[
        FilledButton(
          focusNode: trap.node('accept'),
          // «Принять» до галки неактивна - тот же канон; disabled узел
          // обходом пропускается (тот же фильтр dialogTargets -
          // :not([disabled])).
          onPressed: _checked ? () => Navigator.of(context).pop(true) : null,
          child: const Text(kAcceptLabel),
        ),
        OutlinedButton(
          focusNode: trap.node('decline'),
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text(kDeclineLabel),
        ),
      ],
    );
  }
}
