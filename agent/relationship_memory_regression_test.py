"""
agent/relationship_memory_regression_test.py — owner mandate: "мне нужен
у неё характер, она обидчива... простое извени - не канает" — SQL-backed
grievance/forgiveness state (agent/relationship_memory.py), replacing
the old JSON-file-based agent/forgiveness_model.py so this memory lives
under the same "10-year bastion" access-control model as everything
else in this database.

Covers:
    1. Schema: grievance/forgiveness_capacity positioned correctly,
       classified "C" (UPDATE legitimate, DELETE blocked for free via
       the existing class-C trigger handling — no new trigger code).
    2. repositories.py CRUD: record/get/find_similar/bump/update-status/
       list-active/count-by-status for grievance; get/set for
       forgiveness_capacity (including the find-or-default vs
       find-or-create distinction).
    3. relationship_memory.py state machine (ported 1:1 from the old
       ForgivenessModel): new grievance vs bumping a similar open one;
       a plain low-sincerity apology stays "acknowledged" (the owner's
       own complaint this file exists to fix); a sincere one advances
       to "understood" and restores capacity; progress_healing()
       correctly refuses before 2 hours have passed AND succeeds once
       they have; get_summary()/memory_facts()/
       most_severe_active_grievance().
    4. Structural: pet/chat_local.py makes exactly ONE Ollama call per
       turn (накал recognition and her reply come from the SAME
       generation — see agent/message_intensity.py), and states
       relationship memory as plain facts, never a scripted reaction.

Run: /home/iam/venv/bin/python3 -m agent.relationship_memory_regression_test
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

from agent.db.sql.schema import ALL_TABLES_IN_ORDER, TABLE_CLASSIFICATION
import agent.db.sql.repositories as repo
import agent.relationship_memory as rm

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
# 1. Schema.
# ============================================================

_table_names = [n for n, _ in ALL_TABLES_IN_ORDER]
check("1: grievance exists in ALL_TABLES_IN_ORDER", "grievance" in _table_names)
check("1: forgiveness_capacity exists in ALL_TABLES_IN_ORDER", "forgiveness_capacity" in _table_names)
check("1: grievance classified 'C'", TABLE_CLASSIFICATION.get("grievance") == "C")
check("1: forgiveness_capacity classified 'C'", TABLE_CLASSIFICATION.get("forgiveness_capacity") == "C")


# ============================================================
# Stateful fake connection for grievance/forgiveness_capacity.
# ============================================================

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None
        self._results = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        upper = norm.upper()
        self._result = None
        self._results = None

        if upper.startswith("INSERT INTO GRIEVANCE"):
            gid, user_id, event_type, description, severity, context, created_at, updated_at = params
            self.conn.grievances[gid] = {
                "id": gid, "user_id": user_id, "event_type": event_type, "description": description,
                "severity": severity, "status": "registered", "apology_sincerity": 0.0,
                "context": context, "created_at": created_at, "apology_at": None,
                "understood_at": None, "forgiven_at": None, "updated_at": updated_at,
            }
        elif upper.startswith("SELECT * FROM GRIEVANCE WHERE ID=%S"):
            (gid,) = params
            self._result = dict(self.conn.grievances[gid]) if gid in self.conn.grievances else None
        elif "STATUS != 'FORGIVEN'" in upper:
            user_id, prefix = params
            matches = [
                g for g in self.conn.grievances.values()
                if g["user_id"] == user_id and g["status"] != "forgiven" and g["description"][:20] == prefix
            ]
            matches.sort(key=lambda g: g["created_at"], reverse=True)
            self._result = dict(matches[0]) if matches else None
        elif upper.startswith("UPDATE GRIEVANCE SET SEVERITY=%S"):
            severity, updated_at, gid = params
            self.conn.grievances[gid]["severity"] = severity
            self.conn.grievances[gid]["status"] = "registered"
            self.conn.grievances[gid]["updated_at"] = updated_at
        elif upper.startswith("UPDATE GRIEVANCE SET"):
            # Fixed shape from repositories.update_grievance_status():
            # (status, updated_at, apology_sincerity, apology_at,
            # understood_at, forgiven_at, id) — COALESCE(%s, col) means
            # a None param leaves that column's stored value unchanged.
            status, updated_at, apology_sincerity, apology_at, understood_at, forgiven_at, gid = params
            g = self.conn.grievances[gid]
            g["status"] = status
            g["updated_at"] = updated_at
            if apology_sincerity is not None:
                g["apology_sincerity"] = apology_sincerity
            if apology_at is not None:
                g["apology_at"] = apology_at
            if understood_at is not None:
                g["understood_at"] = understood_at
            if forgiven_at is not None:
                g["forgiven_at"] = forgiven_at
        elif "STATUS NOT IN" in upper:
            (user_id,) = params
            matches = [
                g for g in self.conn.grievances.values()
                if g["user_id"] == user_id and g["status"] not in ("forgiven", "unforgiven")
            ]
            matches.sort(key=lambda g: g["created_at"])
            self._results = [dict(g) for g in matches]
        elif upper.startswith("SELECT COUNT(*) AS C FROM GRIEVANCE"):
            user_id, status = params
            self._result = {"c": sum(
                1 for g in self.conn.grievances.values() if g["user_id"] == user_id and g["status"] == status
            )}
        elif upper.startswith("SELECT * FROM FORGIVENESS_CAPACITY"):
            (user_id,) = params
            self._result = dict(self.conn.capacities[user_id]) if user_id in self.conn.capacities else None
        elif upper.startswith("INSERT INTO FORGIVENESS_CAPACITY"):
            user_id, capacity, last_forgiveness, updated_at = params
            existing = self.conn.capacities.get(user_id)
            if existing:
                existing["capacity"] = capacity
                if last_forgiveness is not None:
                    existing["last_forgiveness"] = last_forgiveness
                existing["updated_at"] = updated_at
            else:
                self.conn.capacities[user_id] = {
                    "user_id": user_id, "capacity": capacity,
                    "last_forgiveness": last_forgiveness, "updated_at": updated_at,
                }

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results or []


class FakeConnection:
    def __init__(self):
        self.grievances = {}
        self.capacities = {}

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


# ============================================================
# 2. repositories.py CRUD.
# ============================================================

conn = FakeConnection()
repo.record_grievance(conn, "g1", "owner", "insult", "aaaaaaaaaaaaaaaaaaaa-original", 0.8, context={"word": "глупая"})
g1 = repo.get_grievance(conn, "g1")
check("2: record_grievance()/get_grievance() round trip", g1["status"] == "registered" and g1["severity"] == 0.8)
check("2: context JSON round-trips as a dict", g1["context"] == {"word": "глупая"})

similar = repo.find_similar_open_grievance(conn, "owner", "aaaaaaaaaaaaaaaaaaaa-different-tail")
check(
    "2: find_similar_open_grievance() matches on the first 20 chars",
    similar is not None and similar["id"] == "g1",
)
repo.bump_grievance(conn, "g1", 0.9)
check("2: bump_grievance() updates severity and resets status", repo.get_grievance(conn, "g1")["severity"] == 0.9)

repo.update_grievance_status(conn, "g1", "acknowledged", apology_sincerity=0.3, apology_at=datetime.utcnow())
g1b = repo.get_grievance(conn, "g1")
check(
    "2: update_grievance_status() sets only the given fields",
    g1b["status"] == "acknowledged" and g1b["apology_sincerity"] == 0.3 and g1b["understood_at"] is None,
)

active = repo.list_active_grievances(conn, "owner")
check("2: list_active_grievances() returns the still-open grievance", len(active) == 1 and active[0]["id"] == "g1")
check("2: count_grievances_by_status() counts correctly", repo.count_grievances_by_status(conn, "owner", "acknowledged") == 1)

cap_default = repo.get_forgiveness_capacity(conn, "owner")
check("2: get_forgiveness_capacity() defaults to 50.0 when never set", cap_default["capacity"] == 50.0)
repo.set_forgiveness_capacity(conn, "owner", 42.0)
check("2: set_forgiveness_capacity() persists (find-or-create in one statement)", repo.get_forgiveness_capacity(conn, "owner")["capacity"] == 42.0)
repo.set_forgiveness_capacity(conn, "owner", 55.0)
check("2: set_forgiveness_capacity() updates an existing row, not a duplicate", repo.get_forgiveness_capacity(conn, "owner")["capacity"] == 55.0)


# ============================================================
# 3. relationship_memory.py state machine.
# ============================================================

conn2 = FakeConnection()

gid = rm.add_grievance(conn2, "owner", "insult", "bbbbbbbbbbbbbbbbbbbb-you are so stupid", 0.7)
check("3: add_grievance() registers a new grievance", repo.get_grievance(conn2, gid)["status"] == "registered")
cap_after_insult = repo.get_forgiveness_capacity(conn2, "owner")
check(
    "3: a new grievance LOWERS forgiveness_capacity (50 - 0.7*10 = 43)",
    abs(cap_after_insult["capacity"] - 43.0) < 0.01, f"{cap_after_insult}",
)

gid_again = rm.add_grievance(conn2, "owner", "insult", "bbbbbbbbbbbbbbbbbbbb-again, stupid", 0.5)
check("3: a similar open grievance is BUMPED, not duplicated", gid_again == gid)
check(
    "3: bumping raises severity by 30% of the new severity (0.7 + 0.5*0.3 = 0.85)",
    abs(repo.get_grievance(conn2, gid)["severity"] - 0.85) < 0.01,
)
check(
    "3: bumping a RECURRING grievance does NOT independently re-charge capacity "
    "(faithful to the original ForgivenessModel — only a genuinely new grievance does)",
    abs(repo.get_forgiveness_capacity(conn2, "owner")["capacity"] - 43.0) < 0.01,
    f"{repo.get_forgiveness_capacity(conn2, 'owner')}",
)

ok_low = rm.acknowledge_apology(conn2, gid, sincerity=0.2)
check("3: acknowledge_apology() with LOW sincerity returns True (grievance exists)", ok_low is True)
check(
    "3: a plain/insincere apology (sincerity=0.2) stays 'acknowledged', NOT auto-forgiven — "
    "this is the owner's own complaint ('простое извени - не канает') fixed directly",
    repo.get_grievance(conn2, gid)["status"] == "acknowledged",
)

rm.acknowledge_apology(conn2, gid, sincerity=0.9)
check(
    "3: a SINCERE apology (sincerity=0.9 > 0.6) auto-advances to 'understood'",
    repo.get_grievance(conn2, gid)["status"] == "understood",
)
cap_after_sincere = repo.get_forgiveness_capacity(conn2, "owner")
check(
    "3: a sincere apology partially RESTORES forgiveness_capacity",
    cap_after_sincere["capacity"] > cap_after_insult["capacity"],
    f"{cap_after_sincere} vs {cap_after_insult}",
)

healed_too_soon = rm.progress_healing(conn2, gid)
check(
    "3: progress_healing() refuses too soon (< 2 hours since created_at) even with a "
    "sincere, understood apology",
    healed_too_soon is False,
)
check(
    "3: a refused-too-soon grievance moves to 'healing' (still in progress, not stuck)",
    repo.get_grievance(conn2, gid)["status"] == "healing",
)

# Simulate 3 hours having passed.
conn2.grievances[gid]["created_at"] = datetime.utcnow() - timedelta(hours=3)
healed_now = rm.progress_healing(conn2, gid)
check("3: progress_healing() succeeds once enough time has passed and all conditions hold", healed_now is True)
check("3: the grievance's status is now 'forgiven'", repo.get_grievance(conn2, gid)["status"] == "forgiven")

check(
    "3: progress_healing() on an ALREADY-forgiven grievance returns True without changing anything",
    rm.progress_healing(conn2, gid) is True,
)

summary = rm.get_summary(conn2, "owner")
check("3: get_summary() reports zero active grievances after forgiveness", summary["active_grievances"] == 0)
check("3: get_summary() reports forgiven=1", summary["forgiven"] == 1)

facts = rm.memory_facts(repo.get_grievance(conn2, gid))
check(
    "3: memory_facts() returns RAW FACTS only (description/severity/status) — never an "
    "interpreted line for the model to recite (owner correction, confirmed by live "
    "testing against the real local model: накал recognition and reaction must happen "
    "in HER OWN single generation, not be dictated by a script)",
    facts["status"] == "forgiven" and "description" in facts and "severity" in facts,
    f"{facts}",
)

# most_severe_active_grievance().
conn3 = FakeConnection()
rm.add_grievance(conn3, "owner", "insult", "фраза раз", 0.3)
gid_severe = rm.add_grievance(conn3, "owner", "insult", "фраза два — совсем другая", 0.9)
most_severe = rm.most_severe_active_grievance(conn3, "owner")
check(
    "3: most_severe_active_grievance() picks the HIGHEST-severity active one",
    most_severe is not None and most_severe["id"] == gid_severe,
)
check(
    "3: most_severe_active_grievance() returns None for a user with no grievances at all",
    rm.most_severe_active_grievance(conn3, "someone-else") is None,
)


# ============================================================
# 4. Structural: pet/chat_local.py wiring.
# ============================================================

import pet.chat_local as chat_local

_src_respond = inspect.getsource(chat_local._respond_with_character)
check(
    "4: _respond_with_character() makes exactly ONE Ollama call (_call_ollama_raw) — "
    "накал recognition and the visible reply come from the SAME generation, not a "
    "separate classifier call feeding a second one",
    _src_respond.count("_call_ollama_raw(") == 1,
)
_pos_raw_call = _src_respond.find("_call_ollama_raw(")
_pos_parse = _src_respond.find("parse_self_report(")
_pos_apply = _src_respond.find("_apply_self_report(")
check(
    "4: the model is called BEFORE her self-report is parsed, which happens BEFORE it's "
    "written to memory (correct data dependency order)",
    -1 < _pos_raw_call < _pos_parse < _pos_apply,
    f"call={_pos_raw_call} parse={_pos_parse} apply={_pos_apply}",
)

_src_call_raw = inspect.getsource(chat_local._call_ollama_raw)
check(
    "4: _call_ollama_raw() states memory as a plain FACT message (_memory_context_message), "
    "never a scripted reaction",
    "_memory_context_message(" in _src_call_raw,
)

_src_memory_msg = inspect.getsource(chat_local._memory_context_message)
check(
    "4: _memory_context_message() never tells the model HOW to feel — no emotional/tone "
    "vocabulary, only a factual statement of what was said and its status",
    not any(word in _src_memory_msg for word in ("реагируй", "чувству", "тон должен", "покажи")),
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
