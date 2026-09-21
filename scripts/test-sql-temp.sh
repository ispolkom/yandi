#!/usr/bin/env bash
# Runs the SQL integration tests against a PRIVATE, THROW-AWAY MySQL-compatible
# instance: its own datadir and unix socket in a temporary directory, no network
# port, removed on exit. The project's live database is never contacted.
#
#   scripts/test-sql-temp.sh                       (every SQL integration suite)
#   scripts/test-sql-temp.sh agent.some_sql_integration_test   (only the named ones)
#   YANDI_PYTHON=/path/to/python YANDI_TEST_MYSQLD=/usr/sbin/mysqld scripts/test-sql-temp.sh
#
# It applies the project's own schema migration to the temporary instance and
# then runs agent/relationship_idempotency_sql_integration_test.py and
# agent/personal_memory_sql_integration_test.py, agent/turn_atomicity_sql_integration_test.py and
# agent/personal_facts_sql_integration_test.py. Skips (exit 0)
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

# The instance listens on a unix socket in the temporary directory. Where the environment cannot create unix sockets
# at all (a sandbox), it listens on a loopback TCP port chosen here instead, and the tests reach it through the
# "tcp:127.0.0.1:<port>" spelling, which agent/db/sql/connection.py accepts only from a test process, only for the port
# declared below, never for 3306 and never for a non-loopback host.
if "$PYTHON" -c "import socket; socket.socket(socket.AF_UNIX).close()" >/dev/null 2>&1; then
  SOCK="$TMP/mysql.sock"
  LISTEN=(--skip-networking --socket="$SOCK")
  MYSQL_CLIENT=(-S "$SOCK")
else
  PORT="$("$PYTHON" -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()")"
  SOCK="tcp:127.0.0.1:$PORT"
  LISTEN=(--bind-address=127.0.0.1 --port="$PORT" --socket=)
  MYSQL_CLIENT=(-h 127.0.0.1 -P "$PORT" --protocol=TCP)
fi
"$MYSQLD" --no-defaults --initialize-insecure --user="$(id -un)" --datadir="$TMP/data" --basedir=/usr \
  --log-error="$TMP/init.log" >/dev/null 2>&1
"$MYSQLD" --no-defaults --user="$(id -un)" --datadir="$TMP/data" --basedir=/usr "${LISTEN[@]}" \
  --pid-file="$TMP/mysqld.pid" --mysqlx=OFF --log-error="$TMP/err.log" \
  --innodb-buffer-pool-size=64M --performance-schema=OFF >/dev/null 2>&1 &

for _ in $(seq 1 60); do "$MYSQL" --no-defaults -uroot "${MYSQL_CLIENT[@]}" -e "SELECT 1" >/dev/null 2>&1 && break; sleep 1; done
"$MYSQL" --no-defaults -uroot "${MYSQL_CLIENT[@]}" -e \
  "CREATE DATABASE yandi_epistemic; CREATE USER 'tmp_admin'@'localhost' IDENTIFIED BY 'tmp-only-pw'; GRANT ALL ON *.* TO 'tmp_admin'@'localhost' WITH GRANT OPTION;"

export YANDI_SQL_SOCKET="$SOCK" YANDI_SQL_USER=tmp_admin YANDI_SQL_AUTH_MODE=password YANDI_SQL_PASSWORD=tmp-only-pw
# Declares THIS throw-away instance as the only database a test process may open
# (agent/db/sql/connection.py refuses everything else, the live database first).
export YANDI_TEST_MODE=1 YANDI_TEST_ISOLATED_SOCKET="$SOCK"
"$PYTHON" -m agent.db.sql.migrate >/dev/null
export YANDI_TEST_SQL_SOCKET="$SOCK" YANDI_TEST_SQL_ADMIN=tmp_admin YANDI_TEST_SQL_ADMIN_PW=tmp-only-pw
SUITES=("$@")
if [ "${#SUITES[@]}" -eq 0 ]; then
  SUITES=(
    agent.relationship_idempotency_sql_integration_test
    agent.personal_memory_sql_integration_test
    agent.turn_atomicity_sql_integration_test
    agent.personal_facts_sql_integration_test
  )
fi
failed=()
for suite in "${SUITES[@]}"; do
  echo "=== $suite"
  "$PYTHON" -m "$suite" || failed+=("$suite")
done
if [ "${#failed[@]}" -ne 0 ]; then echo "FAILED SQL SUITES: ${failed[*]}"; exit 1; fi
echo "ALL SQL INTEGRATION SUITES PASSED (${#SUITES[@]})"
