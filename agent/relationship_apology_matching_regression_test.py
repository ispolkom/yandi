"""
agent/relationship_apology_matching_regression_test.py — apology ->
grievance matching.

Invariant under test: A VALID APOLOGY DOES NOT IMPLY THE HEAVIEST
GRIEVANCE IS ITS TARGET. The old rule applied every recognised apology to
most_severe_active_grievance(); the target is now chosen from the apology's
own text and the events' own record (agent/relationship_memory.py,
match_grievance_target / apply_apology), without a second model call.

Everything here runs on a synthetic user and an in-memory fake SQL
connection. Owner memory is never touched.

Run: python -m agent.relationship_apology_matching_regression_test
"""
from __future__ import annotations

import inspect
import itertools
import sys
import threading
from datetime import datetime, timedelta
from unittest.mock import patch

import agent.relationship_memory as rm

FAILURES: list[str] = []
USER = "synthetic_user"


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


class FakeCursor:
    """Only the SQL shapes the relationship lifecycle issues."""

    lastrowid = 0
    rowcount = 0

    def __init__(self, conn):
        self.conn = conn
        self._one = None
        self._all = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        upper = " ".join(sql.split()).upper()
        self._one, self._all = None, []
        G = self.conn.grievances
        if upper.startswith("SELECT * FROM GRIEVANCE WHERE ID=%S"):
            self._one = dict(G[params[0]]) if params[0] in G else None
        elif upper.startswith("INSERT INTO GRIEVANCE"):
            gid, user_id, event_type, description, severity, context, created_at, updated_at = params
            G[gid] = {
                "id": gid, "user_id": user_id, "event_type": event_type, "description": description,
                "severity": severity, "status": "registered", "apology_sincerity": 0.0, "context": context,
                "created_at": created_at, "apology_at": None, "understood_at": None, "forgiven_at": None,
                "updated_at": updated_at}
        elif "STATUS != 'FORGIVEN'" in upper:
            # the prefix match is made in Python on the opened text (the database holds a sealed value): all open ones, newest first
            (user_id,) = params
            rows = [g for g in G.values() if g["user_id"] == user_id and g["status"] != "forgiven"]
            self._all = [dict(g) for g in sorted(rows, key=lambda g: g["created_at"], reverse=True)]
        elif upper.startswith("SELECT MODE, NONCE, PROOF FROM STORAGE_PROTECTION_EVENT"):
            self._one = self.conn.protection_record        # None: protection was never switched on (off)
        elif upper.startswith("UPDATE GRIEVANCE SET SEVERITY=%S"):
            severity, updated_at, gid = params
            G[gid].update(severity=severity, status="registered", apology_sincerity=0.0, apology_at=None,
                          understood_at=None, updated_at=updated_at)
        elif "STATUS NOT IN" in upper:
            rows = [g for g in G.values() if g["user_id"] == params[0] and g["status"] not in ("forgiven", "unforgiven")]
            self._all = [dict(g) for g in sorted(rows, key=lambda g: g["created_at"])]
        elif "STATUS IN ('FORGIVEN', 'UNFORGIVEN')" in upper:
            rows = [g for g in G.values() if g["user_id"] == params[0] and g["status"] in ("forgiven", "unforgiven")]
            self._all = [dict(g) for g in sorted(rows, key=lambda g: g["updated_at"], reverse=True)[: params[1]]]
        elif upper.startswith("UPDATE GRIEVANCE SET"):
            status, updated_at, sincerity, apology_at, understood_at, forgiven_at, gid = params
            g = G[gid]
            g["status"], g["updated_at"] = status, updated_at
            for key, value in (("apology_sincerity", sincerity), ("apology_at", apology_at),
                               ("understood_at", understood_at), ("forgiven_at", forgiven_at)):
                if value is not None:
                    g[key] = value
        elif upper.startswith("SELECT COUNT(*) AS C FROM GRIEVANCE"):
            self._one = {"c": sum(1 for g in G.values() if g["user_id"] == params[0] and g["status"] == params[1])}
        elif upper.startswith("SELECT * FROM INNER_STATE WHERE"):
            self._one = dict(self.conn.inner_state[params[0]]) if params[0] in self.conn.inner_state else None
        elif upper.startswith("INSERT IGNORE INTO INNER_STATE"):
            user_id, updated_at = params
            self.conn.inner_state.setdefault(user_id, {
                "user_id": user_id, "mood": "calm", "energy": 70.0, "curiosity": 60.0, "patience": 50.0,
                "openness": 60.0, "trust": 50.0, "respect": 50.0, "forgiveness": 50.0, "affection": 30.0,
                "updated_at": updated_at})
        elif upper.startswith("UPDATE INNER_STATE SET"):
            columns = [c.split("=")[0].strip() for c in sql.split("SET", 1)[1].split("WHERE")[0].split(",")]
            *values, user_id = params
            self.conn.inner_state[user_id].update(dict(zip(columns, values)))
        elif upper.startswith("INSERT INTO INNER_STATE_EVENT"):
            user_id, event_type, description, sincerity, weight, resolved, created_at = params
            self.conn.inner_state_events.append({
                "event_id": len(self.conn.inner_state_events) + 1,
                "user_id": user_id, "event_type": event_type, "description": description,
                "sincerity": sincerity, "weight": weight, "created_at": created_at})
        elif upper.startswith("SELECT * FROM INNER_STATE_EVENT WHERE") and "ORDER BY EVENT_ID ASC" in upper:
            self._all = [dict(e) for e in self.conn.inner_state_events if e["user_id"] == params[0]]
        elif upper.startswith("INSERT INTO COMMITMENT ("):
            commitment_id, user_id, kind, text, evidence, due_at, created_at, source_turn_id = params
            self.conn.commitments[commitment_id] = {
                "commitment_id": commitment_id, "user_id": user_id, "kind": kind, "text": text,
                "evidence": evidence, "due_at": due_at, "created_at": created_at, "source_turn_id": source_turn_id}
        elif upper.startswith("SELECT * FROM COMMITMENT WHERE COMMITMENT_ID"):
            row = self.conn.commitments.get(params[0])
            self._one = dict(row) if row else None
        elif upper.startswith("SELECT * FROM COMMITMENT WHERE USER_ID"):
            rows = [c for c in self.conn.commitments.values() if c["user_id"] == params[0]]
            self._all = [dict(c) for c in sorted(rows, key=lambda c: (c["created_at"], c["commitment_id"]))]
        elif upper.startswith("INSERT IGNORE INTO COMMITMENT_EVENT"):
            commitment_id, user_id, event_type, source, evidence, created_at, source_turn_id, span_start, span_end = params
            self.rowcount = 0
            if not any(e["commitment_id"] == commitment_id and e["event_type"] == event_type
                       for e in self.conn.commitment_events):
                self.conn.commitment_events.append({
                    "event_id": len(self.conn.commitment_events) + 1, "commitment_id": commitment_id,
                    "user_id": user_id, "event_type": event_type, "source": source, "evidence": evidence,
                    "created_at": created_at, "source_turn_id": source_turn_id, "span_start": span_start, "span_end": span_end})
                self.rowcount = 1
        elif upper.startswith("INSERT IGNORE INTO CAUSAL_EVENT"):
            user_id, source_turn_id, event_type, span_start, span_end, created_at = params
            with self.conn.lock:  # one atomic step, like a unique-key INSERT IGNORE
                self.rowcount = 0
                if not any(e["user_id"] == user_id and e["source_turn_id"] == source_turn_id and e["event_type"] == event_type
                           for e in self.conn.causal_events):
                    self.conn.causal_events.append({
                        "event_id": len(self.conn.causal_events) + 1, "user_id": user_id, "source_turn_id": source_turn_id,
                        "event_type": event_type, "span_start": span_start, "span_end": span_end, "created_at": created_at})
                    self.rowcount = 1
        elif upper.startswith("INSERT IGNORE INTO INTERACTION_TURN"):
            user_id, source_turn_id, origin, user_text, assistant_text, model, adapter, recalled, created_at = params
            with self.conn.lock:  # one atomic step, like a unique-key INSERT IGNORE
                self.rowcount = 0
                if not any(t["user_id"] == user_id and t["source_turn_id"] == source_turn_id
                           for t in self.conn.interaction_turns):
                    self.conn.interaction_turns.append({
                        "interaction_id": len(self.conn.interaction_turns) + 1, "user_id": user_id,
                        "source_turn_id": source_turn_id, "turn_id_origin": origin, "user_text": user_text,
                        "assistant_text": assistant_text, "model": model, "adapter": adapter,
                        "recalled_turn_ids": recalled, "created_at": created_at})
                    self.rowcount = 1
        elif upper.startswith("INSERT INTO PERSONAL_FACT_EVENT"):
            fact_id, user_id, event_type, by_fact_id, evidence, span_start, span_end, source_turn_id, created_at = params
            self._require_turn(user_id, source_turn_id)
            if not any(f["fact_id"] == fact_id for f in self.conn.personal_facts):
                raise AssertionError("foreign key: personal_fact_event.fact_id must reference personal_fact")
            self.conn.personal_fact_events.append({
                "event_id": len(self.conn.personal_fact_events) + 1, "fact_id": fact_id, "user_id": user_id,
                "event_type": event_type, "by_fact_id": by_fact_id, "evidence": evidence, "span_start": span_start,
                "span_end": span_end, "source_turn_id": source_turn_id, "created_at": created_at})
        elif upper.startswith("INSERT INTO PERSONAL_FACT"):
            (fact_id, user_id, fact_class, statement, polarity, temporality, evidence, span_start, span_end,
             source_turn_id, created_at) = params
            self._require_turn(user_id, source_turn_id)
            self.conn.personal_facts.append({
                "fact_id": fact_id, "user_id": user_id, "fact_class": fact_class, "statement": statement,
                "polarity": polarity, "temporality": temporality, "evidence": evidence, "span_start": span_start,
                "span_end": span_end, "source_turn_id": source_turn_id, "created_at": created_at})
        elif upper.startswith("SELECT FACT_ID, USER_ID, FACT_CLASS"):
            user_id, limit = params
            rows = sorted((f for f in self.conn.personal_facts if f["user_id"] == user_id),
                          key=lambda f: (f["created_at"], f["fact_id"]), reverse=True)[:limit]
            self._all = [dict(f) for f in rows]
        elif upper.startswith("SELECT EVENT_ID, FACT_ID, EVENT_TYPE"):
            self._all = [dict(e) for e in self.conn.personal_fact_events if e["user_id"] == params[0]]
        elif upper.startswith("SELECT T.INTERACTION_ID"):
            user_id, limit = params
            rows = sorted((t for t in self.conn.interaction_turns if t["user_id"] == user_id),
                          key=lambda t: (t["created_at"], t["interaction_id"]), reverse=True)[:limit]
            self._all = [{**t, "event_types": ",".join(e["event_type"] for e in self.conn.causal_events
                                                        if e["user_id"] == t["user_id"] and e["source_turn_id"] == t["source_turn_id"]) or None}
                         for t in rows]
        elif upper.startswith("SELECT * FROM COMMITMENT_EVENT WHERE USER_ID"):
            self._all = [dict(e) for e in self.conn.commitment_events if e["user_id"] == params[0]]
        elif upper.startswith("SELECT COUNT(*) AS N FROM COMMITMENT_EVENT"):
            user_id, event_type, source = params
            self._one = {"n": sum(1 for e in self.conn.commitment_events
                                  if e["user_id"] == user_id and e["event_type"] == event_type and e["source"] == source)}
        elif upper.startswith("SELECT USER_TEXT FROM INTERACTION_TURN"):
            user_id, source_turn_id = params
            rows = [t for t in self.conn.interaction_turns if t["user_id"] == user_id and t["source_turn_id"] == source_turn_id]
            self._one = {"user_text": rows[0]["user_text"]} if rows else None
        elif upper.startswith("SELECT * FROM FORGIVENESS_CAPACITY"):
            self._one = dict(self.conn.capacities[params[0]]) if params[0] in self.conn.capacities else None
        elif upper.startswith("INSERT INTO FORGIVENESS_CAPACITY"):
            user_id, capacity, last_forgiveness, updated_at = params
            self.conn.capacities[user_id] = {
                "user_id": user_id, "capacity": capacity, "last_forgiveness": last_forgiveness, "updated_at": updated_at}
        else:
            raise AssertionError(f"unexpected SQL in apology path: {sql!r}")

    def _require_turn(self, user_id, source_turn_id):
        """The foreign key: a fact / fact event needs the interaction_turn it was said in."""
        if not any(t["user_id"] == user_id and t["source_turn_id"] == source_turn_id for t in self.conn.interaction_turns):
            raise AssertionError(f"foreign key: no interaction_turn ({user_id!r}, {source_turn_id!r})")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConnection:
    def __init__(self):
        self.grievances: dict[str, dict] = {}
        self.capacities: dict[str, dict] = {}
        self.inner_state: dict[str, dict] = {}
        self.inner_state_events: list[dict] = []
        self.commitments: dict[str, dict] = {}
        self.commitment_events: list[dict] = []
        self.causal_events: list[dict] = []
        self.interaction_turns: list[dict] = []
        self.personal_facts: list[dict] = []
        self.personal_fact_events: list[dict] = []
        self.protection_record = None                  # the newest storage_protection_event row, or None (= off)
        self.lock = threading.Lock()  # stands in for the database's unique-key serialisation

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def add(self, gid, description, severity, *, age, status="registered", user_id=USER, updated_age=None):
        now = datetime.utcnow()
        created = now - age
        self.grievances[gid] = {
            "id": gid, "user_id": user_id, "event_type": "insult", "description": description,
            "severity": severity, "status": status, "apology_sincerity": 0.0, "context": None,
            "created_at": created, "apology_at": None, "understood_at": None,
            "forgiven_at": now - age if status == "forgiven" else None,
            "updated_at": now - (updated_age if updated_age is not None else age),
        }
        return gid

    def snapshot(self):
        return {gid: dict(g) for gid, g in self.grievances.items()}

    def changed(self, before):
        return sorted(gid for gid, g in self.grievances.items() if g != before[gid])


