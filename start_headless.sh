#!/bin/bash
# YANDI headless server — без браузера, только хранилище + оркестратор
# Запуск: ./start_headless.sh [PORT]

set -e

PORT=${1:-9010}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="python3"

# Зависимости
pip install -q fastapi uvicorn redis requests pydantic httpx trafilatura 2>/dev/null

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
