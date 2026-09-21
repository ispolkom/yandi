"""
agent/personal_facts.py — the PERSON'S OWN FACTS, remembered with provenance.

    USER REPORTED FACT != OBJECTIVE TRUTH (a fact means "the person said so").
    RAW TURN != DERIVED FACT.   FACT MUST HAVE A SOURCE TURN.
    OLD FACT IS HISTORY: A CORRECTION APPENDS, NEVER OVERWRITES.
    SAME TURN RETRY != NEW FACT HISTORY.   SAME TEXT IN ANOTHER TURN = A NEW OCCURRENCE.
    SESSION != PERSON.

`personal_fact` holds a fact's identity and first statement, `personal_fact_event`
everything that later happens to it (restated in another turn; superseded by a
correction). Nothing is ever updated or deleted: a fact's STATUS is folded from
its events when it is read:

    current      stated as current, never superseded
    historical   the person said it WAS so (temporality past) and it was never restated as current
    superseded   a later correction replaced it (the correcting fact and turn are recorded)

Identity is not raw-text identity: whether a statement restates or corrects a
known fact is the extractor's judgement over the known facts (validated: the target
must be one of the person's own current facts). As a backstop, the very same
NORMALISED proposition (the checked statement with its polarity and time) as a
current fact is one more occurrence of it (a `restated` event with the new turn's
evidence), never a second fact; two different wordings that the extractor does not
link stay two facts.

Facts are written only for an IDENTIFIED turn, in the same transaction as that
turn's interaction_turn (the source turn is a foreign key, so a fact cannot exist
without it) and claimed once per turn in causal_event ('personal_facts'), so a
retry applies nothing twice. Every function takes an open connection and does not
commit (repositories.py convention).

Retrieval is bounded and comes in two routes chosen by pet/fact_extraction.py's
classification of the message (a retrieval mode only; it never writes): a person
asking what is known about them, or about something in their own life, gets their
current profile (capped) plus a few historical facts marked as past; any other
message gets only the facts that share content with it. Neither route depends on
how long ago the source turn was or on the interaction window.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from agent import causal_events
from agent.db.sql import repositories as repo
from agent.relationship_memory import content_stems

CLAIM_TYPE = "personal_facts"   # one claim per (person, turn): the turn's facts are applied once

CURRENT, HISTORICAL, SUPERSEDED = "current", "historical", "superseded"

MAX_PROFILE_CURRENT = 15
MAX_PROFILE_HISTORICAL = 3
MAX_RELEVANT = 4
MIN_RELEVANCE = 0.18
MAX_STATEMENT_CHARS = 200


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fold(facts: Iterable[Dict[str, Any]], events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Facts newest first, each with its folded `status`, `restated` (number of
    later occurrences) and `superseded_by`. Pure: no I/O."""
    by_fact: Dict[str, List[Dict[str, Any]]] = {}
    for e in events:
        by_fact.setdefault(e["fact_id"], []).append(e)
    out = []
    for f in facts:
        evs = by_fact.get(f["fact_id"], [])
        superseded = next((e for e in evs if e["event_type"] == "superseded"), None)
        restated = [e for e in evs if e["event_type"] == "restated"]
        if superseded:
            status = SUPERSEDED
        elif f["temporality"] == "past":
            status = HISTORICAL
        else:
            status = CURRENT
        out.append({**f, "status": status, "restated": len(restated),
                    "superseded_by": superseded["by_fact_id"] if superseded else None})
    return out


def list_facts(conn, user_id: str) -> List[Dict[str, Any]]:
    """Every fact of this person with its folded status (newest first)."""
    return fold(repo.list_personal_facts(conn, user_id), repo.list_personal_fact_events(conn, user_id))


def known_for_linking(folded: Optional[List[Dict[str, Any]]], limit: int = 30) -> List[Dict[str, Any]]:
    """The person's CURRENT facts, as link targets for the extractor (newest first)."""
    return [{"fact_id": f["fact_id"], "statement": f["statement"]}
            for f in (folded or []) if f["status"] == CURRENT][:limit]


