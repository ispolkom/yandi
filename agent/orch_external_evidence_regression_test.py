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
original trace - it only persists an immutable, trace-linked observation.

Uses isolated temp directories for both TRACES_DIR and EVENTS_DIR (never
touches the real registry/dataset/orch_traces or
registry/dataset/delayed_validation directories).

Run: /home/iam/venv/bin/python3 -m agent.orch_external_evidence_regression_test
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

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


_orig_traces_dir = oee.TRACES_DIR
_orig_events_dir = oee.EVENTS_DIR

with tempfile.TemporaryDirectory() as tmp_dir:
    traces_dir = Path(tmp_dir) / "orch_traces"
    events_dir = Path(tmp_dir) / "delayed_validation"
    traces_dir.mkdir()
    events_dir.mkdir()
    oee.TRACES_DIR = traces_dir
    oee.EVENTS_DIR = events_dir

    try:
        # ── seed a fake persisted trace, matching the real day-bucketed
        #    jsonl layout ──
        real_trace = {
            "trace_id": "trace_1787830000_deadbeef",
            "query": "Что такое DHT?",
            "trust": "PARTIALLY_SUPPORTED",
            "timestamp": 1787830000.0,
        }
        with open(traces_dir / "20260827.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps(real_trace) + "\n")
            f.write(json.dumps({"trace_id": "trace_other", "trust": "UNVERIFIED"}) + "\n")

        # ── find_trace_by_id: real trace_id found ──
        found = oee.find_trace_by_id("trace_1787830000_deadbeef")
        check(
            "find_trace_by_id locates a real, seeded trace by exact id",
            found is not None and found.get("trust") == "PARTIALLY_SUPPORTED",
            f"found={found}",
        )

        # ── find_trace_by_id: unknown id -> None, not a crash/fabrication ──
        missing = oee.find_trace_by_id("trace_does_not_exist")
        check(
            "find_trace_by_id returns None (not a fabricated result) for "
            "an id that isn't in any trace file",
            missing is None,
            f"missing={missing}",
        )

        # ── find_trace_by_id: empty id -> None immediately, no scan ──
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
            "ORIGINAL trust from the trace it linked to",
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

        # ── the event is actually persisted to disk, not just returned ──
        day_files = list(events_dir.glob("*.jsonl"))
        check(
            "record_delayed_validation actually persists to EVENTS_DIR "
            "(day-bucketed jsonl), not just returns an in-memory dict",
            len(day_files) == 1,
            f"files={day_files}",
        )
        persisted = json.loads(day_files[0].read_text().splitlines()[0])
        check(
            "the persisted record on disk matches what was returned",
            persisted["event_id"] == event["event_id"],
            f"persisted={persisted}",
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
        #    original trace file ──
        trace_file_after = (traces_dir / "20260827.jsonl").read_text()
        trace_file_expected = (
            json.dumps(real_trace) + "\n" +
            json.dumps({"trace_id": "trace_other", "trust": "UNVERIFIED"}) + "\n"
        )
        check(
            "recording delayed validations never mutates the original "
            "trace file on disk (agent's canonical Trust computation is "
            "not touched by this module - by design, see its docstring)",
            trace_file_after == trace_file_expected,
            f"after={trace_file_after!r}",
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

    finally:
        oee.TRACES_DIR = _orig_traces_dir
        oee.EVENTS_DIR = _orig_events_dir

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
