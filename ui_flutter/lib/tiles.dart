/// Плитки библиотеки с обложками (W2, шаг 4б) - тот же канон, что
/// `tileHtml`/`TILE`/`tileCols` и ленивые превью в ui_src/app.js:28-34,
/// 2963-2985, 2989-3055, 3531-3560 и `#grid-rows.tiles`/`.tile*` в
/// ui_src/app.css:647-739. Обложки приходят только из локальных файлов
/// бэкенда (`get_thumbs`/`get_thumb`, app/gui.py:3196-3241: data-URI из
/// файла рядом с видео, ничего не качается снаружи), заглушка называется
/// подписью канона «нет обложки».
///
/// Числа и подписи здесь - из канона, не на глаз: `tools/check_flutter_mirror.py`
/// сверяет их с app.js, index.html, app.css и схемой настроек.
library;

import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import 'grid_cells.dart';
import 'theme.dart';
import 'video_card.dart';

/// Размерный ряд канона `TILE` (app.js:28-32): ширина плитки, высота ряда
/// плиток (виртуализация считает по ней) и зазор. Числа px - вне токен-шкалы,
/// это канонные константы JS, перенесенные числом и сверяемые стражем.
const Map<String, ({double w, double row, double gap})> kTileSizes =
    <String, ({double w, double row, double gap})>{
  'small': (w: 160, row: 168, gap: 8),
  'medium': (w: 240, row: 218, gap: 10),
  'large': (w: 320, row: 262, gap: 12),
};

/// Ид за один вызов `get_thumbs` - тот же `THUMB_BATCH` канона (app.js:34) и
/// тот же лимит бэкенда (`THUMB_BATCH_LIMIT`, app/gui.py:70): сверх лимита
/// бэкенд отказывает текстом, клиент дробит сам.
const int kThumbBatchMax = 100;

/// Сколько data-URI обложек держим в памяти - тот же `THUMB_CACHE_MAX`
/// канона (app.js:33): LRU, свежие в конец, старейший снимается при превышении
/// (`rememberThumb` app.js:2993-2999).
const int kThumbCacheMax = 400;

/// Задержка коалесинга батча - тот же `setTimeout(..., 120)` канона
/// (app.js:3020-3042): плитки, появившиеся в одном кадре, спрашивают одним
/// вызовом, а не десятками.
const int kThumbDebounceMs = 120;

/// Play-кнопка канона: 44 px из `.tile-play` (app.css:685-691) - вне
/// токен-шкалы, канонное число, сверяемое стражем с CSS.
const double kTilePlayPx = 44;

/// Подписи канона (плитка и переключатели вида) - те же строки, что
/// `tileHtml` (app.js:3531-3559) и ряд `view-toggle`/`size-toggle`
/// (ui_src/index.html:109-124). Клиент не выдумывает слова.
const String kNoThumbLabel = 'нет обложки';
const String kTileCheckAria = 'Выбрать плитку';
const String kTilePlayAria = 'Открыть в плеере';
const String kViewToggleAria = 'Вид библиотеки';
const String kViewListLabel = 'Списком';
const String kViewGridLabel = 'Плиткой с превью';
const String kSizeToggleAria = 'Размер плитки';
const String kSizeSmallLabel = 'Мелкая плитка';
const String kSizeMediumLabel = 'Средняя плитка';
const String kSizeLargeLabel = 'Крупная плитка';

/// Типы обложек, которые бэкенд кладёт в data-URI (app/gui.py:71-73
/// `THUMB_MIME` по расширению файла); прочий тип (`application/octet-stream`)
/// не декодируется в картинку и называется заглушкой. Браузер при битом `src`
/// рисует сломанный `img` без подписи - форма названа расхождением.
const Set<String> kThumbMimes = <String>{
  'image/jpeg', 'image/png', 'image/webp', 'image/avif',
};

/// Декод data-URI обложки (`app/gui.py:76-92 _thumb_data_uri`): байты из
/// base64 после `;base64,`. Возвращает null, когда URI не картинка или
/// основание битое - заглушка называется, а не молчит.
Uint8List? decodeThumbDataUri(Object? dataUri) {
  if (dataUri is! String) return null;
  if (!dataUri.startsWith('data:')) return null;
  final int comma = dataUri.indexOf(';base64,');
  if (comma < 0) return null;
  final String mime = dataUri.substring(5, comma);
  if (!kThumbMimes.contains(mime)) return null;
  try {
    return base64Decode(dataUri.substring(comma + 8));
  } on FormatException {
    return null;
  }
}

