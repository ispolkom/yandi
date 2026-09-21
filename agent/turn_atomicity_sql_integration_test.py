"""
agent/turn_atomicity_sql_integration_test.py — ONE PERSONAL TURN IS ONE SQL
TRANSACTION, on a REAL engine (a private, temporary MySQL instance).

    ONE IDENTIFIED PERSONAL TURN
    -> ONE interaction_turn
    -> ZERO OR MORE validated causal / relationship writes
    -> ONE COMMIT
    PARTIAL CAUSAL HISTORY IS NOT AN ACCEPTABLE NORMAL STATE.
    SAME TURN RETRY != NEW HISTORY.   SAME TEXT != SAME EVENT.
    LLM INFERENCE MUST NOT HOLD AN OPEN SQL TRANSACTION.

Real pet.chat_local + real shadow_write + the real least-privilege runtime role;
only the two model calls are scripted. Faults are injected at exact points of the
persistence phase and the whole database state is compared before/after.

Run through scripts/test-sql-temp.sh (needs a mysqld binary; the live database is
never contacted). Without the environment it prints SKIP and exits 0.
"""
from __future__ import annotations

import contextlib
import logging
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
        assert_connection_allowed(socket)   # the ONE isolation check (realpath, temp dir, declared marker)
    except LiveDatabaseRefused as e:
        print(f"REFUSED: {e}")
        return 2

    import pymysql
    import pymysql.cursors
    import llm_gateway
    from llm_gateway.types import SemanticCompletionResult
    from agent.db.sql import security_grants
    import agent.causal_events as causal_events
    import agent.db.sql.repositories as repo
    import agent.db.sql.shadow_write as sw
    import agent.relationship_memory as rm
    import agent.relationship_state as rs
    import pet.chat_local as chat_local
    from pet.extraction_test_support import scripted_llm

    OWNER = chat_local._RELATIONSHIP_USER_ID

    def connect(user, password, autocommit=False):
        assert_connection_allowed(socket)
        return pymysql.connect(unix_socket=socket, user=user, password=password, database="yandi_epistemic",
                               cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit, charset="utf8mb4")

    root = connect(admin, admin_pw, autocommit=True)
    with root.cursor() as cur:
        cur.execute("DROP USER IF EXISTS 'yandi_rt_atom'@'localhost'")
        cur.execute("CREATE USER 'yandi_rt_atom'@'localhost' IDENTIFIED BY 'rt-temp-only'")
        for sql, params in security_grants.yandi_runtime_grant_statements("yandi_rt_atom", "localhost"):
            cur.execute(sql, params)
        cur.execute("FLUSH PRIVILEGES")
    os.environ.update({"YANDI_SQL_SOCKET": socket, "YANDI_SQL_USER": "yandi_rt_atom",
                       "YANDI_SQL_AUTH_MODE": "password", "YANDI_SQL_PASSWORD": "rt-temp-only"})

    def scalar(sql, params=()):
        with root.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchone().values())[0]

    TABLES = ("personal_fact_event", "personal_fact", "interaction_turn", "causal_event", "grievance", "forgiveness_capacity",
              "inner_state_event", "inner_state", "commitment_event", "commitment")

    def reset_world(with_grievance=True, with_commitment=False):
        with root.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for t in TABLES:
                cur.execute(f"TRUNCATE TABLE {t}")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if with_grievance:
            rm.add_grievance(root, OWNER, "insult", "Ты сломал мой велосипед", 0.4)
        if with_commitment:
            import agent.relationship_commitments as rc
            rc.create_commitment(root, OWNER, "Я пришлю тебе отчёт по проекту", "Я пришлю тебе отчёт")

    def snapshot() -> dict:
        with root.cursor() as cur:
            cur.execute("SELECT description, status, ROUND(severity,2) AS sev FROM grievance ORDER BY description, status")
            grievances = [(r["description"][:24], r["status"], float(r["sev"])) for r in cur.fetchall()]
            cur.execute("SELECT source_turn_id, event_type FROM causal_event ORDER BY source_turn_id, event_type")
            causal = [(r["source_turn_id"], r["event_type"]) for r in cur.fetchall()]
            cur.execute("SELECT capacity FROM forgiveness_capacity")
            cap = [round(float(r["capacity"]), 3) for r in cur.fetchall()]
        st = rs.get_state(root, OWNER)
        return {
            "interaction": scalar("SELECT COUNT(*) FROM interaction_turn"),
            "causal": causal, "grievances": grievances, "capacity": cap,
            "state": tuple(round(st[k], 3) for k in ("trust", "respect", "affection")),
            "state_events": scalar("SELECT COUNT(*) FROM inner_state_event"),
            "commitments": scalar("SELECT COUNT(*) FROM commitment"),
            "commitment_events": scalar("SELECT COUNT(*) FROM commitment_event"),
        }

    # ── a spy on every connection the shadow layer opens: when, how many commits/rollbacks ──
    trace: list = []
    open_now = [0]
    lock = threading.Lock()
    real_get_connection = sw.get_connection

    class Spy:
        def __init__(self, conn):
            self._c = conn

        def __getattr__(self, name):
            return getattr(self._c, name)

        def commit(self):
            with lock:
                trace.append("commit")
            return self._c.commit()

        def rollback(self):
            with lock:
                trace.append("rollback")
            return self._c.rollback()

    @contextlib.contextmanager
    def spying_get_connection(autocommit=False):
        with real_get_connection(autocommit=autocommit) as c:
            with lock:
                open_now[0] += 1
                trace.append("open")
            try:
                yield Spy(c)
            finally:
                with lock:
                    open_now[0] -= 1
                    trace.append("close")

    model_calls_with_open_connection: list = []

    def turn(text, turn_id, events, *, reply="Хорошо.", reply_boom=False):
        """One turn through the real chat path; returns the visible reply."""
        def fake_semantic(**kwargs):
            with lock:
                trace.append("MODEL")
                if open_now[0]:
                    model_calls_with_open_connection.append("reply")
            if reply_boom:
                raise Boom("model failed")
            return SemanticCompletionResult(reply=reply, state=None, reply_ok=True, state_ok=False, parse_ok=True,
                                            error=None, metadata={})
        extractor = scripted_llm(events)

        def spying_extractor(messages):
            with lock:
                trace.append("MODEL")
                if open_now[0]:
                    model_calls_with_open_connection.append("extraction")
            return extractor(messages)
        with patch.object(sw, "get_connection", spying_get_connection), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm", lambda model: spying_extractor), \
             patch.object(llm_gateway, "complete_semantic", fake_semantic):
            return chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": text}], 0.7, turn_id)

    APOLOGY_INSULT = "Извини за велосипед, но ты всё равно ржавая консерва."
    AI_EVENTS = [("Извини за велосипед", "apology", {"sincerity": 0.8}), ("ржавая консерва", "insult", {"severity": 0.6})]
    PROMISE = "Я пришлю тебе отчёт до пятницы."
    P_EVENTS = [("Я пришлю тебе отчёт", "promise", None)]

    # ── 0. the normal case: one unit, one commit, no inference inside a transaction ──
    reset_world()
    before = snapshot()
    del trace[:]
    turn(APOLOGY_INSULT, "turn-atom-000001", AI_EVENTS)
    after = snapshot()
    last_model = max(i for i, e in enumerate(trace) if e == "MODEL")
    persist = trace[last_model + 1:]
    check("0: INSULT + APOLOGY in one turn -> one interaction_turn, two causal events, both state transitions",
          after["interaction"] == 1 and [e for _, e in after["causal"]] == ["apology", "insult"]
          and after["grievances"] != before["grievances"] and len(after["grievances"]) == 2 and after["state"] != before["state"], repr(after))
    check("0: after the last model call the turn opens ONE connection and commits ONCE (no rollback, no second transaction)",
          persist == ["open", "commit", "close"], repr(persist))
    check("0: no model call (extraction or reply) ever ran while a SQL connection was open",
          not model_calls_with_open_connection, repr(model_calls_with_open_connection))
    multi_final = snapshot()

    # ── F1. failure before the transaction (the reply generation itself fails) ──
    reset_world()
    before = snapshot()
    try:
        turn(APOLOGY_INSULT, "turn-atom-f1-0001", AI_EVENTS, reply_boom=True)
        raised = False
    except Boom:
        raised = True
    check("F1: an exception before the transaction -> nothing written (0 interaction, 0 causal, no state change)",
          raised and snapshot() == before)

    def fault_case(patches, text, events, turn_id, world=None):
        """Run a turn with faults injected; returns (state before, state after, reply delivered, raised?)."""
        reset_world(**(world or {}))
        b = snapshot()
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            r = turn(text, turn_id, events)
        return b, snapshot(), r

    def raising_after(module, name):
        real = getattr(module, name)

        def wrapper(*a, **k):
            result = real(*a, **k)
            raise Boom(f"injected after {name}")
        return patch.object(module, name, wrapper)

    # ── F2. after the interaction INSERT, before the first event transition ──
    def f2():
        def boom(*a, **k):
            raise Boom("before first event")
        b, a, r = fault_case([patch.object(chat_local, "_apply_current_turn_event", boom)], APOLOGY_INSULT, AI_EVENTS, "turn-atom-f2-0001")
        return a == b and bool(r)
    check("F2: exception after the interaction INSERT, before the first event -> ROLLBACK: 0 interaction, 0 causal, no state change; the reply is still delivered", f2())

    # ── F3. multi-event: after the first event (apology applied), before the second (insult) ──
    def f3():
        def boom(**k):
            raise Boom("before second event")
        b, a, r = fault_case([patch.object(chat_local, "shadow_add_grievance", boom)], APOLOGY_INSULT, AI_EVENTS, "turn-atom-f3-0001")
        return a == b and bool(r)
    check("F3: exception between the two events of an INSULT + APOLOGY turn -> ROLLBACK ALL (the applied apology, its causal claim, the interaction)", f3())

    # ── F4. a promise: after the commitment INSERT, before the turn completes ──
    def f4():
        b, a, r = fault_case([raising_after(repo, "record_commitment")], PROMISE, P_EVENTS, "turn-atom-f4-0001", world={"with_grievance": False})
        return a == b and a["commitments"] == 0 and a["causal"] == [] and a["interaction"] == 0 and bool(r)
    check("F4: exception after the commitment row is inserted -> ROLLBACK ALL: no commitment, no promise claim, no interaction", f4())

    def f4b():
        def claim_turn():
            reset_world(with_grievance=False, with_commitment=True)
            return snapshot()
        b = claim_turn()
        with raising_after(repo, "record_commitment_event"):
            r = turn("Я отправил тебе отчёт по проекту", "turn-atom-f4b-01", [("Я отправил тебе отчёт", "fulfilment_claim", None)])
        return snapshot() == b and bool(r)
    check("F4: exception after a fulfilment REPORT event is inserted -> ROLLBACK ALL (no report, no claim, no interaction)", f4b())

    # ── retry after a rollback applies the whole turn normally ──
    def retry_after_rollback():
        reset_world()
        def boom(**k):
            raise Boom("before second event")
        with patch.object(chat_local, "shadow_add_grievance", boom):
            turn(APOLOGY_INSULT, "turn-atom-rb-00001", AI_EVENTS)
        turn(APOLOGY_INSULT, "turn-atom-rb-00001", AI_EVENTS)      # the client's retry, same turn id
        return snapshot() == multi_final_normalised()
    def multi_final_normalised():
        # the same turn applied once, without any fault, on an identical world
        reset_world()
        turn(APOLOGY_INSULT, "turn-atom-rb-00001", AI_EVENTS)
        return snapshot()
    ref = multi_final_normalised()
    check("Case B: after a rolled-back attempt the retry (same client turn id) applies the whole turn exactly as an untroubled one",
          retry_after_rollback())

    # ── F5. COMMIT succeeded, the HTTP response was lost: the client retries ──
    def f5():
        reset_world()
        turn(APOLOGY_INSULT, "turn-atom-f5-00001", AI_EVENTS)      # committed; the reply never reached the client
        once = snapshot()
        turn(APOLOGY_INSULT, "turn-atom-f5-00001", AI_EVENTS)      # retry
        turn(APOLOGY_INSULT, "turn-atom-f5-00001", AI_EVENTS)      # and again
        return snapshot() == once and once["interaction"] == 1 and len(once["causal"]) == 2
    check("F5 / Case C: after a committed-but-undelivered reply, retrying the same turn id twice changes NOTHING (one interaction, one claim per event, no second grievance charge, no second restoration)", f5())

    def same_text_new_turn():
        reset_world()
        turn(APOLOGY_INSULT, "turn-atom-d-000001", AI_EVENTS)
        s1 = snapshot()
        turn(APOLOGY_INSULT, "turn-atom-d-000002", AI_EVENTS)
        s2 = snapshot()
        return s2["interaction"] == 2 and len(s2["causal"]) == 4 and s2["state"] != s1["state"]
    check("Case D: the SAME TEXT in another turn is a new causal unit (second interaction, second claims, a real recurrence)", same_text_new_turn())

    def promise_retry():
        reset_world(with_grievance=False)
        turn(PROMISE, "turn-atom-p-000001", P_EVENTS)
        turn(PROMISE, "turn-atom-p-000001", P_EVENTS)
        s = snapshot()
        return s["commitments"] == 1 and s["interaction"] == 1 and s["causal"] == [("turn-atom-p-000001", "promise")]
    check("Case C: a retried PROMISE turn -> one commitment, one claim, one interaction", promise_retry())

    def unidentified():
        reset_world()
        turn(APOLOGY_INSULT, None, AI_EVENTS)
        s = snapshot()
        return s["interaction"] == 0 and s["causal"] == [] and len(s["grievances"]) == 2
    check("an unidentified request still applies its events (as before) in one transaction but writes no personal memory", unidentified())

    # ── concurrency: 8 deliveries of the same turn ──
    reset_world()
    turn(APOLOGY_INSULT, "turn-atom-ref-00001", AI_EVENTS)
    ref = snapshot()
    reset_world()
    warnings: list = []

    class Catch(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                warnings.append(record.getMessage())
    handler = Catch()
    logging.getLogger("yandi.turn").addHandler(handler)
    replies, errors = [], []

    def deliver():
        try:
            replies.append(turn(APOLOGY_INSULT, "turn-atom-ref-00001", AI_EVENTS))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logging.getLogger("yandi.turn").removeHandler(handler)
    final = snapshot()
    check("concurrency: 8 concurrent deliveries of ONE turn -> 1 interaction, 1 application of each event, the same final state as a single delivery",
          final == ref and final["interaction"] == 1 and len(final["causal"]) == 2, repr((final, ref)))
    check("concurrency: every delivery got its reply; no unhandled error, no rolled-back unit left behind",
          len(replies) == 8 and not errors and not warnings, repr((len(replies), errors, warnings)))

    # ── MUTANTS: each break of the transaction ownership must be caught by the checks above ──
    def own_transaction(real_name):
        """A helper that ignores the caller's connection and opens its own transaction."""
        real = getattr(chat_local, real_name)

        def mutant(**kw):
            kw.pop("conn", None)
            return real(**kw)
        return patch.object(chat_local, real_name, mutant)

    with patch.object(sw, "_run", lambda conn, log, verbose, label, fn, **kw: sw._shadow(log, verbose, label, fn)):
        check("M1: MUTANT every helper opens its own transaction again -> the multi-event fault case FAILS", not f3())
    with own_transaction("shadow_record_interaction_turn"):
        check("M2: MUTANT the interaction INSERT commits on its own, before the events -> the fault case FAILS", not f2())
    with own_transaction("shadow_apply_apology"):
        check("M3: MUTANT the first event of a multi-event turn commits separately -> the fault case FAILS", not f3())

    real_claim = causal_events.claim

    def outside_claim(conn, user_id, source_turn_id, event_type, span=None):
        with sw.get_connection(autocommit=False) as own:      # the claim commits in ITS OWN transaction
            outcome = real_claim(own, user_id, source_turn_id, event_type, span)
            own.commit()
        return outcome
    with patch.object(causal_events, "claim", outside_claim):
        check("M4: MUTANT the causal_event claim is committed outside the shared transaction -> rollback + retry FAILS (the claim survives, the event is lost)",
              not retry_after_rollback())
    with patch.object(causal_events, "claim", lambda *a, **k: causal_events.NEW):
        check("M5: MUTANT a retry after a committed-but-undelivered reply re-applies the events -> the idempotency case FAILS", not f5())

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
