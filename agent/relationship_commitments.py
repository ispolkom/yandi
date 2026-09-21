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
words. Exactly one is wired into the personal chat: a promise to deliver
something IN the chat (kind in_chat), whose deliverable then appears in a later
message of the person (pet/commitment_verification.py finds it,
record_direct_fulfilment() writes it). A promise about the world (paying,
sending, going somewhere) can only ever be REPORTED here: YANDI has not observed
it. record_verification() stays the generic entry for a future independent
verifier. Broken promises are not inferred from silence or from a passed
deadline.

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

KIND_IN_CHAT = "in_chat"        # fulfilment is the delivery of something IN the chat: YANDI can observe it directly
KIND_EXTERNAL = "external"      # fulfilment happens in the world: only a report can ever reach YANDI (never verifiable here)
KIND_GENERAL = "general"        # unclassified (promises made before verification existed): treated as external
VERIFIER_IN_CHAT = "in_chat_direct"
MAX_VERIFIABLE = 5

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
    conn, user_id: str, text: str, evidence: str, due_at=None, kind: str = KIND_GENERAL,
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
    repo.record_commitment(conn, commitment_id, user_id, kind, text, evidence, due_at=due_at, created_at=_now(),
                           source_turn_id=source_turn_id)
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


def verifiable_commitments(conn, user_id: str, current_turn_id: Optional[str]) -> List[Dict[str, Any]]:
    """The promises whose fulfilment YANDI could observe directly in a later chat message: classified in_chat, not yet
    verified, and NOT made in the current turn (a promise is never fulfilled by the very message that made it; a retry of
    the turn that made it must not verify it either). Oldest first, at most MAX_VERIFIABLE (the newest ones)."""
    rows = commitment_statuses(conn, user_id)
    if rows and "source_turn_id" not in rows[0]:
        return []       # schema v18 not applied: no provenance, so nothing can be verified
    live = [c for c in rows
            if c["kind"] == KIND_IN_CHAT and c["status"] in ("open", "reported_fulfilled")
            and not (current_turn_id and c.get("source_turn_id") == current_turn_id)]
    return live[-MAX_VERIFIABLE:]


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
    result["recorded"] = repo.record_commitment_event(
        conn, commitment_id, user_id, CLAIMED, USER_REPORT, evidence, created_at=_now(),
        source_turn_id=source_turn_id, span_start=span[0] if span else None, span_end=span[1] if span else None)
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


def record_direct_fulfilment(
    conn, user_id: str, commitment_id: Optional[str], evidence: str, source_turn_id: Optional[str],
    span: Optional[tuple] = None,
) -> Dict[str, Any]:
    """YANDI OBSERVED the promised deliverable in the current identified turn: `evidence` is the exact span of that turn's
    message (reconstructed by code, judged by a blind check upstream). Appends the verified event WITH its provenance
    (source turn and span) and, only if that row is new, applies the one bounded trust transition.

    This proves that the promised in-chat action was performed. It proves nothing about the world. Refused (nothing
    written): no identified turn; a commitment that is not the person's, not classified in_chat, made in this very turn or
    already resolved; a turn with no immutable source record, or whose stored text does not contain `evidence` at `span`
    (the provenance is checked against the record, not against the caller); a turn that already verified something (one
    evidence span verifies at most one commitment: the claim is the causal event (turn, "commitment_verified")).

    ALL-OR-NOTHING with its caller's transaction: nothing here swallows an error, so a failure after the verified event
    (or in the trust transition) propagates and the caller rolls the whole turn back. Returns
    {"target", "recorded", "state_changed", "reward"}."""
    result = {"target": None, "recorded": False, "state_changed": False, "reward": 0.0}
    if not source_turn_id or not span or not evidence:
        return result   # no identified turn, no provenance, no verification
    # The reward depends on how many proofs the person already gave, and the coordinates are read-modify-write. The person's
    # state row is locked BEFORE anything below is read (a locking read sees the latest committed state; the plain reads that
    # follow then take their snapshot after any concurrent verification has committed, so two concurrent turns cannot both
    # be "the first proof"). The ledger is append-only, so the count itself cannot be a locking read. A caller that already
    # read in this transaction locks first itself (agent/db/sql/shadow_write.shadow_lock_relationship_state).
    repo.get_or_create_inner_state(conn, user_id, for_update=True)
    commitment = _owned(conn, user_id, commitment_id)
    if not commitment or commitment.get("kind") != KIND_IN_CHAT or commitment.get("source_turn_id") == source_turn_id:
        return result
    kinds = {e["event_type"] for e in repo.list_commitment_events(conn, user_id) if e["commitment_id"] == commitment_id}
    if VERIFIED_KEPT in kinds or VERIFIED_BROKEN in kinds:
        return result       # already resolved: one commitment, one verified outcome
    turn_text = repo.get_interaction_turn_text(conn, user_id, source_turn_id)
    if turn_text is None or turn_text[span[0]:span[1]] != evidence:
        return result       # the evidence is not, byte for byte, in the immutable record of the turn it claims to come from
    if not causal_events.may_apply(causal_events.claim(conn, user_id, source_turn_id, "commitment_verified", span)):
        return result       # this turn already verified something (or this is a retry of it)
    result["target"] = commitment_id
    prior = repo.count_commitment_events(conn, user_id, VERIFIED_KEPT, VERIFIER_IN_CHAT)
    result["recorded"] = repo.record_commitment_event(
        conn, commitment_id, user_id, VERIFIED_KEPT, VERIFIER_IN_CHAT, evidence, created_at=_now(),
        source_turn_id=source_turn_id, span_start=span[0], span_end=span[1], require_provenance=True)
    if result["recorded"]:
        result["reward"] = relationship_state.record_observed_commitment(conn, user_id, prior)
        result["state_changed"] = True
    return result
