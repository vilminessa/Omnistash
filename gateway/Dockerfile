# Контейнер-шлюз канала link: хаб (только stdlib) + MCP-сервер.
# Контекст сборки - корень репозитория (см. compose.yaml).
FROM python:3.12-slim

WORKDIR /app

COPY tools/link.py ./link.py
COPY gateway/mcp_server.py ./mcp_server.py
COPY gateway/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# mcp - официальный SDK (streamable HTTP), uvicorn - его ASGI-сервер
RUN pip install --no-cache-dir "mcp>=2,<3" "uvicorn>=0.34"

ENV LINK_PORT=18581 \
    LINK_DIR=/data \
    MCP_PORT=8809 \
    PYTHONIOENCODING=utf-8

VOLUME /data
EXPOSE 18581 8809

# healthcheck: статус хаба = демон отвечает на локальный ctrl-порт
HEALTHCHECK --interval=30s --timeout=6s --start-period=15s --retries=3 \
    CMD python link.py status --port ${LINK_PORT} >/dev/null 2>&1 || exit 1

# entrypoint сам следит за двумя процессами: hub-link живёт всегда,
# MCP перезапускается отдельно (его падение не роняет контейнер)
CMD ["./entrypoint.sh"]