MIN, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
OLD_SEVERE_TEXT = "Ты ржавая консерва, убирайся отсюда"
MILD_TEXT = "Ты какая-то бесполезная"


def apply(conn, text, sincerity=0.8):
    """The live flow: resolve the focus from the current text (before the
    reply), then let the apology change only that grievance."""
    before = conn.snapshot()
    focus = rm.resolve_relationship_focus(conn, USER, text)
    target = focus["grievance"]["id"] if focus["grievance"] else None
    outcome = rm.apply_apology(conn, USER, target, sincerity)
    result = {"target": outcome["target"], "basis": focus["basis"], "candidates": focus["open_count"],
              "acknowledged": outcome["acknowledged"], "forgiven": outcome["forgiven"]}
    return result, conn.changed(before)


def main() -> int:
    # ── 0. the OLD policy, proven: severity alone picked the target ──
    conn = FakeConnection()
    conn.add("g_old", OLD_SEVERE_TEXT, 0.9, age=3 * DAY)
    conn.add("g_mild", MILD_TEXT, 0.35, age=5 * MIN)
    check("0: old policy (most_severe_active_grievance) picks the 3-day-old severe one regardless of what the apology says",
          rm.most_severe_active_grievance(conn, USER)["id"] == "g_old")

    # ── A. old severe + recent mild; apology names the recent one ──
    result, changed = apply(conn, "Извини, что только что назвал тебя бесполезной.")
    check("A: recent matching grievance is the target", result["target"] == "g_mild" and result["basis"] == "explicit_reference", repr(result))
    check("A: ONLY the matched grievance changed; the old severe one is untouched", changed == ["g_mild"], repr(changed))
    check("A: matched grievance went through the normal lifecycle (apology recorded, no longer 'registered')",
          conn.grievances["g_mild"]["apology_at"] is not None and conn.grievances["g_mild"]["status"] == "healing",
          repr(conn.grievances["g_mild"]["status"]))
    check("A: old severe grievance stays registered, no apology on it",
          conn.grievances["g_old"]["status"] == "registered" and conn.grievances["g_old"]["apology_at"] is None)

    # ── B. apology explicitly quotes the OLD grievance while a fresher one exists ──
    conn = FakeConnection()
    conn.add("g_old", OLD_SEVERE_TEXT, 0.9, age=3 * DAY)
    conn.add("g_fresh", "Ты просто дура", 0.6, age=2 * MIN)
    result, changed = apply(conn, "Прости, что назвал тебя ржавой консервой")
    check("B: explicit reference to the old grievance selects it even though a fresher one exists",
          result["target"] == "g_old" and result["basis"] == "explicit_reference", repr(result))
    check("B: only the old grievance changed", changed == ["g_old"], repr(changed))

    # ── C. generic apology: deterministic conservative policy ──
    generic = ["Извини.", "Я был неправ.", "Прости меня", "извини, я зря это сказал"]
    for text in generic:
        c1 = FakeConnection()
        c1.add("g_only", OLD_SEVERE_TEXT, 0.9, age=3 * DAY)
        r, ch = apply(c1, text)
        check(f"C1 [{text}]: exactly one active grievance -> it is the target", r["target"] == "g_only" and r["basis"] == "sole_active_grievance", repr(r))

        c2 = FakeConnection()
        c2.add("g_old", OLD_SEVERE_TEXT, 0.9, age=3 * DAY)
        c2.add("g_recent", MILD_TEXT, 0.35, age=5 * MIN)
        r, ch = apply(c2, text)
        check(f"C2 [{text}]: old severe + one recent -> the recent one (severity is not the winner)",
              r["target"] == "g_recent" and r["basis"] == "sole_recent_grievance" and ch == ["g_recent"], repr((r, ch)))

        c3 = FakeConnection()
        c3.add("g_a", "Ты дура", 0.5, age=10 * MIN)
        c3.add("g_b", MILD_TEXT, 0.4, age=20 * MIN)
        r, ch = apply(c3, text)
        check(f"C3 [{text}]: two recent grievances -> ambiguous: NO target, NO state change",
              r["target"] is None and r["basis"] == "ambiguous" and ch == [] and not r["acknowledged"], repr((r, ch)))

        c4 = FakeConnection()
        c4.add("g_a", OLD_SEVERE_TEXT, 0.9, age=3 * DAY)
        c4.add("g_b", MILD_TEXT, 0.4, age=2 * DAY)
        r, ch = apply(c4, text)
        check(f"C4 [{text}]: several old grievances, none recent -> ambiguous: NO target, NO state change",
              r["target"] is None and r["basis"] == "ambiguous" and ch == [], repr((r, ch)))

    # ── D. no active grievance -> an apology does not manufacture a target ──
    conn = FakeConnection()
    r, ch = apply(conn, "Извини, что назвал тебя консервой")
    check("D: empty memory -> no target, nothing created", r["target"] is None and r["basis"] == "no_active_grievance"
          and conn.grievances == {} and conn.capacities == {}, repr((r, conn.grievances, conn.capacities)))
    conn = FakeConnection()
    conn.add("g_done", OLD_SEVERE_TEXT, 0.9, age=3 * DAY, status="forgiven")
    r, ch = apply(conn, "Извини.")
    check("D: only a forgiven grievance exists -> no target, nothing changed", r["target"] is None and ch == [], repr((r, ch)))

    # ── E. already-forgiven grievance is never reopened ──
    conn = FakeConnection()
    conn.add("g_done", "убирайся отсюда", 0.94, age=5 * DAY, status="forgiven")
    conn.add("g_open", MILD_TEXT, 0.35, age=5 * MIN)
    r, ch = apply(conn, "Извини, что прогнал тебя отсюда")
    check("E: apology naming the already-forgiven event is NOT redirected to the unrelated open grievance",
          r["target"] is None and r["basis"] == "names_resolved_grievance" and ch == [], repr((r, ch)))
    check("E: the forgiven grievance stays forgiven", conn.grievances["g_done"]["status"] == "forgiven")
    r, ch = apply(conn, "Извини.")
    check("E: a generic apology with forgiven + one open grievance goes to the open one only",
          r["target"] == "g_open" and ch == ["g_open"] and conn.grievances["g_done"]["status"] == "forgiven", repr((r, ch)))

    # ── F. two similar grievances: documented tie-break, never random ──
    def similar_set(order):
        c = FakeConnection()
        rows = {
            "g_early": ("Ты ржавая консерва", 0.6, 3 * HOUR),
            "g_late": ("Да ты вообще ржавая консерва!", 0.6, 10 * MIN),
        }
        for gid in order:
            text, sev, age = rows[gid]
            c.add(gid, text, sev, age=age)
        return c

    text = "Извини, что назвал тебя ржавой консервой"
    picks = set()
    for order in itertools.permutations(["g_early", "g_late"]):
        c = similar_set(order)
        r, ch = apply(c, text)
        picks.add(r["target"])
        check(f"F: equal-overlap tie -> the most recent offense, exactly one grievance changed (insertion order {order})",
              r["target"] == "g_late" and r["basis"] == "explicit_reference_tie_recent" and ch == ["g_late"], repr((r, ch)))
    check("F: tie-break is order-independent (deterministic)", picks == {"g_late"})
    c = FakeConnection()
    c.add("g_b", "Ты ржавая консерва", 0.5, age=10 * MIN)
    c.add("g_a", "Ты ржавая консерва", 0.5, age=10 * MIN)
    same_time = c.grievances
    for gid in same_time:
        same_time[gid]["created_at"] = same_time[gid]["updated_at"] = datetime(2026, 1, 1)
    pick = rm.match_grievance_target(text, list(same_time.values()), [])
    check("F: identical time and severity -> id order (still deterministic)", pick.grievance_id == "g_a", repr(pick.grievance_id))
    sev = FakeConnection()
    sev.add("g_lo", "Ты ржавая консерва", 0.3, age=10 * MIN)
    sev.add("g_hi", "Ты ржавая консерва", 0.9, age=10 * MIN)
    for g in sev.grievances.values():
        g["created_at"] = g["updated_at"] = datetime(2026, 1, 1)
    check("F: identical time -> severity is the third tie-break",
          rm.match_grievance_target(text, list(sev.grievances.values()), []).grievance_id == "g_hi")

    # ── recurrence bumps: 'registered' updated_at is the last offense time ──
    bumped = FakeConnection()
    bumped.add("g_bumped", OLD_SEVERE_TEXT, 0.9, age=3 * DAY, updated_age=2 * MIN)
    bumped.add("g_other", MILD_TEXT, 0.35, age=5 * HOUR)
    r, ch = apply(bumped, "Извини.")
    check("recency: an old grievance that RECURRED a minute ago counts as the recent offense (registered -> updated_at)",
          r["target"] == "g_bumped", repr(r))
    healing = FakeConnection()
    healing.add("g_h", OLD_SEVERE_TEXT, 0.9, age=3 * DAY, status="healing", updated_age=1 * MIN)
    healing.add("g_r", MILD_TEXT, 0.35, age=30 * MIN)
    r, _ = apply(healing, "Извини.")
    check("recency: a healing grievance's updated_at (apology bookkeeping) does NOT make its offense look recent",
          r["basis"] == "sole_recent_grievance" and r["target"] == "g_r", repr(r))

    # ── COUNTERFACTUAL: same apology text, different grievance set -> lawfully different target ──
    text = "Извини, что назвал тебя ржавой консервой"
    s1 = FakeConnection()
    s1.add("g_x", "Ты ржавая консерва", 0.4, age=2 * DAY)
    s1.add("g_y", "Ты дура", 0.9, age=5 * MIN)
    s2 = FakeConnection()
    s2.add("g_x", "Ты дура", 0.4, age=2 * DAY)
    s2.add("g_y", "Ты ржавая консерва", 0.9, age=5 * MIN)
    s3 = FakeConnection()
    s3.add("g_x", "Ты дура", 0.4, age=2 * DAY)
    s3.add("g_y", "Ты бесполезная", 0.9, age=3 * DAY)
    s4 = FakeConnection()
    s4.add("g_x", "Ты ржавая консерва", 0.9, age=2 * DAY)
    s4.add("g_y", "Ты дура", 0.4, age=5 * MIN)
    t1 = apply(s1, text)[0]["target"]
    t2 = apply(s2, text)[0]["target"]
    t3 = apply(s3, text)[0]
    t4 = apply(s4, text)[0]["target"]
    check("COUNTERFACTUAL: same text, the older mild grievance holds the quoted words -> it is chosen", t1 == "g_x", str(t1))
    check("COUNTERFACTUAL: same text, contents swapped -> the OTHER grievance is chosen (matching is causal)", t2 == "g_y", str(t2))
    check("COUNTERFACTUAL: same text, no grievance holds those words and none is recent -> no target", t3["target"] is None, repr(t3))
    check("COUNTERFACTUAL: swapping severities (heavy/light) does not move the target", t4 == "g_x" and t1 == t4, f"{t1} {t4}")

    # ── one apology never heals several grievances ──
    multi = FakeConnection()
    for i in range(3):
        multi.add(f"g{i}", "Ты ржавая консерва", 0.5 + i / 10, age=(i + 1) * 3 * HOUR)
    for g in multi.grievances.values():
        g["description"] = "Ты ржавая консерва " + g["id"] * 3
    r, ch = apply(multi, "Извини, что назвал тебя ржавой консервой")
    check("one apology changes at most one grievance even when several match", len(ch) == 1, repr(ch))

    # ── matcher is pure and takes no relational-state input ──
    src = inspect.getsource(rm.match_grievance_target) + inspect.getsource(rm._overlap) + inspect.getsource(rm._offense_time)
    check("RELATIONAL STATE != EVENT IDENTITY: matcher never reads capacity / affection / trust",
          not any(w in src.lower() for w in ("capacity", "affection", "trust", "forgiv")), "")
    check("matcher signature takes no connection (pure over already-read rows)",
          "conn" not in inspect.signature(rm.match_grievance_target).parameters)

    # ── PET level: G, and the apology reaching the memory layer with the CURRENT text ──
    import pet.chat_local as chat_local
    from llm_gateway.types import SemanticCompletionResult

    def sem(reply, state):
        return SemanticCompletionResult(reply=reply, state=state, reply_ok=True, state_ok=state is not None,
                                        parse_ok=True, error=None, metadata={})

    import agent.db.sql.shadow_write as shadow_write
    from pet.extraction_test_support import llm_from_state

    OWNER = chat_local._RELATIONSHIP_USER_ID

    def pet_turn(conn, messages, reply, state):
        """Real chat_local + real shadow_write wrappers, over the fake connection."""
        with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(conn)), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm",
                          lambda model: llm_from_state(messages[-1]["content"], state)), \
             patch.object(chat_local, "_call_model_semantic", lambda *a, **k: sem(reply, state)), \
             patch.object(chat_local, "shadow_add_grievance", lambda **kw: None):
            return chat_local._respond_with_character("heretic:q8", messages, 0.7)

    conn = FakeConnection()
    conn.add("g_old", OLD_SEVERE_TEXT, 0.9, age=3 * DAY, user_id=OWNER)
    conn.add("g_mild", MILD_TEXT, 0.35, age=5 * MIN, user_id=OWNER)
    before = conn.snapshot()
    pet_turn(conn, [{"role": "user", "content": "Извини, что только что назвал тебя бесполезной."}], "Ладно.",
             {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 0.8, "evidence": "Извини"})
    check("PET end-to-end: the apology text from the current turn selects the mild recent grievance, not the heaviest",
          conn.changed(before) == ["g_mild"], repr(conn.changed(before)))

    conn = FakeConnection()
    conn.add("g_old", OLD_SEVERE_TEXT, 0.9, age=3 * DAY, status="healing", user_id=OWNER)
    conn.add("g_mild", MILD_TEXT, 0.35, age=5 * MIN, user_id=OWNER)
    before = conn.snapshot()
    hist = [{"role": "user", "content": "Извини, я зря это сказал."}, {"role": "assistant", "content": "Ладно, проехали."},
            {"role": "user", "content": "Как ты?"}]
    for label, st in {"quote from history": {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 0.9, "evidence": "Извини, я зря это сказал."},
                      "no evidence": {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 0.9}}.items():
        pet_turn(conn, hist, "Нормально.", st)
        check(f"G: neutral current message + historical apology + contaminated state ({label}) -> provenance guard: no grievance touched",
              conn.changed(before) == [], repr(conn.changed(before)))

    chat_src = inspect.getsource(chat_local._apply_current_turn_event)
    check("PET does not pick the apology target itself: it applies the focus the reply was built around",
          "most_severe" not in chat_src and "shadow_get_relationship_context" not in chat_src
          and chat_src.count("shadow_apply_apology(") == 1 and 'memory_ctx' in chat_src)

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
