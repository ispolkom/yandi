#!/usr/bin/env bash
# Installs the YANDI Council Bridge into Firefox ESR (enterprise policy, merged into any existing policies.json).
# Usage:  sudo pet/install_extension.sh [--dry-run | --uninstall]      (see: python3 scripts/install_extension.py --help)
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/install_extension.py" "$@"
