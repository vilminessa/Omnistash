#!/bin/sh
# Запуск шлюза: hub-link (брокер) поднимается ПЕРВЫМ и живёт своей
# жизнью; MCP-сервер - вторым процессом. set -e НЕ ставим: падение MCP
# не должно убивать контейнер (иначе рестарт-цикл роняет и брокер).
# Данные (/data) и токен - общие.

echo "шлюз: поднимаю hub-link (порт ${LINK_PORT}, каталог ${LINK_DIR})" >&2
python /app/link.py serve --id hub --token "$TOKEN" \
    --port "$LINK_PORT" --dir "$LINK_DIR" &
HUB_PID=$!

# ждём, пока hub ответит на ctrl-порт (иначе MCP упадет на холодном старте)
for _ in $(seq 1 20); do
    if python /app/link.py status --port "$LINK_PORT" >/dev/null 2>&1; then
        echo "шлюз: hub-link отвечает" >&2
        break
    fi
    sleep 0.5
done

echo "шлюз: поднимаю MCP-сервер (порт ${MCP_PORT})" >&2
python /app/mcp_server.py &
MCP_PID=$!

# если любой процесс умер - не крутим контейнер в цикле: держим hub,
# а MCP перезапускаем через backoff
while :; do
    if ! kill -0 "$HUB_PID" 2>/dev/null; then
        echo "шлюз: hub-link умер - перезапускаю" >&2
        python /app/link.py serve --id hub --token "$TOKEN" \
            --port "$LINK_PORT" --dir "$LINK_DIR" &
        HUB_PID=$!
    fi
    if ! kill -0 "$MCP_PID" 2>/dev/null; then
        echo "шлюз: MCP-сервер умер - перезапускаю через 3с" >&2
        sleep 3
        python /app/mcp_server.py &
        MCP_PID=$!
    fi
    sleep 5
done
