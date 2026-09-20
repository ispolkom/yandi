"""
agent/relationship_commitments.py — the PROMISE LEDGER of the personal chat:
what a person promised YANDI and what became of it, with an epistemic ladder
instead of a single "done" flag.

    USER SAID "I did it"      !=      YANDI KNOWS it was done      (TRUST != TRUTH)
    ONE CAUSAL EVENT          ->      ONE STATE TRANSITION
    THE LLM DOES NOT WRITE TRUST.    STATE DOES NOT INVENT EVENTS.

Lifecycle (append-only; the current status is FOLDED from the events, history
is never rewritten):

    commitment created                       status: open
      -> fulfillment_claimed (user_report)   status: reported_fulfilled  (no trust change: only words)
      -> verified_fulfilled  (a verifier)    status: verified_fulfilled  (trust up)
      -> verified_broken     (a verifier)    status: verified_broken     (trust down)

`overdue` is a derived flag (due date passed without a verified outcome); it
never moves anything by itself: an unverifiable or ambiguous deadline is not a
broken promise. Only a VERIFIED outcome calls relationship_state; a verifier is
any component that can establish the outcome independently of the person's own
words. None is wired into the personal chat yet, so today trust is moved only
through record_verification() by such a verifier.

Linking a claim to a promise is deterministic and never invents an event: it
picks the promise the CURRENT message is about (content overlap, or the only
open one). More than one candidate and no clear winner -> no target -> nothing
is written. Whether a message IS a promise or a claim comes only from the
model's provenance-checked state (pet/chat_local.py), never from keywords here.

UNIQUE (commitment_id, event_type) in the ledger makes one outcome exactly one
row; record_verification() moves the state only when that row was new, so the
same fulfilment can never raise trust twice.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from agent import causal_events
from agent import relationship_memory as rm
from agent import relationship_state
from agent.db.sql import repositories as repo

CLAIMED = "fulfillment_claimed"
VERIFIED_KEPT = "verified_fulfilled"
VERIFIED_BROKEN = "verified_broken"
USER_REPORT = "user_report"
MAX_FOCUS_CANDIDATES = 3

# Words that report "it is done" carry no identity of WHICH promise. They only
# take part in content-overlap scoring; they never decide that an event happened.
_FULFILMENT_FILLER = {
    rm._stem(w) for w in (
        "сделал сделала выполнил выполнила готово готов готова наконец уже всё все обещал обещала обещание "
        "как договаривались договорились исполнил исполнила закончил закончила завершил завершила"
    ).split()
}


def _now() -> datetime:
    return rm._now()


def _stems(text: Any) -> set:
    return rm._stems(text, drop_non_content=True) - _FULFILMENT_FILLER


def _fold(commitment: Dict[str, Any], events: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    kinds = {e["event_type"] for e in events}
    if VERIFIED_KEPT in kinds:
        status = "verified_fulfilled"
    elif VERIFIED_BROKEN in kinds:
        status = "verified_broken"
    elif CLAIMED in kinds:
        status = "reported_fulfilled"
    else:
        status = "open"
    due = rm._as_datetime(commitment.get("due_at"))
    overdue = bool(due and now > due and status in ("open", "reported_fulfilled"))
    return {**commitment, "status": status, "overdue": overdue}


def commitment_statuses(conn, user_id: str, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Every commitment of the person with its status folded from the events."""
    now = now or _now()
    by_commitment: Dict[str, List[Dict[str, Any]]] = {}
    for event in repo.list_commitment_events(conn, user_id):
        by_commitment.setdefault(event["commitment_id"], []).append(event)
    return [_fold(c, by_commitment.get(c["commitment_id"], []), now) for c in repo.list_commitments(conn, user_id)]


def create_commitment(
    conn, user_id: str, text: str, evidence: str, due_at=None, kind: str = "general",
    source_turn_id: Optional[str] = None, span: Optional[tuple] = None,
) -> Dict[str, Any]:
    """Record a promise the person made in the CURRENT message. The caller has
    already validated it (extraction step + verbatim evidence).

    Identity is CAUSAL, not textual: with a `source_turn_id` the promise is the
    causal event (turn, "promise"), so a retry of the same delivery creates
    nothing new, while the same words in another turn are a second promise
    (SAME TEXT != SAME EVENT). Returns {"commitment_id", "created"} and
    "duplicate": True when the delivery was already applied."""
    if not causal_events.may_apply(causal_events.claim(conn, user_id, source_turn_id, "promise", span)):
        return {"commitment_id": None, "created": False, "duplicate": True}
    commitment_id = f"c_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    repo.record_commitment(conn, commitment_id, user_id, kind, text, evidence, due_at=due_at, created_at=_now())
    return {"commitment_id": commitment_id, "created": True}


