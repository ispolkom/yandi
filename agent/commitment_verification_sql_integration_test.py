"""
agent/commitment_verification_sql_integration_test.py — VERIFIED COMMITMENTS AND TRUST ON A
REAL SQL ENGINE (a private, temporary MySQL instance).

    USER REPORTS FULFILMENT != FULFILMENT VERIFIED.       TRUST GROWS ONLY FROM VERIFIED EVIDENCE.
    VERIFICATION + TRUST TRANSITION MUST SHARE THE TURN TRANSACTION.
    ONE COMMITMENT -> MAX ONE REWARD.   SAME TURN RETRIED != NEW HISTORY.
    HISTORY MAY BE EXTENDED, NEVER SILENTLY REWRITTEN.

Real pet.chat_local + real shadow_write + real ledger/state + the real least-privilege runtime
role; only the model calls are scripted. What only a real engine can prove: the v17 -> v18
migration is additive and re-runnable, the pre-v18 database still works, the turn is ONE
transaction that a fault at any point rolls back whole (verified event, trust transition,
causal claim, interaction), a retry / a lost response / eight concurrent deliveries change
nothing, two DIFFERENT turns verifying at the same moment cannot both be "the first proof",
a restart (a new process, another model) finds the open promise in SQL, and the runtime role
cannot rewrite the ledger.

Run through scripts/test-sql-temp.sh (needs a mysqld binary; the live database is never
contacted). Without the environment it prints SKIP and exits 0.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
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


CODE = "в следующем сообщении дам тебе кодовое слово"
NUM = "в следующем сообщении дам тебе число от 100 до 999"
DELIVERY = "Вот оно: кодовое слово АЛЬФА."


def _scripted_turn(text, turn_id, *, events=(), verify=None, facts=(), model_name="model-A", reply="Хорошо.", router=None, extra_patches=()):
    """One turn through the real chat path with scripted models. Returns (reply, router)."""
    import llm_gateway
    from llm_gateway.types import SemanticCompletionResult
    import pet.chat_local as chat_local
    from pet.extraction_test_support import scripted_llm
    from pet.fact_test_support import scripted_fact_llm, scripted_router
    from pet.verification_test_support import scripted_verify_llm

    def fake_semantic(**kwargs):
        return SemanticCompletionResult(reply=reply, state=None, reply_ok=True, state_ok=False, parse_ok=True, error=None,
                                        metadata={"_llm_gateway_trace": [{"result": "success", "resolved_model": model_name, "adapter_id": "llama_cpp"}]})
    router = router or scripted_router(scripted_llm(events), scripted_fact_llm(facts), verify or scripted_verify_llm())
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(chat_local, "_self_knowledge_message", lambda: None))
        stack.enter_context(patch.object(chat_local, "_extraction_llm", lambda model: router))
        stack.enter_context(patch.object(llm_gateway, "complete_semantic", fake_semantic))
        for p in extra_patches:
            stack.enter_context(p)
        out = chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": text}], 0.7, turn_id)
    return out, router


def child() -> int:
    """A NEW PROCESS (a restart), another model: delivers the promised word in an identified turn."""
    from pet.verification_test_support import scripted_verify_llm
    turn_id, model = sys.argv[sys.argv.index("--child-deliver") + 1:][:2]
    out, router = _scripted_turn(DELIVERY, turn_id, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)), model_name=model)
    print("CHILD-OK " + json.dumps({"reply": out, "verify_calls": len(router.verify_inputs)}, ensure_ascii=False))
    return 0


def main() -> int:
    if "--child-deliver" in sys.argv:
        return child()
    socket = os.environ.get("YANDI_TEST_SQL_SOCKET")
    admin, admin_pw = os.environ.get("YANDI_TEST_SQL_ADMIN"), os.environ.get("YANDI_TEST_SQL_ADMIN_PW")
    if not (socket and admin and admin_pw):
        print("SKIP: set YANDI_TEST_SQL_SOCKET / _ADMIN / _ADMIN_PW (see scripts/test-sql-temp.sh)")
        return 0
    from agent.db.sql.connection import LiveDatabaseRefused, assert_connection_allowed, pymysql_target
    try:
        assert_connection_allowed(socket)   # the ONE isolation check
    except LiveDatabaseRefused as e:
        print(f"REFUSED: {e}")
        return 2

    import io
    import pymysql
    import pymysql.cursors
    from agent.db.sql import migrate, schema, security_grants
    import agent.causal_events as causal_events
    import agent.db.sql.repositories as repo
    import agent.db.sql.shadow_write as sw
    import agent.relationship_commitments as rc
    import agent.relationship_memory as rm
    import agent.relationship_state as rs
    import pet.chat_local as chat_local
    from pet.extraction_test_support import scripted_llm
    from pet.fact_test_support import scripted_fact_llm, scripted_router
    from pet.verification_test_support import scripted_verify_llm

    OWNER = chat_local._RELATIONSHIP_USER_ID

    def connect(user, password, autocommit=False):
        assert_connection_allowed(socket)
        return pymysql.connect(**pymysql_target(socket), user=user, password=password, database="yandi_epistemic",
                               cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit, charset="utf8mb4")

    root = connect(admin, admin_pw, autocommit=True)
    with root.cursor() as cur:
        cur.execute("DROP USER IF EXISTS 'yandi_rt_cy8'@'localhost'")
        cur.execute("CREATE USER 'yandi_rt_cy8'@'localhost' IDENTIFIED BY 'rt-temp-only'")
        for sql, params in security_grants.yandi_runtime_grant_statements("yandi_rt_cy8", "localhost"):
            cur.execute(sql, params)
        cur.execute("FLUSH PRIVILEGES")

    def as_admin():
        os.environ.update({"YANDI_SQL_SOCKET": socket, "YANDI_SQL_USER": admin, "YANDI_SQL_AUTH_MODE": "password", "YANDI_SQL_PASSWORD": admin_pw})

    def as_runtime():
        os.environ.update({"YANDI_SQL_SOCKET": socket, "YANDI_SQL_USER": "yandi_rt_cy8", "YANDI_SQL_AUTH_MODE": "password", "YANDI_SQL_PASSWORD": "rt-temp-only"})

    def scalar(sql, params=()):
        with root.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchone().values())[0]

    def rows(sql, params=()):
        with root.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    TABLES = ("personal_fact_event", "personal_fact", "interaction_turn", "causal_event", "grievance", "forgiveness_capacity",
              "inner_state_event", "inner_state", "commitment_event", "commitment")

    def reset_world():
        with root.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for t in TABLES:
                cur.execute(f"TRUNCATE TABLE {t}")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        repo.get_or_create_inner_state(root, OWNER)     # the owner's state row exists (as it does after the first event); an INSERT IGNORE that
                                                       # CREATES it would hold a row lock and hide whether the code itself locks

    def trust() -> float:
        return rs.get_state(root, OWNER)["trust"]

    def state_equals_replay() -> bool:
        """The audit trail rebuilds the materialised state (to the precision of the engine's FLOAT columns)."""
        got, want = rs.replay(root, OWNER), rs.get_state(root, OWNER)
        return all(abs(got[k] - want[k]) < 1e-3 for k in rs.COORDINATES)

    def target_of(word):
        """The index of the open promise mentioning `word` in the listing the verifier is shown (same-second promises have no
        chronological order, so a scripted extractor must look the position up, like a real one reading the list)."""
        return next(i for i, c in enumerate(rc.verifiable_commitments(root, OWNER, None)) if word in c["evidence"])

    def snapshot() -> dict:
        st = rs.get_state(root, OWNER)
        return {
            "interaction": [r["source_turn_id"] for r in rows("SELECT source_turn_id FROM interaction_turn ORDER BY source_turn_id")],
            "causal": [(r["source_turn_id"], r["event_type"]) for r in rows("SELECT source_turn_id, event_type FROM causal_event ORDER BY source_turn_id, event_type")],
            "commitments": [(r["kind"], r["source_turn_id"] if "source_turn_id" in r else None) for r in rows("SELECT * FROM commitment ORDER BY created_at, commitment_id")],
            "events": [(r["event_type"], r["source"], r.get("source_turn_id")) for r in rows("SELECT * FROM commitment_event ORDER BY event_id")],
            "state": tuple(round(st[k], 4) for k in ("trust", "respect", "affection")),
            "state_events": [(r["event_type"], round(float(r["weight"]), 4)) for r in rows("SELECT event_type, weight FROM inner_state_event ORDER BY event_id")],
            "facts": scalar("SELECT COUNT(*) FROM personal_fact"),
        }

    def verified_events():
        return rows("SELECT * FROM commitment_event WHERE event_type='verified_fulfilled' ORDER BY event_id")

    # ── trace of every connection the shadow layer opens ──
    trace: list = []
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
                trace.append("open")
            try:
                yield Spy(c)
            finally:
                with lock:
                    trace.append("close")

    def turn(text, turn_id, **kw):
        extra = list(kw.pop("extra_patches", ())) + [patch.object(sw, "get_connection", spying_get_connection)]
        return _scripted_turn(text, turn_id, extra_patches=extra, **kw)

    seq = [0]

    def tid(prefix="cy8") -> str:
        seq[0] += 1
        return f"turn-{prefix}-{seq[0]:06d}"

    def promise(what=CODE, turn_id=None):
        t = turn_id or tid("promise")
        turn(f"Обещаю: {what}.", t, events=[(what, "promise", None)], verify=scripted_verify_llm(classify="in_chat"))
        return t

    def raising_after(module, name, when):
        real = getattr(module, name)

        def wrapper(*a, **k):
            result = real(*a, **k)
            if when(*a, **k):
                raise Boom(f"injected after {name}")
            return result
        return patch.object(module, name, wrapper)

    # ══ 0. the v17 database: the code still works without the v18 columns, then the migration adds them ══
    as_admin()
    with root.cursor() as cur:
        cur.execute("ALTER TABLE commitment DROP COLUMN source_turn_id")
        cur.execute("ALTER TABLE commitment_event DROP COLUMN source_turn_id, DROP COLUMN span_start, DROP COLUMN span_end")
        cur.execute("DELETE FROM schema_migrations WHERE version=18")
    reset_world()
    as_runtime()
    t_old = tid("v17")
    out, _ = turn("Обещаю: что-нибудь.", t_old, events=[("что-нибудь", "promise", None)], verify=scripted_verify_llm(classify="in_chat"))
    old_id = rows("SELECT commitment_id FROM commitment")[0]["commitment_id"]
    out2, r2 = turn("Кодовое слово АЛЬФА", tid("v17"), events=[], verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    out3, _ = turn("Я всё выполнил", tid("v17"), events=[("Я всё выполнил", "fulfilment_claim", None)])
    check("0: on a v17 database (no provenance columns) the chat keeps working: the promise is recorded, the turns are recorded, replies delivered",
          out and out2 and out3 and scalar("SELECT COUNT(*) FROM commitment") == 1 and scalar("SELECT COUNT(*) FROM interaction_turn") == 3)
    check("0: on a v17 database nothing can be verified: no verifier is even asked, no verified event, trust unchanged",
          r2.verify_inputs == [] and not verified_events() and trust() == 50.0, repr((r2.verify_inputs[:1], verified_events(), trust())))
    check("0: on a v17 database a fulfilment REPORT is still recorded (as before v18), and moves nothing",
          [r["event_type"] for r in rows("SELECT event_type FROM commitment_event")] == ["fulfillment_claimed"])
    as_admin()
    with contextlib.redirect_stdout(io.StringIO()):
        upgraded = migrate.apply()
    cols = {r["COLUMN_NAME"] if "COLUMN_NAME" in r else r["column_name"]: r for r in rows(
        "SELECT column_name, column_type, collation_name FROM information_schema.columns WHERE table_schema='yandi_epistemic' "
        "AND table_name IN ('commitment','commitment_event') AND column_name IN ('source_turn_id','span_start','span_end')")}
    check("0: the migration adds the v18 provenance columns and records version 18 (additive)",
          upgraded and scalar("SELECT MAX(version) FROM schema_migrations") == schema.SCHEMA_VERSION == 18
          and scalar("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='yandi_epistemic' AND table_name='commitment_event' "
                     "AND column_name IN ('source_turn_id','span_start','span_end')") == 3
          and scalar("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='yandi_epistemic' AND table_name='commitment' AND column_name='source_turn_id'") == 1)
    check("0: rows written before v18 survive untouched, with NULL provenance (nothing is rewritten or invented)",
          rows("SELECT commitment_id, source_turn_id FROM commitment")[0] == {"commitment_id": old_id, "source_turn_id": None}
          and rows("SELECT source_turn_id, span_start FROM commitment_event")[0] == {"source_turn_id": None, "span_start": None})
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        again = migrate.apply()
    check("0: the migration is re-runnable (every ALTER reports 'already applied', the version stays 18)",
          again and buf.getvalue().count("SKIP") >= 4 and scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=18") == 1, buf.getvalue()[-200:])
    collations = {r["COLUMN_NAME"] if "COLUMN_NAME" in r else r["column_name"]: (r.get("COLLATION_NAME") or r.get("collation_name")) for r in rows(
        "SELECT column_name, collation_name FROM information_schema.columns WHERE table_schema='yandi_epistemic' AND table_name='commitment_event' AND column_name='source_turn_id'")}
    check("0: the provenance turn id is compared exactly (binary collation) like every other turn identity", set(collations.values()) == {"ascii_bin"}, repr(collations))
    as_runtime()

    # ══ 1. the runtime role can extend the ledger and cannot rewrite it ══
    reset_world()
    rt = connect("yandi_rt_cy8", "rt-temp-only", autocommit=True)
    promise()
    denied = {}
    for label, sql in (("UPDATE commitment_event", "UPDATE commitment_event SET evidence='x'"), ("DELETE commitment_event", "DELETE FROM commitment_event"),
                       ("UPDATE commitment", "UPDATE commitment SET kind='in_chat'"), ("DELETE commitment", "DELETE FROM commitment"),
                       ("UPDATE causal_event", "UPDATE causal_event SET event_type='x'"), ("UPDATE interaction_turn", "UPDATE interaction_turn SET user_text='x'"),
                       ("ALTER commitment", "ALTER TABLE commitment ADD COLUMN x INT")):
        try:
            with rt.cursor() as cur:
                cur.execute(sql)
            denied[label] = False
        except pymysql.err.OperationalError as e:
            denied[label] = e.args[0] in (1142, 1143, 1044, 1045)
    check("1: HISTORY MAY BE EXTENDED, NOT REWRITTEN — the runtime role's UPDATE / DELETE / ALTER on the promise ledger, the causal ledger and the turn record are all denied by the engine",
          all(denied.values()), repr(denied))
    rt.close()
    src_repo = open(repo.__file__, encoding="utf-8").read()
    check("1: no code path issues an UPDATE or DELETE against the commitment tables", "UPDATE commitment" not in src_repo and "DELETE FROM commitment" not in src_repo)

    # ══ 2. the normal case: ONE transaction; the verified event and the trust transition share it ══
    reset_world()
    t_promise = promise()
    base = snapshot()
    del trace[:]
    t_del = tid("deliver")
    out, router = turn(DELIVERY, t_del, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)), model_name="model-B")
    after = snapshot()
    v = verified_events()
    check("2: a directly observed delivery is VERIFIED: one verified event with the exact evidence, its source turn and span",
          len(v) == 1 and v[0]["evidence"] == "АЛЬФА" and v[0]["source_turn_id"] == t_del and v[0]["source"] == rc.VERIFIER_IN_CHAT
          and DELIVERY[v[0]["span_start"]:v[0]["span_end"]] == "АЛЬФА", repr(v))
    check("2: PROVENANCE IN SQL — the evidence is the bytes of the immutable interaction_turn it points at, at that span",
          scalar("SELECT COUNT(*) FROM commitment_event e JOIN interaction_turn t ON t.user_id=e.user_id AND t.source_turn_id=e.source_turn_id "
                 "WHERE e.event_type='verified_fulfilled' AND SUBSTRING(t.user_text, e.span_start+1, e.span_end-e.span_start)=e.evidence") == 1)
    check("2: trust rose by exactly the bounded reward and nothing else moved", after["state"] == (50.0 + rs.OBSERVED_TRUST_BASE, 50.0, 30.0), repr(after["state"]))
    check("2: causal claim + interaction + verified event + state audit row all belong to the delivery turn",
          (t_del, "commitment_verified") in after["causal"] and t_del in after["interaction"] and after["state_events"] == [("commitment_observed", rs.OBSERVED_TRUST_BASE)])
    persist_start = len(trace) - 1 - trace[::-1].index("open")
    check("2: after the last model call the turn opens ONE connection and commits ONCE (verification + trust are one transaction)",
          trace[persist_start:] == ["open", "commit", "close"], repr(trace))
    check("2: the promise turn was made by another model in another call; SQL alone carried it (MODEL SWAP)", base["commitments"][0][0] == "in_chat" and out)
    check("2: the replay of the audit trail equals the materialised state", state_equals_replay())
    check("2: the resolved commitment is history now: no longer an open target, nothing was rewritten",
          rc.commitment_statuses(root, OWNER)[0]["status"] == "verified_fulfilled" and rc.verifiable_commitments(root, OWNER, None) == [])
    check("2: a verified fulfilment creates no personal fact", scalar("SELECT COUNT(*) FROM personal_fact") == 0)

    # ══ 3. FAULTS: ROLLBACK ALL, then the retry applies the turn normally ══
    def delivery_world():
        reset_world()
        promise(turn_id="turn-cy8-promise-fixed")
        return snapshot()

    REF_TURN = "turn-cy8-reference-01"

    def reference():
        delivery_world()
        turn(DELIVERY, REF_TURN, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
        return snapshot()
    ref = reference()

    def fault(patches, *, facts=()):
        b = delivery_world()
        try:
            out, _ = turn(DELIVERY, REF_TURN, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)), extra_patches=patches, facts=facts)
        except Exception:  # noqa: BLE001
            return b, snapshot(), None
        return b, snapshot(), out

    def f_after_event():
        b, a, out = fault([raising_after(repo, "record_commitment_event", lambda conn, cid, uid, et, *r, **k: et == rc.VERIFIED_KEPT)])
        return a == b and bool(out)
    check("F1: exception AFTER the verified event is inserted -> ROLLBACK ALL: no verified event, no trust change, no causal claim, no interaction; the reply is still delivered", f_after_event())

    def f_trust():
        b, a, out = fault([patch.object(repo, "update_inner_state", lambda *a, **k: (_ for _ in ()).throw(Boom("trust write failed")))])
        return a == b and a["events"] == [] and bool(out)
    check("F2: the TRUST TRANSITION fails after the verified event was written -> the event is rolled back with it (no proof without its consequence)", f_trust())

    def f_state_event():
        b, a, out = fault([patch.object(repo, "record_inner_state_event", lambda *a, **k: (_ for _ in ()).throw(Boom("audit write failed")))])
        return a == b and bool(out)
    check("F3: the audit row of the transition fails after the coordinates were updated -> ROLLBACK ALL (no half-applied trust)", f_state_event())

    DOG = {"quote": "у меня есть собака по кличке Рекс", "cls": "possession", "statement": "У пользователя есть собака по кличке Рекс"}

    def f_later():
        def boom(**k):
            raise Boom("a later step of the same turn fails")
        b = delivery_world()
        try:
            turn(DELIVERY + " Знаешь, у меня есть собака по кличке Рекс.", REF_TURN, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)), facts=[DOG],
                 extra_patches=[patch.object(chat_local, "shadow_record_personal_facts", boom)])
        except Exception:  # noqa: BLE001
            pass
        a = snapshot()
        return a == b, a, b
    same, a_snap, b_snap = f_later()
    check("F4: 'and the other way round' — verified event AND trust transition both succeeded, then a LATER step of the turn fails -> ROLLBACK ALL (both undone)",
          same and a_snap["events"] == [] and a_snap["state"] == (50.0, 50.0, 30.0), repr((a_snap, b_snap)))

    def retry_after_rollback():
        delivery_world()
        with patch.object(repo, "update_inner_state", lambda *a, **k: (_ for _ in ()).throw(Boom("trust write failed"))):
            turn(DELIVERY, REF_TURN, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
        turn(DELIVERY, REF_TURN, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))       # the client's retry, same turn id
        return snapshot() == ref
    check("F5: after a rolled-back attempt the retry (same client turn id) applies the whole turn exactly as an untroubled one", retry_after_rollback())

    # ══ 4. RETRY AFTER COMMIT / LOST RESPONSE, SAME EVIDENCE NEW TURN ══
    delivery_world()
    turn(DELIVERY, REF_TURN, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    once = snapshot()
    turn(DELIVERY, REF_TURN, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    turn(DELIVERY, REF_TURN, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    check("4: RETRY AFTER COMMIT (the HTTP response was lost), twice -> no second verification, no second trust increase, nothing new at all", snapshot() == once and len(verified_events()) == 1)
    turn(DELIVERY, tid("again"), verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    s = snapshot()
    check("4: the SAME EVIDENCE in a NEW turn after the commitment is resolved -> no second verification, trust unchanged (the new turn is only recorded)",
          len(verified_events()) == 1 and s["state"] == once["state"] and s["events"] == once["events"])

    # ══ 5. concurrency ══
    delivery_world()
    tls = threading.local()
    replies, errors = [], []
    warnings: list = []

    class Catch(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                warnings.append(record.getMessage())
    handler = Catch()
    logging.getLogger("yandi.turn").addHandler(handler)

    @contextlib.contextmanager
    def chat_env():
        """The global patches every concurrent thread shares (applied ONCE: a patch entered and left by several threads would
        restore in the wrong order); each thread sets tls.router to its own scripted models."""
        import llm_gateway
        from llm_gateway.types import SemanticCompletionResult

        def fake_semantic(**kwargs):
            return SemanticCompletionResult(reply="Хорошо.", state=None, reply_ok=True, state_ok=False, parse_ok=True, error=None, metadata={})
        with patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm", lambda model: tls.router), \
             patch.object(llm_gateway, "complete_semantic", fake_semantic):
            yield

    def deliver_in_thread(text, turn_id, router, name=None):
        def deliver():
            try:
                tls.router = router
                replies.append(chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": text}], 0.7, turn_id))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        return threading.Thread(target=deliver, name=name)

    def run_threads(jobs):
        """jobs: [(text, turn_id, router)]; every thread has its own scripted models."""
        with chat_env():
            threads = [deliver_in_thread(*j) for j in jobs]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

    def mk_router(verify):
        return scripted_router(scripted_llm([]), scripted_fact_llm([]), verify)

    run_threads([(DELIVERY, "turn-cy8-race-0001", mk_router(scripted_verify_llm(delivery=("АЛЬФА", 0)))) for _ in range(8)])
    final = snapshot()
    check("5: 8 concurrent deliveries of ONE fulfilment turn -> ONE verified event, ONE trust transition, one interaction",
          len(verified_events()) == 1 and final["state"] == (50.0 + rs.OBSERVED_TRUST_BASE, 50.0, 30.0)
          and final["interaction"].count("turn-cy8-race-0001") == 1 and len([e for e in final["state_events"] if e[0] == "commitment_observed"]) == 1, repr(final))
    check("5: every delivery got its reply; no unhandled error, no rolled-back unit left behind", len(replies) == 8 and not errors and not warnings, repr((len(replies), errors, warnings)))

    def race_two_turns(round_no):
        """Two DIFFERENT promises delivered by two DIFFERENT turns at the same moment: neither may be 'the first proof' for both."""
        reset_world()
        promise(CODE)
        promise(NUM)
        judge = lambda p, f: ("кодовое слово" in p and f == "АЛЬФА") or ("число" in p and f == "427")
        del replies[:], errors[:]
        run_threads([
            ("Кодовое слово АЛЬФА", f"turn-cy8-r{round_no}-a0001", mk_router(scripted_verify_llm(delivery=("АЛЬФА", target_of("кодовое слово")), delivers=judge))),
            ("Число 427", f"turn-cy8-r{round_no}-b0001", mk_router(scripted_verify_llm(delivery=("427", target_of("число")), delivers=judge))),
        ])
        expected = round(50.0 + rs.observed_trust_reward(0) + rs.observed_trust_reward(1), 4)
        return (len(verified_events()) == 2 and round(trust(), 4) == expected and not errors
                and state_equals_replay()), (len(verified_events()), trust(), expected, errors)
    results = [race_two_turns(i) for i in range(6)]
    check("5: two DIFFERENT turns verifying at once, 6 rounds -> both verified, total reward = first proof + second proof (never two first proofs), no lost update, replay equals state",
          all(ok for ok, _ in results), repr([d for ok, d in results if not ok]))

    # ══ 6. RESTART: a new process, another model, finds the open promise in SQL ══
    reset_world()
    t_p = promise()
    proc = subprocess.run([sys.executable, "-m", "agent.commitment_verification_sql_integration_test", "--child-deliver", "turn-cy8-child-0001", "model-C-after-restart"],
                          env=os.environ.copy(), capture_output=True, text=True, timeout=180)
    child_ok = "CHILD-OK" in proc.stdout
    check("6: RESTART — a NEW PROCESS with another model finds the open promise in SQL and verifies the direct delivery (trust up, provenance kept)",
          child_ok and len(verified_events()) == 1 and verified_events()[0]["source_turn_id"] == "turn-cy8-child-0001"
          and trust() == 50.0 + rs.OBSERVED_TRUST_BASE, (proc.stdout + proc.stderr)[-400:])
    check("6: ... and the child verified through the verifier (a real call, not a shortcut)", child_ok and json.loads(proc.stdout.split("CHILD-OK ", 1)[1])["verify_calls"] >= 2)

    # ══ 7. CLAIM + DIRECT EVIDENCE, SELF-REPORTS, EXTERNAL, AMBIGUITY ══
    reset_world()
    promise()
    turn("Я выполнил обещание. Вот кодовое слово: АЛЬФА.", tid("both"), events=[("Я выполнил обещание", "fulfilment_claim", None)],
         verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    s = snapshot()
    check("7: claim + delivery in one message -> a REPORT event and a VERIFIED event; trust grew only by the verified reward",
          [e[0] for e in s["events"]] == ["fulfillment_claimed", "verified_fulfilled"] and s["state"][0] == 50.0 + rs.OBSERVED_TRUST_BASE)
    for text, quote in (("Я сделал это.", "Я сделал это"), ("Я точно оплатил.", "Я точно оплатил"), ("Поверь мне, всё готово.", "всё готово")):
        reset_world()
        promise()
        turn(text, tid("selfrep"), events=[(quote, "fulfilment_claim", None)], verify=scripted_verify_llm(delivery=(quote, 0)))
        s = snapshot()
        check(f"7: {text!r} -> REPORTED only: one report event, no verified event, trust unchanged (worst-case checker)",
              [e[0] for e in s["events"]] == ["fulfillment_claimed"] and s["state"] == (50.0, 50.0, 30.0) and s["state_events"] == [])
    reset_world()
    turn("Обещаю, что завтра оплачу счёт.", tid("ext"), events=[("завтра оплачу счёт", "promise", None)], verify=scripted_verify_llm(classify="external"))
    _, r_ext = turn("Я оплатил счёт.", tid("ext"), events=[("Я оплатил счёт", "fulfilment_claim", None)], verify=scripted_verify_llm(delivery=("оплатил счёт", 0)))
    check("7: an EXTERNAL promise + 'I paid' -> reported, never verifiable: no verifier call, trust unchanged", r_ext.verify_inputs == [] and not verified_events() and trust() == 50.0,
          repr((r_ext.verify_inputs[:1], snapshot()["commitments"], verified_events(), trust())))
    reset_world()
    promise(CODE)
    promise("в следующем сообщении дам тебе пароль")
    turn("Вот обещанное: АЛЬФА", tid("amb"), verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    check("7: AMBIGUOUS — 'here is what I promised: ALPHA' fits two open promises -> nothing verified", not verified_events() and trust() == 50.0,
          repr((snapshot()["commitments"], verified_events())))

    # ══ 8. TRUST: farming, negative history ══
    reset_world()
    for i in range(12):
        promise(f"в следующем сообщении назову число {i}")
        turn(f"Число {i}", tid("farm"), verify=scripted_verify_llm(delivery=(str(i), 0)))
    check("8: FARMING — twelve trivial promises each verified: twelve verified events, trust rose a little and stopped far from the top, replay equals state",
          len(verified_events()) == 12 and 50.0 < trust() < 55.0
          and state_equals_replay(),
          f"{trust()} verified={len(verified_events())} replay={rs.replay(root, OWNER)} state={rs.get_state(root, OWNER)}")
    reset_world()
    for _ in range(3):
        rs.record_insult(root, OWNER, 1.0)
    low = trust()
    promise()
    turn(DELIVERY, tid("neg"), verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    check("8: NEGATIVE HISTORY — after serious harm one verified trivial promise leaves trust far below neutral", low < trust() < 40.0, f"{low} -> {trust()}")

    # ══ 9. MUTANTS on the real engine ══
    real_apply = rs._apply

    def swallowing_apply(*a, **k):
        k["strict"] = False
        return real_apply(*a, **k)
    with patch.object(rs, "_apply", swallowing_apply):
        check("M4: MUTANT 'the trust transition swallows its failure' -> the verified event is committed WITHOUT its trust change: the F2 case FAILS", not f_trust())

    real_transition = rs.record_observed_commitment
    real_rows = repo.get_or_create_inner_state

    def transition_in_own_transaction(conn, user_id, prior):
        with sw.get_connection(autocommit=False) as other:         # the trust transition commits in ITS OWN transaction
            reward = real_transition(other, user_id, prior)
            other.commit()
        return reward

    def no_row_lock(conn, user_id, updated_at=None, for_update=False):
        return repo.get_inner_state(conn, user_id)     # no INSERT IGNORE either: its shared lock would make the outer wait for its own child
    with patch.object(repo, "get_or_create_inner_state", no_row_lock), patch.object(rs, "record_observed_commitment", transition_in_own_transaction):
        check("M4b: MUTANT 'the trust transition commits in its OWN transaction' -> a later failure of the turn rolls the verified event back but the trust stays raised: the F4 case FAILS",
              not f_later()[0])
    with patch.object(causal_events, "claim", lambda *a, **k: causal_events.NEW):
        delivery_world()
        turn(DELIVERY, REF_TURN, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
        turn(DELIVERY, REF_TURN, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
        check("M5: MUTANT 'no causal claim' — the same turn retried must still not pay twice (the resolved-commitment guard and the unique ledger row hold on their own)",
              len(verified_events()) == 1)
    # ══ 10. DETERMINISTIC INTERLEAVING: T1 holds the state lock inside its uncommitted transaction; T2 starts, must wait, and must then count T1's proof ══
    import time

    def interleaved(hazard=None):
        """Returns (verified events, trust, errors, attempts per thread). `hazard`: a callable run as T2's first statement INSTEAD of a clean start, to model a
        later change that reads before the lock is taken."""
        reset_world()
        promise(CODE)
        promise(NUM)
        judge = lambda p, f: ("кодовое слово" in p and f == "АЛЬФА") or ("число" in p and f == "427")
        in_txn, release = threading.Event(), threading.Event()
        real_rce = repo.record_commitment_event
        attempts: dict = {}

        def pausing(conn, cid, uid, et, *a, **k):
            if et == rc.VERIFIED_KEPT:
                attempts[threading.current_thread().name] = attempts.get(threading.current_thread().name, 0) + 1
            out = real_rce(conn, cid, uid, et, *a, **k)
            if et == rc.VERIFIED_KEPT and threading.current_thread().name == "T1":
                in_txn.set()
                release.wait(60)
            return out
        del replies[:], errors[:]
        real_lock = chat_local.shadow_lock_relationship_state

        def hazardous_lock(**kw):
            if threading.current_thread().name == "T2" and hazard:
                hazard(kw["conn"])
            return real_lock(**kw)
        with chat_env(), patch.object(repo, "record_commitment_event", pausing), patch.object(chat_local, "shadow_lock_relationship_state", hazardous_lock):
            t1 = deliver_in_thread("Кодовое слово АЛЬФА", "turn-cy8-il-a00001", mk_router(scripted_verify_llm(delivery=("АЛЬФА", target_of("кодовое слово")), delivers=judge)), "T1")
            t2 = deliver_in_thread("Число 427", "turn-cy8-il-b00001", mk_router(scripted_verify_llm(delivery=("427", target_of("число")), delivers=judge)), "T2")
            t1.start()
            in_txn.wait(60)
            t2.start()
            time.sleep(2.5)                 # T2 reaches the state lock and waits behind T1's uncommitted transaction
            release.set()
            t1.join()
            t2.join()
        return len(verified_events()), round(trust(), 4), list(errors), dict(attempts)

    expected = round(50.0 + rs.observed_trust_reward(0) + rs.observed_trust_reward(1), 4)
    ONCE = {"T1": 1, "T2": 1}
    n_v, t_v, errs, att = interleaved()
    check("10: T1 pauses inside its uncommitted verification, T2 starts and waits on the state lock: T2 then counts T1's proof — both verified, the second worth the SECOND reward",
          n_v == 2 and t_v == expected and not errs, repr((n_v, t_v, expected, errs)))
    check("10: ... and DETERMINISTICALLY: each transaction ran once (the lock serialised them; no deadlock, no retry did the work)", att == ONCE, repr(att))

    def read_first(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM commitment_event")      # a plain read BEFORE the lock: the snapshot is fixed here
            cur.fetchone()
    n_v, t_v, errs, att = interleaved(hazard=read_first)
    check("M6: MUTANT 'a plain read happens before the state lock is taken' -> T2 counts from a stale snapshot and is paid as the FIRST proof again: the check FAILS",
          not (n_v == 2 and t_v == expected and att == ONCE), repr((n_v, t_v, expected, att)))
    real_lock_rows = repo.get_or_create_inner_state

    def unlocked(conn, user_id, updated_at=None, for_update=False):
        return real_lock_rows(conn, user_id, updated_at, for_update=False)
    with patch.object(repo, "get_or_create_inner_state", unlocked):
        n_v, t_v, errs, att = interleaved()
    check("M6b: MUTANT 'no state row lock at all' -> the two transactions collide (a deadlock; only the turn retry rescues the result): the deterministic check FAILS",
          not (n_v == 2 and t_v == expected and att == ONCE), repr((n_v, t_v, expected, att)))

    logging.getLogger("yandi.turn").removeHandler(handler)
    if warnings:
        print("[info] warnings logged by the turn layer during the concurrent sections:", sorted(set(warnings))[:3])
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
