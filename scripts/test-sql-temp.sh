#!/usr/bin/env bash
# Runs the SQL integration tests against a PRIVATE, THROW-AWAY MySQL-compatible
# instance: its own datadir and unix socket in a temporary directory, no network
# port, removed on exit. The project's live database is never contacted.
#
#   scripts/test-sql-temp.sh
#   YANDI_PYTHON=/path/to/python YANDI_TEST_MYSQLD=/usr/sbin/mysqld scripts/test-sql-temp.sh
#
# It applies the project's own schema migration to the temporary instance and
# then runs agent/relationship_idempotency_sql_integration_test.py. Skips (exit 0)
# when no mysqld binary is available or the script runs as root.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${YANDI_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$ROOT/.venv/bin/python3" ]; then PYTHON="$ROOT/.venv/bin/python3"
  elif [ -x "$HOME/venv/bin/python3" ]; then PYTHON="$HOME/venv/bin/python3"
  else PYTHON="python3"; fi
fi

MYSQLD="${YANDI_TEST_MYSQLD:-$(command -v mysqld || echo /usr/sbin/mysqld)}"
MYSQL="${YANDI_TEST_MYSQL:-$(command -v mysql || echo /usr/bin/mysql)}"
if [ ! -x "$MYSQLD" ] || [ ! -x "$MYSQL" ]; then echo "SKIP: mysqld/mysql not found"; exit 0; fi
if [ "$(id -u)" = "0" ]; then echo "SKIP: refusing to start a throw-away mysqld as root"; exit 0; fi

TMP="$(mktemp -d)"
cleanup() {
  if [ -f "$TMP/mysqld.pid" ]; then kill "$(cat "$TMP/mysqld.pid")" 2>/dev/null || true; sleep 2; fi
  rm -rf "$TMP"
}
trap cleanup EXIT

SOCK="$TMP/mysql.sock"
"$MYSQLD" --no-defaults --initialize-insecure --user="$(id -un)" --datadir="$TMP/data" --basedir=/usr \
  --log-error="$TMP/init.log" >/dev/null 2>&1
"$MYSQLD" --no-defaults --user="$(id -un)" --datadir="$TMP/data" --basedir=/usr --socket="$SOCK" \
  --skip-networking --pid-file="$TMP/mysqld.pid" --mysqlx=OFF --log-error="$TMP/err.log" \
  --innodb-buffer-pool-size=64M --performance-schema=OFF >/dev/null 2>&1 &

for _ in $(seq 1 60); do [ -S "$SOCK" ] && "$MYSQL" --no-defaults -uroot -S "$SOCK" -e "SELECT 1" >/dev/null 2>&1 && break; sleep 1; done
"$MYSQL" --no-defaults -uroot -S "$SOCK" -e \
  "CREATE DATABASE yandi_epistemic; CREATE USER 'tmp_admin'@'localhost' IDENTIFIED BY 'tmp-only-pw'; GRANT ALL ON *.* TO 'tmp_admin'@'localhost' WITH GRANT OPTION;"

export YANDI_SQL_SOCKET="$SOCK" YANDI_SQL_USER=tmp_admin YANDI_SQL_AUTH_MODE=password YANDI_SQL_PASSWORD=tmp-only-pw
"$PYTHON" -m agent.db.sql.migrate >/dev/null
export YANDI_TEST_SQL_SOCKET="$SOCK" YANDI_TEST_SQL_ADMIN=tmp_admin YANDI_TEST_SQL_ADMIN_PW=tmp-only-pw
"$PYTHON" -m agent.relationship_idempotency_sql_integration_test
