"""
scripts/live_db_fingerprint.py — READ-ONLY fingerprint of the configured YANDI
database: per-table row count and auto-increment counter.

Used to prove that a test run did not write the live owner database
(TEST SUITE MUST NEVER WRITE LIVE OWNER DB): take a fingerprint before, run the
suite, take it again, compare. The auto-increment counter is included because
it also moves when an INSERT was rolled back, which a plain row count misses.

    python scripts/live_db_fingerprint.py save  FILE     # write the fingerprint
    python scripts/live_db_fingerprint.py compare FILE   # exit 1 if it changed

Exit codes: 0 same / saved, 1 changed, 2 database not reachable (the caller
decides whether that is a skip; there is nothing to protect if there is no
database).

The session is READ ONLY and only SELECTs run; this tool cannot change the
database it is fingerprinting. It is not a test process, so it may open the
real connection.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.db.sql.connection import SqlUnavailable, get_connection  # noqa: E402


def _val(row, key, idx):
    return row[key] if isinstance(row, dict) else row[idx]


def fingerprint() -> dict:
    with get_connection(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("SET SESSION information_schema_stats_expiry = 0")
            cur.execute(
                "SELECT table_name AS t FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' ORDER BY table_name"
            )
            names = [str(_val(r, "t", 0)) for r in cur.fetchall()]
            counts = {}
            for name in names:               # count FIRST: opening a table is what makes its auto-increment readable
                cur.execute(f"SELECT COUNT(*) AS n FROM `{name}`")
                counts[name] = int(_val(cur.fetchone(), "n", 0))
            cur.execute(
                "SELECT table_name AS t, auto_increment AS a FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' ORDER BY table_name"
            )
            meta = {str(_val(r, "t", 0)): _val(r, "a", 1) for r in cur.fetchall()}
            out = {name: {"rows": counts[name], "auto_increment": None if meta.get(name) is None else int(meta[name])}
                   for name in names}
            return out


def diff(before: dict, now: dict) -> dict:
    """{table: (before, after)} for every table whose row count or auto-increment
    counter differs (a table that appeared or vanished counts as changed)."""
    return {
        t: (before.get(t), now.get(t))
        for t in sorted(set(before) | set(now))
        if before.get(t) != now.get(t)
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("save", "compare"):
        print(__doc__)
        return 2
    try:
        now = fingerprint()
    except SqlUnavailable as e:
        print(f"live-db-fingerprint: database not reachable ({e}); nothing to compare")
        return 2
    path = Path(argv[2])
    if argv[1] == "save":
        path.write_text(json.dumps(now, sort_keys=True, indent=1))
        print(f"live-db-fingerprint: saved {len(now)} tables, {sum(t['rows'] for t in now.values())} rows")
        return 0
    before = json.loads(path.read_text())
    changed = diff(before, now)
    if changed:
        print(f"live-db-fingerprint: LIVE DATABASE CHANGED during the run ({len(changed)} table(s)):")
        for t, (b, a) in changed.items():
            print(f"  {t}: {b} -> {a}")
        return 1
    print(f"live-db-fingerprint: live database unchanged ({len(now)} tables, {sum(t['rows'] for t in now.values())} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
