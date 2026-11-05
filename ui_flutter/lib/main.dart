import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'api_client.dart';
import 'app.dart';
import 'theme.dart';

/// W0 spike: окно на Flutter (Impeller) поверх HTTP-фасада Omnistash.
///
/// Замеряется без «по ощущению»: время до первого кадра, частота кадров при
/// автопрокрутке виртуализированного списка, латентность вызова фасада и
/// объём журнала. Stats печатаются одной JSON-строкой в stdout - их собирает
/// build/scratch/spike/measure.py.
void main() {
  final started = Stopwatch()..start();
  final env = Platform.environment;
  if (env['OMNISTASH_SPIKE'] == '1') {
    // Измерительный стенд W0 (мерку снимает build/scratch/spike/measure.py,
    // он выставляет эту переменную): тот же транспорт и та же палитра, но
    // геометрия списка фиксирована, чтобы числа из docs/FLUTTER-TRANSITION.md
    // §9 оставались описанием одного и того же бинаря.
    runApp(SpikeApp(
      started: started,
      api: _apiFromEnv(env),
      rows: int.tryParse(env['OMNISTASH_SPIKE_ROWS'] ?? '') ?? 2000,
      scrollSeconds: double.tryParse(env['OMNISTASH_SPIKE_SECONDS'] ?? '') ?? 3.0,
      // Тема из токенов реестра (app/themes.py): выбор человека - две темы,
      // настройка theme. Стенд переключает её переменной окружения, чтобы
      // мерить обе палитры одним бинарем; битое значение -> дефолтная тема.
      themeId: env['OMNISTASH_SPIKE_THEME'] ?? kDefaultThemeId,
      // Geometry стенда W0 (высота ряда 44 px) остаётся той, что мерялась: числа
      // в docs/FLUTTER-TRANSITION.md §9 описывают её; токены высоты строки
      // (theme.dart: rowHeight) переснимаются на волне W2 со списком программы.
      rowExtent: double.tryParse(env['OMNISTASH_SPIKE_ROW_EXTENT'] ?? '') ?? 44.0,
    ));
    return;
  }
  // Производственный экран (W2, шаг 1): библиотека из живого фасада; тема и
  // размер текста приходят из настройки бэкенда (app.dart: OmnistashApp).
  runApp(OmnistashApp(facade: _apiFromEnv(env)));
}

ApiClient? _apiFromEnv(Map<String, String> env) {
  final url = env['OMNISTASH_API_URL'] ?? '';
  final token = env['OMNISTASH_API_TOKEN'] ?? '';
  final pin = env['OMNISTASH_API_CERT_SHA256'] ?? '';
  if (url.isEmpty || token.isEmpty || pin.isEmpty) return null;
  final hostPort = url.split('//').last.split(':');
  if (hostPort.length < 2) return null;
  final port = int.tryParse(hostPort[1].split('/').first);
  if (port == null) return null;
  return ApiClient(host: hostPort[0], port: port, token: token, certSha256: pin);
}

class SpikeApp extends StatelessWidget {
  const SpikeApp({super.key, required this.started, required this.api,
    required this.rows, required this.scrollSeconds, required this.themeId,
    required this.rowExtent});

  final Stopwatch started;
  final ApiClient? api;
  final int rows;
  final double scrollSeconds;
  final String themeId;
  final double rowExtent;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Omnistash W0 spike',
      debugShowCheckedModeBanner: false,
      // Никаких seed-цветов и литералов: палитра берётся из токенов темы
      // (theme_tokens.dart), а не из colorSchemeSeed.
      theme: buildTheme(themeId),
      home: SpikeHome(started: started, api: api, rows: rows,
        scrollSeconds: scrollSeconds, rowExtent: rowExtent),
    );
  }
}

class SpikeHome extends StatefulWidget {
  const SpikeHome({super.key, required this.started, required this.api,
    required this.rows, required this.scrollSeconds, required this.rowExtent});

  final Stopwatch started;
  final ApiClient? api;
  final int rows;
  final double scrollSeconds;
  final double rowExtent;

  @override
  State<SpikeHome> createState() => _SpikeHomeState();
}

