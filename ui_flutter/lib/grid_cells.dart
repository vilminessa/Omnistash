/// Ячейки строки списка (W2, шаг 4): канон `rowHtml` из ui_src/app.js:3464-3511.
///
/// Состав строк совпадает с шапкой: видимые колонки `LIST_COLS` (гейты
/// `col_*` - тот же `colVisible`, скрытая колонка не рисуется ни там, ни
/// здесь), порядок тот же. Ширины канона - px-числа из `LIST_COLS`
/// (`w: [wide, narrow]`, узкий экран по breakpoint `max-width: 900px` из
/// `applyListColumns` app.js:3206). Числа px лежат вне токен-шкалы, поэтому
/// здесь они перенесены как веса `flex` - тот же канонный источник, сверку
/// ведёт `tools/check_flutter_mirror.py` против `ui_src/app.js`: форма
/// названа расхождением (CSS `grid-template-columns` с горизонтальным
/// скроллом `--list-w` ложится на волну W3, а flex делит доступную ширину
/// пропорцией; колонка «Название» в каноне `minmax(180px, 1fr)` - остаток
/// ширины, здесь вес минимума 180/140).
///
/// Ячейки не отдельные `gridcell`-узлы семантики: строка названа целиком
/// (docs/DESIGN-UI.md §3.1), ячейки - визуальные колонки для глаз; названо
/// расхождением с `role="gridcell"` канона (AT в Flutter читает строку
/// одним узлом с `selected`).
///
/// Чекбокс строки - `.row-check` канона: выбор, не открытие карточки (тот же
/// `stopPropagation` на `.row-check` app.js:3408,3423). Форма названа
/// расхождением: чекбокс не отдельная точка Tab (в каноне input обходится),
/// выбор ведётся Space на строке и кликом чекбокса - обход строк с roving
/// tabindex ложится на волну W3 вместе с грид-ролями.
///
/// Значения ячеек - те же формы, что канон: `humanDuration` (app.js:917),
/// `shortDate` (app.js:945), `humanSize` (app.js:909 - единицы
/// Б/КиБ/МиБ/ГиБ/ТиБ, дробная часть один знак), `bigCount` (app.js:3513 -
/// «—» на пустое, разряды разделителем: неразрывный NBSP назван
/// расхождением, `intl` не в зависимостях), «Теги» - теги ПЛОЩАДКИ
/// (`platform_tags`, первые три через запятую, пустой список «—» -
/// app.js:3495-3498; локальные пометки живут в карточке).
library;

import 'package:flutter/material.dart';

import 'theme.dart';
import 'video_card.dart';

/// Колонки канона `LIST_COLS`: ключ ячейки, подпись шапки (для чекбокса -
/// пустая, как канон `label: ""`), настройка-гейт (`''` без гейта) и веса
/// wide/narrow из канонных px-ширин. Порядок - тот же `LIST_COLS`.
const List<({String key, String label, String gate, double wide, double narrow})>
    kRowCells = <({String key, String label, String gate, double wide, double narrow})>[
  (key: 'check', label: '', gate: '', wide: 26, narrow: 26),
  (key: 'status', label: 'Статус', gate: '', wide: 116, narrow: 104),
  (key: 'media', label: 'Тип', gate: '', wide: 70, narrow: 58),
  (key: 'title', label: 'Название', gate: '', wide: 180, narrow: 140),
  (key: 'channel', label: 'Канал', gate: '', wide: 190, narrow: 130),
  (key: 'duration', label: 'Время', gate: '', wide: 72, narrow: 64),
  (key: 'date', label: 'Дата', gate: '', wide: 100, narrow: 88),
  (key: 'size', label: 'Размер', gate: '', wide: 92, narrow: 78),
  (key: 'views', label: 'Просмотры', gate: 'col_views', wide: 86, narrow: 74),
  (key: 'likes', label: 'Лайки', gate: 'col_likes', wide: 70, narrow: 62),
  (key: 'comments', label: 'Комменты', gate: 'col_comments', wide: 74, narrow: 64),
  (key: 'tags', label: 'Теги', gate: 'col_tags', wide: 160, narrow: 110),
  (key: 'sync', label: 'Синк', gate: 'col_sync', wide: 92, narrow: 76),
];

/// Ключ `ORDER_COLS` сортировки канона для ячейки (LIST_COLS `sortable`;
/// `check` не сортируется - пустая строка, это названо в страже).
const Map<String, String> kCellSortKeys = <String, String>{
  'status': 'status', 'media': 'media_kind', 'title': 'title',
  'channel': 'channel', 'duration': 'duration', 'date': 'date',
  'size': 'size', 'views': 'views', 'likes': 'likes',
  'comments': 'comments', 'tags': 'tag', 'sync': 'sync',
};

