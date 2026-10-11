"""Mesh-сервер: отдаёт манифесты и файлы участникам (с Range).

Только то, что у меня проверено: файл отдаётся, если для него известен
sha256 (files.hash) - ничем непроверенным не делимся. Манифест
подписывается нашим ключом на лету (manifest.build), поэтому дата синка
и хеш всегда свежие относительно базы.

Работает в отдельном потоке (ThreadingHTTPServer): соединения участников
не трогают ни окно, ни очередь. Логи http.server глушим - строки уходят
в журнал программы самим сервером, а не в stderr.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import channel as channel_mod
from . import manifest as manifest_mod

# Отдаём только байты: тела файлов чанками, без чтения в память целиком.
CHUNK = 256 * 1024


class _Server(ThreadingHTTPServer):
    """TCPServer без socket.getfqdn в server_bind + TLS на приёме.

    Стандартный server_bind зовёт getfqdn(адрес): на Windows обратный
    DNS для 0.0.0.0 висит на таймаутах - замер на этой машине: 17 секунд
    на ПОДЪЁМ сервера. Имя хоста участнику не нужно - только порт.

    Каждое входящее соединение заворачивается в TLS (см. mesh.channel):
    handshake идёт здесь, с таймаутом - медленный/чужой клиент не держит
    поток; сбой рукопожатия не валит сервер (handle_error глушит).
    """

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
            sock.settimeout(channel_mod.HANDSHAKE_TIMEOUT)
            tls = self.ctx.wrap_socket(sock, server_side=True)
            tls.settimeout(None)       # раздача файлов - без лимита
            return tls, addr
        except (OSError, ssl.SSLError) as exc:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def handle_error(self, request, client_address) -> None:
        """Отказ handshake (старый клиент, мусор) - не авария сервера.

        Ошибки внутри do_GET ловятся самим обработчиком и уходят
        участнику как HTTP-ответ; сюда попадает только то, что случилось
        до/вне запроса - печатаем одну строку вместо стектрейса.
        """
        import sys
        error = sys.exc_info()[1]
        if error is not None:
            self.log(f"Mesh-сервер: соединение от {client_address[0]} "
                     f"отклонено: {type(error).__name__}: {error}")


class _Handler(BaseHTTPRequestHandler):
    server_version = "OmnistashMesh/1"
    database = None          # app.db.Database (свое соединение - в потоке)

    def log_message(self, *args) -> None:   # noqa: D102 - в stderr не пишем
        pass

    # -- разбор ------------------------------------------------------------

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, reason: str) -> None:
        self._json(code, {"error": reason})

    def _find_file(self, remote_id: str):
        """(path, digest, size, synced_at) - только с известным хешом."""
        conn = self.database.conn
        row = conn.execute(
            """SELECT f.path, f.hash, f.size, v.synced_at
                 FROM videos v JOIN files f ON f.video_id = v.id
                WHERE v.remote_id = ? AND v.platform <> 'local'
                  AND f.kind = 'video' AND f.missing = 0
                  AND f.hash IS NOT NULL
                ORDER BY f.id LIMIT 1""",
            (str(remote_id),)).fetchone()
        if row is None:
            return None
        return dict(row)

    # -- маршруты ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - имя из http.server
        path = (self.path or "").split("?", 1)[0]
        try:
            if path.startswith("/manifest/"):
                remote_id = path[len("/manifest/"):]
                found = self._find_file(remote_id)
                if not found:
                    self._error(404, "нет проверенного файла")
                    return
                item = manifest_mod.build(
                    remote_id=remote_id, digest=found["hash"],
                    size=int(found["size"] or 0),
                    synced_at=found["synced_at"])
                self._json(200, item)
                return

            if path.startswith("/file/"):
                self._send_file(path[len("/file/"):])
                return

            self._error(404, "неизвестный путь")
        except (BrokenPipeError, ConnectionResetError):
            pass                             # участник отключился - не беда
        except Exception as exc:             # noqa: BLE001 - сервер не должен умереть
            try:
                self._error(500, f"внутренняя ошибка: {exc}")
            except Exception:  # noqa: BLE001
                pass

    def _send_file(self, remote_id: str) -> None:
        import os
        found = self._find_file(remote_id)
        if not found:
            self._error(404, "нет проверенного файла")
            return
        path = found["path"]
        try:
            total = os.path.getsize(path)
        except OSError:
            self._error(404, "файл недоступен")
            return

        start, end = 0, total - 1
        range_header = self.headers.get("Range")
        partial = False
        if range_header and range_header.startswith("bytes="):
            try:
                first, _, last = range_header[len("bytes="):].partition("-")
                start = int(first) if first else 0
                end = int(last) if last else total - 1
                end = min(end, total - 1)
                if start > end or start >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.end_headers()
                    return
                partial = True
            except ValueError:
                partial = False

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()

        with open(path, "rb") as handle:
            handle.seek(start)
            left = length
            while left > 0:
                chunk = handle.read(min(CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


class PeerServer:
    """HTTPS-сервер участника: start() - фоном, stop() - дожидается."""

    def __init__(self, database, port: int = 0, log=None):
        self._database = database
        self._port = int(port)
        self._log = log or (lambda message: None)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """Фактический порт (0 в конструкторе -> системный, для тестов)."""
        return self._httpd.server_address[1] if self._httpd else self._port

    @property
    def running(self) -> bool:
        return self._httpd is not None and self._thread is not None \
            and self._thread.is_alive()

    def start(self) -> int:
        """Поднять сервер. Возвращает порт (повторный вызов - без вреда)."""
        if self.running:
            return self.port
        handler = type("_BoundHandler", (_Handler,),
                       {"database": self._database})
        httpd = _Server(("0.0.0.0", self._port), handler)
        httpd.daemon_threads = True
        httpd.log = self._log
        # TLS обязателен: материал эфемерный (память), аттестация - в
        # сертификате; отказ здесь = сервер не поднимется (журнал скажет).
        try:
            httpd.ctx = channel_mod.server_context()
        except Exception as exc:  # noqa: BLE001 - нет идентичности/профиля
            httpd.server_close()
            self._log(f"Mesh-сервер не поднялся (нет TLS-материала): {exc}")
            raise
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        daemon=True,
                                        name="omnistash-mesh-server")
        self._thread.start()
        return self.port

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd, self._thread = None, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
