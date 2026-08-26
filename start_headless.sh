#!/bin/bash
# YANDI headless server — без браузера, только хранилище + оркестратор
# Запуск: ./start_headless.sh [PORT]

PORT=${1:-9010}
cd "$(dirname "$0")"

# Зависимости
pip install -q fastapi uvicorn redis requests pydantic httpx trafilatura 2>/dev/null

echo "🧠 YANDI Knowledge Server (headless)"
echo "   Port: $PORT"
echo "   Mode: storage + orchestrator (no browser)"
echo ""

python3 -m uvicorn pet.council_chat_server:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level warning
