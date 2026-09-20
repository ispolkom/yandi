#!/usr/bin/env bash
# Runs the core deterministic regression suites (mocked model, in-memory fakes;
# no live model, no live database, no network).
#
#   scripts/test-core.sh
#   YANDI_PYTHON=/path/to/python scripts/test-core.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${YANDI_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$ROOT/.venv/bin/python3" ]; then PYTHON="$ROOT/.venv/bin/python3"
  elif [ -x "$HOME/venv/bin/python3" ]; then PYTHON="$HOME/venv/bin/python3"
  else PYTHON="python3"; fi
fi

SUITES=(
  llm_gateway.client_regression_test
  llm_gateway.remote_backend_regression_test
  llm_gateway.llamacpp_backend_regression_test
  llm_gateway.intelligence_bridge_regression_test
  llm_gateway.secure_store_regression_test
  pet.pet_chat_local_regression_test
  pet.pet_event_provenance_regression_test
  pet.pet_relationship_focus_regression_test
  pet.pet_relationship_state_causality_regression_test
  agent.message_intensity_regression_test
  agent.relationship_memory_regression_test
  agent.relationship_apology_matching_regression_test
  agent.relationship_healing_clock_regression_test
  agent.relationship_state_regression_test
  agent.relationship_commitments_regression_test
  agent.epistemic_canonical_trust_shadow_regression_test
  agent.writeback_episodic_sql_regression_test
  agent.db_sql_shadow_write_regression_test
  agent.db_sql_security_injection_regression_test
)

failed=()
for suite in "${SUITES[@]}"; do
  echo "=== $suite"
  if "$PYTHON" -m "$suite" >"${TMPDIR:-/tmp}/yandi-test-core.out" 2>&1; then
    tail -n 2 "${TMPDIR:-/tmp}/yandi-test-core.out"
  else
    tail -n 25 "${TMPDIR:-/tmp}/yandi-test-core.out"
    failed+=("$suite")
  fi
done

echo
if [ "${#failed[@]}" -eq 0 ]; then
  echo "ALL CORE SUITES PASSED (${#SUITES[@]})"
else
  echo "FAILED: ${failed[*]}"
  exit 1
fi
