#!/usr/bin/env bash
# Собирает нативный Rust-модуль yandi_rs в ТЕКУЩЕЕ Python-окружение и проверяет его тестами параллельности.
# Ничего не включает в бою: Python остаётся активным по умолчанию (переключатели — отдельно, см. конец вывода).
#
#   ./scripts/rust-build-and-verify.sh                 # использует текущий python (лучше — из вашего venv)
#   YANDI_PYTHON=/path/to/venv/bin/python ./scripts/rust-build-and-verify.sh
#
# Что нужно заранее: Rust (cargo) и `pip install maturin` в том же окружении. Подробности — rustlib/README.md.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${YANDI_PYTHON:-python3}"
say() { printf '\n== %s\n' "$*"; }

say "1/4 проверка инструментов"
command -v cargo >/dev/null || { echo "НЕ НАЙДЕН cargo (Rust). Установите: https://rustup.rs"; exit 1; }
"$PY" -c "import maturin" 2>/dev/null || command -v maturin >/dev/null || { echo "НЕ НАЙДЕН maturin. Выполните: $PY -m pip install maturin"; exit 1; }
echo "cargo: $(cargo --version)   python: $($PY --version 2>&1)"

say "2/4 сборка (cargo test + maturin develop --release) — несколько минут при первом запуске"
( cd rustlib/yandi_rs && cargo test --lib 2>&1 | tail -3 && cargo test --no-default-features --lib 2>&1 | tail -3 ) || { echo "ОШИБКА: юнит-тесты Rust не прошли (в обоих режимах: с Python и без)"; exit 1; }
VENV_BIN="$(dirname "$("$PY" -c 'import sys;print(sys.executable)')")"
export VIRTUAL_ENV="$(dirname "$VENV_BIN")"
( cd rustlib/yandi_rs && PATH="$VENV_BIN:$PATH" maturin develop --release 2>&1 | tail -3 ) || { echo "ОШИБКА: сборка не удалась"; exit 1; }
"$PY" -c "import yandi_rs; print('yandi_rs собран и импортируется:', yandi_rs.__file__)" || exit 1
# родной шлюз к моделям (rustlib/yandi_llm): юнит-тесты без Python + мост для дифференциальных тестов против llm_gateway
( cd rustlib/yandi_llm && cargo test --no-default-features --lib 2>&1 | tail -3 && PATH="$VENV_BIN:$PATH" maturin develop --release 2>&1 | tail -2 ) || { echo "ОШИБКА: сборка yandi_llm не удалась"; exit 1; }
# встроенное хранилище состояния (замена Redis): юнит-тесты + мост для сверки с НАСТОЯЩИМ redis-server (нужен redis-server в PATH, иначе тест пропускается)
( cd rustlib/yandi_state && cargo test --no-default-features --lib 2>&1 | tail -3 && PATH="$VENV_BIN:$PATH" maturin develop --release 2>&1 | tail -2 ) || { echo "ОШИБКА: сборка yandi_state не удалась"; exit 1; }

say "3/4 проверка параллельности с Python (все *_rust_parity_test + массовый Unicode-фаззинг)"
export YANDI_TEST_MODE=1
fail=0; n=0
for f in $(ls agent/*_rust_parity_test.py pet/*_rust_parity_test.py 2>/dev/null) agent/rust_unicode_fuzz_parity_test.py agent/rust_python_text_semantics_parity_test.py llm_gateway/native_parity_test.py llm_gateway/native_remote_parity_test.py llm_gateway/native_secure_store_parity_test.py llm_gateway/native_client_parity_test.py agent/state_native_parity_test.py; do
  m="$(echo "${f%.py}" | tr '/' '.')"
  n=$((n+1))
  if "$PY" -m "$m" >/dev/null 2>&1; then echo "  ok    $m"; else echo "  ПРОВАЛ $m"; fail=$((fail+1)); fi
done
echo "прогнано тестов параллельности: $n, провалов: $fail"

say "4/4 итог"
if [ "$fail" -ne 0 ]; then echo "Есть провалы — Rust НЕ включать. Пришлите вывод разработчику."; exit 1; fi
cat <<'TXT'
Всё сходится. Rust-модуль собран, Python по-прежнему используется по умолчанию (ничего не включено).

Как включить (по одному, откатывается удалением переменной и перезапуском):
  export YANDI_SOURCE_CLUSTERING_ENGINE=rust      # кластеризация источников: ×11–12 быстрее на больших пулах
  export YANDI_CLAIM_GRAPH_ENGINE=rust            # граф утверждений: ×6–7
  export YANDI_HARDENING_ENGINE=rust              # защита от ложного слияния утверждений: ×12
  export YANDI_CLAIM_VALIDATOR_ENGINE=rust        # фильтр мусорных утверждений: ×10
  export YANDI_OBJECT_RESOLVER_ENGINE=rust        # тип объекта запроса: ×8
  export YANDI_INTENT_ROUTER_ENGINE=rust          # тип запроса: ×5
  export YANDI_TARGET_ROUTER_ENGINE=rust          # адресат запроса: ×4–5
  export YANDI_RELATIONSHIP_MEMORY_ENGINE=rust    # стеммер обид/извинений/личных фактов: ×9
  export YANDI_PET_EXTRACTION_ENGINE=rust         # нарезка сообщения на слова, «похоже на секрет», «внутри цитаты»: ×3–12
  export YANDI_TOOL_SHELL_ENGINE=rust             # охранный шлюз shell-команд агента: ×5
  export YANDI_ORCH_TAG_TREE_ENGINE=rust          # энтропия дерева тегов: ×3
Остальные переключатели (см. rustlib/README.md) дают мало скорости; их включать необязательно.
Замер на вашей машине:  python scripts/rust_bench.py   (в том же окружении)
TXT