/// Кэш обложек по канону `thumbCache` (app.js:2989-2999): LRU на
/// `kThumbCacheMax`, отсутствие ответа держится пустышкой, чтобы не спрашивать
/// одно и то же на каждом прокручивании окна (`ids.forEach` app.js:3034-3036).
/// Ключ без значения - ещё ждём ответа (канон `cached === undefined`, img
/// остаётся без src).
class ThumbCache {
  ThumbCache({this.capacity = kThumbCacheMax});

  final int capacity;

  /// Порядок вставки Dart `Map` - тот же LRU канона (свежие в конец).
  final Map<int, Uint8List?> _entries = <int, Uint8List?>{};

  bool has(int id) => _entries.containsKey(id);

  /// null - ждём ответа; значение null - «обложки нет/не читается».
  Uint8List? of(int id) => _entries[id];

  void remember(int id, Uint8List? bytes) {
    if (_entries.containsKey(id)) _entries.remove(id);
    _entries[id] = bytes;
    if (_entries.length > capacity) {
      _entries.remove(_entries.keys.first);
    }
  }

  int get size => _entries.length;

  void clear() => _entries.clear();
}

/// Сколько колонок плиток поместилось - та же формула `tileCols` канона
/// (app.js:2967-2972): ширина контейнера решает, не настройка; минус 2 px
/// рамки контейнера (`clientWidth - 2`).
int tileColsFor(double width, String sizeKey) {
  final ({double w, double row, double gap}) cfg =
      kTileSizes[sizeKey] ?? kTileSizes['medium']!;
  final int cols = ((width - 2 + cfg.gap) / (cfg.w + cfg.gap)).floor();
  return cols < 1 ? 1 : cols;
}

/// Подпись плитки для AT. Канон `tileHtml` не объявляет `role="row"` и
/// `tabindex="-1"` (обходятся чекбокс и play-кнопка), поэтому обход стрелками
/// плиток - волна W3 (названо). Имя плитки - те же части, что `_rowSemantics`
/// называет для строки списка (§3.1): название, канал, длительность, тип
/// дорожки, статус и пометки.
String tileSemantics(Map<String, Object?> row) {
  final String title = row['title'] is String ? row['title'] as String : '—';
  final String channel = row['channel'] is String ? row['channel'] as String : '';
  final String media = row['media_label'] is String
      ? row['media_label'] as String
      : (row['media_kind'] == 'audio' ? 'аудио' : 'видео');
  final String rating = ratingBadgeOf(row['user_rating']);
  final List<String> parts = <String>[
    title,
    if (channel.isNotEmpty) channel,
    humanDurationOf(row['duration_s']),
    media,
    statusLabelOf(row),
    if (rating.isNotEmpty) 'рейтинг ${rating.substring(1)}',
    if (watchedOf(row)) 'просмотрено',
  ];
  return parts.join(', ');
}

/// Подпись `tile-sub` канона (app.js:3553-3558): канал, ` · ` длительность
/// (если она есть), ` · аудио` для аудио-дорожки. Пустой канал канон рисует
/// пустой строкой - здесь то же, «—» не выдумывается.
String tileSubOf(Map<String, Object?> row) {
  final String channel = row['channel'] is String ? row['channel'] as String : '';
  final List<String> parts = <String>[
    channel,
    if (row['duration_s'] is num && (row['duration_s'] as num) > 0)
      humanDurationOf(row['duration_s']),
    if (row['media_kind'] == 'audio') 'аудио',
  ];
  return parts.join(' · ');
}

