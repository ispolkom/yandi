#!/usr/bin/env bash
# Runs the core deterministic regression suites (mocked model, in-memory fakes;
# no live model, no live database, no network).
#
# TEST SUITE MUST NEVER WRITE THE LIVE OWNER DATABASE. Every suite runs with
# YANDI_TEST_MODE=1, and the connection layer refuses any real database
# connection from a test process (agent/db/sql/connection.py). On top of that:
#   * a core suite must not even ATTEMPT a connection (the guard prints
#     "[live-db-guard] REFUSED"; that fails the run — hermetic by construction,
#     not merely saved by the guard);
#   * when a database is reachable, its per-table row counts / auto-increment
#     counters are fingerprinted before and after (scripts/live_db_fingerprint.py,
#     read-only) and any change fails the run.
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
  pet.pet_event_extraction_regression_test
  pet.pet_event_provenance_regression_test
  pet.pet_relationship_focus_regression_test
  pet.pet_relationship_state_causality_regression_test
  pet.pet_commitment_events_regression_test
  pet.pet_turn_identity_regression_test
  pet.pet_personal_memory_regression_test
  agent.message_intensity_regression_test
  agent.relationship_memory_regression_test
  agent.relationship_apology_matching_regression_test
  agent.relationship_healing_clock_regression_test
  agent.relationship_state_regression_test
  agent.relationship_commitments_regression_test
  agent.relationship_idempotency_regression_test
  agent.epistemic_canonical_trust_shadow_regression_test
  agent.writeback_episodic_sql_regression_test
  agent.db_sql_shadow_write_regression_test
  agent.db_sql_security_injection_regression_test
  agent.db_sql_test_isolation_regression_test
)

FP="$(mktemp)"
trap 'rm -f "$FP"' EXIT
fp_state="skipped"
if env -u YANDI_TEST_MODE "$PYTHON" scripts/live_db_fingerprint.py save "$FP" >/dev/null 2>&1; then fp_state="taken"; fi

failed=()
for suite in "${SUITES[@]}"; do
  echo "=== $suite"
  if YANDI_TEST_MODE=1 "$PYTHON" -m "$suite" >"${TMPDIR:-/tmp}/yandi-test-core.out" 2>&1; then
    tail -n 2 "${TMPDIR:-/tmp}/yandi-test-core.out"
    if grep -q "\[live-db-guard\] REFUSED" "${TMPDIR:-/tmp}/yandi-test-core.out"; then
      echo "  ^ FAILED: this suite tried to open a database connection (the guard refused it); core suites must be hermetic"
      failed+=("$suite (attempted a database connection)")
    fi
  else
    tail -n 25 "${TMPDIR:-/tmp}/yandi-test-core.out"
    failed+=("$suite")
  fi
done

echo
if [ "$fp_state" = "taken" ]; then
  if ! env -u YANDI_TEST_MODE "$PYTHON" scripts/live_db_fingerprint.py compare "$FP"; then
    failed+=("live database changed during the run")
  fi
else
  echo "live database not reachable: before/after fingerprint skipped"
fi

if [ "${#failed[@]}" -eq 0 ]; then
  echo "ALL CORE SUITES PASSED (${#SUITES[@]})"
else
  echo "FAILED: ${failed[*]}"
  exit 1
fi