def record_turn_facts(conn, user_id: str, source_turn_id: Optional[str], facts: List[Any]) -> Dict[str, Any]:
    """Apply the validated facts of ONE identified turn (already in interaction_turn
    on this connection). Returns {"applied": bool, "new": n, "restated": n, "superseded": n}.
    applied=False: nothing to apply, or this turn's facts were already applied (a retry)."""
    result = {"applied": False, "new": 0, "restated": 0, "superseded": 0}
    if not facts:
        return result
    if not source_turn_id:
        raise ValueError("personal facts need the client-minted source turn id")
    if not causal_events.may_apply(causal_events.claim(conn, user_id, source_turn_id, CLAIM_TYPE)):
        return result
    current = {f["fact_id"]: f for f in list_facts(conn, user_id) if f["status"] == CURRENT}
    now = _now()
    for f in facts:
        target = current.get(f.target_fact_id) if f.target_fact_id else None
        if target is None and f.relation != "replaces":
            # the extractor did not link it, but it is the very proposition of a current fact: one more occurrence
            key = _proposition_key(f.statement, f.polarity, f.temporality)
            target = next((c for c in current.values()
                           if _proposition_key(c["statement"], c["polarity"], c["temporality"]) == key), None)
            if target is not None:
                f = replace(f, relation="same")
        if f.relation == "same" and target is not None and (
                _proposition_key(f.statement, f.polarity, f.temporality)
                != _proposition_key(target["statement"], target["polarity"], target["temporality"])):
            target = None      # the extractor called it a restatement, but it says something else: a new fact
        if f.relation == "same" and target is not None:
            repo.insert_personal_fact_event(
                conn, target["fact_id"], user_id, "restated", None, f.evidence, f.start, f.end, source_turn_id, created_at=now)
            result["restated"] += 1
            continue
        fact_id = f"pf_{int(now.timestamp())}_{uuid.uuid4().hex[:10]}"
        repo.insert_personal_fact(
            conn, fact_id, user_id, f.fact_class, f.statement[:MAX_STATEMENT_CHARS], f.polarity, f.temporality,
            f.evidence, f.start, f.end, source_turn_id, created_at=now)
        result["new"] += 1
        if f.relation == "replaces" and target is not None:
            repo.insert_personal_fact_event(
                conn, target["fact_id"], user_id, "superseded", fact_id, f.evidence, f.start, f.end, source_turn_id,
                created_at=now)
            current.pop(target["fact_id"], None)
            result["superseded"] += 1
    result["applied"] = True
    return result


def _proposition_key(statement: str, polarity: str, temporality: str) -> tuple:
    """Identity of a NORMALISED proposition (the checked, model-worded statement with its
    polarity and time), never of the raw message text: the same proposition stated in
    another turn is one more occurrence of the fact, not a second fact."""
    words = " ".join((statement or "").casefold().replace("ё", "е").split()).strip(" .,;:!?…")
    return words, polarity, temporality


def _relevance(current: set, fact: Dict[str, Any]) -> float:
    stems = content_stems(f"{fact['statement']} {fact['evidence']}")
    if not current or not stems:
        return 0.0
    shared = len(current & stems)
    return shared / math.sqrt(len(current) * len(stems)) if shared else 0.0


def select_for_prompt(
    folded: Optional[List[Dict[str, Any]]], current_text: str, *, profile: bool,
) -> List[Dict[str, Any]]:
    """The facts to state in the reply prompt. profile=True (the person asks what
    is known about them / about something in their life): the current facts, capped,
    plus a few historical ones. profile=False: only facts that share content with the
    message. Superseded facts (the person corrected them) are never stated."""
    if not folded:
        return []
    live = [f for f in folded if f["status"] != SUPERSEDED]
    if profile:
        current = [f for f in live if f["status"] == CURRENT][:MAX_PROFILE_CURRENT]
        historical = [f for f in live if f["status"] == HISTORICAL][:MAX_PROFILE_HISTORICAL]
        chosen = current + historical
    else:
        stems = content_stems(current_text)
        scored = [(_relevance(stems, f), f) for f in live]
        chosen = [f for score, f in sorted(scored, key=lambda t: t[0], reverse=True)
                  if score >= MIN_RELEVANCE][:MAX_RELEVANT]
    return [{
        "fact_id": f["fact_id"], "statement": f["statement"], "polarity": f["polarity"], "status": f["status"],
        "when": f["created_at"].strftime("%Y-%m-%d") if isinstance(f["created_at"], datetime) else "",
        "source_turn_id": f["source_turn_id"], "restated": f["restated"],
    } for f in chosen]