def resolve_commitment_focus(conn, user_id: str, current_text: str) -> Dict[str, Any]:
    """Which promise (if any) the CURRENT message is about, resolved before the
    reply so the reply and a later claim share one target.

    Read-only; never decides that the message is a claim. Policy:
      1. content overlap with a promise's own words, among promises that are
         open OR already reported (a repeated claim must find ITS promise, not
         another one): a unique best -> it; a tie -> ambiguous;
      2. no overlap with any: exactly one OPEN promise -> it; otherwise
         ambiguous.
    Returns {"commitment": row | None, "basis": str, "open_count": int,
    "candidates": [rows]} (candidates only when ambiguous). `commitment`
    carries its folded `status`."""
    items = commitment_statuses(conn, user_id)
    open_items = [c for c in items if c["status"] == "open"]
    live = [c for c in items if c["status"] in ("open", "reported_fulfilled")]
    if not live:
        return {"commitment": None, "basis": "no_open_commitment", "open_count": 0, "candidates": []}
    current = _stems(current_text)
    if current:
        scored = [(len(current & _stems(c["text"])) / len(current), c) for c in live]
        best = max(score for score, _ in scored)
        if best > 0:
            top = [c for score, c in scored if score == best]
            if len(top) == 1:
                return {"commitment": top[0], "basis": "explicit_reference", "open_count": len(open_items), "candidates": []}
            return {"commitment": None, "basis": "ambiguous", "open_count": len(open_items), "candidates": top[:MAX_FOCUS_CANDIDATES]}
    if len(open_items) == 1:
        return {"commitment": open_items[0], "basis": "sole_open_commitment", "open_count": 1, "candidates": []}
    if not open_items:
        return {"commitment": None, "basis": "no_open_commitment", "open_count": 0, "candidates": []}
    return {"commitment": None, "basis": "ambiguous", "open_count": len(open_items),
            "candidates": open_items[-MAX_FOCUS_CANDIDATES:]}


def _owned(conn, user_id: str, commitment_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not commitment_id:
        return None
    row = repo.get_commitment(conn, commitment_id)
    return row if row and row["user_id"] == user_id else None


def record_fulfillment_claim(
    conn, user_id: str, commitment_id: Optional[str], evidence: str,
    source_turn_id: Optional[str] = None, span: Optional[tuple] = None,
) -> Dict[str, Any]:
    """The person REPORTS having kept a promise (validated current message,
    verbatim evidence). Recorded once; changes no relationship coordinate,
    because a report is words, not verification. With a `source_turn_id` the
    report is the causal event (turn, "fulfilment_claim"): a retry adds nothing."""
    result = {"target": None, "recorded": False, "state_changed": False}
    if not _owned(conn, user_id, commitment_id):
        return result
    result["target"] = commitment_id
    if not causal_events.may_apply(causal_events.claim(conn, user_id, source_turn_id, "fulfilment_claim", span)):
        return result
    result["recorded"] = repo.record_commitment_event(conn, commitment_id, user_id, CLAIMED, USER_REPORT, evidence, created_at=_now())
    return result


def record_verification(
    conn, user_id: str, commitment_id: Optional[str], kept: bool, source: str, evidence: Optional[str] = None,
) -> Dict[str, Any]:
    """A VERIFIER established the outcome independently of the person's words.
    Appends the outcome and, only if that row is new, applies the one state
    transition. The person's own report is never a source here."""
    if not source or source == USER_REPORT:
        raise ValueError("a verified outcome needs a verifier; the person's own report is not verification")
    result = {"target": None, "recorded": False, "state_changed": False}
    commitment = _owned(conn, user_id, commitment_id)
    if not commitment:
        return result
    result["target"] = commitment_id
    kinds = {e["event_type"] for e in repo.list_commitment_events(conn, user_id) if e["commitment_id"] == commitment_id}
    if VERIFIED_KEPT in kinds or VERIFIED_BROKEN in kinds:
        return result  # already resolved: one commitment, one verified outcome
    event_type = VERIFIED_KEPT if kept else VERIFIED_BROKEN
    result["recorded"] = repo.record_commitment_event(conn, commitment_id, user_id, event_type, source, evidence, created_at=_now())
    if result["recorded"]:
        relationship_state.record_verified_commitment(conn, user_id, kept)
        result["state_changed"] = True
    return result
