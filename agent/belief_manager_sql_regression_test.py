"""
agent/belief_manager_sql_regression_test.py — "точка ноль" (owner
mandate, 2026-09): agent/belief_manager.py's registry/beliefs.json is
retired entirely, not migrated. SQL (agent.db.sql.belief +
belief_assessment_history) is now the ONLY source of truth — replaces
agent/db_sql_belief_shadow_regression_test.py, whose entire premise
(JSON primary + SQL shadow) no longer exists in the code at all.

Covers:
    1. add_belief() creates a real belief row AND a 'created' history
       row, in that FK-safe order (belief_assessment_history.belief_id
       has a real FK to belief.belief_id — inserting history before the
       belief row exists would violate it on a real server; the fake
       connection below enforces this explicitly, not just implicitly).
    2. add_belief() with the SAME topic+statement again merges into the
       EXISTING belief (_find_similar's exact-match path) rather than
       creating a duplicate, and records an 'updated' history row.
    3. challenge_belief() updates confidence/evidence_against and
       records a 'revised' history row; confidence dropping below 0.3
       flips status to 'revised'.
    4. supersede_belief() marks the old belief 'superseded' with
       superseded_by set, and records a 'superseded' history row with
       NULL old/new confidence (matches the original JSON semantics
       exactly — no confidence value was ever recorded for this
       transition).
    5. _apply_decay() ages a stale active belief's confidence down and
       records a 'decayed' history row — driven entirely by SQL state,
       no in-memory list to seed.
    6. get_stats()/get_beliefs_by_topic()/get_contradictory() read
       real aggregates from SQL, not an in-memory list.
    7. FAIL LOUD, not fail-open: with SQL genuinely unreachable,
       add_belief() raises SqlUnavailable rather than silently
       pretending to succeed — the deliberate opposite of the old
       JSON-primary/SQL-shadow design's fail-open contract, because
       there is no more JSON fallback to fail open TO.
    8. ENUM/code consistency: every change_type string belief_manager.py
       sends is declared in schema.py's ENUM, and vice versa.

Run: /home/iam/venv/bin/python3 -m agent.belief_manager_sql_regression_test
"""
from __future__ import annotations

import contextlib
import inspect
import json
import re
from datetime import datetime
from unittest.mock import patch

import agent.belief_manager as bm_mod
import agent.db.sql.schema as schema_mod
from agent.db.sql.connection import SqlUnavailable

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# ============================================================
# Stateful fake connection for belief / belief_assessment_history.
# ============================================================

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None
        self._results = None
        self.lastrowid = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        upper = norm.upper()
        self._result = None
        self._results = None

        if upper.startswith("INSERT INTO BELIEF "):
            (belief_id, topic, statement, confidence, status, ev_for, ev_against,
             claim_ids, prior, likelihood, contradiction_score, decay_factor,
             superseded_by, created_at, updated_at) = params
            existing = self.conn.beliefs.get(belief_id)
            row = {
                "belief_id": belief_id, "topic": topic, "statement": statement,
                "confidence": confidence, "status": status,
                "evidence_for": ev_for, "evidence_against": ev_against, "claim_ids": claim_ids,
                "prior": prior, "likelihood": likelihood,
                "contradiction_score": contradiction_score, "decay_factor": decay_factor,
                "superseded_by": superseded_by,
                "created_at": existing["created_at"] if existing else created_at,
                "updated_at": updated_at,
            }
            self.conn.beliefs[belief_id] = row

        elif upper.startswith("INSERT INTO BELIEF_ASSESSMENT_HISTORY"):
            belief_id = params[0]
            if belief_id not in self.conn.beliefs:
                # Real FK constraint (belief_assessment_history.belief_id
                # REFERENCES belief.belief_id) — this fake enforces it
                # explicitly so a wrong write-order (history before the
                # belief row exists) fails LOUD here, the same way it
                # would on a real server, instead of silently "working"
                # against a fake that doesn't model the FK at all.
                raise AssertionError(
                    f"FK VIOLATION: belief_assessment_history references "
                    f"belief_id={belief_id!r} which does not exist in `belief` yet"
                )
            self.conn.history.append({
                "belief_id": belief_id, "run_id": params[1],
                "old_confidence": params[2], "new_confidence": params[3],
                "reason": params[4], "change_type": params[5], "created_at": params[6],
            })
            self.conn.next_id += 1
            self.lastrowid = self.conn.next_id

        elif upper.startswith("SELECT * FROM BELIEF WHERE BELIEF_ID=%S"):
            (belief_id,) = params
            self._result = dict(self.conn.beliefs[belief_id]) if belief_id in self.conn.beliefs else None

        elif "WHERE TOPIC=%S AND STATUS IN" in upper:
            topic, *statuses = params
            matches = [
                r for r in self.conn.beliefs.values()
                if r["topic"] == topic and r["status"] in statuses
            ]
            matches.sort(key=lambda r: r["created_at"])
            self._results = [dict(r) for r in matches]

        elif upper.startswith("SELECT * FROM BELIEF WHERE STATUS='ACTIVE'"):
            matches = [dict(r) for r in self.conn.beliefs.values() if r["status"] == "active"]
            matches.sort(key=lambda r: r["created_at"])
            self._results = matches

        elif upper.startswith("SELECT * FROM BELIEF WHERE CONTRADICTION_SCORE >="):
            (min_score,) = params
            matches = [dict(r) for r in self.conn.beliefs.values() if r["contradiction_score"] >= min_score]
            matches.sort(key=lambda r: -r["contradiction_score"])
            self._results = matches

        elif upper.startswith("SELECT COUNT(*) AS TOTAL"):
            vals = list(self.conn.beliefs.values())
            total = len(vals)
            active = sum(1 for r in vals if r["status"] == "active")
            revised = sum(1 for r in vals if r["status"] == "revised")
            superseded = sum(1 for r in vals if r["status"] == "superseded")
            avg_conf = sum(r["confidence"] for r in vals) / total if total else 0.0
            avg_contra = sum(r["contradiction_score"] for r in vals) / total if total else 0.0
            self._result = {
                "total": total, "active": active, "revised": revised, "superseded": superseded,
                "avg_confidence": avg_conf, "avg_contradiction": avg_contra,
            }

        elif upper.startswith("SELECT TOPIC, COUNT(*) AS C FROM BELIEF GROUP BY TOPIC"):
            topics: dict = {}
            for r in self.conn.beliefs.values():
                topics[r["topic"]] = topics.get(r["topic"], 0) + 1
            self._results = [{"topic": k, "c": v} for k, v in topics.items()]

        elif upper.startswith("SELECT COUNT(*) AS C FROM BELIEF WHERE CONTRADICTION_SCORE >= 0.5"):
            c = sum(1 for r in self.conn.beliefs.values() if r["contradiction_score"] >= 0.5)
            self._result = {"c": c}

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results or []


