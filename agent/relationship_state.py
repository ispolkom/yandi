"""
agent/relationship_state.py — CANONICAL RELATIONSHIP STATE of the personal
chat: how YANDI currently stands towards one person.

    EVENTS WRITE STATE.
    STATE DOES NOT INVENT EVENTS.
    THE LLM DOES NOT WRITE RELATIONSHIP STATE.

Coordinates (0..100), each with a different meaning:

  trust        how much she expects this person not to harm or deceive her.
  respect      how much this person's conduct keeps their worth to her as a
               partner in conversation. Insults hit it hardest.
  affection    how dear the interaction with THIS person is. Slow to grow.
  forgiveness_capacity
               how much recovery from harm is currently possible. Owned by
               agent/relationship_memory.py (its own table); read here so the
               four coordinates are one view.

The biography of what happened (grievances, apologies, healing) stays in
agent/relationship_memory.py. This module holds only the CURRENT stance and
the deterministic event -> delta rules (the persona's dynamics). Only
lifecycle events that were already validated upstream (a current-message
provenance-checked insult / an accepted apology) call the write functions, and
the model never supplies a coordinate.

APOLOGY != TRUST RESTORED: an accepted apology opens healing and gives back a
bounded part of the RESPECT that the offense cost; it never restores trust or
affection. Trust needs later behaviour, and no such event is validated yet,
so it is deliberately not inferred.

STORAGE. The person's row in the existing `inner_state` table (columns trust /
respect / affection) and an audit trail in `inner_state_event`, keyed by the
PERSON id ("owner"), never by a session id. The orchestrator's keyword-driven
InnerStateManager keeps using that table for session ids only and is not
connected to this module. A dedicated table needs DDL rights the runtime DB
user does not have; the storage is confined to _read_row()/_write_row() so it
can move without touching the rules.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agent.db.sql import repositories as repo

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, float] = {"trust": 50.0, "respect": 50.0, "affection": 30.0}
COORDINATES = tuple(DEFAULTS)

# Persona dynamics: how this personality's relationships move. Constants, not
# model output.
INSULT_RESPECT_PER_SEVERITY = 20.0
INSULT_TRUST_PER_SEVERITY = 8.0
INSULT_AFFECTION_PER_SEVERITY = 3.0
APOLOGY_RESPECT_RECOVERY_SHARE = 0.3  # of the respect the offense cost, scaled by sincerity


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _current(row: Dict[str, Any] | None) -> Dict[str, float]:
    row = row or {}
    return {c: float(row.get(c, DEFAULTS[c])) for c in COORDINATES}


def get_state(conn, user_id: str) -> Dict[str, Any]:
    """Read-only view of the four coordinates. A person with no history sits
    at the defaults; reading never writes."""
    state: Dict[str, Any] = _current(repo.get_inner_state(conn, user_id))
    state["forgiveness_capacity"] = float(repo.get_forgiveness_capacity(conn, user_id)["capacity"])
    return state


def _apply(conn, user_id: str, event_type: str, deltas: Dict[str, float], sincerity: float, weight: float) -> None:
    """Move the coordinates by `deltas` and append the audit event. A failure
    here must not break the grievance lifecycle it is attached to, but it is
    logged, never swallowed silently."""
    try:
        row = repo.get_or_create_inner_state(conn, user_id)
        before = _current(row)
        after = {c: _clamp(before[c] + deltas.get(c, 0.0)) for c in COORDINATES}
        repo.update_inner_state(conn, user_id, **after)
        note = " ".join(f"{c}{after[c] - before[c]:+.1f}" for c in COORDINATES if after[c] != before[c])
        repo.record_inner_state_event(conn, user_id, event_type, note[:255], sincerity=sincerity, weight=weight)
    except Exception:
        log.warning("relationship_state: could not apply %s for %s", event_type, user_id, exc_info=True)


def record_insult(conn, user_id: str, severity: float) -> None:
    """A validated insult event of the given severity (0..1)."""
    severity = max(0.0, min(1.0, severity))
    _apply(
        conn, user_id, "insult",
        {
            "respect": -INSULT_RESPECT_PER_SEVERITY * severity,
            "trust": -INSULT_TRUST_PER_SEVERITY * severity,
            "affection": -INSULT_AFFECTION_PER_SEVERITY * severity,
        },
        sincerity=0.0, weight=-severity,
    )


def record_accepted_apology(conn, user_id: str, offense_severity: float, sincerity: float) -> None:
    """A validated apology that was ACCEPTED (understood) for an offense of the
    given severity. Gives back only a bounded share of the respect that
    offense cost; trust and affection are untouched. Called once per offense
    cycle by relationship_memory.acknowledge_apology()."""
    offense_severity = max(0.0, min(1.0, offense_severity))
    sincerity = max(0.0, min(1.0, sincerity))
    recovery = APOLOGY_RESPECT_RECOVERY_SHARE * INSULT_RESPECT_PER_SEVERITY * offense_severity * sincerity
    _apply(conn, user_id, "apology_accepted", {"respect": recovery}, sincerity=sincerity, weight=recovery / 10.0)