/// Роль цвета статуса строки - тот же источник, что `.status[data-s=...]`
/// в ui_src/app.css (downloaded -> ok, queued -> info, downloading -> warn,
/// failed -> err, missing -> warn; known и unavailable -> muted). Сверку
/// карты с CSS ведёт tools/check_flutter_mirror.py. Живёт здесь: статусный
/// маркер рисуется и в ячейке грида, и в подписи строки для AT
/// (library_view.dart читает её импортом).
const Map<String, String> kStatusRoles = <String, String>{
  'known': 'muted',
  'queued': 'info',
  'downloading': 'warn',
  'downloaded': 'ok',
  'failed': 'err',
  'missing': 'warn',
  'unavailable': 'muted',
};

/// Статус без подписи реестра (app/repo.py STATUS_LABELS) читается текстом
/// статуса как есть - молчания нет: строка называет, что не распознано.
String statusLabelOf(Map<String, Object?> row) {
  final Object? label = row['status_label'];
  if (label is String && label.isNotEmpty) return label;
  final Object? status = row['status'];
  if (status is String && status.isNotEmpty) return 'статус: $status';
  return 'статус не назван';
}

String roleOfStatus(String? status) => kStatusRoles[status ?? ''] ?? 'muted';

/// Breakpoint узкого экрана канона: `matchMedia("(max-width: 900px)")` из
/// `applyListColumns` (ui_src/app.js:3206) - то же число, сверяется стражем.
const double kGridNarrowPx = 900;

/// Единицы размера канона `humanSize` (ui_src/app.js:911).
const List<String> kSizeUnits = <String>['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'];

/// `aria-label` чекбокса строки и «выбрать всё» - канон из
/// ui_src/index.html:220 (шапка) и app.js:3473 (строка).
const String kSelectRowAria = 'Выбрать строку';
const String kSelectAllAria = 'Выбрать всё';
/// Подсказка чекбокса «выбрать всё» канона (`title` в index.html:220).
const String kSelectAllTitle = 'Выбрать всё на странице';

/// Видимость колонки - тот же `colVisible` (ui_src/app.js:3192): колонка
/// скрыта только при значении настройки `false`; без гейта видима всегда.
bool cellVisible(Map<String, Object?> settings, String gate) =>
    gate.isEmpty || settings[gate] != false;

/// Размер файла в форме канона `humanSize`: деление на 1024 до верхнего
/// знака, целые байты без дроби, остальные - один знак после точки.
/// `size` строки списка - сумма целых дорожек `video` (app/repo.py:
/// list_videos SELECT SUM(size)), пустая - «—» канона.
String humanSizeOf(Object? bytes) {
  double size = bytes is num ? bytes.toDouble() : 0;
  int unit = 0;
  while (size >= 1024 && unit < kSizeUnits.length - 1) {
    size /= 1024;
    unit++;
  }
  return unit == 0
      ? '${size.toInt()} ${kSizeUnits[unit]}'
      : '${size.toStringAsFixed(1)} ${kSizeUnits[unit]}';
}

/// Счётчик площадки «на момент синка»: пустое значение - «—» (канон
/// `bigCount` app.js:3513: прочерк честнее пустого нуля).
String bigCountOf(Object? value) {
  if (value == null) return '—';
  final String counted = countOf(value);
  return counted.isEmpty ? '—' : counted;
}

/// Дата публикации/синка ячейкой: канон `shortDate` (первые 10 символов
/// iso) - то же `shortDateOf` из карточки; пустое - «—» канона (прочерк
/// `shortDate` app.js:945 для пустого значения).
String dateCellOf(Object? iso) => shortDateOf(iso);

/// Колонка «Теги» канона (app.js:3495): первые три тега площадки через
/// запятую; список пуст - «—».
String tagsCellOf(Object? tags) {
  final List<Object?> list = tags is List ? tags.cast<Object?>() : const <Object?>[];
  final String joined = list.take(3).map((Object? tag) => '$tag').join(', ');
  return joined.isEmpty ? '—' : joined;
}

/// Значок рейтинга канона `starBadge` (app.js:3518): «★N» рядом с
/// названием; 0 - не рисуется.
String ratingBadgeOf(Object? rating) {
  if (rating is! num || rating == 0) return '';
  return '★${rating.toInt()}';
}

