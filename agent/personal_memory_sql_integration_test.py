"""
agent/personal_memory_sql_integration_test.py — personal memory (schema v16,
interaction_turn) against a REAL SQL engine (a private, temporary MySQL instance).

Run it through scripts/test-sql-temp.sh (needs a mysqld binary; the project's
live database is never contacted). Without the environment below it prints SKIP
and exits 0.

    YANDI_TEST_SQL_SOCKET / YANDI_TEST_SQL_ADMIN / YANDI_TEST_SQL_ADMIN_PW

What only a real engine can prove: the v15 -> v16 migration is additive and keeps
existing rows; the UNIQUE key makes concurrent deliveries of the same turn one
record while the same words in another turn stay another record; the recall join
with causal_event works on the binary-collated turn id; the least-privilege
runtime role can append and read the history but can neither rewrite nor delete
it; memory written on one connection is read back on a brand-new one (restart)
and is not visible across persons; text round-trips through utf8mb4.
"""
from __future__ import annotations

import contextlib
import io
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
    import agent.personal_memory as pm

    def connect(user, password, autocommit=False):
        assert_connection_allowed(socket)
        return pymysql.connect(unix_socket=socket, user=user, password=password, database="yandi_epistemic",
                               cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit, charset="utf8mb4")

    def scalar(conn, sql, params=()):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchone().values())[0]

    root = connect(admin, admin_pw, autocommit=True)

    # ── v15 -> v16 upgrade keeps everything that exists ──
    with root.cursor() as cur:
        cur.execute("INSERT INTO grievance (id, user_id, event_type, description, severity, status, created_at, updated_at) "
                    "VALUES ('g_v15_row', 'v15_owner', 'insult', 'row that existed at schema v15', 0.5, 'registered', NOW(), NOW())")
        cur.execute("INSERT IGNORE INTO causal_event (user_id, source_turn_id, event_type, created_at) "
                    "VALUES ('v15_owner', 'turn-v15-0001', 'insult', NOW())")
        cur.execute("DROP TABLE IF EXISTS interaction_turn")
        cur.execute("DELETE FROM schema_migrations WHERE version=16")
        cur.execute("INSERT IGNORE INTO schema_migrations (version, description) VALUES (15, 'simulated v15')")
    check("U: the simulated v15 database has no interaction_turn and version 15",
          scalar(root, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yandi_epistemic' AND table_name='interaction_turn'") == 0
          and scalar(root, "SELECT MAX(version) FROM schema_migrations") == 15)
    os.environ.update({"YANDI_SQL_SOCKET": socket, "YANDI_SQL_USER": admin, "YANDI_SQL_AUTH_MODE": "password", "YANDI_SQL_PASSWORD": admin_pw})
    with contextlib.redirect_stdout(io.StringIO()):
        upgraded = migrate.apply()
    check("U: the migration upgrades v15 -> v16: interaction_turn added, version 16 recorded, existing rows untouched",
          upgraded and scalar(root, "SELECT MAX(version) FROM schema_migrations") == 16
          and scalar(root, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yandi_epistemic' AND table_name='interaction_turn'") == 1
          and scalar(root, "SELECT description FROM grievance WHERE id='g_v15_row'") == "row that existed at schema v15"
          and scalar(root, "SELECT COUNT(*) FROM causal_event WHERE user_id='v15_owner'") == 1)
    with root.cursor() as cur:
        cur.execute("SELECT column_name, collation_name FROM information_schema.columns WHERE table_schema='yandi_epistemic' "
                    "AND table_name='interaction_turn' AND column_name='source_turn_id'")
        row = cur.fetchone()
    check("S: the turn identity is compared exactly (binary collation)", (row.get("collation_name") or row.get("COLLATION_NAME")) == "ascii_bin")
    check("S: interaction_turn is classified append-only (class B) and is a pure addition",
          schema.TABLE_CLASSIFICATION["interaction_turn"] == "B")

    # ── the least-privilege runtime role ──
    with root.cursor() as cur:
        cur.execute("DROP USER IF EXISTS 'yandi_rt_mem'@'localhost'")
        cur.execute("CREATE USER 'yandi_rt_mem'@'localhost' IDENTIFIED BY 'rt-temp-only'")
        for sql, params in security_grants.yandi_runtime_grant_statements("yandi_rt_mem", "localhost"):
            cur.execute(sql, params)
        cur.execute("FLUSH PRIVILEGES")
    rt = connect("yandi_rt_mem", "rt-temp-only")

    def run(fn, conn=None):
        conn = conn or rt
        try:
            result = fn(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    P = "sql_person"
    text = "Мой отец тяжело болеет 🌧, я всю неделю езжу к нему в больницу — «очень устала»."

    # ── one turn -> one record ──
    first = run(lambda c: pm.record_turn(c, P, "turn-MEM-0001", text, "Мне жаль, что так тяжело.", model="model-A", adapter="llama_cpp"))
    again = run(lambda c: pm.record_turn(c, P, "turn-MEM-0001", text, "Другой ответ.", model="model-B", adapter="remote"))
    check("R: SAME TURN retried on the runtime role -> one record; the first delivery is kept, nothing is merged",
          first["recorded"] and not again["recorded"]
          and scalar(root, "SELECT COUNT(*) FROM interaction_turn WHERE user_id=%s", (P,)) == 1
          and scalar(root, "SELECT assistant_text FROM interaction_turn WHERE user_id=%s", (P,)) == "Мне жаль, что так тяжело.")
    check("R: utf8mb4 round trip: Cyrillic, quotes, dash and an emoji come back byte-for-byte",
          scalar(root, "SELECT user_text FROM interaction_turn WHERE user_id=%s", (P,)) == text)
    upper = run(lambda c: pm.record_turn(c, P, "TURN-MEM-0001", text, "x"))
    check("R: turn ids are exact: 'TURN-MEM-0001' is not 'turn-MEM-0001'", upper["recorded"])
    other = run(lambda c: pm.record_turn(c, P, "turn-MEM-0002", text, "y"))
    check("R: SAME WORDS in another turn -> another historical record", other["recorded"]
          and scalar(root, "SELECT COUNT(*) FROM interaction_turn WHERE user_id=%s AND user_text=%s", (P, text)) == 3)

    # ── concurrent deliveries of one turn ──
    outcomes = []

    def deliver():
        c = connect("yandi_rt_mem", "rt-temp-only")
        try:
            outcomes.append(run(lambda cc: pm.record_turn(cc, P, "turn-MEM-RACE", "гонка", "ответ"), c)["recorded"])
        finally:
            c.close()
    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("R: 8 truly concurrent deliveries of ONE turn -> exactly one record (the unique key serialises them)",
          outcomes.count(True) == 1 and scalar(root, "SELECT COUNT(*) FROM interaction_turn WHERE source_turn_id='turn-MEM-RACE'") == 1, repr(outcomes))

    # ── text cap ──
    big = "а" * (repo.INTERACTION_TEXT_CAP + 500)
    run(lambda c: pm.record_turn(c, P, "turn-MEM-BIG1", big, big))
    check("R: an oversized message is capped at the documented length, never rejected",
          scalar(root, "SELECT CHAR_LENGTH(user_text) FROM interaction_turn WHERE source_turn_id='turn-MEM-BIG1'") == repo.INTERACTION_TEXT_CAP)

    # ── recalled provenance (JSON) ──
    run(lambda c: pm.record_turn(c, P, "turn-MEM-0003", "Как дела у меня с отцом?", "Расскажи.", recalled_turn_ids=["turn-MEM-0001"]))
    check("R: recalled_turn_ids round-trips as JSON",
          '"turn-MEM-0001"' in str(scalar(root, "SELECT CAST(recalled_turn_ids AS CHAR) FROM interaction_turn WHERE source_turn_id='turn-MEM-0003'")))

    # ── recall on the real engine: a NEW connection (restart), the join with causal_event ──
    run(lambda c: ce.claim(c, P, "turn-MEM-0001", "insult", (0, 4)))
    fresh = connect("yandi_rt_mem", "rt-temp-only")           # a different process would open exactly this
    got = pm.recall(fresh, P, "Что мне делать с больницей и отцом?")
    fresh.commit()
    check("K: RESTART — a brand-new connection recalls the stored turn (relevant), bounded", 0 < len(got) <= pm.MAX_RECALLED
          and any(m["source_turn_id"] == "turn-MEM-0001" for m in got), repr(got)[:300])
    hit = next((m for m in got if m["source_turn_id"] == "turn-MEM-0001"), None)
    check("K: the relationship event confirmed in that same turn is joined through the exact turn id", hit is not None and hit["events"] == ["insult"])
    check("K: the current turn itself (a retry) is never recalled",
          all(m["source_turn_id"] != "turn-MEM-0001" for m in pm.recall(fresh, P, "Что мне делать с больницей и отцом?", current_turn_id="turn-MEM-0001")))
    check("K: PERSON ISOLATION on the real engine — another person recalls nothing", pm.recall(fresh, "someone_else", "Что мне делать с больницей и отцом?") == [])
    fresh.close()

    # ── append-only for the runtime role ──
    denied = {}
    for label, sql in (("UPDATE", "UPDATE interaction_turn SET user_text='rewritten' WHERE user_id=%s"),
                       ("DELETE", "DELETE FROM interaction_turn WHERE user_id=%s"),
                       ("ALTER", "ALTER TABLE interaction_turn ADD COLUMN x INT"),
                       ("DROP", "DROP TABLE interaction_turn")):
        try:
            with rt.cursor() as cur:
                cur.execute(sql, (P,) if "%s" in sql else ())
            rt.commit()
            denied[label] = False
        except Exception:
            rt.rollback()
            denied[label] = True
    check("A: the runtime role can append and read the history but cannot rewrite, delete or alter it (UPDATE/DELETE/ALTER/DROP all denied)",
          all(denied.values()), repr(denied))
    check("A: ... and the history is exactly as written", scalar(root, "SELECT COUNT(*) FROM interaction_turn WHERE user_text='rewritten'") == 0)

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
