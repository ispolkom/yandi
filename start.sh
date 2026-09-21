#!/usr/bin/env bash
# start.sh — запуск YANDI PET (council chat server)
# Использование: ./start.sh [port]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-9010}"
# Python interpreter: $YANDI_PYTHON, else ./.venv, else ~/venv, else python3
PYTHON="${YANDI_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then PYTHON="$SCRIPT_DIR/.venv/bin/python3"
  elif [ -x "$HOME/venv/bin/python3" ]; then PYTHON="$HOME/venv/bin/python3"
  else PYTHON="python3"; fi
fi

# Проверка Redis
if ! redis-cli ping &>/dev/null; then
  echo "[ERROR] Redis не запущен. Запусти: sudo systemctl start redis"
  exit 1
fi

# Проверка Ollama (опционально — теперь это фоллбэк, не основной путь,
# см. llm_gateway/llamacpp_backend.py, но если и он недоступен, а
# локальный движок споткнётся, деградировать будет не на что)
if ! curl --noproxy '127.0.0.1,localhost' -s http://127.0.0.1:11434/api/tags &>/dev/null; then
  echo "[WARN] Ollama (фоллбэк) не доступен"
fi

# Свой движок инференса (llama.cpp, те же веса, что у Ollama, но без
# HTTP-сервера между нами и моделью) — Ollama остаётся автоматическим
# фоллбэком внутри llm_gateway, если локальный движок недоступен или
# упадёт. См. память ollama-decoupling-plan.
export LLM_GATEWAY_ENABLE_LOCAL=1

# Спросить про выбор модели, только если это реальный терминал и модель
# ещё не настроена — в headless/systemd-запуске (нет TTY) ничего не
# спросит и не заблокирует старт, просто напечатает подсказку. || true
# — сбой этого шага никогда не должен мешать запуску самой ноды.
"$PYTHON" -c "from llm_gateway.setup import maybe_prompt_first_run; maybe_prompt_first_run()" || true

# Защищённая личная память (P1c-2). Если она включена в базе (python -m agent.db.sql.protect seal), этому процессу нужен ключ:
# он берётся у ключевой утилиты ноды с ключом ЭТОЙ машины (пароль не вводится, ключ в файл не пишется).
# По умолчанию включается сам, если утилита собрана; отключить: YANDI_PROTECTED_STORAGE=0 ./start.sh
KEYS_TOOL="${YANDI_KEYS_TOOL:-$SCRIPT_DIR/node/target/release/yandi-keys}"
if [ "${YANDI_PROTECTED_STORAGE:-auto}" = "1" ] && [ ! -x "$KEYS_TOOL" ]; then
  echo "[ERROR] Нет ключевой утилиты: $KEYS_TOOL. Собери: cd node && cargo build --release -p yandi-key-root"
  exit 1
fi
if [ "${YANDI_PROTECTED_STORAGE:-auto}" != "0" ] && [ -x "$KEYS_TOOL" ]; then
  export YANDI_STORAGE_KEY_TOOL="$KEYS_TOOL"
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
