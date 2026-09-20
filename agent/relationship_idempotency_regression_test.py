"""
agent/relationship_idempotency_regression_test.py — CAUSAL EVENT IDEMPOTENCY.

    SAME TEXT != SAME EVENT.
    SAME SOURCE EVENT RETRIED != NEW EVENT.
    ONE CAUSAL EVENT -> ONE LEDGER ENTRY -> ONE STATE TRANSITION.
    HISTORY IS APPEND-ONLY: A RETRY MUST NOT CREATE HISTORY.

The causal identity of a relationship event is (person, source turn id, event
type), claimed with one atomic INSERT IGNORE on a unique key in the same
transaction as the state writes it guards (agent/causal_events.py). Here it is
exercised on an in-memory fake SQL connection (the unique key is emulated by a
lock); the same protocol is proved against a real SQL engine by
agent/relationship_idempotency_sql_integration_test.py.

The scenarios are also run against deliberately WRONG implementations (mutants),
which they must catch: without idempotency, with identity derived from the text,
and with a check-then-insert race.

Run: python -m agent.relationship_idempotency_regression_test
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import pickle
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import agent.causal_events as ce
import agent.db.sql.repositories as repo
import agent.relationship_commitments as rc
import agent.relationship_memory as rm
import agent.relationship_state as rs
from agent.relationship_apology_matching_regression_test import FakeConnection

FAILURES: list[str] = []
P = "person_a"
T0 = datetime(2026, 6, 1, 12, 0, 0)
INSULT_TEXT = "Ты просто ржавая консерва, от тебя никакого толку"


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def audit(conn, kind):
    return [e for e in conn.inner_state_events if e["event_type"] == kind]


# ── scenarios: each returns True when the invariant holds ─────────────────────

def scenario_insult_retry() -> bool:
    c = FakeConnection()
    a = rm.add_grievance(c, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-A-0001", span=(4, 26))
    b = rm.add_grievance(c, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-A-0001", span=(4, 26))
    st = rs.get_state(c, P)
    return (a is not None and b is None and len(c.grievances) == 1
            and abs(next(iter(c.grievances.values()))["severity"] - 0.6) < 1e-9
            and abs(st["respect"] - 38.0) < 1e-9 and abs(st["forgiveness_capacity"] - 44.0) < 1e-9
            and len(audit(c, "insult")) == 1)


def scenario_same_text_two_turns() -> bool:
    c = FakeConnection()
    rm.add_grievance(c, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-A-0001")
    second = rm.add_grievance(c, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-B-0002")
    st = rs.get_state(c, P)
    g = next(iter(c.grievances.values()))
    return (second is not None and len(c.grievances) == 1 and g["severity"] > 0.6   # a recurrence, by the existing logic
            and len(audit(c, "insult")) == 2 and st["respect"] < 38.0)


def scenario_race() -> bool:
    c = FakeConnection()
    n_threads, results = 8, []
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()
        results.append(rm.add_grievance(c, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-RACE-0001"))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    [t.start() for t in threads]; [t.join() for t in threads]
    return len([r for r in results if r is not None]) == 1 and len(audit(c, "insult")) == 1


# ── mutants: deliberately wrong designs the scenarios must catch ──────────────

def mutant_no_idempotency():
    return patch.object(ce, "claim", lambda conn, user_id, source_turn_id, event_type, span=None: ce.NEW)


def mutant_identity_from_text():
    orig = rm.add_grievance

    def by_text(conn, user_id, event_type, description, severity, context=None, source_turn_id=None, span=None):
        return orig(conn, user_id, event_type, description, severity, context,
                    source_turn_id="txt-" + hashlib.md5(description.encode()).hexdigest(), span=span)
    return patch.object(rm, "add_grievance", by_text)


def mutant_check_then_insert():
    def racy(conn, user_id, source_turn_id, event_type, span=None):
        exists = any(e["user_id"] == user_id and e["source_turn_id"] == source_turn_id and e["event_type"] == event_type
                     for e in conn.causal_events)
        time.sleep(0.02)  # the window between SELECT and INSERT
        if exists:
            return ce.DUPLICATE
        conn.causal_events.append({"user_id": user_id, "source_turn_id": source_turn_id, "event_type": event_type})
        return ce.NEW
    return patch.object(ce, "claim", racy)


def main() -> int:
    # ── 1. relationship events: insult ──
    check("A: INSULT RETRY (same turn, twice) -> one grievance, one capacity charge, one state transition, one audit entry",
          scenario_insult_retry())
    check("B: REAL REPEATED INSULT (same words, two turns) -> two legitimate applications; the second is a recurrence",
          scenario_same_text_two_turns())

    # ── apology ──
    c = FakeConnection()
    gid = rm.add_grievance(c, P, "insult", "Ты сломал мой велосипед", 0.6, source_turn_id="turn-ins-0001")
    before = rs.get_state(c, P)
    first = rm.apply_apology(c, P, gid, 0.9, source_turn_id="turn-apo-0001", span=(0, 6))
    mid = rs.get_state(c, P)
    again = rm.apply_apology(c, P, gid, 0.9, source_turn_id="turn-apo-0001", span=(0, 6))
    after = rs.get_state(c, P)
    check("C: APOLOGY RETRY -> healing, capacity and respect restored ONCE",
          first["acknowledged"] and again["target"] is None and after == mid
          and mid["respect"] > before["respect"] and mid["forgiveness_capacity"] > before["forgiveness_capacity"]
          and len(audit(c, "apology_accepted")) == 1)
    later = rm.apply_apology(c, P, gid, 0.9, source_turn_id="turn-apo-0002")
    check("C: a second, separately sent apology is a second event (applied), yet cannot farm the state (first acceptance only)",
          later["acknowledged"] and rs.get_state(c, P) == mid)

    # ── promise / claim ──
    c = FakeConnection()
    p1 = rc.create_commitment(c, P, "Я завтра сделаю отчёт", "Я завтра сделаю", source_turn_id="turn-pr-0001", span=(0, 15))
    p2 = rc.create_commitment(c, P, "Я завтра сделаю отчёт", "Я завтра сделаю", source_turn_id="turn-pr-0001", span=(0, 15))
    p3 = rc.create_commitment(c, P, "Я завтра сделаю отчёт", "Я завтра сделаю", source_turn_id="turn-pr-0002", span=(0, 15))
    check("D: PROMISE RETRY -> one commitment", p1["created"] and not p2["created"] and p2["duplicate"])
    check("D: the same words in ANOTHER turn are another promise (identity is causal, not textual)", p3["created"] and len(c.commitments) == 2)
    st0 = rs.get_state(c, P)
    r1 = rc.record_fulfillment_claim(c, P, p1["commitment_id"], "Я сделал отчёт", source_turn_id="turn-cl-0001")
    r2 = rc.record_fulfillment_claim(c, P, p1["commitment_id"], "Я сделал отчёт", source_turn_id="turn-cl-0001")
    check("E: FULFILMENT-REPORT RETRY -> one report event, and a report moves no coordinate",
          r1["recorded"] and not r2["recorded"] and len([e for e in c.commitment_events if e["event_type"] == rc.CLAIMED]) == 1
          and rs.get_state(c, P) == st0)

    # ── multi-event turn ──
    c = FakeConnection()
    gid = rm.add_grievance(c, P, "insult", "Ты сломал мой велосипед", 0.4, source_turn_id="turn-old-0001")
    n_before = len(c.causal_events)
    rm.apply_apology(c, P, gid, 0.9, source_turn_id="turn-MIX-0001", span=(0, 6))
    rm.add_grievance(c, P, "insult", "Ты всё равно ржавая консерва и всё", 0.6, source_turn_id="turn-MIX-0001", span=(20, 40))
    snapshot = (c.snapshot(), rs.get_state(c, P), len(c.inner_state_events))
    types = sorted(e["event_type"] for e in c.causal_events[n_before:])
    check("F: INSULT + APOLOGY in ONE turn are two causal events with the same turn id (both applied)", types == ["apology", "insult"], repr(types))
    rm.apply_apology(c, P, gid, 0.9, source_turn_id="turn-MIX-0001", span=(0, 6))
    rm.add_grievance(c, P, "insult", "Ты всё равно ржавая консерва и всё", 0.6, source_turn_id="turn-MIX-0001", span=(20, 40))
    check("F: retrying the whole mixed turn applies nothing more",
          (c.snapshot(), rs.get_state(c, P), len(c.inner_state_events)) == snapshot)

    # ── evidence-span drift on retry cannot create a second event ──
    c = FakeConnection()
    rm.add_grievance(c, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-DRIFT-01", span=(4, 26))
    again = rm.add_grievance(c, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-DRIFT-01", span=(0, 30))
    check("G: a retry whose re-run extraction picked a slightly different span is still the same causal event",
          again is None and len(audit(c, "insult")) == 1 and c.causal_events[0]["span_start"] == 4)

    # ── restart: the guarantee lives in storage, not in process memory ──
    c1 = FakeConnection()
    rm.add_grievance(c1, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-RST-0001")
    with tempfile.NamedTemporaryFile(suffix=".pkl") as f:
        pickle.dump({k: getattr(c1, k) for k in ("grievances", "capacities", "inner_state", "inner_state_events",
                                                 "commitments", "commitment_events", "causal_events")}, f)
        f.flush()
        del c1  # "process 1" ends
        c2 = FakeConnection()  # "process 2": a fresh object that only sees what was persisted
        f.seek(0)
        for k, v in pickle.load(f).items():
            setattr(c2, k, v)
    ce._unavailable_warned = False
    replay = rm.add_grievance(c2, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-RST-0001")
    check("H: RESTART — the same source turn applied by a new process is a no-op",
          replay is None and len(c2.grievances) == 1 and len(audit(c2, "insult")) == 1
          and abs(rs.get_state(c2, P)["respect"] - 38.0) < 1e-9)
    newer = rm.add_grievance(c2, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-RST-0002")
    check("H: ...while a NEW turn after the restart is still an event", newer is not None and len(audit(c2, "insult")) == 2)

    # ── concurrency ──
    check("I: CONCURRENT deliveries of the same turn (8 threads at once) -> exactly one application", scenario_race())

    # ── unknown idempotency is reported, never faked ──
    c = FakeConnection()
    for _ in range(2):
        rm.add_grievance(c, P, "insult", INSULT_TEXT, 0.6)  # no turn id
    check("J: NO stable turn id -> UNKNOWN IDEMPOTENCY != IDEMPOTENT: nothing is deduplicated (and never by text)",
          len(audit(c, "insult")) == 2 and c.causal_events == [])
    c = FakeConnection()
    missing = type("NoSuchTable", (Exception,), {})(1146, "Table 'yandi_epistemic.causal_event' doesn't exist")
    with patch.object(repo, "claim_causal_event", side_effect=missing), patch.object(ce.log, "warning") as warn:
        ce._unavailable_warned = False
        a = rm.add_grievance(c, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-NOV15-01")
        b = rm.add_grievance(c, P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-NOV15-01")
    check("J: ledger table missing (schema v15 not applied) -> events still applied as before, one warning, no guarantee claimed",
          a is not None and b is not None and warn.call_count == 1)
    with patch.object(repo, "claim_causal_event", side_effect=RuntimeError("connection lost")):
        try:
            rm.add_grievance(FakeConnection(), P, "insult", INSULT_TEXT, 0.6, source_turn_id="turn-ERR-0001")
            swallowed = True
        except RuntimeError:
            swallowed = False
    check("J: any other database error propagates (the surrounding transaction rolls back, nothing half-applied)", not swallowed)

    # ── structure: causal identity, never text ──
    src = inspect.getsource(ce)
    check("K: the identity module never hashes or compares message text",
          not any(w in src for w in ("hashlib", "md5", "sha1", "sha256", "hash(")) and
          list(inspect.signature(ce.claim).parameters) == ["conn", "user_id", "source_turn_id", "event_type", "span"])
    repo_src = inspect.getsource(repo.claim_causal_event)
    check("K: the claim is a single atomic INSERT IGNORE (no SELECT-then-INSERT)",
          repo_src.count("INSERT IGNORE INTO causal_event") == 1 and repo_src.count("cur.execute(") == 1)

    # ── the tests must catch wrong implementations ──
    with mutant_no_idempotency():
        check("M1: MUTANT 'no idempotency' is CAUGHT by the retry scenario", not scenario_insult_retry())
    with mutant_identity_from_text():
        check("M2: MUTANT 'identity derived from the text' is CAUGHT by the same-text-two-turns scenario", not scenario_same_text_two_turns())
    check("M2: the correct implementation passes the same-text-two-turns scenario", scenario_same_text_two_turns())
    with mutant_check_then_insert():
        check("M3: MUTANT 'check then insert' is CAUGHT by the concurrency scenario", not scenario_race())

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
