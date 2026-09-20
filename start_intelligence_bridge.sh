#!/usr/bin/env bash
# start_intelligence_bridge.sh — запуск локального моста
# llm_gateway.intelligence_bridge для node/src/ai_rpc (YANDI Node
# Intelligence RPC). Слушает ТОЛЬКО 127.0.0.1:18083 — Rust-нода
# использует его вместо прямого обращения к Ollama, когда отвечает на
# межнодовый интеллектуальный запрос (свой или от пира). Не путать с
# start.sh (PET/council_chat_server) — отдельный процесс, отдельная
# ответственность.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-18083}"
# Python interpreter: $YANDI_PYTHON, else ./.venv, else ~/venv, else python3
PYTHON="${YANDI_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then PYTHON="$SCRIPT_DIR/.venv/bin/python3"
  elif [ -x "$HOME/venv/bin/python3" ]; then PYTHON="$HOME/venv/bin/python3"
  else PYTHON="python3"; fi
fi

cd "$SCRIPT_DIR"
exec "$PYTHON" -m llm_gateway.intelligence_bridge "$PORT"