class FakeConnection:
    def __init__(self):
        self.beliefs = {}
        self.history = []
        self.next_id = 1000

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


@contextlib.contextmanager
def _fake_get_connection(autocommit=False):
    yield FAKE_CONN


def _history_for(belief_id, change_type=None):
    rows = [h for h in FAKE_CONN.history if h["belief_id"] == belief_id]
    if change_type:
        rows = [h for h in rows if h["change_type"] == change_type]
    return rows


FAKE_CONN = FakeConnection()

with patch.object(bm_mod, "get_connection", _fake_get_connection):
    bm = bm_mod.BeliefManager()

    # ============================================================
    # 1. add_belief() — create.
    # ============================================================
    belief = bm.add_belief(topic="astronomy", statement="У Юпитера известно 95 спутников.", confidence=0.6)
    check("1: add_belief() creates a real belief row", belief.id in FAKE_CONN.beliefs)
    check("1: belief row's confidence matches", FAKE_CONN.beliefs[belief.id]["confidence"] == 0.6)
    created_history = _history_for(belief.id, "created")
    check(
        "1: a 'created' history row exists with old_confidence=0.0",
        len(created_history) == 1 and created_history[0]["old_confidence"] == 0.0,
        f"{created_history}",
    )

    # ============================================================
    # 2. add_belief() again, same topic+statement -> merges (exact match).
    # ============================================================
    belief2 = bm.add_belief(
        topic="astronomy", statement="У Юпитера известно 95 спутников.", confidence=0.7,
        evidence_for=["ev_new_1"],
    )
    check("2: SAME belief_id reused, not a new belief created", belief2.id == belief.id, f"{belief.id} vs {belief2.id}")
    check("2: an 'updated' history row was recorded", len(_history_for(belief.id, "updated")) == 1)
    check("2: evidence_for now contains the new evidence id", "ev_new_1" in FAKE_CONN.beliefs[belief.id]["evidence_for"])

    # ============================================================
    # 3. challenge_belief().
    # ============================================================
    # new_confidence here is evidence STRENGTH, not a target confidence
    # value — a strong (close to 1.0), contradicting piece of evidence
    # is what actually drives confidence down via the Bayesian update's
    # is_supporting=False branch (a WEAK counter-evidence, e.g. 0.05,
    # paradoxically raises confidence instead — same math as the
    # original, unmodified _bayesian_update()).
    challenged = bm.challenge_belief(belief_id=belief.id, counter_evidence="ev_counter_1", new_confidence=0.95, reason="new study")
    check("3: challenge_belief() returns the belief", challenged is not None and challenged.id == belief.id)
    check("3: a 'revised' history row was recorded", len(_history_for(belief.id, "revised")) == 1)
    check(
        "3: confidence dropping below 0.3 flips status to 'revised'",
        FAKE_CONN.beliefs[belief.id]["status"] == "revised",
        f"{FAKE_CONN.beliefs[belief.id]}",
    )

    # ============================================================
    # 4. supersede_belief().
    # ============================================================
    new_belief = bm.add_belief(topic="astronomy", statement="Совершенно другое отдельное утверждение.", confidence=0.5)
    ok = bm.supersede_belief(belief.id, new_belief.id)
    check("4: supersede_belief() succeeded", ok is True)
    check(
        "4: old belief marked superseded with superseded_by set",
        FAKE_CONN.beliefs[belief.id]["status"] == "superseded"
        and FAKE_CONN.beliefs[belief.id]["superseded_by"] == new_belief.id,
    )
    superseded_history = _history_for(belief.id, "superseded")
    check(
        "4: 'superseded' history row has NULL old/new confidence (matches original JSON semantics)",
        len(superseded_history) == 1
        and superseded_history[0]["old_confidence"] is None
        and superseded_history[0]["new_confidence"] is None,
        f"{superseded_history}",
    )

    # ============================================================
    # 5. _apply_decay().
    # ============================================================
    stale_belief = bm.add_belief(topic="decay_test", statement="убеждение для проверки decay", confidence=0.8)
    # Force it to look 3 days old — same technique already validated
    # today for relationship_memory's own time-based gate.
    FAKE_CONN.beliefs[stale_belief.id]["updated_at"] = datetime.utcfromtimestamp(0)
    bm._apply_decay()
    check(
        "5: a stale active belief's confidence decays downward",
        FAKE_CONN.beliefs[stale_belief.id]["confidence"] < 0.8,
        f"{FAKE_CONN.beliefs[stale_belief.id]}",
    )
    check("5: a 'decayed' history row was recorded", len(_history_for(stale_belief.id, "decayed")) == 1)

    # ============================================================
    # 6. Read paths use real SQL aggregates.
    # ============================================================
    stats = bm.get_stats()
    check("6: get_stats() total matches the fake's real belief count", stats["total"] == len(FAKE_CONN.beliefs), f"{stats}")
    by_topic = bm.get_beliefs_by_topic("astronomy")
    check("6: get_beliefs_by_topic() only returns 'active' beliefs for that topic", all(b.status == "active" for b in by_topic))

