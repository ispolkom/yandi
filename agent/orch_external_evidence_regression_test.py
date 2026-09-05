"""
agent/orch_external_evidence_regression_test.py — Foundation Repair /
PET_AGENT_BOUNDARY_AUDIT.md Phase 4C regression:
orch_external_evidence.py's find_trace_by_id/record_delayed_validation/
get_delayed_validations.

Root cause covered here: pet/chat_orch.py::_bg_validate() previously
computed and owned the epistemic verdict of a delayed (post-hoc)
external validation itself, mutating only a Redis-cached UI history
record with no durable, trace-linked record and no way for agent (or a
future self-learning system) to ever learn that a delayed check
happened. This module is the minimal AGENT-owned adapter that closes
that gap: it does NOT recompute canonical Trust and does NOT mutate the
original trace/run - it only persists an immutable, trace-linked
observation.

"ТОЧКА НОЛЬ" v13 (owner mandate, 2026-09): registry/dataset/orch_traces/
*.jsonl + registry/dataset/delayed_validation/*.jsonl are retired, not
migrated. find_trace_by_id() now queries verification_run/
answer_assessment; record/get_delayed_validations now use
delayed_validation_event (class B, append-only) — agent/db/sql/
schema.py. A small fake connection stands in for the real
bastion-protected tables.

Run: /home/iam/venv/bin/python3 -m agent.orch_external_evidence_regression_test
"""
from __future__ import annotations

import contextlib

import agent.orch_external_evidence as oee

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


class _FakeCursor:
    lastrowid = 1

    def __init__(self, conn):
        self.conn = conn
        self._result = None
        self._results = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        upper = " ".join(sql.split()).upper()
        self._result = None
        self._results = None
        c = self.conn
        if upper.startswith("SELECT VR.RUN_ID, AA.CANONICAL_TRUST FROM VERIFICATION_RUN"):
            (run_id,) = params
            self._result = dict(c.runs[run_id]) if run_id in c.runs else None
        elif upper.startswith("INSERT INTO DELAYED_VALIDATION_EVENT"):
            event_id, run_id, trace_found, original_trust, source, verdict, reason, raw, created_at = params
            c.events.append({
                "event_id": event_id, "run_id": run_id, "trace_found": trace_found,
                "original_trust": original_trust, "source": source, "verdict": verdict,
                "reason": reason, "raw": raw, "created_at": created_at,
            })
        elif upper.startswith("SELECT * FROM DELAYED_VALIDATION_EVENT WHERE RUN_ID=%S ORDER BY CREATED_AT DESC LIMIT %S"):
            run_id, limit = params
            rows = [dict(e) for e in c.events if e["run_id"] == run_id][::-1][:limit]
            self._results = rows

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results or []


class _FakeConnection:
    def __init__(self):
        self.runs = {}
        self.events = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass


conn = _FakeConnection()


@contextlib.contextmanager
def _fake_get_connection(autocommit=False):
    yield conn


oee.get_connection = _fake_get_connection

# ── seed a fake persisted run+trust, matching what record_question_and_run
#    + complete_run would have written for a real request ──
conn.runs["trace_1787830000_deadbeef"] = {"run_id": "trace_1787830000_deadbeef", "canonical_trust": "PARTIALLY_SUPPORTED"}
conn.runs["trace_other"] = {"run_id": "trace_other", "canonical_trust": "UNVERIFIED"}

# ── find_trace_by_id: real trace_id found ──
found = oee.find_trace_by_id("trace_1787830000_deadbeef")
check(
    "find_trace_by_id locates a real, seeded run by exact id",
    found is not None and found.get("trust") == "PARTIALLY_SUPPORTED",
    f"found={found}",
)

# ── find_trace_by_id: unknown id -> None, not a crash/fabrication ──
missing = oee.find_trace_by_id("trace_does_not_exist")
check(
    "find_trace_by_id returns None (not a fabricated result) for "
    "an id that isn't in any run",
    missing is None,
    f"missing={missing}",
)

# ── find_trace_by_id: empty id -> None immediately, no query ──
check(
    "find_trace_by_id short-circuits on an empty trace_id",
    oee.find_trace_by_id("") is None,
    "",
)

# ── record_delayed_validation: trace found -> captures original_trust ──
event = oee.record_delayed_validation(
    trace_id="trace_1787830000_deadbeef",
    source="deepseek",
    verdict="VERIFIED",
    reason="DeepSeek подтвердил ответ",
    raw="Да, ответ верный и полный.",
)
check(
    "record_delayed_validation captures trace_found=True and the "
    "ORIGINAL trust from the run it linked to",
    event["trace_found"] is True and event["original_trust"] == "PARTIALLY_SUPPORTED",
    f"event={event}",
)
check(
    "the recorded event carries source/verdict/reason/raw exactly "
    "as passed, plus its own event_id and recorded_at",
    event["source"] == "deepseek" and event["verdict"] == "VERIFIED"
    and event["reason"] == "DeepSeek подтвердил ответ"
    and "event_id" in event and "recorded_at" in event,
    f"event={event}",
)

# ── the event is actually persisted to SQL, not just returned ──
check(
    "record_delayed_validation actually persists to delayed_validation_event, "
    "not just returns an in-memory dict",
    len(conn.events) == 1,
    f"events={conn.events}",
)
check(
    "the persisted record matches what was returned",
    conn.events[0]["event_id"] == event["event_id"],
    f"persisted={conn.events[0]}",
)

# ── record_delayed_validation: unknown/empty trace_id -> honest,
#    not fabricated (trace_found=False, original_trust=None) ──
orphan_event = oee.record_delayed_validation(
    trace_id="", source="local_ollama", verdict="PARTIALLY_VERIFIED",
    reason="проверка недоступна", raw="",
)
check(
    "a delayed validation with no resolvable trace_id is still "
    "recorded (never silently dropped), but honestly marked "
    "trace_found=False rather than fabricating a link",
    orphan_event["trace_found"] is False and orphan_event["original_trust"] is None,
    f"orphan_event={orphan_event}",
)

# ── CRITICAL invariant: recording an event never mutates the
#    original run/trust row ──
check(
    "recording delayed validations never mutates the original run's "
    "canonical_trust (agent's canonical Trust computation is not "
    "touched by this module - by design, see its docstring)",
    conn.runs["trace_1787830000_deadbeef"]["canonical_trust"] == "PARTIALLY_SUPPORTED"
    and conn.runs["trace_other"]["canonical_trust"] == "UNVERIFIED",
    f"runs={conn.runs}",
)

# ── get_delayed_validations: returns events for a given trace_id,
#    excludes events for other trace_ids ──
oee.record_delayed_validation(
    trace_id="trace_1787830000_deadbeef", source="local_ollama",
    verdict="PARTIALLY_VERIFIED", reason="второй прогон", raw="",
)
oee.record_delayed_validation(
    trace_id="trace_other", source="deepseek",
    verdict="REJECTED", reason="не про эту трассу", raw="",
)
events_for_trace = oee.get_delayed_validations("trace_1787830000_deadbeef")
check(
    "get_delayed_validations returns exactly the events recorded "
    "for this trace_id (2 so far) and excludes trace_other's event",
    len(events_for_trace) == 2
    and all(e["trace_id"] == "trace_1787830000_deadbeef" for e in events_for_trace),
    f"events_for_trace={events_for_trace}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