class _SpikeHomeState extends State<SpikeHome> {
  final ScrollController _scroll = ScrollController();
  int _frames = 0;
  int? _firstFrameMs;
  double _fps = 0;
  List<String> _journal = const [];
  String? _apiError;
  String? _facadeMs;
  Map<String, Map<String, Object>> _targets = const {};
  int _listBuilt = 0;
  Timer? _ticker;

  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPersistentFrameCallback((_) {
      _frames++;
      _firstFrameMs ??= widget.started.elapsedMilliseconds;
    });
    _ticker = Timer.periodic(const Duration(milliseconds: 500), (_) async {
      final api = widget.api;
      if (api == null) return;
      try {
        final res = await api.call('poll', {'since': 0});
        final logs = (res['logs'] as List? ?? const []).cast<String>();
        setState(() => _journal = logs);
      } catch (error) {
        setState(() => _apiError = error.toString());
      }
    });
    WidgetsBinding.instance.addPostFrameCallback((_) => _startScroll());
    _probeFacade();
  }

  Future<void> _probeFacade() async {
    final api = widget.api;
    if (api == null) return;
    final names = (Platform.environment['OMNISTASH_SPIKE_TARGETS'] ??
        'list_videos').split(',').where((n) => n.isNotEmpty).toList();
    final results = <String, Map<String, Object>>{};
    List<Object?> ids = const [];
    int? firstMs;
    for (final name in names) {
      final sw = Stopwatch()..start();
      try {
        final res = await api.call(name, _argFor(name, ids));
        // Отказ фасада приходит внутри 200 с полем error (гейт 18+,
        // «ничего не выбрано»): называем его причиной, а не молчим.
        final refusal = res['error'] is String ? res['error'] as String : '';
        if (name == 'list_videos' && refusal.isEmpty) {
          // Форма ответа фасада: rows (app/gui.py list_videos), не videos.
          final rows = (res['rows'] as List?) ?? <Object?>[];
          ids = rows.map((row) => (row as Map)['id']).toList();
        }
        if (refusal.isNotEmpty) {
          results[name] = {'ok': false, 'ms': sw.elapsedMilliseconds,
                           'error': refusal};
          continue;
        }
        results[name] = {'ok': true, 'ms': sw.elapsedMilliseconds, 'error': ''};
        firstMs ??= sw.elapsedMilliseconds;
      } catch (error) {
        results[name] = {'ok': false, 'ms': sw.elapsedMilliseconds,
                         'error': error.toString()};
      }
    }
    setState(() {
      _targets = results;
      _facadeMs = firstMs?.toString();
    });
  }

  /// Аргументы целей фасада - те же формы, что у браузерного клиента
  /// (docs/SERVER-CLIENT.md §4): тело [аргумент], `{}` когда аргумента нет.
  Object? _argFor(String name, List<Object?> ids) {
    switch (name) {
      case 'poll':
        return {'since': 0};
      case 'list_videos':
        return {'offset': 0, 'limit': 20};
      case 'save_setting':
        return {'key': 'theme', 'value': 'dark'};
      case 'bulk_rating':
        final picked = ids.length > 2 ? ids.sublist(0, 2) : ids;
        return {'ids': picked, 'rating': 4};
      default:
        // get_initial и другие цели без аргумента: тело `{}` (как в app.js)
        return null;
    }
  }

  void _startScroll() {
    if (!_scroll.hasClients) return;
    final total = _scroll.position.maxScrollExtent;
    _scroll.animateTo(total,
        duration: Duration(milliseconds: (widget.scrollSeconds * 1000).round()),
        curve: Curves.linear);
    Timer(Duration(milliseconds: (widget.scrollSeconds * 1000).round()), () {
      _fps = _frames / widget.scrollSeconds;
      _ticker?.cancel();
      _report();
    });
  }

  void _report() {
    final stats = <String, Object>{
      'first_frame_ms': _firstFrameMs ?? -1,
      'frames': _frames,
      'fps': double.parse(_fps.toStringAsFixed(1)),
      'rows': widget.rows,
      'rows_built': _listBuilt,
      'facade_ms': int.tryParse(_facadeMs ?? '') ?? -1,
      'facade_targets': _targets,
      'api_mode': widget.api == null ? 'mock-rows-без-сервера' : 'http-фасад',
      'api_error': _apiError ?? '',
    };
    // Статистика замера печатается в stdout намеренно: её собирает
    // build/scratch/spike/measure.py (это не продуктовый лог).
    // ignore: avoid_print
    print('SPIKE_STATS ${jsonEncode(stats)}');
  }

  @override
  void dispose() {
    _ticker?.cancel();
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      // Подложка окна - роль --bg из токенов темы, не литерал.
      backgroundColor: Theme.of(context).colorScheme.surfaceContainerLowest,
      body: Column(
        children: [
          Padding(
            padding: EdgeInsets.all(kMetrics.s3),
            child: Row(
              children: [
                Text('Библиотека', style: Theme.of(context).textTheme.titleMedium),
                const Spacer(),
                Text('кадров: $_frames  fps: ${_fps.toStringAsFixed(1)}  '
                    'первый кадр: ${_firstFrameMs ?? '-'} мс'),
              ],
            ),
          ),
          Expanded(
            child: ListView.builder(
              controller: _scroll,
              itemCount: widget.rows,
              // Высота ряда стенда: W0 мерялась с 44 px, и числа в
              // docs/FLUTTER-TRANSITION.md §9 описывают именно этот geometry.
              // Формула токенов (rowHeight: max(rowMin, fontSize * rowFactor))
              // ложится на волне W2 со списком программы, тогда мерка
              // переснимается тем же стендом. Стенд переживает её переменной
              // окружения, чтобы один бинарь мерился и с 44, и с токеном.
              itemExtent: widget.rowExtent,
              itemBuilder: (context, index) {
                _listBuilt++;
                return ListTile(
                  dense: true,
                  title: Text('Строка $index: видео из библиотеки'),
                  subtitle: Text('канал $index · 213 с'),
                  trailing: const Icon(Icons.play_arrow),
                );
              },
            ),
          ),
          SizedBox(
            // Панель журнала в стенде фиксирована: это geometry стенда, а не
            // токен палитры (в окне журнал размечается волной W2).
            height: 110,
            child: ListView(
              children: [
                for (final line in _journal)
                  Text(line, style: Theme.of(context).textTheme.bodySmall),
                if (_apiError != null)
                  Text('Отказ: $_apiError',
                      style: TextStyle(
                          color: Theme.of(context).colorScheme.error)),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
