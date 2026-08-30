#!/usr/bin/env bash
# start.sh — запуск YANDI PET (council chat server)
# Использование: ./start.sh [port]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-9010}"
PYTHON="/home/iam/venv/bin/python3"

# Проверка Redis
if ! redis-cli ping &>/dev/null; then
  echo "[ERROR] Redis не запущен. Запусти: sudo systemctl start redis"
  exit 1
fi

# Проверка Ollama (опционально)
if ! curl --noproxy '127.0.0.1,localhost' -s http://127.0.0.1:11434/api/tags &>/dev/null; then
  echo "[WARN] Ollama не доступен — YANDI Помощник работать не будет"
fi

REQUIRED_SQL_GROUP="yandi-db"
CURRENT_USER="$(id -un)"
if getent group "$REQUIRED_SQL_GROUP" >/dev/null \
  && ! id -nG | tr ' ' '\n' | grep -qx "$REQUIRED_SQL_GROUP" \
  && getent group "$REQUIRED_SQL_GROUP" | awk -F: -v u="$CURRENT_USER" '
    BEGIN { found=0 }
    { split($4, members, ","); for (i in members) if (members[i] == u) found=1 }
    END { exit found ? 0 : 1 }
  '; then
  sg "$REQUIRED_SQL_GROUP" -c \
    "cd $(printf "%q" "$SCRIPT_DIR") && exec $(printf "%q" "$PYTHON") pet/council_chat_server.py --port $(printf "%q" "$PORT") 2>&1"
  exit $?
fi

echo "[OK] Запуск PET на порту $PORT..."
cd "$SCRIPT_DIR"
exec "$PYTHON" pet/council_chat_server.py --port "$PORT" 2>&1
