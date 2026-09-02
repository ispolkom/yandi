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

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from agent.db.sql import repositories as repo

MIN_HEALING_HOURS = 2.0
SINCERITY_AUTO_UNDERSTAND_THRESHOLD = 0.6
FORGIVENESS_MIN_SINCERITY = 0.4
FORGIVENESS_MIN_CAPACITY = 30.0
MAX_UNFORGIVEN_FOR_NEW_FORGIVENESS = 2


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _age_hours(row: Dict[str, Any]) -> float:
    created = row["created_at"]
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    return (_now() - created).total_seconds() / 3600.0


def add_grievance(
    conn, user_id: str, event_type: str, description: str, severity: float,
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """Registers a new grievance, or bumps an existing open one with the
    same description prefix — identical semantics to ForgivenessModel.
    add_grievance(). Returns the grievance id (new or bumped)."""
    severity = min(1.0, severity)
    existing = repo.find_similar_open_grievance(conn, user_id, description)
    if existing:
        # Faithful to the original ForgivenessModel.add_grievance(): a
        # RECURRENCE of an already-open grievance raises its own
        # severity, but does NOT independently re-charge
        # forgiveness_capacity — only a genuinely NEW grievance does
        # (below).
        new_severity = min(1.0, existing["severity"] + severity * 0.3)
        repo.bump_grievance(conn, existing["id"], new_severity)
        return existing["id"]

    grievance_id = f"g_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    repo.record_grievance(conn, grievance_id, user_id, event_type, description, severity, context)
    _adjust_capacity(conn, user_id, delta=-severity * 10)
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
    if sincerity > SINCERITY_AUTO_UNDERSTAND_THRESHOLD:
        repo.update_grievance_status(
            conn, grievance_id, "understood",
            apology_sincerity=sincerity, apology_at=now, understood_at=now,
        )
        _adjust_capacity(conn, grievance["user_id"], delta=sincerity * 5)
    else:
        repo.update_grievance_status(
            conn, grievance_id, "acknowledged",
            apology_sincerity=sincerity, apology_at=now,
        )
    return True


def progress_healing(conn, grievance_id: str) -> bool:
    """Advances the healing process one step. Returns True iff the
    grievance is (now, or already was) forgiven. Same six conditions as
    ForgivenessModel._check_forgiveness_conditions(): a real apology, a
    real understanding, sincerity >= 0.4, at least 2 hours elapsed,
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
    if _age_hours(grievance) < MIN_HEALING_HOURS:
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
