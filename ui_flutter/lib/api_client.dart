import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';

/// Транспорт W0: тот же контракт, что у браузерного клиента
/// (docs/SERVER-CLIENT.md §4): `POST /api/<имя>`, тело `[аргумент]`
/// (`{}` когда аргумента нет), заголовок `Bearer <токен>`.
/// Скоупы решает сервер; здесь клиент только называет цель и читает
/// честный отказ с причиной.
/// Клиент HTTP-фасада `--serve`: POST /api/<имя>, Bearer, доверие по пину.
///
/// Проводка повторяет браузерный клиент (ui_src/app.js:547) и серверный
/// разбор тела (app/http/server.py: _body): `{}` когда аргумента нет,
/// `[аргумент]` когда есть. Скоупы решает сервер, клиент только называет
/// цель; отказ скоупа приходит текстом ответа и показывается как причина.
abstract class Facade {
  /// Вызвать цель фасада. `arg == null` - цель без аргумента (тело `{}`).
  Future<Map<String, Object?>> call(String name, Object? arg);
}

class ApiClient implements Facade {
  ApiClient({
    required this.host,
    required this.port,
    required this.token,
    required this.certSha256,
  });

  final String host;
  final int port;
  final String token;

  /// sha256 DER-сертификата слушателя. Для W0 это пин конкретного
  /// mock-сервера; в W3 пин меняется на отпечаток SPKI (эфемерный
  /// сертификат сервера рождается заново на каждый запуск, поэтому
  /// DER-пин годится только для одного прогона).
  final String certSha256;

  @override
  Future<Map<String, Object?>> call(String name, Object? arg) async {
    final client = HttpClient()..badCertificateCallback = _trust;
    try {
      // Проводка как у браузерного клиента (ui_src/app.js:547): `{}` когда
      // аргумента нет, `[аргумент]` когда есть - сервер различает формы
      // (app/http/server.py: _body) и вызов без аргумента не падает.
      final encoded = arg == null ? '{}' : jsonEncode([arg]);
      final uri = Uri.parse('https://$host:$port/api/$name');
      // openUrl, а не open: open строит http-URI, TLS-канал не открывается.
      final request = await client.openUrl('POST', uri);
      request.headers.set(HttpHeaders.authorizationHeader, 'Bearer $token');
      request.headers.contentType = ContentType.json;
      final bodyBytes = utf8.encode(encoded);
      request.headers.contentLength = bodyBytes.length;
      request.add(bodyBytes);
      final response = await request.close();
      final text = await utf8.decoder.bind(response).join();
      if (response.statusCode != 200) {
        throw ApiFailure(name, 'сервер ответил ${response.statusCode}: '
            '${_clip(text)}');
      }
      final decoded = jsonDecode(text);
      if (decoded is! Map<String, Object?>) {
        throw ApiFailure(name, 'ответ не объект JSON: ${_clip(text)}');
      }
      return decoded;
    } on ApiFailure {
      rethrow;
    } catch (error) {
      throw ApiFailure(name, 'канал не открыт: $error');
    } finally {
      client.close();
    }
  }

  /// Доверие по пину: сертификат принимается только если его DER совпал
  /// с закрепленным sha256. Иначе отказ называется причиной, а не молчит.
  bool _trust(X509Certificate certificate, String host, int port) {
    final der = _derFromPem(certificate.pem);
    if (der == null) {
      stderr.writeln('TLS: сертификат не разобран из PEM - отказ');
      return false;
    }
    final digest = sha256.convert(der).toString();
    if (digest != certSha256) {
      stderr.writeln('TLS: отпечаток сертификата $digest не совпал с '
          'пином $certSha256 - отказ');
      return false;
    }
    return true;
  }
}

class ApiFailure implements Exception {
  ApiFailure(this.target, this.reason);
  final String target;
  final String reason;

  @override
  String toString() => '$target: $reason';
}

List<int>? _derFromPem(String pem) {
  final lines = pem.split('\n');
  final start = lines.indexOf('-----BEGIN CERTIFICATE-----');
  final end = lines.indexOf('-----END CERTIFICATE-----');
  if (start < 0 || end <= start) return null;
  final body = lines.sublist(start + 1, end).map((l) => l.trim()).join();
  try {
    return base64.decode(body);
  } catch (error) {
    return null;
  }
}

String _clip(String text, [int max = 180]) =>
    text.length <= max ? text : text.substring(0, max);