/// Значок «просмотрено» канона `watchBadge` (app.js:3525): «✓», цвет --ok.
bool watchedOf(Map<String, Object?> row) => row['watched_at'] != null;

/// Подпись значения ячейки (табличные цифры - эквивалент `--nums` канона).
Widget cellText(BuildContext context, String text, {Color? color}) {
  final ThemeData theme = Theme.of(context);
  return Text(text,
      style: theme.textTheme.bodySmall?.copyWith(
          color: color,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()]),
      maxLines: 1,
      overflow: TextOverflow.ellipsis);
}

/// Чекбокс выбора строки: выбор, карточку не открывает (тот же канон -
/// `.row-check` гасит click строки, app.js:3408,3423). Material гасит
/// указатель внутри чекбокса, жест строки выше его не получает.
/// `container: true` - отдельный узел семантики: без него подпись чекбокса
/// и подписи ячеек сливаются с подписью строки в один узел (факт прогона
/// теста: точный `bySemanticsLabel` перестал находить строку).
Widget rowCheck(BuildContext context, bool checked, VoidCallback onToggle) {
  return Semantics(
    container: true,
    label: kSelectRowAria,
    checked: checked,
    child: Checkbox(
      value: checked,
      onChanged: (_) => onToggle(),
    ),
  );
}

/// Чекбокс «выбрать всё» шапки (канон `#sel-all`: галка change-делегированием
/// app.js:7282-7289 ведёт все отрисованные строки окна прокрутки). Здесь -
/// все загруженные строки (виртуальное окно браузера отмечает только
/// отрисованные строки - форма названа расхождением). Галка канона -
/// обычный input без mixed: частичный выбор она не показывает и не
/// синхронизируется с отдельными строками. Здесь состояние читается из
/// списка честно: checked только когда отмечены все строки, а клик из
/// частичного выбора ведёт «выбрать всё» (тот же браузер: click из
/// unchecked ставит checked); mixed не подменяется - частичный выбор
/// называет счётчик панели «Выбрано: N» (расхождение формы названо).
Widget selectAllCheck(BuildContext context, bool allChecked,
    ValueChanged<bool?> onChanged) {
  return Semantics(
    container: true,
    label: '$kSelectAllAria · $kSelectAllTitle',
    checked: allChecked,
    child: Tooltip(
      message: kSelectAllTitle,
      child: Checkbox(
        value: allChecked,
        onChanged: onChanged,
      ),
    ),
  );
}

/// Маркер статуса строки: цвет роли из реестра (тот же `.status[data-s=...]`)
/// и подпись статуса.
Widget statusCell(BuildContext context, String themeId, String role, String label) {
  final Color color = roleColor(themeId, role);
  return Row(
    children: <Widget>[
      Semantics(
        // Маркер: 7 px канона вне токен-шкалы - ближайший шаг s3 (тот же
        // приём шага 1, названо расхождением).
        excludeSemantics: true,
        child: Container(
          width: kMetrics.s3,
          height: kMetrics.s3,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: color,
            border: Border.all(color: color),
          ),
        ),
      ),
      SizedBox(width: kMetrics.s2),
      Expanded(child: cellText(context, label, color: color)),
    ],
  );
}

