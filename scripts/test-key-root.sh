#!/usr/bin/env bash
# The key root (node/key_root): format, build, tests (temporary directories only, a fake machine id, never ~/.yandi_keys), the node that uses it, mutants.
# Offline. Nothing here touches the owner's real key directory.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${YANDI_PYTHON:-python3}"
cd "$ROOT/node"
echo "=== cargo fmt --check (key_root)"
cargo fmt -p yandi-key-root -- --check
echo "=== cargo test --offline (key_root: unit, integration, the yandi-keys tool)"
cargo test --offline -p yandi-key-root
echo "=== cargo check --offline (the whole node with the new key handling)"
cargo check --offline
echo "=== mutants K1-K16"
cd "$ROOT"
"$PYTHON" scripts/key_root_mutants.py
echo "ALL KEY ROOT CHECKS PASSED"
