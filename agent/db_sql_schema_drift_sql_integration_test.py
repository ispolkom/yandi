"""
agent/db_sql_schema_drift_sql_integration_test.py — schema drift on a REAL engine: a table created before a column was added to
its CREATE TABLE really does miss that column, agent/db/sql/schema_drift.py really finds it, and `migrate.py`'s ALTER really
closes the gap without touching the table's existing rows.

Run through scripts/test-sql-temp.sh (needs a mysqld binary; the live database is never contacted). Without the environment it
prints SKIP and exits 0.

Run: python -m agent.db_sql_schema_drift_sql_integration_test
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

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
    from agent.db.sql.connection import LiveDatabaseRefused, assert_connection_allowed, pymysql_target
    try:
        assert_connection_allowed(socket)
    except LiveDatabaseRefused as e:
        print(f"REFUSED: {e}")
        return 2

    import pymysql
    import pymysql.cursors
    from agent.db.sql import migrate, schema, schema_drift

    def connect(user, password, autocommit=True):
        assert_connection_allowed(socket)
        return pymysql.connect(**pymysql_target(socket), user=user, password=password, database="yandi_epistemic",
                               cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit, charset="utf8mb4")

    root = connect(admin, admin_pw)

    def run(sql, params=()):
        with root.cursor() as cur:
            cur.execute(sql, params)

    def scalar(sql, params=()):
        with root.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchone().values())[0]

    os.environ.update({"YANDI_SQL_SOCKET": socket, "YANDI_SQL_USER": admin, "YANDI_SQL_AUTH_MODE": "password", "YANDI_SQL_PASSWORD": admin_pw})
    with contextlib.redirect_stdout(io.StringIO()):
        migrate.apply()

    check("N0: on an up-to-date database there is no drift at all", schema_drift.find_drift(root) == {})

    # ── recreate the exact scenario that broke live: semantic_edge exists WITHOUT triggering_claim_ids ──
    run("SET FOREIGN_KEY_CHECKS=0")
    run("DROP TABLE IF EXISTS semantic_edge")
    run("SET FOREIGN_KEY_CHECKS=1")
    run("""
        CREATE TABLE semantic_edge (
            edge_id             VARCHAR(20) PRIMARY KEY,
            family_a            VARCHAR(20) NOT NULL,
            family_b            VARCHAR(20) NOT NULL,
            edge_type            ENUM('contradicts','supports','depends_on') NOT NULL,
            reason                VARCHAR(120) NULL,
            observation_count    INT NOT NULL DEFAULT 1,
            created_at           DATETIME NOT NULL,
            last_seen_at         DATETIME NOT NULL,
            KEY idx_se_pair (family_a, family_b, edge_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    run("DELETE FROM schema_migrations WHERE version = %s", (schema.SCHEMA_VERSION,))
    run("INSERT INTO claim_family (family_id, domain, canonical_text, created_at, updated_at) VALUES ('fam_a', 'd', 'a', NOW(), NOW()), ('fam_b', 'd', 'b', NOW(), NOW())")
    run("INSERT INTO semantic_edge (edge_id, family_a, family_b, edge_type, reason, created_at, last_seen_at) "
        "VALUES ('edg_x', 'fam_a', 'fam_b', 'contradicts', 'seed', NOW(), NOW())")

    check("N1: semantic_edge genuinely lacks the column now (reproduces the live crash)",
          scalar("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='semantic_edge' "
                "AND column_name='triggering_claim_ids'") == 0)

    drift = schema_drift.find_drift(root)
    check("N2: schema_drift finds exactly this gap (read-only: it changed nothing)", drift.get("semantic_edge") == ["triggering_claim_ids"])
    check("N3: …and nothing else changed while it looked", scalar("SELECT COUNT(*) FROM semantic_edge") == 1
          and scalar("SELECT edge_id FROM semantic_edge") == "edg_x")

    # ── the exact write that crashed the live orchestrator: it must fail BEFORE the migration and succeed AFTER ──
    def try_upsert():
        import agent.db.sql.repositories as repo
        conn2 = connect(admin, admin_pw, autocommit=False)
        try:
            repo.upsert_semantic_edge(conn2, "edg_y", "fam_a", "fam_b", "supports", "test", triggering_claim_ids=["cl_1", "cl_2"])
            conn2.commit()
            return None
        except Exception as e:  # noqa: BLE001
            conn2.rollback()
            return e
        finally:
            conn2.close()

    before_error = try_upsert()
    check("N4: before the migration, the exact write that broke the live orchestrator fails with the reported error",
          before_error is not None and "triggering_claim_ids" in str(before_error) and "1054" in str(before_error))

    with contextlib.redirect_stdout(io.StringIO()):
        upgraded = migrate.apply()
    check("N5: the migration reports success and the column now exists", upgraded and scalar(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='semantic_edge' "
        "AND column_name='triggering_claim_ids'") == 1)
    check("N6: the row written before the migration is untouched, and its new column is NULL (nothing invented)",
          scalar("SELECT edge_id FROM semantic_edge WHERE edge_id='edg_x'") == "edg_x"
          and scalar("SELECT triggering_claim_ids FROM semantic_edge WHERE edge_id='edg_x'") is None)
    check("N7: schema_drift now reports semantic_edge clean", "semantic_edge" not in schema_drift.find_drift(root))

    after_error = try_upsert()
    check("N8: after the migration, the SAME write now succeeds", after_error is None
          and scalar("SELECT triggering_claim_ids FROM semantic_edge WHERE edge_id='edg_y'") == '["cl_1", "cl_2"]')

    with contextlib.redirect_stdout(io.StringIO()) as buf:
        again = migrate.apply()
    check("N9: running the migration again is a no-op (idempotent) and does not re-touch the row", again
          and "already applied" in buf.getvalue() and scalar("SELECT edge_id FROM semantic_edge WHERE edge_id='edg_x'") == "edg_x")

    # ── belief's three drifted columns, all at once ──
    run("SET FOREIGN_KEY_CHECKS=0")
    run("ALTER TABLE belief DROP COLUMN evidence_for, DROP COLUMN evidence_against, DROP COLUMN claim_ids")
    run("SET FOREIGN_KEY_CHECKS=1")
    run("DELETE FROM schema_migrations WHERE version = %s", (schema.SCHEMA_VERSION,))
    check("N10: schema_drift finds all three, for belief, in the order they are declared", schema_drift.find_drift(root).get("belief") == ["evidence_for", "evidence_against", "claim_ids"])
    with contextlib.redirect_stdout(io.StringIO()):
        migrate.apply()
    check("N11: the migration adds all three and drift is clean again", schema_drift.find_drift(root) == {})

    # ── a table that does not exist at all is NOT reported as drift (that is CREATE TABLE IF NOT EXISTS's own job) ──
    run("DROP TABLE IF EXISTS knowledge_query_archive")
    run("DELETE FROM schema_migrations WHERE version = %s", (schema.SCHEMA_VERSION,))
    check("N12: a table missing entirely is not reported by schema_drift (a different problem, migrate.py's CREATE TABLE handles it)",
          "knowledge_query_archive" not in schema_drift.find_drift(root))
    with contextlib.redirect_stdout(io.StringIO()):
        migrate.apply()
    check("N13: …and the migration still creates it", scalar(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='knowledge_query_archive'") == 1)

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
