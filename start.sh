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

echo "[OK] Запуск PET на порту $PORT..."
cd "$SCRIPT_DIR"
exec "$PYTHON" pet/council_chat_server.py --port "$PORT" 2>&1
