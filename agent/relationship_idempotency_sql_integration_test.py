"""
agent/relationship_idempotency_sql_integration_test.py — the idempotency ledger
and schema v15 against a REAL SQL engine (a private, temporary MySQL instance).

Not part of scripts/test-core.sh (it needs a mysqld binary): run it through
scripts/test-sql-temp.sh, which starts a throw-away instance in a temporary
directory (own socket, no network, own datadir; the project's live database is
never contacted), applies the project's own migration, runs this file, and
removes everything. Without the environment below it prints SKIP and exits 0.

    YANDI_TEST_SQL_SOCKET   unix socket of the temporary instance
    YANDI_TEST_SQL_ADMIN    account with DDL/GRANT rights on yandi_epistemic
    YANDI_TEST_SQL_ADMIN_PW its password (a throw-away value)

What only a real engine can prove: the migration is idempotent and leaves
existing rows alone, the UNIQUE key serialises truly concurrent deliveries of
the same turn, a rolled-back transaction leaves no claim behind, and the
least-privilege runtime role (SELECT/INSERT + narrow UPDATE grants, no DDL) is
enough for the whole flow while causal_event stays append-only for it.
"""
from __future__ import annotations

import os
import sys
import threading

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    socket = os.environ.get("YANDI_TEST_SQL_SOCKET")
    admin, admin_pw = os.environ.get("YANDI_TEST_SQL_ADMIN"), os.environ.get("YANDI_TEST_SQL_ADMIN_PW")
    if not (socket and admin and admin_pw):
        print("SKIP: set YANDI_TEST_SQL_SOCKET / _ADMIN / _ADMIN_PW (see scripts/test-sql-temp.sh)")
        return 0
    from agent.db.sql.connection import LiveDatabaseRefused, assert_connection_allowed
    try:
        assert_connection_allowed(socket)   # the ONE isolation check (realpath, temp dir, declared marker)
    except LiveDatabaseRefused as e:
        print(f"REFUSED: {e}")
        return 2

    import pymysql
    import pymysql.cursors
    from agent.db.sql import migrate, schema, security_grants
    import agent.causal_events as ce
    import agent.db.sql.repositories as repo
    import agent.relationship_commitments as rc
    import agent.relationship_memory as rm
    import agent.relationship_state as rs

    def connect(user, password, autocommit=False):
        assert_connection_allowed(socket)
        return pymysql.connect(unix_socket=socket, user=user, password=password, database="yandi_epistemic",
                               cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit, charset="utf8mb4")

    def scalar(conn, sql, params=()):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchone().values())[0]

    root = connect(admin, admin_pw, autocommit=True)

    # ── schema (v15 added these tables; v16 added interaction_turn on top) ──
    check("S: the current schema version is recorded", scalar(root, "SELECT MAX(version) FROM schema_migrations") == schema.SCHEMA_VERSION == 16)
    for table in ("causal_event", "commitment", "commitment_event"):
        check(f"S: table {table} exists", scalar(root, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yandi_epistemic' AND table_name=%s", (table,)) == 1)
    with root.cursor() as cur:
        cur.execute("SELECT column_name, collation_name FROM information_schema.columns WHERE table_schema='yandi_epistemic' AND table_name='causal_event' AND column_name IN ('source_turn_id','event_type')")
        collations = {r["column_name"] if "column_name" in r else r["COLUMN_NAME"]: (r.get("collation_name") or r.get("COLLATION_NAME")) for r in cur.fetchall()}
    check("S: the turn identity is compared exactly (binary collation), not case- or accent-insensitively", set(collations.values()) == {"ascii_bin"}, repr(collations))

    # ── upgrading a v14 database: drop the three v15 tables and the v15 version row, keep data, migrate ──
    with root.cursor() as cur:
        cur.execute("INSERT INTO grievance (id, user_id, event_type, description, severity, status, created_at, updated_at) "
                    "VALUES ('g_v14_row', 'v14_owner', 'insult', 'row that existed at schema v14', 0.5, 'registered', NOW(), NOW())")
        cur.execute("DROP TABLE interaction_turn")
        cur.execute("DROP TABLE causal_event"); cur.execute("DROP TABLE commitment_event"); cur.execute("DROP TABLE commitment")
        cur.execute("DELETE FROM schema_migrations WHERE version IN (15, 16)")
        cur.execute("INSERT IGNORE INTO schema_migrations (version, description) VALUES (14, 'simulated v14')")
    check("U: the simulated v14 database has schema version 14 and no v15 tables",
          scalar(root, "SELECT MAX(version) FROM schema_migrations") == 14
          and scalar(root, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yandi_epistemic' AND table_name IN ('causal_event','commitment','commitment_event')") == 0)
    os.environ.update({"YANDI_SQL_SOCKET": socket, "YANDI_SQL_USER": admin, "YANDI_SQL_AUTH_MODE": "password", "YANDI_SQL_PASSWORD": admin_pw})
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        upgraded = migrate.apply()
    check("U: the migration upgrades v14 -> current: the v15 tables and interaction_turn added, version recorded, the v14 row untouched",
          upgraded and scalar(root, "SELECT MAX(version) FROM schema_migrations") == schema.SCHEMA_VERSION
          and scalar(root, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yandi_epistemic' AND table_name IN ('causal_event','commitment','commitment_event')") == 3
          and scalar(root, "SELECT description FROM grievance WHERE id='g_v14_row'") == "row that existed at schema v14")

    # ── the migration is re-runnable and touches nothing that exists ──
    with root.cursor() as cur:
        cur.execute("INSERT INTO grievance (id, user_id, event_type, description, severity, status, created_at, updated_at) "
                    "VALUES ('g_preexisting', 'legacy_owner', 'insult', 'pre-migration row', 0.5, 'registered', NOW(), NOW())")
    before = (scalar(root, "SELECT COUNT(*) FROM grievance"), scalar(root, "SELECT COUNT(*) FROM schema_migrations"),
              scalar(root, "SELECT description FROM grievance WHERE id='g_preexisting'"))
    with contextlib.redirect_stdout(io.StringIO()):
        ok_again = migrate.apply()
    after = (scalar(root, "SELECT COUNT(*) FROM grievance"), scalar(root, "SELECT COUNT(*) FROM schema_migrations"),
             scalar(root, "SELECT description FROM grievance WHERE id='g_preexisting'"))
    check("S: re-running the migration succeeds, adds no version row and leaves existing rows untouched", ok_again and before == after, repr((before, after)))
    ddl = " ".join(d for _, d in schema.ALL_TABLES_IN_ORDER).upper() + " ".join(d for _, d in schema.ALTER_STATEMENTS_IN_ORDER).upper()
    check("S: the v15 tables are pure additions (no DROP / TRUNCATE / DELETE anywhere in the schema DDL)",
          all(w not in ddl for w in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM")))

    # ── the least-privilege runtime role ──
    with root.cursor() as cur:
        cur.execute("DROP USER IF EXISTS 'yandi_rt'@'localhost'")
        cur.execute("CREATE USER 'yandi_rt'@'localhost' IDENTIFIED BY 'rt-temp-only'")
        for sql, params in security_grants.yandi_runtime_grant_statements("yandi_rt", "localhost"):
            cur.execute(sql, params)
        cur.execute("FLUSH PRIVILEGES")
    rt = connect("yandi_rt", "rt-temp-only")

    def run(fn):
        try:
            result = fn(rt)
            rt.commit()
            return result
        except Exception:
            rt.rollback()
            raise

    U = "sql_person"
    # ── insult retry / same words in another turn, on the real engine ──
    a = run(lambda c: rm.add_grievance(c, U, "insult", "Ты просто ржавая консерва, от тебя толку", 0.6, source_turn_id="turn-SQL-0001", span=(4, 20)))
    b = run(lambda c: rm.add_grievance(c, U, "insult", "Ты просто ржавая консерва, от тебя толку", 0.6, source_turn_id="turn-SQL-0001", span=(4, 20)))
    check("R: INSULT RETRY on the runtime role -> one application (grievance, capacity, audit, ledger row)",
          a is not None and b is None
          and scalar(root, "SELECT COUNT(*) FROM grievance WHERE user_id=%s", (U,)) == 1
          and scalar(root, "SELECT COUNT(*) FROM inner_state_event WHERE user_id=%s AND event_type='insult'", (U,)) == 1
          and scalar(root, "SELECT COUNT(*) FROM causal_event WHERE user_id=%s", (U,)) == 1
          and abs(scalar(root, "SELECT capacity FROM forgiveness_capacity WHERE user_id=%s", (U,)) - 44.0) < 1e-6)
    upper = run(lambda c: rm.add_grievance(c, U, "insult", "Ты просто ржавая консерва, от тебя толку", 0.6, source_turn_id="TURN-SQL-0001"))
    check("R: turn ids are exact: 'TURN-SQL-0001' is not 'turn-SQL-0001' (a different delivery, a real event)", upper is not None)
    again = run(lambda c: rm.add_grievance(c, U, "insult", "Ты просто ржавая консерва, от тебя толку", 0.6, source_turn_id="turn-SQL-0002"))
    check("R: SAME WORDS in another turn -> a legitimate second event (recurrence)", again is not None
          and scalar(root, "SELECT COUNT(*) FROM inner_state_event WHERE user_id=%s AND event_type='insult'", (U,)) == 3)

    # ── restart: brand-new connections, same guarantee ──
    rt.close(); root2 = connect(admin, admin_pw, autocommit=True); rt = connect("yandi_rt", "rt-temp-only")
    n_before = scalar(root2, "SELECT COUNT(*) FROM inner_state_event WHERE user_id=%s", (U,))
    dup = run(lambda c: rm.add_grievance(c, U, "insult", "Ты просто ржавая консерва, от тебя толку", 0.6, source_turn_id="turn-SQL-0002"))
    check("H: RESTART (new connections, new objects) -> the same turn is still a no-op", dup is None
          and scalar(root2, "SELECT COUNT(*) FROM inner_state_event WHERE user_id=%s", (U,)) == n_before)

    # ── rollback leaves no claim behind ──
    try:
        def boom(c):
            assert ce.claim(c, U, "turn-ROLL-0001", "insult") == ce.NEW
            raise RuntimeError("the surrounding write failed")
        run(boom)
    except RuntimeError:
        pass
    check("T: a rolled-back transaction leaves NO claim: the event can still be applied later",
          scalar(root2, "SELECT COUNT(*) FROM causal_event WHERE source_turn_id='turn-ROLL-0001'") == 0
          and run(lambda c: rm.add_grievance(c, U, "insult", "другая обида совсем", 0.5, source_turn_id="turn-ROLL-0001")) is not None)

    # ── true concurrency ──
    results, n = [], 8
    barrier = threading.Barrier(n)

    def worker():
        conn = connect("yandi_rt", "rt-temp-only")
        barrier.wait()
        try:
            r = rm.add_grievance(conn, "race_person", "insult", "гонка за одно и то же сообщение", 0.6, source_turn_id="turn-RACE-SQL-1")
            conn.commit()
            results.append(r)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            results.append(("ERR", repr(exc)))
        finally:
            conn.close()
    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]; [t.join() for t in threads]
    applied = [r for r in results if r is not None and not (isinstance(r, tuple) and r and r[0] == "ERR")]
    errors = [r for r in results if isinstance(r, tuple) and r and r[0] == "ERR"]
    check("I: 8 connections deliver the SAME turn at the same instant -> exactly one application (no deadlock, no duplicate)",
          len(applied) == 1 and not errors
          and scalar(root2, "SELECT COUNT(*) FROM inner_state_event WHERE user_id='race_person'") == 1
          and scalar(root2, "SELECT COUNT(*) FROM grievance WHERE user_id='race_person'") == 1, repr((len(applied), errors[:1])))

    # ── apology / promise / claim on the runtime role ──
    gid = run(lambda c: rm.add_grievance(c, "apo_person", "insult", "Ты сломал мой велосипед", 0.6, source_turn_id="turn-AP-INS-01"))
    first = run(lambda c: rm.apply_apology(c, "apo_person", gid, 0.9, source_turn_id="turn-AP-0001", span=(0, 6)))
    second = run(lambda c: rm.apply_apology(c, "apo_person", gid, 0.9, source_turn_id="turn-AP-0001", span=(0, 6)))
    check("C: APOLOGY RETRY -> restoration once (one apology_accepted audit row)",
          first["acknowledged"] and second["target"] is None
          and scalar(root2, "SELECT COUNT(*) FROM inner_state_event WHERE user_id='apo_person' AND event_type='apology_accepted'") == 1)
    p1 = run(lambda c: rc.create_commitment(c, "pr_person", "Я пришлю отчёт", "Я пришлю", source_turn_id="turn-PR-0001"))
    p2 = run(lambda c: rc.create_commitment(c, "pr_person", "Я пришлю отчёт", "Я пришлю", source_turn_id="turn-PR-0001"))
    p3 = run(lambda c: rc.create_commitment(c, "pr_person", "Я пришлю отчёт", "Я пришлю", source_turn_id="turn-PR-0002"))
    check("D: PROMISE RETRY -> one commitment; the same words in another turn -> a second one",
          p1["created"] and not p2["created"] and p3["created"]
          and scalar(root2, "SELECT COUNT(*) FROM commitment WHERE user_id='pr_person'") == 2)
    c1 = run(lambda c: rc.record_fulfillment_claim(c, "pr_person", p1["commitment_id"], "Я отправил", source_turn_id="turn-CL-0001"))
    c2 = run(lambda c: rc.record_fulfillment_claim(c, "pr_person", p1["commitment_id"], "Я отправил", source_turn_id="turn-CL-0001"))
    check("E: FULFILMENT-REPORT RETRY -> one report event, and no coordinate moved",
          c1["recorded"] and not c2["recorded"]
          and scalar(root2, "SELECT COUNT(*) FROM commitment_event WHERE user_id='pr_person'") == 1
          and scalar(root2, "SELECT COUNT(*) FROM inner_state_event WHERE user_id='pr_person'") == 0)
    v = run(lambda c: rc.record_verification(c, "pr_person", p1["commitment_id"], True, source="independent_check"))
    v2 = run(lambda c: rc.record_verification(c, "pr_person", p1["commitment_id"], True, source="independent_check"))
    check("V: a VERIFIED outcome moves trust exactly once on the real engine (commitment_event UNIQUE key)",
          v["state_changed"] and not v2["recorded"]
          and scalar(root2, "SELECT COUNT(*) FROM inner_state_event WHERE user_id='pr_person' AND event_type='commitment_kept'") == 1
          and abs(rs.get_state(rt, "pr_person")["trust"] - 58.0) < 1e-6)

    # ── the ledger is append-only for the runtime role ──
    denied = {}
    for label, sql in (("UPDATE", "UPDATE causal_event SET event_type='x' WHERE user_id='race_person'"),
                       ("DELETE", "DELETE FROM causal_event WHERE user_id='race_person'"),
                       ("UPDATE commitment_event", "UPDATE commitment_event SET source='x'"),
                       ("DDL", "ALTER TABLE causal_event ADD COLUMN x INT")):
        try:
            with rt.cursor() as cur:
                cur.execute(sql)
            rt.commit()
            denied[label] = False
        except Exception:  # noqa: BLE001
            rt.rollback()
            denied[label] = True
    check("A: the runtime role can neither UPDATE nor DELETE the ledger rows, nor change the schema (history is append-only)",
          all(denied.values()), repr(denied))
    check("A: ...and existing pre-migration data was never touched", scalar(root2, "SELECT description FROM grievance WHERE id='g_preexisting'") == "pre-migration row")

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
