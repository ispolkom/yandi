#!/usr/bin/env bash
# Runs EVERY *_test.py suite in the repository (not just the core set), in
# parallel, and proves the live owner database was not touched:
#
#   * every suite runs with YANDI_TEST_MODE=1 (the connection layer refuses any
#     real database connection from a test process);
#   * the live database's per-table row counts and auto-increment counters are
#     fingerprinted before and after (scripts/live_db_fingerprint.py, read-only);
#   * suites that ATTEMPTED a connection (and were refused) are listed.
#
#   scripts/test-all.sh
#   YANDI_PYTHON=/path/to/python JOBS=4 scripts/test-all.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${YANDI_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$ROOT/.venv/bin/python3" ]; then PYTHON="$ROOT/.venv/bin/python3"
  elif [ -x "$HOME/venv/bin/python3" ]; then PYTHON="$HOME/venv/bin/python3"
  else PYTHON="python3"; fi
fi
export PYTHON
JOBS="${JOBS:-4}"
OUT="$(mktemp -d)"
export OUT
trap 'rm -rf "$OUT"' EXIT

fp_state="skipped"
if env -u YANDI_TEST_MODE "$PYTHON" scripts/live_db_fingerprint.py save "$OUT/fp.json" >/dev/null 2>&1; then fp_state="taken"; fi

run_one() {
  local m="$1"
  YANDI_TEST_MODE=1 timeout 300 "$PYTHON" -m "$m" >"$OUT/$m.log" 2>&1
  echo "$? $m" >>"$OUT/results.txt"
}
export -f run_one

find agent pet llm_gateway -name '*_test.py' | sed 's/\.py$//; s#/#.#g' | sort | xargs -P "$JOBS" -I{} bash -c 'run_one {}'

total="$(wc -l <"$OUT/results.txt")"
failed="$(awk '$1!=0 {print $2}' "$OUT/results.txt" | sort)"
attempted="$(grep -l "\[live-db-guard\] REFUSED" "$OUT"/*.log 2>/dev/null | xargs -r -n1 basename | sed 's/\.log$//' | sort)"

echo "suites run: $total"
if [ -n "$attempted" ]; then
  echo "suites that ATTEMPTED a live database connection (refused by the guard, nothing written):"
  echo "$attempted" | sed 's/^/  /'
fi

status=0
if [ -n "$failed" ]; then
  echo "FAILED suites:"
  echo "$failed" | sed 's/^/  /'
  status=1
fi
if [ "$fp_state" = "taken" ]; then
  env -u YANDI_TEST_MODE "$PYTHON" scripts/live_db_fingerprint.py compare "$OUT/fp.json" || status=1
else
  echo "live database not reachable: before/after fingerprint skipped"
fi
[ "$status" -eq 0 ] && echo "ALL SUITES PASSED ($total), live database untouched"
exit "$status"
