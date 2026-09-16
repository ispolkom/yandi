"""
One-time helper: apply the already-designed yandi_runtime UPDATE grants
(security_grants.py's own yandi_runtime_grant_statements()) using the
exact same connection path that agent/db/sql/migrate.py already used
successfully — sidesteps the `mysql` CLI's local socket/auth_socket
vs. TCP/password ambiguity seen when running this by hand.

Run exactly like migrate.py:
    sudo YANDI_SQL_USER=root YANDI_SQL_PASSWORD='...' \
        /home/iam/venv/bin/python3 -m agent.db.sql.apply_runtime_grants
"""
from __future__ import annotations

from agent.db.sql.connection import get_connection, is_configured
from agent.db.sql.security_grants import yandi_runtime_grant_statements


def main() -> int:
    if not is_configured():
        print("NOT CONFIGURED: set YANDI_SQL_USER and YANDI_SQL_PASSWORD first.")
        return 1

    stmts = yandi_runtime_grant_statements("yandi_runtime", "localhost")
    with get_connection(autocommit=True) as conn:
        with conn.cursor() as cur:
            for sql, params in stmts:
                cur.execute(sql, params)
                print(f"OK   {sql % (repr(params[0]), repr(params[1]))}")
            cur.execute("FLUSH PRIVILEGES")
            print("OK   FLUSH PRIVILEGES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