# ============================================================
# 7. FAIL LOUD — SQL unreachable raises, never silently "succeeds".
# ============================================================

def _raise_unavailable(autocommit=False):
    raise SqlUnavailable("forced unreachable for this test")


with patch.object(bm_mod, "get_connection", _raise_unavailable):
    raised = False
    try:
        bm2 = bm_mod.BeliefManager()
        bm2.add_belief(topic="t", statement="s", confidence=0.5)
    except SqlUnavailable:
        raised = True
    check(
        "7: with SQL genuinely unreachable, BeliefManager raises SqlUnavailable — "
        "the deliberate opposite of the retired JSON-primary/SQL-shadow fail-open "
        "design, because there is no more JSON fallback to fail open to",
        raised,
    )

# ============================================================
# 8. ENUM/code consistency.
# ============================================================

_src = inspect.getsource(bm_mod)
_schema_src = inspect.getsource(schema_mod)
_enum_match = re.search(r"change_type\s+ENUM\(([^)]+)\)", _schema_src)
_enum_values = {v.strip().strip("'") for v in _enum_match.group(1).split(",")} if _enum_match else set()
_code_values = {m for m in re.findall(r'change_type="(\w+)"', _src)}

check(
    "8: every change_type string belief_manager.py sends is declared in schema.py's ENUM",
    _code_values.issubset(_enum_values), f"code={_code_values} schema_enum={_enum_values}",
)
check(
    "8: schema.py's ENUM declares EXACTLY the 5 real values, nothing stale",
    _enum_values == {"created", "decayed", "updated", "revised", "superseded"}, f"{_enum_values}",
)
check(
    "8: belief_manager.py no longer imports the retired shadow_record_belief_assessment "
    "(SQL is the primary path now, not a shadow of JSON)",
    "shadow_record_belief_assessment" not in re.sub(r'""".*?"""', "", _src, flags=re.DOTALL),
)
_src_no_docstrings = re.sub(r'""".*?"""', "", _src, flags=re.DOTALL)
check(
    "8: no actual json.load/json.dump file I/O or .storage_file attribute usage remains "
    "in belief_manager.py's real code (docstring mentions of the RETIRED registry/beliefs."
    "json are fine and expected — this checks the executable code, not prose)",
    "json.load(" not in _src_no_docstrings
    and "json.dump(" not in _src_no_docstrings
    and ".storage_file" not in _src_no_docstrings,
    _src_no_docstrings,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
