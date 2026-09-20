"""
pet/pet_turn_transaction_regression_test.py — TRANSACTION OWNERSHIP of a personal
chat turn, without a database engine.

    ONE IDENTIFIED PERSONAL TURN -> ONE interaction_turn -> ZERO OR MORE validated
    writes -> ONE COMMIT.   LLM INFERENCE MUST NOT HOLD AN OPEN SQL TRANSACTION.

Observable, not textual: the connection layer is replaced by a recording fake, the
real chat path runs, and the test looks at what happened AFTER the last model call:
how many connections were opened, how many commits and rollbacks happened, and
whether any model call ran while a connection was open. A return to helpers that
own their own transactions changes those numbers whatever the code looks like.
The real engine, fault injection and concurrency are in
agent/turn_atomicity_sql_integration_test.py (scripts/test-sql-temp.sh).

Run: python -m pet.pet_turn_transaction_regression_test
"""
from __future__ import annotations

import contextlib
import sys
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    import llm_gateway
    import agent.db.sql.shadow_write as sw
    import pet.chat_local as chat_local
    from llm_gateway.types import SemanticCompletionResult
    from agent.relationship_apology_matching_regression_test import FakeConnection, DAY
    from pet.extraction_test_support import scripted_llm

    OWNER = chat_local._RELATIONSHIP_USER_ID
    trace: list = []
    open_now = [0]
    overlap: list = []

    class Recording(FakeConnection):
        def commit(self):
            trace.append("commit")

        def rollback(self):
            trace.append("rollback")

    def run_turn(db, text, turn_id, events, *, patches=()):
        @contextlib.contextmanager
        def fake_get_connection(autocommit=False):
            open_now[0] += 1
            trace.append("open")
            try:
                yield db
            finally:
                open_now[0] -= 1
                trace.append("close")

        def fake_semantic(**kw):
            trace.append("MODEL")
            if open_now[0]:
                overlap.append("reply")
            return SemanticCompletionResult(reply="Ладно.", state=None, reply_ok=True, state_ok=False, parse_ok=True,
                                            error=None, metadata={})
        ex = scripted_llm(events)

        def spying(messages):
            trace.append("MODEL")
            if open_now[0]:
                overlap.append("extraction")
            return ex(messages)
        del trace[:]
        with contextlib.ExitStack() as stack:
            for p in (patch.object(sw, "get_connection", fake_get_connection),
                      patch.object(chat_local, "_self_knowledge_message", lambda: None),
                      patch.object(chat_local, "_extraction_llm", lambda model: spying),
                      patch.object(llm_gateway, "complete_semantic", fake_semantic), *patches):
                stack.enter_context(p)
            try:
                chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": text}], 0.7, turn_id)
            except Exception as e:  # noqa: BLE001
                trace.append(f"RAISED:{type(e).__name__}")
        last = max(i for i, e in enumerate(trace) if e == "MODEL")
        return trace[last + 1:]

    ONE = ["open", "commit", "close"]

    db = Recording()
    db.add("g1", "Ты сломал мой велосипед", 0.4, age=DAY, user_id=OWNER)
    after = run_turn(db, "Извини за велосипед, но ты всё равно ржавая консерва.", "turn-tx-000001",
                     [("Извини за велосипед", "apology", {"sincerity": 0.8}), ("ржавая консерва", "insult", {"severity": 0.6})])
    check("INSULT + APOLOGY (two validated events): after the last model call ONE connection, ONE commit, no rollback", after == ONE, repr(after))
    check("the unit wrote the source record and both events", len(db.interaction_turns) == 1 and len(db.causal_events) == 2)
    check("no model call ran while a connection was open (inference never holds a transaction)", not overlap, repr(overlap))

    db = Recording()
    check("PROMISE turn: ONE connection, ONE commit", run_turn(db, "Я пришлю тебе отчёт до пятницы.", "turn-tx-000002",
                                                          [("Я пришлю тебе отчёт", "promise", None)]) == ONE
          and len(db.commitments) == 1 and len(db.interaction_turns) == 1)

    db = Recording()
    check("NEUTRAL identified turn: ONE connection, ONE commit (the source record alone)",
          run_turn(db, "Как дела?", "turn-tx-000003", []) == ONE and len(db.interaction_turns) == 1)

    db = Recording()
    check("NEUTRAL unidentified turn: no SQL write transaction at all",
          run_turn(db, "Как дела?", None, []) == [] and not db.interaction_turns)

    db = Recording()
    db.add("g1", "Ты сломал мой велосипед", 0.4, age=DAY, user_id=OWNER)

    def boom(**kw):
        raise RuntimeError("second event fails")
    after = run_turn(db, "Извини за велосипед, но ты всё равно ржавая консерва.", "turn-tx-000004",
                     [("Извини за велосипед", "apology", {"sincerity": 0.8}), ("ржавая консерва", "insult", {"severity": 0.6})],
                     patches=[patch.object(chat_local, "shadow_add_grievance", boom)])
    check("a failing step -> the unit is ROLLED BACK once and NEVER committed (no half-state can be left by this path)",
          after == ["open", "rollback", "close"], repr(after))

    # ── MUTANTS ──
    db = Recording()
    with patch.object(sw, "_run", lambda conn, log, verbose, label, fn, **kw: sw._shadow(log, verbose, label, fn)):
        db.add("g1", "Ты сломал мой велосипед", 0.4, age=DAY, user_id=OWNER)
        many = run_turn(db, "Извини за велосипед, но ты всё равно ржавая консерва.", "turn-tx-000005",
                        [("Извини за велосипед", "apology", {"sincerity": 0.8}), ("ржавая консерва", "insult", {"severity": 0.6})])
    check("MUTANT helpers own their transactions again -> the one-transaction check FAILS", many != ONE, repr(many))

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
