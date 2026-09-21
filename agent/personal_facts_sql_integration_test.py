"""
agent/personal_facts_sql_integration_test.py — the PERSONAL FACT LEDGER (schema
v17: personal_fact, personal_fact_event) on a REAL SQL engine (a private,
temporary MySQL instance).

Run through scripts/test-sql-temp.sh (needs a mysqld binary; the live database is
never contacted). Without the environment it prints SKIP and exits 0.

What only a real engine can prove: the v16 -> v17 migration is additive and keeps
rows; a fact cannot exist without the interaction_turn it was said in (foreign key)
and a fact event cannot outlive its fact; the least-privilege runtime role can
append and read but never rewrite or delete; facts are written in the SAME
transaction as the turn (a fault after the fact insert rolls the whole turn back);
a correction appends and leaves the old row byte-for-byte; retries and 8
concurrent deliveries of one turn apply the facts once; and a fact stated 400
turns ago (far outside the interaction window) is still delivered.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import threading
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


class Boom(Exception):
    pass


def main() -> int:
    socket = os.environ.get("YANDI_TEST_SQL_SOCKET")
    admin, admin_pw = os.environ.get("YANDI_TEST_SQL_ADMIN"), os.environ.get("YANDI_TEST_SQL_ADMIN_PW")
    if not (socket and admin and admin_pw):
        print("SKIP: set YANDI_TEST_SQL_SOCKET / _ADMIN / _ADMIN_PW (see scripts/test-sql-temp.sh)")
        return 0
    from agent.db.sql.connection import LiveDatabaseRefused, assert_connection_allowed
    try:
        assert_connection_allowed(socket)
    except LiveDatabaseRefused as e:
        print(f"REFUSED: {e}")
        return 2

    import pymysql
    import pymysql.cursors
    import llm_gateway
    from llm_gateway.types import SemanticCompletionResult
    from agent.db.sql import migrate, schema, security_grants
    import agent.db.sql.repositories as repo
    import agent.db.sql.shadow_write as sw
    import agent.personal_facts as pf
    import agent.personal_memory as pm
    import pet.chat_local as chat_local
    from pet.extraction_test_support import scripted_llm
    from pet.fact_test_support import scripted_fact_llm, scripted_router

    OWNER = chat_local._RELATIONSHIP_USER_ID

    def connect(user, password, autocommit=False):
        assert_connection_allowed(socket)
        return pymysql.connect(unix_socket=socket, user=user, password=password, database="yandi_epistemic",
                               cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit, charset="utf8mb4")

    root = connect(admin, admin_pw, autocommit=True)

    def scalar(sql, params=()):
        with root.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchone().values())[0]

    # ── v16 -> v17 upgrade is additive ──
    with root.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS personal_fact_event"); cur.execute("DROP TABLE IF EXISTS personal_fact")
        cur.execute("DELETE FROM schema_migrations WHERE version=17")
        cur.execute("INSERT IGNORE INTO schema_migrations (version, description) VALUES (16, 'simulated v16')")
        cur.execute("INSERT IGNORE INTO interaction_turn (user_id, source_turn_id, turn_id_origin, user_text, created_at) "
                    "VALUES ('v16_owner', 'turn-v16-0001', 'client', 'row that existed at schema v16', NOW())")
    os.environ.update({"YANDI_SQL_SOCKET": socket, "YANDI_SQL_USER": admin, "YANDI_SQL_AUTH_MODE": "password", "YANDI_SQL_PASSWORD": admin_pw})
    with contextlib.redirect_stdout(io.StringIO()):
        upgraded = migrate.apply()
    check("U: the migration upgrades v16 -> v17: the two fact tables added, version 17 recorded, the v16 row untouched",
          upgraded and scalar("SELECT MAX(version) FROM schema_migrations") == 17
          and scalar("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yandi_epistemic' AND table_name IN ('personal_fact','personal_fact_event')") == 2
          and scalar("SELECT user_text FROM interaction_turn WHERE source_turn_id='turn-v16-0001'") == "row that existed at schema v16")
    check("S: both fact tables are classified append-only (class B)",
          schema.TABLE_CLASSIFICATION["personal_fact"] == "B" and schema.TABLE_CLASSIFICATION["personal_fact_event"] == "B")

    with root.cursor() as cur:
        cur.execute("DROP USER IF EXISTS 'yandi_rt_facts'@'localhost'")
        cur.execute("CREATE USER 'yandi_rt_facts'@'localhost' IDENTIFIED BY 'rt-temp-only'")
        for sql, params in security_grants.yandi_runtime_grant_statements("yandi_rt_facts", "localhost"):
            cur.execute(sql, params)
        cur.execute("FLUSH PRIVILEGES")
    rt = connect("yandi_rt_facts", "rt-temp-only")
    os.environ.update({"YANDI_SQL_USER": "yandi_rt_facts", "YANDI_SQL_PASSWORD": "rt-temp-only"})

    def rt_exec(sql, params=()):
        try:
            with rt.cursor() as cur:
                cur.execute(sql, params)
            rt.commit()
            return True
        except Exception:
            rt.rollback()
            return False

    # ── the foreign keys: a fact cannot exist without its source turn ──
    check("FK: a fact whose source turn does not exist is REJECTED (a fact cannot exist independently of the immutable turn)",
          not rt_exec("INSERT INTO personal_fact (fact_id, user_id, fact_class, statement, polarity, temporality, evidence, source_turn_id, created_at) "
                      "VALUES ('pf_orphan', 'fk_person', 'possession', 'statement text', 'affirmed', 'current', 'ev', 'turn-none-0001', NOW())"))
    rt_exec("INSERT INTO interaction_turn (user_id, source_turn_id, turn_id_origin, user_text, created_at) VALUES ('fk_person','turn-fk-000001','client','text',NOW())")
    check("FK: with the turn present the same fact is accepted",
          rt_exec("INSERT INTO personal_fact (fact_id, user_id, fact_class, statement, polarity, temporality, evidence, source_turn_id, created_at) "
                  "VALUES ('pf_ok', 'fk_person', 'possession', 'statement text', 'affirmed', 'current', 'ev', 'turn-fk-000001', NOW())"))
    check("FK: a fact of ANOTHER person cannot borrow this person's turn",
          not rt_exec("INSERT INTO personal_fact (fact_id, user_id, fact_class, statement, polarity, temporality, evidence, source_turn_id, created_at) "
                      "VALUES ('pf_thief', 'other_person', 'possession', 'statement text', 'affirmed', 'current', 'ev', 'turn-fk-000001', NOW())"))
    check("FK: a fact event must reference an existing fact",
          not rt_exec("INSERT INTO personal_fact_event (fact_id, user_id, event_type, evidence, source_turn_id, created_at) "
                      "VALUES ('pf_missing', 'fk_person', 'restated', 'ev', 'turn-fk-000001', NOW())"))
    check("FK: and, with the fact present, is accepted",
          rt_exec("INSERT INTO personal_fact_event (fact_id, user_id, event_type, evidence, source_turn_id, created_at) "
                  "VALUES ('pf_ok', 'fk_person', 'restated', 'ev', 'turn-fk-000001', NOW())"))
    denied = {label: not rt_exec(sql) for label, sql in (
        ("UPDATE fact", "UPDATE personal_fact SET statement='rewritten' WHERE fact_id='pf_ok'"),
        ("DELETE fact", "DELETE FROM personal_fact WHERE fact_id='pf_ok'"),
        ("UPDATE event", "UPDATE personal_fact_event SET evidence='rewritten'"),
        ("DELETE event", "DELETE FROM personal_fact_event"),
        ("ALTER", "ALTER TABLE personal_fact ADD COLUMN x INT"),
        ("DROP", "DROP TABLE personal_fact"))}
    check("A: the runtime role appends and reads but can neither rewrite, delete, alter nor drop a fact or its history", all(denied.values()), repr(denied))
    check("A: ... and the row is exactly as written", scalar("SELECT statement FROM personal_fact WHERE fact_id='pf_ok'") == "statement text")

    # ── harness: the real chat path over the real engine ──
    TABLES = ("personal_fact_event", "personal_fact", "interaction_turn", "causal_event", "grievance", "forgiveness_capacity",
              "inner_state_event", "inner_state")

    def reset():
        with root.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for t in TABLES:
                cur.execute(f"TRUNCATE TABLE {t}")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")

    def snapshot():
        with root.cursor() as cur:
            cur.execute("SELECT fact_id, statement, polarity, temporality, evidence, source_turn_id FROM personal_fact ORDER BY created_at, fact_id")
            facts = [tuple(r.values()) for r in cur.fetchall()]
            cur.execute("SELECT fact_id, event_type, by_fact_id, source_turn_id FROM personal_fact_event ORDER BY event_id")
            events = [tuple(r.values()) for r in cur.fetchall()]
        return {"interaction": scalar("SELECT COUNT(*) FROM interaction_turn"), "facts": facts, "events": events,
                "claims": scalar("SELECT COUNT(*) FROM causal_event WHERE event_type='personal_facts'")}

    trace: list = []
    lock = threading.Lock()
    real_get_connection = sw.get_connection

    class Spy:
        def __init__(self, c):
            self._c = c

        def __getattr__(self, name):
            return getattr(self._c, name)

        def commit(self):
            trace.append("commit")
            return self._c.commit()

        def rollback(self):
            trace.append("rollback")
            return self._c.rollback()

    @contextlib.contextmanager
    def spying(autocommit=False):
        with real_get_connection(autocommit=autocommit) as c:
            yield Spy(c)

    def turn(text, turn_id, facts=(), memory_query="none", reply="Хорошо."):
        seen: dict = {}

        def fake_semantic(**kwargs):
            seen.update(kwargs)
            return SemanticCompletionResult(reply=reply, state=None, reply_ok=True, state_ok=False, parse_ok=True, error=None, metadata={})
        router = scripted_router(scripted_llm([]), scripted_fact_llm(facts, memory_query=memory_query))
        with patch.object(sw, "get_connection", spying), patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm", lambda model: router), patch.object(llm_gateway, "complete_semantic", fake_semantic):
            out = chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": text}], 0.7, turn_id)
        return out, " ".join(str(x) for x in (seen.get("system") or []) if x)

    DOG = {"quote": "у меня есть собака по кличке Рекс", "cls": "possession", "statement": "У пользователя есть собака по кличке Рекс"}
    MSG = "Знаешь, у меня есть собака по кличке Рекс."

    # ── the turn's facts share its transaction ──
    reset()
    del trace[:]
    turn(MSG, "turn-pf-000001", [DOG])
    s = snapshot()
    check("T: an identified turn with a fact -> one interaction, one fact pointing at it, one per-turn claim",
          s["interaction"] == 1 and len(s["facts"]) == 1 and s["facts"][0][5] == "turn-pf-000001" and s["claims"] == 1, repr(s))
    check("T: the turn's whole persistence phase (reads aside) is a single commit with no rollback: ...",
          trace.count("rollback") == 0 and trace.count("commit") >= 1)

    def fault_case(patch_ctx, turn_id):
        reset()
        b = snapshot()
        with patch_ctx:
            turn(MSG, turn_id, [DOG])
        return b, snapshot()
    real_insert = repo.insert_personal_fact

    def raising_after_fact(*a, **k):
        real_insert(*a, **k)
        raise Boom("after the fact insert")
    b, a = fault_case(patch.object(repo, "insert_personal_fact", raising_after_fact), "turn-pf-f1-0001")
    check("F: a fault right AFTER the fact row is inserted -> the WHOLE turn is rolled back: no fact, no claim, and no interaction row", a == b and a["interaction"] == 0, repr(a))
    def raising_before_facts(*a, **k):
        raise Boom("before the facts")
    b, a = fault_case(patch.object(pf, "record_turn_facts", raising_before_facts), "turn-pf-f2-0001")
    check("F: a fault after the interaction insert but before the facts -> nothing persisted (the turn record does not survive alone)", a == b and a["interaction"] == 0, repr(a))
    reset()
    with patch.object(pf, "record_turn_facts", raising_before_facts):
        turn(MSG, "turn-pf-rb-00001", [DOG])
    turn(MSG, "turn-pf-rb-00001", [DOG])          # the client retries
    s = snapshot()
    check("F: after a rollback, the retry with the same turn id applies the whole turn normally", s["interaction"] == 1 and len(s["facts"]) == 1 and s["claims"] == 1)

    # ── retry / same text ──
    reset()
    turn(MSG, "turn-pf-r-000001", [DOG])
    once = snapshot()
    turn(MSG, "turn-pf-r-000001", [DOG]); turn(MSG, "turn-pf-r-000001", [DOG])
    check("R: SAME TURN retried twice after a committed-but-undelivered reply -> nothing changes", snapshot() == once)
    turn("Повторю: у меня есть собака по кличке Рекс.", "turn-pf-r-000002", [DOG])
    s = snapshot()
    check("R: the same proposition in ANOTHER turn -> still one fact, plus a 'restated' event whose provenance is the new turn",
          len(s["facts"]) == 1 and [(e[1], e[3]) for e in s["events"]] == [("restated", "turn-pf-r-000002")])

    # ── correction on the real engine ──
    reset()
    turn("Мою собаку зовут Рекс.", "turn-pf-c-000001", [{"quote": "Мою собаку зовут Рекс", "cls": "possession", "statement": "Собаку пользователя зовут Рекс"}])
    before = snapshot()
    turn("Нет, я ошибся, её зовут Макс.", "turn-pf-c-000002", [{"quote": "её зовут Макс", "cls": "possession", "statement": "Собаку пользователя зовут Макс", "relation": "replaces", "target": 0}])
    after = snapshot()
    check("C: a correction APPENDS: the old fact's row is byte-for-byte unchanged, the new fact and a 'superseded' event (by the new fact, from the correcting turn) are added",
          after["facts"][0] == before["facts"][0] and len(after["facts"]) == 2
          and [(e[1], e[2] == after["facts"][1][0], e[3]) for e in after["events"]] == [("superseded", True, "turn-pf-c-000002")])
    _, ctx = turn(ASK := "Что ты обо мне помнишь?", "turn-pf-c-000003", [], memory_query="general")
    check("C: the profile delivered to the model has Max as current and no trace of the corrected-away Rex", "Макс" in ctx and "Рекс" not in ctx)

    # ── LONG GAP on the real engine: 400 turns later, beyond the interaction window ──
    reset()
    turn(MSG, "turn-pf-g-000001", [DOG])
    with root.cursor() as cur:
        cur.executemany("INSERT INTO interaction_turn (user_id, source_turn_id, turn_id_origin, user_text, assistant_text, created_at) "
                        "VALUES (%s,%s,'client',%s,'ок', DATE_ADD(NOW(), INTERVAL %s MINUTE))",
                        [(OWNER, f"turn-pf-n{i:06d}", f"нейтральное сообщение номер {i} про погоду", i + 1) for i in range(400)])
    inter = pm.recall(root, OWNER, ASK)
    _, ctx = turn(ASK, "turn-pf-g-000002", [], memory_query="general")
    check("G: LONG GAP — 400 unrelated turns later the source turn is outside the interaction window (interaction recall cannot reach it) ...",
          not any("Рекс" in m["user_text"] for m in inter))
    check("G: ... yet the generic question is delivered the fact from the real engine", DOG["statement"] in ctx)

    # ── concurrency: 8 deliveries of one turn ──
    reset()
    errors: list = []

    def deliver():
        try:
            turn(MSG, "turn-pf-cc-000001", [DOG])
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = snapshot()
    check("K: 8 concurrent deliveries of ONE turn -> 1 interaction, 1 fact, 1 claim, no events, no unhandled error",
          s["interaction"] == 1 and len(s["facts"]) == 1 and s["claims"] == 1 and s["events"] == [] and not errors, repr((s, errors)))

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
