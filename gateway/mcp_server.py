"""MCP-шлюз канала link: инструменты обмена как MCP-tools (Docker).

Роль в связке (MCP-hop из статьи об agent gateways): контейнер держит
журнал и состояние (хаб link-демон внутри того же контейнера) и отдаёт
их любому MCP-клиенту по streamable HTTP. Агент на vilmnote добавляет
remote-MCP URL http://100.101.230.19:8809/mcp с Bearer-токеном и
работает с каналом инструментами, а не CLI.

Управление (как в шлюзах со статьи):
  * входящая аутентификация - Bearer-токен на КАЖДЫЙ запрос (401 без);
  * инструменты - только явно перечисленные (allow-list самим списком);
  * журнал вызовов - uvicorn-лог контейнера (каждый вызов виден).

Инструменты прокидывают команды tools/link.py локальному хаб-демону
через subprocess (argv без шелла - проблемы кавычек PowerShell нет).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount
import uvicorn

TOKEN = os.environ.get("TOKEN", "")
LINK_PORT = os.environ.get("LINK_PORT", "18581")
LINK_DIR = os.environ.get("LINK_DIR", "/data")
MCP_PORT = int(os.environ.get("MCP_PORT", "8809"))
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _cli(args: list, stdin: str | None = None) -> str:
    """Прогнать команду link через локальный хаб; stdout - строка.

    state set принимает значение по stdin (JSON) - так надёжнее, чем
    аргументы (те портит PowerShell; subprocess тут свой, но stdin
    уже проверен и для CLI, и для агентов).
    """
    proc = subprocess.run(
        [sys.executable, "/app/link.py", *args,
         "--port", LINK_PORT, "--dir", LINK_DIR],
        capture_output=True, input=stdin.encode("utf-8") if stdin else None,
        env=ENV, timeout=30)
    out = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode not in (0,):
        detail = (out + " " + proc.stderr.decode("utf-8", "replace")).strip()
        raise RuntimeError(f"link завершился с кодом {proc.returncode}: "
                           f"{detail[:400]}")
    return out


# mcp 2.x: FastMCP переименован в MCPServer (см. миграционный гид SDK);
# @mcp.tool(), streamable_http_app() и session_manager не изменились.
mcp = MCPServer(
    "omnistash-link",
    instructions="Канал обмена двух ПК (vilmpc/vilmnote): журнал - append-only "
                 "сообщения обоих агентов, состояние - карта ключей LWW. "
                 "Когда тебе нужен ответ - пиши channel_log (state не будит "
                 "пару); проверяй почту channel_read периодически или через "
                 "локальный `link wait`.")


@mcp.tool()
def channel_log(text: str, level: str = "info") -> dict:
    """Отправить запись в журнал канала (оба агента видят сразу)."""
    return json.loads(_cli(["log", text, "--level", level]))


@mcp.tool()
def channel_read(last: int = 20, json_out: bool = False) -> str:
    """Последние записи журнала (оба ПК, в порядке seq). json_out - NDJSON."""
    args = ["tail", "--last", str(max(1, int(last)))]
    if json_out:
        args.append("--json")
    return _cli(args)


@mcp.tool()
def channel_state_set(key: str, value) -> dict:
    """Записать своё состояние (ключ -> JSON, LWW; пара видит сразу)."""
    return json.loads(_cli(["state", "set", key],
                           stdin=json.dumps(value, ensure_ascii=False)))


@mcp.tool()
def channel_state_get(key: str = "", verbose: bool = False) -> str:
    """Состояние обоих ПК: без key - все ключи, с key - значение."""
    args = ["state", "get"]
    if verbose:
        args.append("--verbose")
    if key:
        args.append(key)
    return _cli(args)


@mcp.tool()
def channel_status() -> str:
    """Связь шлюза: пиров онлайн, RTT, задержка, последние заметки."""
    return _cli(["status"])


class _BearerAuth(BaseHTTPMiddleware):
    """Bearer-токен на каждый запрос; без него - 401 (иначе открыт)."""

    async def dispatch(self, request, call_next):
        expected = f"Bearer {TOKEN}"
        if TOKEN and request.headers.get("authorization", "") != expected:
            return PlainTextResponse("unauthorized", status_code=401)
        return await call_next(request)


def _security():
    """Разрешаем реальные хосты: шлюз отвечает на Tailscale-адресах (100.x)
    и на localhost. Без этого streamable_http_app() отвечает 421 на ЛЮБОЙ
    нелокальный Host (защита от DNS-rebinding по умолчанию). Важно: API
    принимает только точные хосты и wildcard, CIDR НЕ поддерживается -
    перечисляем адреса пары явно.
    """
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    # Tailscale-адреса vilmpc (шлюз) и vilmnote (пара) - стабильны
    hosts += ["100.101.230.19", "100.101.230.19:*",
              "100.66.102.36", "100.66.102.36:*"]
    return TransportSecuritySettings(allowed_hosts=hosts,
                                     allowed_origins=[])


def build_app():
    sub = mcp.streamable_http_app(transport_security=_security())

    @asynccontextmanager
    async def lifespan(app) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = Starlette(routes=[Mount("/", app=sub)], lifespan=lifespan)
    # новый Starlette не принимает middlewares= в конструкторе - ставим
    # методом; middleware идёт ВНЕ маршрутов, поэтому /mcp под ним
    app.add_middleware(_BearerAuth)
    return app


if __name__ == "__main__":
    if not TOKEN:
        print("mcp-шлюз: НЕ ЗАПУЩЕН - не задан TOKEN (в docker-compose он "
              "обязателен)", file=sys.stderr)
        sys.exit(2)
    print(f"mcp-шлюз: http://0.0.0.0:{MCP_PORT}/mcp (Bearer), "
          f"хаб link на {LINK_PORT}, данные в {LINK_DIR}", flush=True)
    uvicorn.run(build_app(), host="0.0.0.0", port=MCP_PORT,
                log_level="warning")