/// Обложка плитки: `bytes == null` и `awaiting == true` - ответа ещё нет,
/// пустая подложка `--surface-2` без подписи (канон: `img` без src, `alt=""`);
/// `bytes == null` и `awaiting == false` - заглушка «нет обложки»
/// (`tile-nothumb`, app.js:3013-3018). Битый декод ведёт ту же заглушку
/// (форма названа расхождением выше).
Widget tileThumb(BuildContext context, Uint8List? bytes, bool awaiting) {
  final ThemeData theme = Theme.of(context);
  final Widget missing = Center(
    child: Text(kNoThumbLabel, style: theme.textTheme.labelSmall),
  );
  return AspectRatio(
    // `.tile-thumb`: aspect-ratio 16/9, object-fit cover, фон --surface-2.
    aspectRatio: 16 / 9,
    child: Container(
      color: theme.colorScheme.surfaceContainerLow,
      child: bytes == null
          ? (awaiting ? const SizedBox.shrink() : missing)
          : Image.memory(bytes, fit: BoxFit.cover,
              errorBuilder: (BuildContext ctx, Object error, StackTrace? stack) => missing),
    ),
  );
}

/// Плитка канона `tileHtml`: обложка 16:9, чекбокс «Выбрать плитку» слева
/// сверху, play-кнопка 44×44 с подложкой `--scrim` по центру (замена №4:
/// значок всегда видим, hover только меняет фон), кружок статуса + звёздный
/// значок + «просмотрено» справа сверху, название в две строки (--fs-sm,
/// lh tight) и подпись под ним (--fs-cap, muted, одна строка с многоточием).
Widget tileCard(
  BuildContext context,
  String themeId,
  Map<String, Object?> row, {
  required bool picked,
  required Uint8List? bytes,
  required bool awaiting,
  required VoidCallback onToggle,
  required VoidCallback onOpen,
  required VoidCallback onPlay,
  required FocusNode playNode,
}) {
  final ThemeData theme = Theme.of(context);
  final Color line = roleColor(themeId, picked ? 'accent' : 'line');
  final Color scrim = roleColor(themeId, 'scrim');
  final Color onScrim = roleColor(themeId, 'on-scrim');
  return GestureDetector(
    // Одиночный клик - карточка, двойной - плеер (тот же `bindGridItems`
    // app.js:3400-3435 для `.row` и `.tile`): чекбокс и play-кнопка гасят
    // propagation, карточку не открывают. Задержка канона 240 мс против
    // `kDoubleTapTimeout` 300 мс Flutter - форма названа расхождением.
    onTap: onOpen,
    onDoubleTap: onPlay,
    child: Semantics(
      container: true,
      label: tileSemantics(row),
      selected: picked,
      button: true,
      onTap: onOpen,
      child: Container(
        // `.tile`: 1px --line (выбранная - --accent), --surface, радиус
        // --radius, overflow hidden. Тень hover (`--shadow-1`) в Flutter
        // не переносится числом - форма названа расхождением (elevation
        // ложится на волну W3).
        decoration: BoxDecoration(
          color: theme.colorScheme.surface,
          border: Border.all(color: line),
          borderRadius: BorderRadius.circular(kMetrics.r2),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            Stack(
              children: <Widget>[
                tileThumb(context, bytes, awaiting),
                // `.tile-check-wrap` и `.tile-badges`: top/left и top/right
                // 6px - вне токен-шкалы, ближайший шаг s3 (тот же приём шага 1).
                Positioned(
                  top: kMetrics.s3,
                  left: kMetrics.s3,
                  child: Semantics(
                    container: true,
                    label: kTileCheckAria,
                    checked: picked,
                    child: Checkbox(value: picked, onChanged: (_) => onToggle()),
                  ),
                ),
                Positioned(
                  top: kMetrics.s3,
                  right: kMetrics.s3,
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: <Widget>[
                      statusDot(themeId, row),
                      if (ratingBadgeOf(row['user_rating']).isNotEmpty)
                        Text(ratingBadgeOf(row['user_rating']),
                            style: theme.textTheme.labelSmall),
                      if (watchedOf(row))
                        Text('✓', style: theme.textTheme.labelSmall?.copyWith(
                            color: roleColor(themeId, 'ok'))),
                    ],
                  ),
                ),
                Positioned.fill(
                  child: Center(
                    child: Semantics(
                      container: true,
                      label: kTilePlayAria,
                      button: true,
                      child: Tooltip(
                        message: kTilePlayAria,
                        child: Ink(
                          width: kTilePlayPx,
                          height: kTilePlayPx,
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            color: scrim,
                          ),
                          child: InkWell(
                            focusNode: playNode,
                            borderRadius: BorderRadius.circular(kMetrics.rFull),
                            // Право `watch` подписывается до перехода:
                            // отказ называет вызывающий код текстом словаря
                            // бэка (`access.denial`), как карточка шага 3,
                            // а не уходит в 403 молча.
                            onTap: onPlay,
                            child: Center(
                              // Глиф ▶ декоративен: канонный aria-label
                              // кнопки называет цель текстом, глиф в имя не
                              // входит (иначе merged-подпись узла
                              // «Открыть в плеере\n▶» не читается точным
                              // bySemanticsLabel - факт дампа семантики
                              // прогона шага 4б).
                              child: Semantics(
                                excludeSemantics: true,
                                child: Text('▶', style: theme.textTheme.bodyMedium
                                    ?.copyWith(color: onScrim)),
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ],
            ),
            // `.tile-title`: 8px боковые, 6px сверху, --fs-sm, 2 строки.
            Padding(
              padding: EdgeInsets.only(
                  left: kMetrics.s4, right: kMetrics.s4, top: kMetrics.s3),
              child: Text(row['title'] is String ? row['title'] as String : '—',
                  style: theme.textTheme.bodySmall,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis),
            ),
            Padding(
              padding: EdgeInsets.only(
                  left: kMetrics.s4, right: kMetrics.s4, bottom: kMetrics.s3),
              child: Text(tileSubOf(row),
                  style: theme.textTheme.labelSmall,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis),
            ),
          ],
        ),
      ),
    ),
  );
}

/// Кружок статуса плитки: `.tile-badges .status { font-size: 0 }` - нужен
/// только кружок, подпись статуса читается AT (`title` канона).
Widget statusDot(String themeId, Map<String, Object?> row) {
  final String status = row['status'] is String ? row['status'] as String : '';
  final String role = kStatusRoles[status] ?? 'muted';
  final Color color = roleColor(themeId, role);
  return Semantics(
    label: statusLabelOf(row),
    child: Container(
      width: kMetrics.s3,
      height: kMetrics.s3,
      decoration: BoxDecoration(shape: BoxShape.circle, color: color),
    ),
  );
}

/// Переключатели вида канона (`#view-toggle` и `#size-toggle`,
/// ui_src/index.html:109-124): группа с `aria-label` канона, цели - подписи
/// канона (`title`/`aria-label`), активная цель - та, что соответствует
/// текущей настройке (`applyViewState` app.js:2974-2985 - класс `active`).
///
/// Форма названа расхождением: канон рисует глифы ☰/▦ и М/С/К, здесь цели
/// подписаны словами канона без глифов.
Widget viewToggle(BuildContext context, String viewMode,
    ValueChanged<String> onSelect) {
  return Semantics(
    container: true,
    label: kViewToggleAria,
    child: SegmentedButton<String>(
      segments: const <ButtonSegment<String>>[
        ButtonSegment<String>(value: 'list', label: Text(kViewListLabel)),
        ButtonSegment<String>(value: 'grid', label: Text(kViewGridLabel)),
      ],
      selected: <String>{viewMode == 'grid' ? 'grid' : 'list'},
      showSelectedIcon: false,
      onSelectionChanged: (Set<String> next) {
        onSelect(next.isEmpty ? 'list' : next.first);
      },
    ),
  );
}

/// Ряд размера плитки - не рисуется в списке (тот же канон
/// `$("size-toggle").hidden = !grid`, app.js:2979: скрытый контрол не в
/// Tab-обходе); в gridMode рисуется group с подписями канона.
Widget sizeToggle(BuildContext context, String tileSize, bool gridMode,
    ValueChanged<String> onSelect) {
  if (!gridMode) return const SizedBox.shrink();
  return Semantics(
    container: true,
    label: kSizeToggleAria,
    child: SegmentedButton<String>(
      segments: const <ButtonSegment<String>>[
        ButtonSegment<String>(value: 'small', label: Text(kSizeSmallLabel)),
        ButtonSegment<String>(value: 'medium', label: Text(kSizeMediumLabel)),
        ButtonSegment<String>(value: 'large', label: Text(kSizeLargeLabel)),
      ],
      selected: <String>{kTileSizes.containsKey(tileSize) ? tileSize : 'medium'},
      showSelectedIcon: false,
      onSelectionChanged: (Set<String> next) {
        if (next.isEmpty) return;
        onSelect(next.first);
      },
    ),
  );
}
