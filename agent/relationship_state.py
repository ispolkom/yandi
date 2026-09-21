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
affection. Trust is restored by BEHAVIOUR: a promise whose fulfilment was
VERIFIED (agent/relationship_commitments.py): today only a delivery observed directly in the chat, worth
a small, shrinking, ceilinged amount of trust. A person's own report of having
kept a promise is recorded but moves nothing (TRUST != TRUTH).

ONE CAUSAL EVENT -> ONE STATE TRANSITION: every write below is called exactly
once per event; the audit trail (`inner_state_event`) carries each event's
machine-readable magnitude, so replay() rebuilds the current state from the
trail alone, with the same pure delta functions the live path uses.

STORAGE. The person's row in the existing `inner_state` table (columns trust /
respect / affection) and an audit trail in `inner_state_event`, keyed by the
PERSON id ("owner"), never by a session id. The orchestrator's keyword-driven
InnerStateManager keeps using that table for session ids only and is not
connected to this module. A dedicated table needs DDL rights the runtime DB
user does not have; the storage is confined to get_state()/_apply() and the
replay reader so it can move without touching the rules.
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
# A promise whose fulfilment YANDI OBSERVED DIRECTLY IN THE CHAT (the deliverable itself is in a later message)
# proves only trivial reliability: giving a word or a number when you said you would costs nothing. It moves TRUST
# only, by a small amount that shrinks with every earlier such proof and can never lift trust above a ceiling, so
# many trivial promises cannot buy a relationship (TRIVIAL PROMISE FARMING MUST NOT DOMINATE TRUST). Serious harm
# (an insult costs up to 8 trust, a verified broken promise 15) stays far larger than one such reward.
OBSERVED_TRUST_BASE = 2.0
OBSERVED_TRUST_DECAY = 0.6       # reward for the k-th earlier proof: BASE * DECAY**k (the total is bounded: BASE / (1 - DECAY) = 5)
OBSERVED_TRUST_MIN_REWARD = 0.05  # below this a further proof changes nothing
OBSERVED_TRUST_CEILING = 60.0
KEPT_TRUST, KEPT_RESPECT, KEPT_AFFECTION = 8.0, 3.0, 0.0        # a verified kept promise proves reliability
BROKEN_TRUST, BROKEN_RESPECT, BROKEN_AFFECTION = -15.0, -5.0, -1.0  # a verified broken one hurts trust most


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


def _insult_deltas(severity: float) -> Dict[str, float]:
    severity = max(0.0, min(1.0, severity))
    return {
        "respect": -INSULT_RESPECT_PER_SEVERITY * severity,
        "trust": -INSULT_TRUST_PER_SEVERITY * severity,
        "affection": -INSULT_AFFECTION_PER_SEVERITY * severity,
    }


def _apology_deltas(offense_severity: float, sincerity: float) -> Dict[str, float]:
    offense_severity = max(0.0, min(1.0, offense_severity))
    sincerity = max(0.0, min(1.0, sincerity))
    return {"respect": APOLOGY_RESPECT_RECOVERY_SHARE * INSULT_RESPECT_PER_SEVERITY * offense_severity * sincerity}


def _commitment_deltas(kept: bool) -> Dict[str, float]:
    if kept:
        return {"trust": KEPT_TRUST, "respect": KEPT_RESPECT, "affection": KEPT_AFFECTION}
    return {"trust": BROKEN_TRUST, "respect": BROKEN_RESPECT, "affection": BROKEN_AFFECTION}


def observed_trust_reward(prior_observed: int) -> float:
    """The trust a directly observed in-chat delivery is worth, given how many such proofs the person already
    gave: bounded, geometrically shrinking, zero once negligible. Pure."""
    reward = OBSERVED_TRUST_BASE * OBSERVED_TRUST_DECAY ** max(0, int(prior_observed))
    return round(reward, 4) if reward >= OBSERVED_TRUST_MIN_REWARD else 0.0


def _observed_deltas(reward: float, trust_before: float) -> Dict[str, float]:
    """Trust only (no respect, no affection), and never above the ceiling."""
    return {"trust": max(0.0, min(reward, OBSERVED_TRUST_CEILING - trust_before))}


