"""
agent/relationship_state_regression_test.py — CANONICAL RELATIONSHIP STATE.

    EVENTS WRITE STATE. STATE DOES NOT INVENT EVENTS. THE LLM DOES NOT WRITE
    RELATIONSHIP STATE.

trust / respect / affection (agent/relationship_state.py) move only through
deterministic event -> delta rules called from the grievance lifecycle
(agent/relationship_memory.py), which itself runs only on events already
validated upstream. Synthetic person id, in-memory fake SQL, no model.

Run: python -m agent.relationship_state_regression_test
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import agent.db.sql.repositories as repo
import agent.relationship_memory as rm
import agent.relationship_state as rs
from agent.relationship_apology_matching_regression_test import FakeConnection

FAILURES: list[str] = []
P = "person_a"


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def close(a: float, b: float, eps: float = 1e-6) -> bool:
    return abs(a - b) < eps


def main() -> int:
    # ── 1. read-only, defaults ──
    c = FakeConnection()
    s0 = rs.get_state(c, P)
    check("1: a person with no history sits at the defaults (50 / 50 / 30) and capacity 50",
          (s0["trust"], s0["respect"], s0["affection"], s0["forgiveness_capacity"]) == (50.0, 50.0, 30.0, 50.0), repr(s0))
    check("1: reading the state writes nothing (STATE DOES NOT INVENT EVENTS)",
          c.inner_state == {} and c.inner_state_events == [] and c.grievances == {} and c.capacities == {})

    # ── 2. a validated insult: deltas, distinct meanings, audit trail ──
    rs.record_insult(c, P, 0.6)
    s1 = rs.get_state(c, P)
    check("2: insult of severity 0.6 -> respect -12, trust -4.8, affection -1.8",
          close(s1["respect"], 38.0) and close(s1["trust"], 45.2) and close(s1["affection"], 28.2), repr(s1))
    check("2: the three coordinates mean different things: an insult hits respect > trust > affection",
          (s0["respect"] - s1["respect"]) > (s0["trust"] - s1["trust"]) > (s0["affection"] - s1["affection"]) > 0)
    ev = c.inner_state_events
    check("2: the change is recorded in the event trail (why the state moved)",
          len(ev) == 1 and ev[0]["event_type"] == "insult" and "respect-12.0" in ev[0]["description"], repr(ev))
    for _ in range(12):
        rs.record_insult(c, P, 1.0)
    sn = rs.get_state(c, P)
    check("2: coordinates are clamped to 0..100", sn["respect"] == 0.0 and sn["trust"] >= 0.0 and sn["affection"] >= 0.0, repr(sn))

    # ── 3. apology != trust restored ──
    c = FakeConnection()
    gid = rm.add_grievance(c, P, "insult", "Ты сломал мой велосипед", 0.6)
    after_insult = rs.get_state(c, P)
    rm.acknowledge_apology(c, gid, 0.3)
    check("3: a plain low-sincerity sorry changes no coordinate", rs.get_state(c, P) == after_insult)
    rm.acknowledge_apology(c, gid, 0.9)
    after_apology = rs.get_state(c, P)
    expected_recovery = rs.APOLOGY_RESPECT_RECOVERY_SHARE * rs.INSULT_RESPECT_PER_SEVERITY * 0.6 * 0.9
    check("3: an accepted apology gives back a bounded share of the lost respect",
          close(after_apology["respect"] - after_insult["respect"], expected_recovery), repr(after_apology))
    check("3: APOLOGY != TRUST RESTORED — trust and affection are untouched",
          after_apology["trust"] == after_insult["trust"] and after_apology["affection"] == after_insult["affection"])
    check("3: the recovery is smaller than the damage (respect stays below its pre-offense level)",
          after_apology["respect"] < 50.0)
    rm.acknowledge_apology(c, gid, 0.9)
    check("3: repeating the apology gives nothing more (no farming)", rs.get_state(c, P) == after_apology)

    # ── 4. insult/apology cycles never net-improve the relationship ──
    c = FakeConnection()
    trace = []
    for i in range(4):
        g = rm.add_grievance(c, P, "insult", f"обида номер {i} " + "x" * 20, 0.6)
        rm.acknowledge_apology(c, g, 0.95)
        trace.append(rs.get_state(c, P)["respect"])
    check("4: respect falls monotonically over insult+apology cycles", all(b < a for a, b in zip(trace, trace[1:])), repr(trace))
    check("4: trust never recovers from apologies alone", rs.get_state(c, P)["trust"] < 50.0 - 4 * 4.0)

    # ── 5. the persona's dynamics are code parameters: same event, different persona, different delta ──
    def respect_after_insult():
        cc = FakeConnection()
        rs.record_insult(cc, P, 0.5)
        return rs.get_state(cc, P)["respect"]
    base = respect_after_insult()
    with patch.object(rs, "INSULT_RESPECT_PER_SEVERITY", 40.0):
        thin_skinned = respect_after_insult()
    check("5: COUNTERFACTUAL — same event, a thinner-skinned persona loses more respect", thin_skinned < base, f"{base} vs {thin_skinned}")

    # ── 6. state is keyed by the PERSON; other people are untouched ──
    c = FakeConnection()
    rm.add_grievance(c, "person_a", "insult", "Ты сломал мой велосипед", 0.9)
    check("6: another person's state stays at the defaults",
          rs.get_state(c, "person_b")["respect"] == 50.0 and "person_b" not in c.inner_state)

    # ── 7. a failing state write never breaks the grievance lifecycle, and is logged ──
    c = FakeConnection()
    with patch.object(repo, "update_inner_state", side_effect=RuntimeError("no grant")), \
         patch.object(rs.log, "warning") as warn:
        gid = rm.add_grievance(c, P, "insult", "Ты сломал мой велосипед", 0.6)
    check("7: the grievance is still registered when the state write fails", gid in c.grievances)
    check("7: ...and the failure is logged, not swallowed silently", warn.called)

    # ── 8. the write API takes events, never coordinates ──
    import inspect
    check("8: write functions accept an event severity/sincerity, not coordinate values",
          list(inspect.signature(rs.record_insult).parameters) == ["conn", "user_id", "severity"]
          and list(inspect.signature(rs.record_accepted_apology).parameters) == ["conn", "user_id", "offense_severity", "sincerity"])

    # ── 9. trust from a directly OBSERVED delivery: bounded, shrinking, ceilinged, code-owned, strict ──
    rewards = [rs.observed_trust_reward(k) for k in range(20)]
    check("9: the reward starts at the base, shrinks with every earlier proof, and is zero once negligible",
          rewards[0] == rs.OBSERVED_TRUST_BASE and all(rewards[i] >= rewards[i + 1] for i in range(19)) and rewards[-1] == 0.0
          and all(r >= 0 for r in rewards), repr(rewards))
    check("9: the whole lifetime total is bounded (a geometric series: base / (1 - decay)) and far below a single verified-kept promise's worth of 8 plus",
          sum(rewards) < rs.OBSERVED_TRUST_BASE / (1 - rs.OBSERVED_TRUST_DECAY) and sum(rewards) < 5.0)
    check("9: a serious harm costs more than the first reward and more than all later ones together",
          rs.INSULT_TRUST_PER_SEVERITY > rs.OBSERVED_TRUST_BASE and rs.INSULT_TRUST_PER_SEVERITY > sum(rewards[1:]))
    check("9: a negative or absurd count cannot enlarge the reward", rs.observed_trust_reward(-5) == rs.OBSERVED_TRUST_BASE and rs.observed_trust_reward(10**6) == 0.0)
    c = FakeConnection()
    got = rs.record_observed_commitment(c, P, 0)
    s = rs.get_state(c, P)
    check("9: one observed delivery moves TRUST only (respect and affection untouched), by the reward",
          got == rs.OBSERVED_TRUST_BASE and close(s["trust"], 50.0 + rs.OBSERVED_TRUST_BASE) and s["respect"] == 50.0 and s["affection"] == 30.0, repr(s))
    check("9: the audit row carries the magnitude and replay rebuilds the state from the trail alone",
          [(e["event_type"], e["weight"]) for e in c.inner_state_events] == [("commitment_observed", rs.OBSERVED_TRUST_BASE)]
          and all(close(rs.replay(c, P)[k], s[k]) for k in rs.COORDINATES))
    high = FakeConnection()
    high.inner_state[P] = {"user_id": P, "trust": 90.0, "respect": 50.0, "affection": 30.0}
    rs.record_observed_commitment(high, P, 0)
    check("9: ABOVE the ceiling an observed delivery adds nothing and never subtracts", rs.get_state(high, P)["trust"] == 90.0)
    near = FakeConnection()
    near.inner_state[P] = {"user_id": P, "trust": rs.OBSERVED_TRUST_CEILING - 0.5, "respect": 50.0, "affection": 30.0}
    rs.record_observed_commitment(near, P, 0)
    check("9: near the ceiling the reward is cut so trust lands on it exactly", close(rs.get_state(near, P)["trust"], rs.OBSERVED_TRUST_CEILING))
    check("9: replay of a trail with observed rewards clamps at the ceiling exactly like the live path",
          all(close(rs.replay(near, P)[k], rs.get_state(near, P)[k]) for k in ("respect", "affection")))
    check("9: the write API takes the count of earlier proofs, never a coordinate or a reward",
          list(inspect.signature(rs.record_observed_commitment).parameters) == ["conn", "user_id", "prior_observed"])
    broken = FakeConnection()
    try:
        with patch.object(repo, "update_inner_state", side_effect=RuntimeError("no grant")), patch.object(rs.log, "warning"):
            rs.record_observed_commitment(broken, P, 0)
        strict = False
    except RuntimeError:
        strict = True
    check("9: unlike the older events, this transition NEVER swallows a failed write: the caller's transaction must roll the proof back", strict)
    lenient = FakeConnection()
    with patch.object(repo, "update_inner_state", side_effect=RuntimeError("no grant")), patch.object(rs.log, "warning"):
        rs.record_insult(lenient, P, 0.6)
    check("9: (the older events keep their documented fail-open behaviour)", lenient.inner_state_events == [])
    healed = FakeConnection()
    rs.record_insult(healed, P, 1.0)
    trust_lost = rs.get_state(healed, P)["trust"]
    rs.record_accepted_apology(healed, P, 1.0, 1.0)
    check("9: APOLOGY != TRUST RESTORATION — an accepted apology never gives trust back", rs.get_state(healed, P)["trust"] == trust_lost)

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
