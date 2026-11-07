/// Открытие адресов из карточки: системный браузер, без новых зависимостей.

/// Карточка видео (W2, шаг 3) имеет три «открыть»: страница воспроизведения
/// сервера `/watch/<id>`, адрес площадки (`webpage_url`) и путь файла на диске.
/// Первые два - адреса, которые открывает системный браузер; путь - только
/// текст (проводник папки и системный плеер - нативные цели окна сервера:
/// `app/http/scopes.py: NATIVE` содержит open_file/open_folder/open_url, и по
/// HTTP они не выполняются вовсе; названо расхождением в video_card.dart).
///
/// Здесь нет ни одной pub-зависимости: `cmd /c start` - то, что уже есть на
/// Windows, и рантайм окна ничего не скачивает (docs/FLUTTER-TRANSITION.md §7).
/// Аргументы передаются списком, а не конкатенацией строки: адрес с пробелом
/// или знаком `&` не распадается на несколько команд.
library;

import 'dart:io';

/// Открытие адреса. Возвращает текст отказа (null - адрес открыт).
abstract class UrlOpener {
  Future<String?> open(String url);
}

/// Системный браузер: `cmd /c start "" <адрес>` (обработчик адреса по умолчанию).
class BrowserOpener implements UrlOpener {
  const BrowserOpener();

  @override
  Future<String?> open(String url) async {
    if (url.isEmpty) return 'адрес пуст - открывать нечего';
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      // Не адрес: проводник по пути здесь не открывается (нативная цель), и
      // отдавать путь системному обработчику было бы молчанием о чужом файле.
      return 'это не адрес http(s): системный обработчик не вызывается - '
          'проводник папки остаётся нативной целью окна';
    }
    try {
      final result = await Process.run('cmd', <String>['/c', 'start', '', url]);
      if (result.exitCode != 0) {
        return 'браузер не открыт: cmd вернул ${result.exitCode}: '
            '${_clip(result.stderr.toString())}';
      }
      return null;
    } on Object catch (error) {
      // cmd не найден, процесс не запущен: отказ называется, а не молчит.
      return 'браузер не открыт: $error';
    }
  }
}

String _clip(String text, [int max = 160]) =>
    text.length <= max ? text : text.substring(0, max);