def _apply(
    conn, user_id: str, event_type: str, deltas: Dict[str, float], sincerity: float, weight: float, strict: bool = False,
) -> None:
    """Move the coordinates by `deltas` and append the audit event. A failure
    here must not break the grievance lifecycle it is attached to, but it is
    logged, never swallowed silently. `strict` is for a transition that is the
    PROOF of another write (a verified fulfilment): its failure must reach the
    caller's transaction so that the proof is rolled back with it, never kept
    without its consequence."""
    try:
        row = repo.get_or_create_inner_state(conn, user_id)
        before = _current(row)
        after = {c: _clamp(before[c] + deltas.get(c, 0.0)) for c in COORDINATES}
        repo.update_inner_state(conn, user_id, **after)
        note = " ".join(f"{c}{after[c] - before[c]:+.1f}" for c in COORDINATES if after[c] != before[c])
        repo.record_inner_state_event(conn, user_id, event_type, note[:255], sincerity=sincerity, weight=weight)
    except Exception:
        if strict:
            raise
        log.warning("relationship_state: could not apply %s for %s", event_type, user_id, exc_info=True)


def record_insult(conn, user_id: str, severity: float) -> None:
    """A validated insult event of the given severity (0..1). The audit row's
    weight is the event's magnitude: -severity."""
    severity = max(0.0, min(1.0, severity))
    _apply(conn, user_id, "insult", _insult_deltas(severity), sincerity=0.0, weight=-severity)


def record_accepted_apology(conn, user_id: str, offense_severity: float, sincerity: float) -> None:
    """A validated apology that was ACCEPTED (understood) for an offense of the
    given severity. Gives back only a bounded share of the respect that
    offense cost; trust and affection are untouched. Called once per offense
    cycle by relationship_memory.acknowledge_apology(). The audit row keeps
    the offense severity (weight) and the sincerity, which is all replay needs."""
    offense_severity = max(0.0, min(1.0, offense_severity))
    sincerity = max(0.0, min(1.0, sincerity))
    _apply(
        conn, user_id, "apology_accepted", _apology_deltas(offense_severity, sincerity),
        sincerity=sincerity, weight=offense_severity,
    )


def record_verified_commitment(conn, user_id: str, kept: bool) -> None:
    """A commitment whose outcome was VERIFIED (not merely reported). Called
    exactly once per commitment by relationship_commitments.record_verification()."""
    _apply(
        conn, user_id, "commitment_kept" if kept else "commitment_broken", _commitment_deltas(kept),
        sincerity=1.0, weight=1.0 if kept else -1.0,
    )


def record_observed_commitment(conn, user_id: str, prior_observed: int) -> float:
    """A commitment whose fulfilment was observed DIRECTLY in the chat (verified by
    relationship_commitments.record_direct_fulfilment, exactly once per commitment). Returns the reward
    (before the ceiling). The audit row's weight is that reward, which is all replay needs."""
    reward = observed_trust_reward(prior_observed)
    row = repo.get_or_create_inner_state(conn, user_id, for_update=True)
    _apply(conn, user_id, "commitment_observed", _observed_deltas(reward, _current(row)["trust"]), sincerity=1.0, weight=reward,
           strict=True)
    return reward


def replay_from_events(events) -> Dict[str, float]:
    """Rebuild trust/respect/affection from the audit trail alone (oldest
    first), from the defaults, with the same pure delta rules and clamping as
    the live path. The materialised row is a cache of this fold."""
    state = dict(DEFAULTS)
    for event in events:
        kind, weight, sincerity = event["event_type"], float(event["weight"]), float(event["sincerity"])
        if kind == "insult":
            deltas = _insult_deltas(-weight)
        elif kind == "apology_accepted":
            deltas = _apology_deltas(weight, sincerity)
        elif kind in ("commitment_kept", "commitment_broken"):
            deltas = _commitment_deltas(kind == "commitment_kept")
        elif kind == "commitment_observed":
            deltas = _observed_deltas(weight, state["trust"])
        else:
            continue
        state = {c: _clamp(state[c] + deltas.get(c, 0.0)) for c in COORDINATES}
    return state


def replay(conn, user_id: str) -> Dict[str, float]:
    return replay_from_events(repo.list_inner_state_events_in_order(conn, user_id))