/// Ячейки строки: состав по `cellVisible` - тот же `visibleCols` канона, что
/// рисует шапка; Row с весами wide/narrow (breakpoint узкого экрана).
Widget rowCells(BuildContext context, String themeId, Map<String, Object?> row,
    Map<String, Object?> settings, int index, bool picked,
    VoidCallback onToggle, double availableWidth) {
  final ThemeData theme = Theme.of(context);
  final bool narrow = availableWidth <= kGridNarrowPx;
  final String title = row['title'] is String ? row['title'] as String : 'строка $index';
  final String rating = ratingBadgeOf(row['user_rating']);
  final List<Widget> cells = <Widget>[];
  for (final ({String key, String label, String gate, double wide, double narrow}) cell in kRowCells) {
    if (!cellVisible(settings, cell.gate)) continue;
    final Widget content;
    switch (cell.key) {
      case 'check':
        content = Align(alignment: Alignment.centerLeft,
            child: rowCheck(context, picked, onToggle));
      case 'status':
        content = statusCell(context, themeId,
            roleOfStatus(row['status'] is String ? row['status'] as String : null),
            statusLabelOf(row));
      case 'media':
        content = cellText(context, row['media_label'] is String
            ? row['media_label'] as String
            : (row['media_kind'] == 'audio' ? 'аудио' : 'видео'));
      case 'title':
        content = Row(
          children: <Widget>[
            Expanded(child: Text(title,
                style: theme.textTheme.bodyMedium,
                maxLines: 1,
                overflow: TextOverflow.ellipsis)),
            if (rating.isNotEmpty)
              Semantics(excludeSemantics: true, child: cellText(context, rating)),
            if (watchedOf(row))
              Semantics(excludeSemantics: true, child: cellText(context, '✓',
                  color: roleColor(themeId, 'ok'))),
          ],
        );
      case 'channel':
        content = cellText(context, row['channel'] is String ? row['channel'] as String : '—');
      case 'duration':
        content = cellText(context, humanDurationOf(row['duration_s']));
      case 'date':
        content = cellText(context, dateCellOf(row['uploaded_at']));
      case 'size':
        content = cellText(context, row['size'] == null ? '—' : humanSizeOf(row['size']));
      case 'views':
        content = cellText(context, bigCountOf(row['view_count']));
      case 'likes':
        content = cellText(context, bigCountOf(row['like_count']));
      case 'comments':
        content = cellText(context, bigCountOf(row['comment_count']));
      case 'tags':
        content = cellText(context, tagsCellOf(row['platform_tags']));
      case 'sync':
        content = cellText(context, dateCellOf(row['synced_at']));
      default:
        content = cellText(context, '—');
    }
    // Значения ячеек - не узлы семантики: строка названа подписью из
    // `_rowSemantics` (library_view.dart, §3.1), текст ячеек - визуальные
    // колонки для глаз. Без исключения подписи ячеек добавлялись к подписи
    // строки и точный `bySemanticsLabel` терял строку (факт прогона теста).
    // AT не читает размер, даты и счётчики отдельными cell-узлами:
    // `role="gridcell"` канона ложится на волну W3 - расхождение названо.
    final Widget cellWidget = cell.key == 'check'
        ? content
        : Semantics(excludeSemantics: true, child: content);
    cells.add(Expanded(
      flex: (narrow ? cell.narrow : cell.wide).round(),
      child: Padding(
        padding: EdgeInsets.only(right: cell.key == 'check' ? 0 : kMetrics.s3),
        child: cellWidget,
      ),
    ));
  }
  return Row(children: cells);
}

/// Шапка колонок тем же составом и весами, что строки (тот же
/// `visibleCols` + `applyListColumns`: скрытая гейтом колонка не рисуется ни
/// там, ни здесь). Сортируемые колонки - кнопки с направлением (тот же
/// `toggleSort` шага 2: направление подписью ▲/▼, отдельного флага
/// `aria-sort` в семантике нет - названо расхождением), чекбокс «выбрать
/// всё» - первой ячейкой канона.
Widget headerCells(BuildContext context, Map<String, Object?> settings,
    String sortCol, String sortDir, double availableWidth,
    bool allChecked, ValueChanged<bool?> onAll, ValueChanged<String> onSort) {
  final ThemeData theme = Theme.of(context);
  final bool narrow = availableWidth <= kGridNarrowPx;
  final List<Widget> cells = <Widget>[];
  for (final ({String key, String label, String gate, double wide, double narrow}) cell in kRowCells) {
    if (!cellVisible(settings, cell.gate)) continue;
    if (cell.key == 'check') {
      cells.add(Expanded(
        flex: (narrow ? cell.narrow : cell.wide).round(),
        child: Align(alignment: Alignment.centerLeft,
            child: selectAllCheck(context, allChecked, onAll)),
      ));
      continue;
    }
    final String col = kCellSortKeys[cell.key]!;
    final bool active = sortCol == col;
    final String arrow = active ? (sortDir == 'asc' ? ' ▲' : ' ▼') : '';
    cells.add(Expanded(
      flex: (narrow ? cell.narrow : cell.wide).round(),
      child: Padding(
        padding: EdgeInsets.only(right: kMetrics.s3),
        child: TextButton(
          // Клавиатура: TextButton фокусируется и активируется Enter/Space -
          // тот же доступ, что `tabindex="0"` у ячейки шапки канона
          // (grid-head keydown app.js:7276-7281).
          onPressed: () => onSort(col),
          child: Semantics(
            header: true,
            child: Text('${cell.label}$arrow',
                style: theme.textTheme.bodySmall?.copyWith(
                    fontWeight: active ? FontWeight.w600 : null),
                maxLines: 1,
                overflow: TextOverflow.ellipsis),
          ),
        ),
      ),
    ));
  }
  return Row(children: cells);
}
