"""HTTP-сервер клиента (docs/SERVER-CLIENT.md §3, шаг S1).

Тот же приём, что mesh-сервер (app/mesh/server.py): ThreadingHTTPServer без
`socket.getfqdn` в server_bind - на Windows обратный DNS для 0.0.0.0 висит на
таймаутах (замер: 17 с на подъём), TLS на приёме (эфемерный материал в памяти,
TLS 1.3 минимум), отказ handshake не валит сервер, отдача - чанками под
`pacing.RateLimiter`.

Материал TLS берёт `app/http/tls.py`, а не `channel.server_context()`: там
ключ Ed25519, а TLS-стек браузера его не принимает (замер: отказ рукопожатия
до предупреждения о доверии). Mesh-канал остаётся Ed25519 со SPKI-аттестацией.

Чем отличается от mesh-сервера: здесь права решает токен со скоупами, а не
аттестация собеседника, и тело запроса - JSON. Байтовые маршруты (/thumb,
/media, /subs, /hls) добавляются в S2 и S3; сейчас страница и /api/*.

Честность ответов: сервер называет причину сам ({error, code}); клиент, если
тела нет, подписывает по коду ответа. Ни выдуманных пустых списков, ни
молчаливого null (docs/SERVER-CLIENT.md §3).
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import i18n
from ..mesh import channel as channel_mod
from ..mesh import pacing
from ..ui import build_page
from . import scopes
from . import tls
from .audit import Audit
from .tokens import TokenStore, TokenStoreError

# Тело JSON-запроса: больше - честный 413, а не тихая обрезка.
MAX_BODY_BYTES = 1_000_000
CHUNK = 256 * 1024
HANDSHAKE_TIMEOUT = channel_mod.HANDSHAKE_TIMEOUT


class ServeError(Exception):
    """Сервер не поднялся: нет TLS-материала, порт занят, токены не читаются."""


def bind_address(value) -> str:
    """Значение `serve_bind` -> адрес привязки слушателя.

    Шаг S1 знает только loopback: случайный текст в настройке не должен
    открыть порт наружу без согласия (режимы lan/tailscale/0.0.0.0+allowlist
    приходят в S6, там выбор адреса решается явным согласием).
    """
    text = str(value or "loopback").strip().lower()
    if text in ("", "loopback", "localhost", "127.0.0.1"):
        return "127.0.0.1"
    return "127.0.0.1"


def _san_hosts(bind: str) -> list:
    """Имена сертификата для браузера: localhost + адрес привязки.

    Браузер верифицирует SAN (CN больше не читается), поэтому SAN обязан
    содержать то, по которому человек открывает страницу. IP передаётся
    строкой: SAN строит channel._san_extension (тип IP там же).
    """
    hosts = ["localhost"]
    if bind and bind != "0.0.0.0":
        hosts.append(bind)
    return hosts


class _ServeServer(ThreadingHTTPServer):
    """TCPServer без getfqdn + TLS на приёме (тот же обход, что mesh)."""

    allow_reuse_address = True
    ctx: ssl.SSLContext | None = None
    log = staticmethod(lambda message: None)

    def server_bind(self) -> None:
        if self.allow_reuse_address:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()

    def get_request(self):
        sock, addr = super().get_request()
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            tls = self.ctx.wrap_socket(sock, server_side=True)
            tls.settimeout(None)      # раздача байтов - без лимита handshake
            return tls, addr
        except (OSError, ssl.SSLError):
            try:
                sock.close()
            except OSError:
                pass
            raise

    def handle_error(self, request, client_address) -> None:
        """Отказ handshake - не авария сервера; разрыв соединения молчим."""
        import sys
        error = sys.exc_info()[1]
        if error is None:
            return
        if isinstance(error, (ConnectionResetError, BrokenPipeError,
                              ConnectionAbortedError, TimeoutError)):
            return
        if isinstance(error, OSError) and getattr(error, "winerror", None) in (
                10053, 10054, 10060):
            return
        self.log(f"HTTP-сервер: соединение от {client_address[0]} "
                 f"отклонено: {type(error).__name__}: {error}")


class _Handler(BaseHTTPRequestHandler):
    server_version = "OmnistashServe/1"
    protocol_version = "HTTP/1.1"     # Range и keep-alive (S2/S3) читаются так

    api = None                # app.gui.Api (тот же фасад, что окну)
    tokens = None             # http.tokens.TokenStore
    audit = None              # http.audit.Audit
    page = b""                # страница с меткой сервера (строится при старте)
    rate_bps = 0              # 0 = без ограничения отдачи
    watchers_max = 0          # 0 = без лимита зрителей (байтовые маршруты, S2/S3)
    language = staticmethod(lambda: "ru")   # язык подписей сервера

    def log_message(self, *args) -> None:   # noqa: D102 - в stderr не пишем
        pass

    # -- запись ответа -------------------------------------------------------

    def _write(self, code: int, body: bytes, content_type: str,
               extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        limiter = pacing.RateLimiter(self.rate_bps)
        left = body
        while left:
            piece, left = left[:CHUNK], left[CHUNK:]
            try:
                self.wfile.write(piece)
            except (BrokenPipeError, ConnectionResetError):
                return
            limiter.consume(len(piece))

    def _json(self, code: int, payload, extra: dict | None = None) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            # Не сериализуемый результат - причина видна, а не пустое тело.
            body = json.dumps({"error": f"результат не сериализуется: {exc}",
                               "code": "server"}, ensure_ascii=False).encode("utf-8")
            code = 500
        self._write(code, body, "application/json; charset=utf-8", extra)

    def _error(self, code: int, message: str, key: str = "") -> None:
        payload = {"error": message}
        if key:
            payload["code"] = key
        self._json(code, payload)

    def _answer(self, code: int, payload, name: str = "", scope: str = "",
                label: str = "", note: str = "") -> None:
        """Записать обращение в журнал и ответить.

        Журнал раньше ответа: иначе клиент, прочитавший ответ, мог увидеть
        пустой журнал (гонка между потоками), и «кто что спросил» пропал бы
        при обрыве соединения в середине раздачи.
        """
        self.audit.record(name or (self.path or "-"), code, self._addr(),
                          key_label=label, scope=scope, note=note)
        self._json(code, payload)

    def _addr(self) -> str:
        return str(self.client_address[0]) if self.client_address else "-"

    def _text(self, key: str) -> str:
        return i18n.t(key, self.language()) or key

    # -- маршруты ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - имя из http.server
        path = (self.path or "").partition("?")[0]
        try:
            if path in ("/", "/index.html"):
                self.audit.record("page", 200, self._addr())
                self._write(200, self.page, "text/html; charset=utf-8",
                            {"Cache-Control": "no-store"})
                return
            if path.startswith("/api/"):
                # Тот же контракт, что окну: вызов метода - POST.
                self._answer(405, {"error": self._text("http.post_only"),
                                   "code": "http"},
                             name=path[len("/api/"):])
                return
            self._answer(404, {"error": self._text("http.missing"),
                               "code": "missing"}, name=path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - сервер не должен умереть
            try:
                self._error(500, f"{type(exc).__name__}: {exc}", "server")
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self) -> None:  # noqa: N802 - имя из http.server
        path = (self.path or "").partition("?")[0]
        name = path[len("/api/"):] if path.startswith("/api/") else ""
        addr = self._addr()
        try:
            if not name:
                self._answer(404, {"error": self._text("http.missing"),
                                   "code": "missing"}, name="-")
                return

            token = self.tokens.verify(self._bearer()) if self.tokens else None
            if token is None:
                # Ключа нет или он не из нашего реестра: привязка не завершена.
                self._answer(401, {"error": self._text("http.pairing"),
                                   "code": "pairing"}, name=name)
                return
            label = token.label

            if scopes.is_native(name):
                # Сервер не может открыть диалог на чужой машине: подпись,
                # а не молчание (тост читает res.error как есть).
                self._answer(403, {"error": self._text("http.desktop"),
                                   "code": "desktop", "target": name},
                             name=name, scope="desktop", label=label)
                return

            scope = scopes.scope_for(name)
            if scope is None:
                # Цели нет по HTTP (локальная или внутренняя) - честный 404.
                code = "local" if scopes.is_local(name) else "missing"
                self._answer(404, {"error": self._text("http.missing"),
                                   "code": code, "target": name},
                             name=name, scope=code, label=label)
                return

            if scope not in token.scopes:
                self._answer(403, {"error": self._text("http.scope_denied") + " " + scope,
                                   "code": "scope", "scope": scope},
                             name=name, scope=scope, label=label)
                return

            code, payload, note = self._dispatch(name)
            self._answer(code, payload, name=name, scope=scope,
                         label=label, note=note)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - сервер не должен умереть
            # Причину называем и пишем сразу: 500 без строки в журнале - это
            # «сервер упал, но никто не видел почему».
            try:
                self.audit.record(name, 500, addr)
                self._error(500, f"{type(exc).__name__}: {exc}", "server")
            except Exception:  # noqa: BLE001
                pass

    def _bearer(self) -> str:
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""

    def _body(self):
        """Аргументы вызова: список позиционных или объект kwargs ({} - пусто).

        Тот же нюанс, что с pywebview: метод без параметров не должен получить
        позиционный None, поэтому пустое тело - ноль аргументов.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            # Читаем и выбрасываем тело, иначе соединение «съедит» следующий запрос.
            left = length
            while left > 0:
                piece = self.rfile.read(min(CHUNK, left))
                if not piece:
                    break
                left -= len(piece)
            return None, None, {"error": self._text("http.body_too_large") +
                                f" ({MAX_BODY_BYTES} байт)", "code": "too_large"}
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return [], {}, None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, None, {"error": self._text("http.bad_body") + f": {exc}",
                                "code": "bad_json"}
        if isinstance(payload, list):
            return payload, {}, None          # позиционные аргументы (как pywebview)
        if isinstance(payload, dict):
            return [], payload, None          # именованные аргументы
        return None, None, {"error": self._text("http.bad_body") +
                            f": {type(payload).__name__}", "code": "bad_json"}

    def _dispatch(self, name: str) -> tuple[int, object, str]:
        """Вызвать метод фасада и вернуть (код, тело, заметку для аудита)."""
        method = getattr(self.api, name, None)
        if method is None or not callable(method) or scopes.is_internal(name):
            return 404, {"error": self._text("http.missing"),
                         "code": "missing", "target": name}, ""

        args, kwargs, problem = self._body()
        if problem is not None:
            return (413 if problem.get("code") == "too_large" else 400), problem, ""

        try:
            result = method(*args, **kwargs)
        except TypeError as exc:
            # Аргументы не подходят сигнатуре метода: причину называем,
            # «метод молча вернул null» для клиента не читается как отказ.
            return 400, {"error": f"битый запрос для {name}: {exc}",
                         "code": "bad_json"}, ""
        except Exception as exc:  # noqa: BLE001 - причину называем, не глотаем
            return 500, {"error": f"{type(exc).__name__}: {exc}",
                         "code": "server"}, ""

        # Тело = ровно то, что окну возвращает pywebview: без обёрток и без
        # выдуманных {"ok": true}. Массив остаётся массивом, None - null.
        if result is None:
            return 200, None, "пусто"
        if isinstance(result, dict):
            # Отказ процесса (скан/перенос уже идут) Api отвечает своей
            # подписью {error} при 200 - клиент читает её как тост; 409
            # оставлен байтовым маршрутам (зрители и кеш, S2/S3).
            return 200, result, ("отказ" if result.get("error") else "")
        if isinstance(result, (list, tuple)):
            return 200, list(result), ""
        return 200, result, ""


