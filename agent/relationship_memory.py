"""
agent/relationship_memory.py — SQL-backed character/relationship state
(owner mandate: "мне нужен у неё характер, она обидчива... простое
извини - не канает").

This is a faithful port of agent/forgiveness_model.py's ForgivenessModel
state-machine logic — SAME rules (registered->acknowledged->understood
->healing->forgiven/unforgiven, forgiveness_capacity gating, minimum
2-hour healing time before forgiveness can complete) — but backed by
the dedicated SQL instance (agent/db/sql/repositories.py's grievance/
forgiveness_capacity tables) instead of a plain JSON file under
registry/. agent/forgiveness_model.py and agent/inner_state.py (a
SECOND, incompatible JSON-based model found dormant in the same
codebase) are both left untouched — neither is wired into production,
and this module supersedes both for any NEW integration work.

WHY THIS HAD TO MOVE OUT OF A JSON FILE: this whole codebase's "10-year
bastion" work (Layers 1-4) exists specifically to make sure only
YANDI's own runtime process can change what she remembers — a plain
JSON file under registry/ is writable by any OS user with filesystem
access (the owner's own login, Claude Code, Codex, anything else
running as `iam`), which would make the bastion meaningless for
exactly this piece of memory: whether she's actually offended.

CALLER'S RESPONSIBILITY: every function here takes an already-open SQL
connection and does NOT commit — same convention as agent/db/sql/
repositories.py's own functions (see that module's docstring). The
caller (agent/message_intensity.py's integration into pet/chat_local.py)
owns the transaction.
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from agent import causal_events, relationship_state
from agent.db.sql import repositories as repo

MIN_HEALING_HOURS = 2.0
SINCERITY_AUTO_UNDERSTAND_THRESHOLD = 0.6
FORGIVENESS_MIN_SINCERITY = 0.4
FORGIVENESS_MIN_CAPACITY = 30.0
MAX_UNFORGIVEN_FOR_NEW_FORGIVENESS = 2


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _healing_age_hours(row: Dict[str, Any]) -> Optional[float]:
    """Hours since the CURRENT healing phase began: the first ACCEPTED
    (understood) apology of this offense cycle (`understood_at`). None if no
    apology has been accepted yet; a plain low-sincerity "sorry" that was only
    acknowledged does not start healing.

    This is a different clock from the AGE OF THE OFFENSE (`created_at`, or
    `updated_at` for a recurrence, see _offense_time()). An old grievance that
    is apologised for today has a healing age of about zero, not of its
    offense age."""
    started = row.get("understood_at")
    if isinstance(started, str):
        started = datetime.fromisoformat(started)
    if started is None:
        return None
    return (_now() - started).total_seconds() / 3600.0


def add_grievance(
    conn, user_id: str, event_type: str, description: str, severity: float,
    context: Optional[Dict[str, Any]] = None,
    source_turn_id: Optional[str] = None, span: Optional[tuple] = None,
) -> Optional[str]:
    """Registers a new grievance, or bumps an existing open one with the
    same description prefix — identical semantics to ForgivenessModel.
    add_grievance(). Returns the grievance id (new or bumped).

    With a `source_turn_id` the offense is a CAUSAL event (turn, event_type):
    a retry of the same delivery finds it already applied and returns None
    without touching the grievance, capacity or relationship state. A second,
    separately sent message with identical words has its own turn id and is
    applied (a recurrence, by the logic below). See agent/causal_events.py."""
    severity = min(1.0, severity)
    if not causal_events.may_apply(causal_events.claim(conn, user_id, source_turn_id, event_type, span)):
        return None
    existing = repo.find_similar_open_grievance(conn, user_id, description)
    if existing:
        # A RECURRENCE of an already-open grievance raises its own severity
        # and starts a new offense cycle. It costs forgiveness_capacity in
        # proportion to the severity it actually ADDED (a genuinely new
        # grievance costs its whole severity, below): otherwise repeating an
        # offense would be free while each cycle's apology and forgiveness
        # still restore capacity, so the continuous state would drift up
        # under repeated offenses.
        new_severity = min(1.0, existing["severity"] + severity * 0.3)
        repo.bump_grievance(conn, existing["id"], new_severity)
        _adjust_capacity(conn, user_id, delta=-(new_severity - existing["severity"]) * 10)
        relationship_state.record_insult(conn, user_id, severity)
        return existing["id"]

    grievance_id = f"g_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    repo.record_grievance(conn, grievance_id, user_id, event_type, description, severity, context)
    _adjust_capacity(conn, user_id, delta=-severity * 10)
    relationship_state.record_insult(conn, user_id, severity)
    return grievance_id


def acknowledge_apology(conn, grievance_id: str, sincerity: float) -> bool:
    """Records that an apology was heard. A sincere-enough apology
    (>0.6, same threshold as the original model) immediately advances
    to 'understood' and partially restores forgiveness_capacity — a
    PLAIN "sorry" with low measured sincerity stays at 'acknowledged'
    only, which is exactly the owner's own complaint this module exists
    to fix ("простое извини - не канает")."""
    grievance = repo.get_grievance(conn, grievance_id)
    if not grievance:
        return False
    now = _now()
    # The healing clock starts when an apology is first ACCEPTED (understood)
    # in this offense cycle: a repeated sincere apology neither restarts it
    # nor is ignored, and a plain "sorry" never starts it. A recurrence
    # resets the cycle (repo.bump_grievance).
    if sincerity > SINCERITY_AUTO_UNDERSTAND_THRESHOLD:
        first_acceptance = grievance.get("understood_at") is None
        repo.update_grievance_status(
            conn, grievance_id, "understood",
            apology_sincerity=sincerity, apology_at=now,
            understood_at=grievance.get("understood_at") or now,
        )
        if first_acceptance:
            # One accepted apology restores capacity once per offense cycle;
            # repeating it must not farm the continuous state.
            _adjust_capacity(conn, grievance["user_id"], delta=sincerity * 5)
            relationship_state.record_accepted_apology(conn, grievance["user_id"], grievance["severity"], sincerity)
    else:
        repo.update_grievance_status(
            conn, grievance_id, "acknowledged",
            apology_sincerity=sincerity, apology_at=now,
        )
    return True


def progress_healing(conn, grievance_id: str) -> bool:
    """Advances the healing process one step. Returns True iff the
    grievance is (now, or already was) forgiven. Same six conditions as
    ForgivenessModel._check_forgiveness_conditions() (except that the elapsed time is
    measured from the healing phase, not the offense): a real apology, a
    real understanding, sincerity >= 0.4, at least 2 hours since the healing phase
    began (the first accepted apology of this offense cycle, NOT the offense),
    forgiveness_capacity >= 30, and no more than 2 other unforgiven
    grievances outstanding."""
    grievance = repo.get_grievance(conn, grievance_id)
    if not grievance:
        return False
    if grievance["status"] in ("forgiven", "unforgiven"):
        return grievance["status"] == "forgiven"

    if _forgiveness_conditions_met(conn, grievance):
        now = _now()
        repo.update_grievance_status(conn, grievance_id, "forgiven", forgiven_at=now)
        capacity = repo.get_forgiveness_capacity(conn, grievance["user_id"])
        repo.set_forgiveness_capacity(
            conn, grievance["user_id"], min(100.0, capacity["capacity"] + 10), last_forgiveness=now,
        )
        return True

    if grievance["status"] in ("acknowledged", "understood"):
        repo.update_grievance_status(conn, grievance_id, "healing")
    return False


def _forgiveness_conditions_met(conn, grievance: Dict[str, Any]) -> bool:
    if grievance.get("apology_at") is None:
        return False
    if grievance.get("understood_at") is None:
        return False
    if grievance["apology_sincerity"] < FORGIVENESS_MIN_SINCERITY:
        return False
    healing_age = _healing_age_hours(grievance)
    if healing_age is None or healing_age < MIN_HEALING_HOURS:
        return False
    capacity = repo.get_forgiveness_capacity(conn, grievance["user_id"])
    if capacity["capacity"] < FORGIVENESS_MIN_CAPACITY:
        return False
    if repo.count_grievances_by_status(conn, grievance["user_id"], "unforgiven") > MAX_UNFORGIVEN_FOR_NEW_FORGIVENESS:
        return False
    return True


def _adjust_capacity(conn, user_id: str, delta: float) -> None:
    current = repo.get_forgiveness_capacity(conn, user_id)
    repo.set_forgiveness_capacity(conn, user_id, max(0.0, min(100.0, current["capacity"] + delta)))


def get_active_grievances(conn, user_id: str) -> List[Dict[str, Any]]:
    return repo.list_active_grievances(conn, user_id)


def get_summary(conn, user_id: str) -> Dict[str, Any]:
    active = get_active_grievances(conn, user_id)
    capacity = repo.get_forgiveness_capacity(conn, user_id)
    return {
        "active_grievances": len(active),
        "forgiven": repo.count_grievances_by_status(conn, user_id, "forgiven"),
        "unforgiven": repo.count_grievances_by_status(conn, user_id, "unforgiven"),
        "forgiveness_capacity": round(capacity["capacity"], 1),
        "last_forgiveness": capacity["last_forgiveness"],
    }



# Owner correction (verbatim, after live-testing against the real local
# model): "Она сперва должна определить, как к ней обращаются... но
# отвечать адекватно должна САМА... не как нам хочется, а как хочется
# ей." Even a "situation" narrative telling her how she's allowed to
# feel (an earlier version of this function) is still deciding FOR her.
# What actually belongs here is the RAW FACT of what happened — nothing
# more — pet/chat_local.py states it plainly in the system prompt and
# lets the model's own single generation both recognize the tone and
# react to it, in the same call that also produces her visible reply
# (agent/message_intensity.py parses her own self-report back out of
# that same generation afterward).
def memory_facts(grievance: Dict[str, Any]) -> Dict[str, Any]:
    """The raw facts pet/chat_local.py needs to state plainly: what was
    said, her own previously self-reported severity for it, and where
    the process currently stands."""
    return {
        "description": grievance["description"],
        "severity": grievance["severity"],
        "status": grievance["status"],
    }


def most_severe_active_grievance(conn, user_id: str) -> Optional[Dict[str, Any]]:
    active = get_active_grievances(conn, user_id)
    if not active:
        return None
    return max(active, key=lambda g: g["severity"])


# ============================================================
# APOLOGY -> GRIEVANCE MATCHING.
#
# A valid apology does NOT imply that the heaviest grievance is its
# target (that was the old rule: most_severe_active_grievance()). An
# apology is about a specific event, so the target is chosen from the
# apology's own text and the events' own record, deterministically and
# without a second model call:
#
#   1. explicit reference: the content words the apology names
#      ("...что назвал тебя бесполезной") overlap an ACTIVE grievance's
#      description. Best overlap wins. Equal overlap -> most recent
#      offense, then higher severity, then id (never random).
#   2. the apology names something that is ALREADY RESOLVED (overlaps a
#      forgiven/unforgiven grievance strictly better than any active
#      one) -> no target. It is never redirected to an unrelated open
#      grievance and never reopens the settled one.
#   3. generic / unmatched apology ("извини"): no invented link.
#        - exactly one active grievance -> that one;
#        - else exactly one active grievance whose offense is recent
#          (within APOLOGY_LOCALITY_HOURS of now) -> that one;
#        - else AMBIGUOUS -> no target, no state change.
#   4. no active grievance -> no target (an apology never creates one).
#
# The same selection runs BEFORE generation (resolve_relationship_focus) so
# the reply is built around the grievance that a later apology will
# actually change.
#
# Severity is only a late tie-break. Affection / forgiveness capacity /
# trust are deliberately NOT inputs: RELATIONAL STATE != EVENT IDENTITY.
# ============================================================

APOLOGY_LOCALITY_HOURS = 1.0
_EPOCH = datetime(1970, 1, 1)

_STEM_SUFFIXES = sorted(
    (
        "ыми", "ими", "ого", "его", "ому", "ему", "ами", "ями", "ала", "ила",
        "ой", "ый", "ий", "ая", "яя", "ое", "ее", "ую", "юю", "ые", "ие", "ых", "их", "ым", "им",
        "ом", "ем", "ов", "ев", "ей", "ою", "ею", "ам", "ям", "ах", "ях", "ал", "ил", "ли", "ть",
        "а", "я", "о", "е", "у", "ю", "ы", "и", "ь", "й", "л",
    ),
    key=len, reverse=True,
)

# Words that make an utterance an apology / describe the act of
# offending / locate it in time. They carry no identity of WHICH event.
_NON_CONTENT_WORDS = (
    "извини извините прости простите прошу прощения прощенье сожалею виноват виновата неправ неправа зря "
    "ошибся ошибалась ошибался ошибка обидел обидела обидные обиду обиделась обидеть оскорбил оскорбила "
    "оскорбление оскорблял назвал назвала обозвал обозвала сказал сказала слова слово сказанное "
    "что только тебя тебе тобой был была было это этого очень вообще просто сейчас потом тогда тоже если когда "
    "чтобы как так там вот еще все мне меня мной мои свои себя нее ней ты вы вас вам тут для про над без при "
    "или они она оно его них ним того тому том такой такое такая зачем почему недавно минуту назад раньше "
    "вчера сегодня надо нужно можно могу хочу хотел хотела был быть буду будет"
).split()


def _normalize(text: Any) -> str:
    return (str(text or "")).casefold().replace("ё", "е")


def _stem(token: str) -> str:
    for suffix in _STEM_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


_NON_CONTENT_STEMS = {_stem(w) for w in _NON_CONTENT_WORDS}


def _stems(text: Any, *, drop_non_content: bool) -> set:
    tokens = re.findall(r"[a-zа-я0-9]+", _normalize(text))
    stems = {_stem(t) for t in tokens}
    if drop_non_content:
        stems -= _NON_CONTENT_STEMS
    return {s for s in stems if len(s) >= 3}


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


def _offense_time(grievance: Dict[str, Any]) -> datetime:
    """When the offense last happened. A 'registered' grievance has had no
    apology transition, so its updated_at is its creation or its last
    recurrence (bump_grievance resets status to 'registered'). Any other
    status has updated_at moved by apology/healing, which is not an offense,
    so created_at is used."""
    created = _as_datetime(grievance.get("created_at")) or _EPOCH
    if grievance.get("status") == "registered":
        return _as_datetime(grievance.get("updated_at")) or created
    return created


def _overlap(apology_stems: set, grievance: Dict[str, Any]) -> float:
    if not apology_stems:
        return 0.0
    shared = apology_stems & _stems(grievance.get("description"), drop_non_content=False)
    return len(shared) / len(apology_stems)


class GrievanceMatch:
    """Outcome of match_grievance_target(): the chosen grievance (or None)
    and WHY, so the decision is inspectable and testable."""

    __slots__ = ("grievance", "basis", "candidates")

    def __init__(self, grievance: Optional[Dict[str, Any]], basis: str, candidates: int):
        self.grievance = grievance
        self.basis = basis
        self.candidates = candidates

    @property
    def grievance_id(self) -> Optional[str]:
        return self.grievance["id"] if self.grievance else None


def match_grievance_target(
    apology_text: str,
    active: List[Dict[str, Any]],
    resolved: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> GrievanceMatch:
    """Pure: picks at most ONE active grievance that the CURRENT message is
    about (an apology names an event; so does any other reference to the
    past). It never decides WHAT the message is (insult / apology / neutral);
    that stays with the validated model state. See the section comment
    above for the policy."""
    if not active:
        return GrievanceMatch(None, "no_active_grievance", 0)
    now = now or _now()

    apology_stems = _stems(apology_text, drop_non_content=True)
    if apology_stems:
        scored = [(_overlap(apology_stems, g), g) for g in active]
        best_active = max(score for score, _ in scored)
        best_resolved = max((_overlap(apology_stems, g) for g in (resolved or [])), default=0.0)
        if best_resolved > best_active:
            return GrievanceMatch(None, "names_resolved_grievance", len(active))
        if best_active > 0:
            top = [g for score, g in scored if score == best_active]
            if len(top) == 1:
                return GrievanceMatch(top[0], "explicit_reference", len(active))
            chosen = min(top, key=lambda g: (
                -(_offense_time(g) - _EPOCH).total_seconds(),
                -float(g.get("severity") or 0.0),
                str(g.get("id")),
            ))
            return GrievanceMatch(chosen, "explicit_reference_tie_recent", len(active))

    if len(active) == 1:
        return GrievanceMatch(active[0], "sole_active_grievance", 1)
    local = [g for g in active if (now - _offense_time(g)).total_seconds() / 3600.0 <= APOLOGY_LOCALITY_HOURS]
    if len(local) == 1:
        return GrievanceMatch(local[0], "sole_recent_grievance", len(active))
    return GrievanceMatch(None, "ambiguous", len(active))


MAX_FOCUS_CANDIDATES = 3


def resolve_relationship_focus(
    conn, user_id: str, current_text: str, now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Which open grievance (if any) the CURRENT user message is about,
    resolved BEFORE the reply is generated so the reply's memory context and
    a later apology write share one causal target.

    This only selects a historical target from the current text and the
    grievance ledger. It does not recognise an insult or an apology and
    never writes.

    Returns {"grievance": row | None, "basis": str, "open_count": int,
    "candidates": [rows]}; `candidates` is filled only for basis
    "ambiguous" (the most recent open grievances, newest first)."""
    active = get_active_grievances(conn, user_id)
    resolved = repo.list_recent_resolved_grievances(conn, user_id) if active else []
    match = match_grievance_target(current_text, active, resolved, now)
    candidates: List[Dict[str, Any]] = []
    if match.basis == "ambiguous":
        candidates = sorted(active, key=lambda g: _offense_time(g), reverse=True)[:MAX_FOCUS_CANDIDATES]
    return {"grievance": match.grievance, "basis": match.basis, "open_count": len(active), "candidates": candidates}


def apply_apology(
    conn, user_id: str, grievance_id: Optional[str], sincerity: float,
    source_turn_id: Optional[str] = None, span: Optional[tuple] = None,
) -> Dict[str, Any]:
    """Run the existing lifecycle (acknowledge -> progress healing) on the
    grievance the reply was already built around. `grievance_id` comes from
    resolve_relationship_focus(); with no target, or a target that is no
    longer an open grievance of this user, nothing is written. One call
    changes at most one grievance. With a `source_turn_id` the apology is a
    causal event (turn, "apology"): applying the same delivery again is a
    no-op (no second healing step, no second capacity or respect restoration)."""
    result = {"target": None, "acknowledged": False, "forgiven": False}
    if not grievance_id:
        return result
    grievance = repo.get_grievance(conn, grievance_id)
    if not grievance or grievance["user_id"] != user_id or grievance["status"] in ("forgiven", "unforgiven"):
        return result
    if not causal_events.may_apply(causal_events.claim(conn, user_id, source_turn_id, "apology", span)):
        return result
    result["target"] = grievance_id
    result["acknowledged"] = acknowledge_apology(conn, grievance_id, sincerity)
    result["forgiven"] = progress_healing(conn, grievance_id)
    return result
