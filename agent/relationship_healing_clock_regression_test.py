"""
agent/relationship_healing_clock_regression_test.py — TEMPORAL SEMANTICS OF
FORGIVENESS.

Two different clocks:
  AGE OF THE OFFENSE        created_at (or updated_at of a recurrence)
  AGE OF THE HEALING PHASE  the first ACCEPTED (understood) apology of the current offense cycle

Forgiveness needs MIN_HEALING_HOURS of the healing phase, not of the offense.
An old grievance apologised for today has a healing age of about zero and must
not become forgiven instantly; a recurrence starts a new cycle.

Runs on an in-memory fake SQL connection with a controlled clock and synthetic
users only.

Run: python -m agent.relationship_healing_clock_regression_test
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import agent.db.sql.repositories as repo
import agent.relationship_memory as rm
from agent.relationship_apology_matching_regression_test import FakeConnection

FAILURES: list[str] = []
USER = "clock_user"
T0 = datetime(2026, 6, 1, 12, 0, 0)
H, DAY = timedelta(hours=1), timedelta(days=1)


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


class Clock:
    def __init__(self, start):
        self.now = start

    def advance(self, delta):
        self.now += delta

    def __call__(self):
        return self.now


def status(conn, gid):
    return conn.grievances[gid]["status"]


def main() -> int:
    clock = Clock(T0)
    with patch.object(rm, "_now", clock), patch.object(repo, "_now", clock):
        # ── 1. old grievance + apology accepted NOW ──
        clock.now = T0 - 30 * DAY
        conn = FakeConnection()
        gid = rm.add_grievance(conn, USER, "insult", "Ты сломал мой велосипед", 0.6)
        clock.now = T0
        g = conn.grievances[gid]
        check("1: offense age is 30 days", abs((clock() - rm._offense_time(g)).total_seconds() - 30 * 86400) < 1)
        check("1: no apology yet -> no healing phase (None), never 0 and never the offense age", rm._healing_age_hours(g) is None)

        rm.acknowledge_apology(conn, gid, 0.9)
        g = conn.grievances[gid]
        check("1: healing age right after the apology is ~0 (not 30 days)", abs(rm._healing_age_hours(g)) < 1e-6, repr(rm._healing_age_hours(g)))
        forgiven = rm.progress_healing(conn, gid)
        check("1: old offense + fresh sincere apology is NOT instantly forgiven", forgiven is False and status(conn, gid) == "healing",
              status(conn, gid))

        # ── 2. registered -> healing -> forgiven, and the cooldown in between ──
        clock.advance(1 * H)
        check("2: 1h into healing: still healing, not forgiven", rm.progress_healing(conn, gid) is False and status(conn, gid) == "healing")
        clock.advance(1 * H + timedelta(minutes=1))
        check("2: 2h+ into healing: forgiven", rm.progress_healing(conn, gid) is True and status(conn, gid) == "forgiven")
        check("2: forgiven_at recorded", conn.grievances[gid]["forgiven_at"] is not None)

        # ── 3. counterfactual: identical grievance and apology, only the apology time differs ──
        def scenario(apology_hours_ago):
            c = FakeConnection()
            clock.now = T0 - 30 * DAY
            g_id = rm.add_grievance(c, USER, "insult", "Ты сломал мой велосипед", 0.6)
            clock.now = T0 - apology_hours_ago * H
            rm.acknowledge_apology(c, g_id, 0.9)
            clock.now = T0
            return rm.progress_healing(c, g_id)
        check("3: counterfactual — apology 3h ago -> forgiven", scenario(3) is True)
        check("3: counterfactual — apology 10 min ago (same 30-day-old offense) -> not forgiven", scenario(1 / 6) is False)

        # ── 4. a repeated apology does not restart the healing clock ──
        clock.now = T0
        c = FakeConnection()
        gid = rm.add_grievance(c, USER, "insult", "Ты сломал мой велосипед", 0.6)
        rm.acknowledge_apology(c, gid, 0.9)
        first = c.grievances[gid]["understood_at"]
        clock.advance(1 * H)
        rm.acknowledge_apology(c, gid, 0.9)
        check("4: second apology keeps the phase start", c.grievances[gid]["understood_at"] == first)
        clock.advance(1 * H + timedelta(minutes=1))
        rm.acknowledge_apology(c, gid, 0.9)
        check("4: an apology 2h after the first one can complete forgiveness (a repeat does not delay it)",
              rm.progress_healing(c, gid) is True)

        # ── 4b. a plain "sorry" (acknowledged only) does not start the healing phase ──
        c = FakeConnection()
        clock.now = T0
        gid = rm.add_grievance(c, USER, "insult", "Ты сломал мой велосипед", 0.6)
        rm.acknowledge_apology(c, gid, 0.3)
        clock.advance(5 * H)
        check("4b: a plain sorry only acknowledges; no healing phase, nothing to forgive by time",
              status(c, gid) == "acknowledged" and rm._healing_age_hours(c.grievances[gid]) is None
              and rm.progress_healing(c, gid) is False)
        rm.acknowledge_apology(c, gid, 0.9)
        check("4b: the healing phase begins when the sincere apology is accepted (not at the earlier plain sorry)",
              abs(rm._healing_age_hours(c.grievances[gid])) < 1e-6 and rm.progress_healing(c, gid) is False)

        # ── 5. no apology -> never forgiven, however old ──
        c = FakeConnection()
        clock.now = T0 - 90 * DAY
        gid = rm.add_grievance(c, USER, "insult", "Ты сломал мой велосипед", 0.6)
        clock.now = T0
        check("5: an ancient grievance without an apology is never forgiven by time alone",
              rm.progress_healing(c, gid) is False and status(c, gid) == "registered")

        # ── 6. recurrence starts a NEW offense cycle ──
        c = FakeConnection()
        clock.now = T0
        gid = rm.add_grievance(c, USER, "insult", "Ты сломал мой велосипед", 0.6)
        rm.acknowledge_apology(c, gid, 0.9)
        clock.advance(3 * H)
        again = rm.add_grievance(c, USER, "insult", "Ты сломал мой велосипед (снова)", 0.6)
        g = c.grievances[gid]
        check("6: same offense recurs -> same grievance bumped", again == gid)
        check("6: the previous cycle's apology / understanding are cleared",
              g["status"] == "registered" and g["apology_at"] is None and g["understood_at"] is None and g["apology_sincerity"] == 0.0)
        check("6: earlier apology cannot forgive the new offense", rm.progress_healing(c, gid) is False)
        rm.acknowledge_apology(c, gid, 0.3)
        clock.advance(5 * H)
        check("6: a NEW low-sincerity apology is only 'acknowledged' — the old understanding does not carry over",
              status(c, gid) == "acknowledged" and rm.progress_healing(c, gid) is False)
        rm.acknowledge_apology(c, gid, 0.9)
        check("6: the new cycle's healing clock starts at the new sincere apology", abs(rm._healing_age_hours(c.grievances[gid])) < 1e-6)

        # ── 7. the other forgiveness gates are unchanged by the new clock ──
        c = FakeConnection()
        clock.now = T0 - 2 * DAY
        gid = rm.add_grievance(c, USER, "insult", "Ты сломал мой велосипед", 0.6)
        rm.acknowledge_apology(c, gid, 0.9)
        clock.now = T0
        c.capacities[USER]["capacity"] = 5.0
        check("7: enough time but forgiveness capacity too low -> not forgiven", rm.progress_healing(c, gid) is False)
        c.capacities[USER]["capacity"] = 80.0
        check("7: capacity restored -> forgiven", rm.progress_healing(c, gid) is True)

        # ── 8. existing rows need no migration ──
        c = FakeConnection()
        c.add("legacy_healing", "Ты сломал мой велосипед", 0.6, age=10 * DAY, status="healing", user_id=USER)
        c.grievances["legacy_healing"].update(apology_at=T0 - 3 * H, understood_at=T0 - 3 * H, apology_sincerity=0.8)
        c.capacities[USER] = {"user_id": USER, "capacity": 60.0, "last_forgiveness": None, "updated_at": None}
        # FakeConnection.add stamps rows with the real clock; the fixture times above are relative to T0
        c.grievances["legacy_healing"]["created_at"] = T0 - 10 * DAY
        check("8: an existing 'healing' row with an old understood_at is forgiven by the new clock (no schema change)",
              rm.progress_healing(c, "legacy_healing") is True)
        c.add("legacy_odd", "Другая обида", 0.5, age=10 * DAY, status="understood", user_id=USER)
        c.grievances["legacy_odd"]["created_at"] = T0 - 10 * DAY
        check("8: an 'understood' row without any understood_at is conservatively NOT forgiven",
              rm.progress_healing(c, "legacy_odd") is False)

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