class ServeServer:
    """HTTPS-сервер клиента: start() - фоном рядом с окном, stop() - дожидается."""

    def __init__(self, api, *, bind: str = "127.0.0.1", port: int = 1001,
                 rate_bps: int = 0, watchers_max: int = 0, log=None,
                 tokens: TokenStore | None = None, audit: Audit | None = None,
                 language=None, page_marker: dict | None = None):
        self._api = api
        self._bind = str(bind or "127.0.0.1")
        # 0 = системный порт (тесты и «порт не задан»); `port or 1001`
        # превращал 0 в 1001 - тест попадал бы на чужой слушатель этого порта.
        self._port = 1001 if port is None else int(port)
        self._rate_bps = int(rate_bps or 0)
        self._watchers_max = int(watchers_max or 0)
        self._log = log or (lambda message: None)
        # Реестр токенов и журнал читаются лениво - при первом старте: битый
        # файл токенов не должен ронять окно (Api строится до слушателя), а
        # отказ стартовать с неизвестными правами - виден в журнале.
        self._tokens = tokens
        self._audit = audit
        self._language = language or (lambda: i18n.DEFAULT_LANGUAGE)
        self._marker = page_marker if page_marker is not None else {"base": ""}
        self._httpd: _ServeServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """Фактический порт (0 в конструкторе -> системный, для тестов)."""
        return self._httpd.server_address[1] if self._httpd else self._port

    @property
    def running(self) -> bool:
        return self._httpd is not None and self._thread is not None \
            and self._thread.is_alive()

    def base_url(self) -> str:
        """Адрес, с которого человек открывает страницу (https всегда)."""
        host = self._bind if self._bind not in ("0.0.0.0", "") else "127.0.0.1"
        return f"https://{host}:{self.port}"

    def start(self) -> int:
        """Поднять слушатель. Возвращает порт (повторный вызов - без вреда)."""
        if self.running:
            return self.port
        try:
            page = build_page(marker=self._marker).encode("utf-8")
        except Exception as exc:  # noqa: BLE001 - нет ui_src - сервер не поднимется
            raise ServeError(str(exc)) from exc

        # Права читаются до подъёма: поднимать слушатель с неизвестными
        # скоупами - это «доступ есть, но какой - непонятно».
        try:
            self._tokens = self._tokens or TokenStore()
            self._audit = self._audit or Audit()
        except TokenStoreError as exc:
            raise ServeError(str(exc)) from exc

        handler = type("_BoundHandler", (_Handler,), {
            "api": self._api,
            "tokens": self._tokens,
            "audit": self._audit,
            "page": page,
            "rate_bps": self._rate_bps,
            "watchers_max": self._watchers_max,
            "language": staticmethod(self._language),
        })
        httpd = _ServeServer((self._bind, self._port), handler)
        httpd.daemon_threads = True
        httpd.log = self._log
        # TLS обязателен. Материал эфемерный (память), SAN - адрес привязки,
        # ключ ECDSA: TLS-стек браузера отказывает Ed25519 ещё до решения
        # человека о доверии (app/http/tls.py). Отпечаток SPKI того же
        # сертификата пишем в журнал - клиент запоминает именно его (TOFU, §5).
        try:
            cert_pem, key_pem = tls.material(_san_hosts(self._bind))
            httpd.ctx = tls.context_from(cert_pem, key_pem)
        except Exception as exc:  # noqa: BLE001 - нет профиля и материала TLS
            httpd.server_close()
            raise ServeError(f"TLS-контекст не построен: {exc}") from exc
        self._log(f"Доступ по HTTPS: отпечаток ключа страницы "
                  f"{tls.fingerprint(cert_pem)[:16]}… (клиент запоминает его)")

        self._audit.rotate()
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        daemon=True, name="omnistash-serve")
        self._thread.start()
        return self.port

    def stop(self) -> None:
        """Снять слушатель; соединение в середине ответа не валит выход."""
        httpd, self._httpd = self._httpd, None
        thread, self._thread = self._thread, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)

    def tokens_state(self) -> dict:
        """Сколько ключей живёт и какие права (значений токенов здесь нет)."""
        if self._tokens is None:
            # Реестр ещё не читался: слушатель не поднят или стартовал с
            # отказом - «ключей ноль» было бы правдой только про счёт.
            return {"tokens": [], "count": 0,
                    "error": "реестр токенов не прочитан: слушатель не поднят"}
        try:
            listed = self._tokens.list()
        except TokenStoreError as exc:
            return {"error": str(exc), "tokens": [], "count": 0}
        return {"tokens": listed, "count": len(listed)}
