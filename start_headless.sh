#!/bin/bash
# YANDI headless server — без браузера, только хранилище + оркестратор
# Запуск: ./start_headless.sh [PORT]

set -e

PORT=${1:-9010}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="python3"

# Зависимости
pip install -q fastapi uvicorn redis requests pydantic httpx trafilatura 2>/dev/null

# Свой движок инференса (llama.cpp, те же веса, что у Ollama, но без
# HTTP-сервера между нами и моделью) — Ollama остаётся автоматическим
# фоллбэком внутри llm_gateway, если локальный движок недоступен или
# упадёт. См. память ollama-decoupling-plan.
export LLM_GATEWAY_ENABLE_LOCAL=1

# Спросить про выбор модели, только если это реальный терминал и модель
# ещё не настроена — под systemd (нет TTY) ничего не спросит и не
# заблокирует старт, просто напечатает подсказку. || true — сбой этого
# шага никогда не должен мешать запуску самой ноды.
"$PYTHON" -c "from llm_gateway.setup import maybe_prompt_first_run; maybe_prompt_first_run()" || true

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
    "cd $(printf "%q" "$SCRIPT_DIR") && exec $(printf "%q" "$PYTHON") -m uvicorn pet.council_chat_server:app --host 0.0.0.0 --port $(printf "%q" "$PORT") --log-level warning"
  exit $?
fi

cd "$SCRIPT_DIR"

echo "🧠 YANDI Knowledge Server (headless)"
echo "   Port: $PORT"
echo "   Mode: storage + orchestrator (no browser)"
echo ""

python3 -m uvicorn pet.council_chat_server:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level warning
