#!/usr/bin/env bash
# The node's core supervisor (node/core_supervisor): format, build, tests, real-core tests, contract scenarios, mutants.
# Offline (the crates come from the local cargo registry cache). Nothing here touches the live system.
#
#   scripts/test-node-supervisor.sh
#   YANDI_PYTHON=/path/to/python scripts/test-node-supervisor.sh    # a python with the core's requirements (uvicorn, cryptography, ...)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${YANDI_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$ROOT/.venv/bin/python3" ]; then PYTHON="$ROOT/.venv/bin/python3"
  elif [ -x "$HOME/venv/bin/python3" ]; then PYTHON="$HOME/venv/bin/python3"
  else PYTHON="python3"; fi
fi

cd "$ROOT/node"
echo "=== cargo fmt --check (core_supervisor)"
cargo fmt -p yandi-core-supervisor -- --check
echo "=== cargo check --offline (core_supervisor, then the whole node)"
cargo check --offline -p yandi-core-supervisor
cargo check --offline --lib
echo "=== cargo test --offline (core_supervisor: unit + supervision against a stand-in core)"
cargo test --offline -p yandi-core-supervisor
echo "=== the node's managed-core wiring"
cargo test --offline --lib managed_core
echo "=== cargo test --ignored (the supervisor against the REAL Python core)"
YANDI_TEST_PYTHON="$PYTHON" cargo test --offline -p yandi-core-supervisor --test real_core -- --ignored
echo "=== contract: the supervisor scenarios through the Rust harness"
cargo build --offline -p yandi-core-supervisor --example conformance_harness
cd "$ROOT"
"$PYTHON" -m contract.runner run --target none --only supervisor. --supervisor-harness "$ROOT/node/target/debug/examples/conformance_harness"
echo "=== mutants S1-S14"
"$PYTHON" scripts/supervisor_mutants.py
echo "ALL NODE SUPERVISOR CHECKS PASSED"
