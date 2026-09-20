"""
agent/personal_memory.py — LONG-TERM PERSONAL MEMORY that survives a restart, a
lost Redis and a change of language model.

    MODEL MAY CHANGE. MEMORY MUST SURVIVE.
    MEMORY IS FUNCTIONAL ONLY IF: STORED -> READ LATER -> CAUSALLY CHANGES BEHAVIOUR.
    MEMORY MAY AFFECT A REPLY; MEMORY MUST NOT BECOME A NEW USER EVENT.
    READING MEMORY != EXPERIENCING A NEW EVENT.
    ONE USER TURN -> ONE SOURCE RECORD. SAME TURN RETRIED != NEW HISTORY.
    SAME TEXT != SAME TURN. SESSION != PERSON.
    HISTORY MAY BE EXTENDED, NEVER SILENTLY REWRITTEN.

Two layers, deliberately not mixed:

  * SOURCE: `interaction_turn`, one immutable row per (person, source turn):
    what was said, what was answered, when, by which model. Append-only; a retry
    of the same turn is ignored, the same words in another turn are another row.
  * INTERPRETATION: which past turns matter for the message in front of us. It is
    COMPUTED at read time from the source rows and the causal_event ledger (the
    relationship events confirmed in the same turn); nothing derived is stored, so a
    better interpretation tomorrow rewrites nothing and a stale summary cannot
    replace what was actually said.

What is recalled is bounded (MAX_RECALLED turns, each cut short) and is chosen by
(a) relevance to the current message (shared content stems of what the PERSON
said, never of YANDI's own earlier replies, so a reply that once quoted a memory
cannot launder itself into relevance), (b) whether a relationship event was
confirmed in that turn, and (c) recency, with a couple of slots for the latest
exchanges (what a restarted process or a new model needs for continuity). Turns
the caller already has in its own context window and the current turn itself (a
retry) are never recalled.

Recalled memory goes into the reply generation only. It is never given to the
event extractor, never recorded as a new event, and the turn that used it stores
which earlier turns it was shown (`recalled_turn_ids`) so the provenance stays
auditable.

Every function takes an open SQL connection and does not commit (repositories.py
convention). The caller owns the transaction and the fail-open policy.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent.db.sql import repositories as repo
from agent.relationship_memory import content_stems

MAX_RECALLED = 4          # memories shown to the model for one reply
RECENT_SLOTS = 2          # of those, reserved for the latest exchanges (continuity)
RECENT_MAX_AGE_DAYS = 30  # a "recent" exchange older than this is only recalled by relevance
WINDOW = 300              # how many of the person's latest turns are considered
MIN_RELEVANCE = 0.18      # cosine of content stems (person's words vs. the current message)
EVENT_BONUS = 0.35        # a relationship event was confirmed in that turn
MAX_SIDE_CHARS = 240      # per side of a recalled exchange

SERVER = "server"   # turn id minted by the server: recorded, but no retry guarantee
CLIENT = "client"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def mint_server_turn_id() -> str:
    """A turn id for a request that carried none. It identifies the record, not a
    retry: two deliveries of an id-less request are two turns (UNKNOWN IDEMPOTENCY
    != IDEMPOTENT)."""
    return "srv-" + uuid.uuid4().hex


def record_turn(
    conn, user_id: str, source_turn_id: Optional[str], user_text: str, assistant_text: Optional[str],
    *, model: Optional[str] = None, adapter: Optional[str] = None,
    recalled_turn_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Append the source record of this turn. Returns
    {"recorded": bool, "source_turn_id": str, "origin": "client"|"server"}.
    `recorded` is False when this exact turn was already recorded (a retry):
    nothing is written or changed then."""
    origin = CLIENT if source_turn_id else SERVER
    turn_id = source_turn_id or mint_server_turn_id()
    recorded = repo.record_interaction_turn(
        conn, user_id, turn_id, origin, user_text, assistant_text, model=model, adapter=adapter,
        recalled_turn_ids=recalled_turn_ids, created_at=_now(),
    )
    return {"recorded": recorded, "source_turn_id": turn_id, "origin": origin}


def _clip(text: Optional[str]) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_SIDE_CHARS else text[: MAX_SIDE_CHARS - 1].rstrip() + "…"


def _relevance(current: set, past_user_text: str) -> float:
    past = content_stems(past_user_text)
    if not current or not past:
        return 0.0
    shared = len(current & past)
    return shared / math.sqrt(len(current) * len(past)) if shared else 0.0


def recall(
    conn, user_id: str, current_text: str, *, current_turn_id: Optional[str] = None,
    in_context_texts: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """The bounded, chronologically ordered memories relevant to this message.

    [] means "nothing worth recalling" (an empty history and an irrelevant one
    look the same here); an exception means UNKNOWN and is the caller's to
    handle. Each item: {interaction_id, source_turn_id, when, user_text,
    assistant_text, events, basis} with `basis` one of "relevant", "event",
    "recent"."""
    in_context = {" ".join((t or "").split()) for t in (in_context_texts or [])}
    now = _now()
    current = content_stems(current_text)

    candidates = []
    for row in repo.list_recent_interaction_turns(conn, user_id, WINDOW):
        if current_turn_id and row["source_turn_id"] == current_turn_id:
            continue  # the current turn itself (a retry): never its own memory
        if " ".join((row["user_text"] or "").split()) in in_context:
            continue  # already in the reply's own context window
        events = [e for e in (row.get("event_types") or "").split(",") if e]
        created = row["created_at"]
        age_days = max((now - created).total_seconds() / 86400.0, 0.0) if isinstance(created, datetime) else 1e9
        relevance = _relevance(current, row["user_text"])
        candidates.append({**row, "events": events, "age_days": age_days, "relevance": relevance})

    def score(c) -> float:
        return c["relevance"] + (EVENT_BONUS if c["events"] else 0.0) + 0.2 * math.exp(-c["age_days"] / 30.0)

    chosen: Dict[int, Dict[str, Any]] = {}
    recent = [c for c in candidates if c["age_days"] <= RECENT_MAX_AGE_DAYS][:RECENT_SLOTS]  # newest first
    for c in recent:
        chosen[c["interaction_id"]] = {**c, "basis": "recent"}
    eligible = [c for c in candidates if c["relevance"] >= MIN_RELEVANCE and c["interaction_id"] not in chosen]
    eligible.sort(key=score, reverse=True)
    for c in eligible:
        if len(chosen) >= MAX_RECALLED:
            break
        chosen[c["interaction_id"]] = {**c, "basis": "relevant"}
    if len(chosen) < MAX_RECALLED:
        # a turn where a relationship event was confirmed matters even when today's words do not overlap
        # with it, but only the freshest ones (the grievance/promise memory carries the durable part)
        for c in sorted((c for c in candidates if c["events"] and c["interaction_id"] not in chosen
                         and c["age_days"] <= RECENT_MAX_AGE_DAYS), key=score, reverse=True):
            if len(chosen) >= MAX_RECALLED:
                break
            chosen[c["interaction_id"]] = {**c, "basis": "event"}

    out = []
    for c in sorted(chosen.values(), key=lambda c: (c["created_at"], c["interaction_id"])):
        out.append({
            "interaction_id": c["interaction_id"], "source_turn_id": c["source_turn_id"],
            "when": c["created_at"].strftime("%Y-%m-%d") if isinstance(c["created_at"], datetime) else "",
            "user_text": _clip(c["user_text"]), "assistant_text": _clip(c["assistant_text"]) if c["assistant_text"] else None,
            "events": c["events"], "basis": c["basis"],
        })
    return out
