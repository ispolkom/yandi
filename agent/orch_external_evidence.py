"""
agent/orch_external_evidence.py — Delayed External Evidence.

PET_AGENT_BOUNDARY_AUDIT.md Phase 4C: minimal AGENT-owned adapter for
recording a validation result that arrives AFTER a request's response was
already returned and its trace persisted (pet's post-hoc DeepSeek/P2P/
local-model validation in chat_orch.py::_bg_validate). Agent owns the
interpretation and persistence of this event; pet performs only the
transport (asking external validators, already implemented via existing
agent/ functions - orch_node_selector/orch_validator/orch_arbiter/
orch_ai_validator) and reports the raw result here, linked by trace_id.

Scope, deliberately minimal (per the customer's own Phase 4C decision,
and "НЕ ТРОГАТЬ ПОКА: ... Self-learning"): this records the delayed event
and links it to the original trace by trace_id, capturing the original
canonical Trust for later before/after comparison. It does NOT recompute
canonical Trust automatically and does NOT mutate the original trace -
that is roadmap Phase I-2/I-3 (Delayed Supervision / Outcome Revision)
territory, explicitly out of scope here. "Trust не меняется только ради
consensus" (the customer's own words) is satisfied trivially in this
minimal version: nothing in this module ever changes Trust: it only
appends an immutable, trace-linked observation for a future, separately-
scoped Delayed Supervision mechanism to consume.

"ТОЧКА НОЛЬ" v13 (owner mandate, 2026-09): registry/dataset/
delayed_validation/{day}.jsonl is retired, not migrated. State now lives
in delayed_validation_event (class B, append-only) — agent/db/sql/
schema.py. find_trace_by_id() now queries verification_run/question_
occurrence directly instead of linearly scanning JSONL day-files.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every
function here.
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


def find_trace_by_id(trace_id: str) -> Optional[dict]:
    """Find the persisted run + its question text by trace_id (== SQL
    run_id). Returns a small dict shaped like the OLD JSONL trace's own
    top-level fields (trace_id, trust) — the only two fields this
    module's own caller (record_delayed_validation) ever read off it."""
    if not trace_id:
        return None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT vr.run_id, aa.canonical_trust FROM verification_run vr "
                "LEFT JOIN answer_assessment aa ON aa.run_id = vr.run_id "
                "WHERE vr.run_id=%s ORDER BY aa.created_at DESC LIMIT 1",
                (trace_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {"trace_id": row["run_id"], "trust": row.get("canonical_trust")}


def record_delayed_validation(
    trace_id: str,
    source: str,
    verdict: str,
    reason: str = "",
    raw: str = "",
) -> dict:
    """Persist a delayed external validation event, linked to its
    originating trace by id (if found - trace_id may be empty or the
    run may not exist; both are recorded honestly via trace_found, not
    silently dropped or faked).

    Never mutates the original run/Trust - see this module's docstring
    for why that boundary is deliberate, not an oversight.

    Returns the recorded event dict, so the caller (pet) can project it
    into a UI without re-deriving anything or computing its own verdict.
    """
    trace = find_trace_by_id(trace_id)
    event_id = f"dv_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    event = {
        "event_id":       event_id,
        "trace_id":       trace_id,
        "trace_found":    trace is not None,
        "original_trust": trace.get("trust") if trace else None,
        "source":         source,
        "verdict":        verdict,
        "reason":         reason,
        "raw":            (raw or "")[:2000],
        "recorded_at":    time.time(),
    }

    with get_connection() as conn:
        repo.record_delayed_validation_event(
            conn, event_id, trace_id or None, trace is not None,
            trace.get("trust") if trace else None, source, verdict, reason=reason, raw=raw,
        )
        conn.commit()

    return event


def get_delayed_validations(trace_id: str, max_files: int = 30) -> list[dict]:
    """Вернуть все delayed-validation события для данного trace_id
    (для UI/будущего self-learning: 'что происходило с этой трассой
    после того, как ответ был отдан'). `max_files` kept as a parameter
    name for call-site compatibility — now just the row limit."""
    if not trace_id:
        return []
    with get_connection() as conn:
        rows = repo.list_delayed_validation_events(conn, trace_id, limit=max_files)
    return [
        {
            "event_id": r["event_id"], "trace_id": r["run_id"], "trace_found": bool(r["trace_found"]),
            "original_trust": r.get("original_trust"), "source": r["source"], "verdict": r["verdict"],
            "reason": r.get("reason"), "raw": r.get("raw"), "recorded_at": r["created_at"],
        }
        for r in rows
    ]
