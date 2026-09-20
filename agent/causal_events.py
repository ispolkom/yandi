"""
agent/causal_events.py — CAUSAL IDENTITY of relationship events.

    SAME TEXT != SAME EVENT.
    SAME SOURCE EVENT RETRIED != NEW EVENT.
    ONE CAUSAL EVENT -> ONE LEDGER ENTRY -> ONE STATE TRANSITION.
    HISTORY IS APPEND-ONLY: A RETRY MUST NOT CREATE HISTORY.

A source turn is one delivery of one user message, identified by an id the
client mints when the message is created (`turn_id`). The causal event is
(person, source turn, event type): an insult and an apology in the same turn
are two events; the same insult delivered twice is one. Two separately sent
messages with identical words carry two different turn ids and are two events
(a legitimate recurrence). Nothing here hashes or compares message text.

The claim is one atomic INSERT IGNORE on a unique key (see the causal_event DDL
and repositories.claim_causal_event), performed in the SAME transaction as the
state writes it guards: a retry of an event that was fully applied finds the
row and applies nothing, a transaction that rolled back leaves no row so the
event can still be applied later, and two concurrent deliveries are serialised
by the unique key rather than by a check-then-insert.

Honest limits, reported instead of hidden (UNKNOWN IDEMPOTENCY != IDEMPOTENT):
  * `unstable` -- the caller has no stable source turn id (a client that does
    not send one): the event is applied exactly as before, without a guarantee.
  * `unavailable` -- the ledger table does not exist yet (schema v15 not applied):
    the event is applied without a guarantee, with a one-time warning.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from agent.db.sql import repositories as repo

log = logging.getLogger("yandi.events")

NEW = "new"                # first application of this causal event: apply it
DUPLICATE = "duplicate"    # already applied: apply nothing
UNSTABLE = "unstable"      # no stable source turn id: applied without a guarantee
UNAVAILABLE = "unavailable"  # ledger missing (schema v15 not applied): applied without a guarantee

_unavailable_warned = False


def _is_missing_table(exc: BaseException) -> bool:
    args = getattr(exc, "args", ())
    if args and args[0] == 1146:  # MySQL ER_NO_SUCH_TABLE
        return True
    text = str(exc).lower()
    return "doesn't exist" in text or "does not exist" in text


def is_missing_table(exc: BaseException) -> bool:
    """True for "this table does not exist (yet)": a schema that is not applied."""
    return _is_missing_table(exc)


def claim(
    conn, user_id: str, source_turn_id: Optional[str], event_type: str,
    span: Optional[Tuple[int, int]] = None,
) -> str:
    """Decide whether this causal event may be applied now. Returns NEW,
    DUPLICATE, UNSTABLE or UNAVAILABLE; only DUPLICATE forbids applying."""
    global _unavailable_warned
    if not source_turn_id:
        log.debug("causal event %s: no stable source turn id, applied without a guarantee", event_type)
        return UNSTABLE
    start, end = span if span else (None, None)
    try:
        is_new = repo.claim_causal_event(conn, user_id, source_turn_id, event_type, start, end)
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_table(exc):
            raise
        if not _unavailable_warned:
            _unavailable_warned = True
            log.warning("causal_event ledger unavailable (schema v15 not applied?): "
                        "relationship events are applied without retry protection")
        return UNAVAILABLE
    outcome = NEW if is_new else DUPLICATE
    log.info("causal event %s type=%s turn=%s", "applied" if is_new else "duplicate (no-op)", event_type, source_turn_id)
    return outcome


def may_apply(status: str) -> bool:
    return status != DUPLICATE
