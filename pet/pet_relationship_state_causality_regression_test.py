"""
pet/pet_relationship_state_causality_regression_test.py — DISCRETE EVENTS ->
CONTINUOUS RELATIONSHIP STATE -> FUTURE BEHAVIOUR.

On the personal-chat path the continuous relationship state is
`forgiveness_capacity` (owned by agent/relationship_memory.py). Direction of
causation must be EVENT -> STATE CHANGE, never STATE -> invented event:

  * offenses, recurrences, accepted apologies and forgiveness move it;
  * it is stated to the model as a plain fact, so it can change the reply;
  * it gates forgiveness, so it changes later persistent behaviour;
  * reading or stating it never writes an event.

A persistent state counts as functionally real only if changing or removing it
counterfactually changes what happens next. Synthetic owner, in-memory fake
SQL, controlled clock; no model is called.

Run: python -m pet.pet_relationship_state_causality_regression_test
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from unittest.mock import patch

FAILURES: list[str] = []
T0 = datetime(2026, 6, 1, 12, 0, 0)
H = timedelta(hours=1)


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


class Clock:
    def __init__(self):
        self.now = T0

    def advance(self, delta):
        self.now += delta

    def __call__(self):
        return self.now


def main() -> int:
    import agent.db.sql.repositories as repo
    import agent.db.sql.shadow_write as shadow_write
    import agent.relationship_memory as rm
    import pet.chat_local as chat_local
    from agent.relationship_apology_matching_regression_test import FakeConnection

    OWNER = chat_local._RELATIONSHIP_USER_ID
    clock = Clock()

    def capacity(conn):
        return repo.get_forgiveness_capacity(conn, OWNER)["capacity"]

    def context(conn, text="Как ты ко мне относишься?"):
        with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(conn)):
            return shadow_write.shadow_get_relationship_context(user_id=OWNER, current_text=text)

    with patch.object(rm, "_now", clock), patch.object(repo, "_now", clock):
        # ── 1. EVENT -> STATE: every lifecycle event that should move the state does, once ──
        conn = FakeConnection()
        check("1: a user with no history sits at the default 50", capacity(conn) == 50.0)
        gid = rm.add_grievance(conn, OWNER, "insult", "Ты сломал мой велосипед", 0.6)
        c1 = capacity(conn)
        check("1: a new offense lowers the state by 10 x severity", abs(c1 - 44.0) < 1e-6, str(c1))
        rm.add_grievance(conn, OWNER, "insult", "Ты сломал мой велосипед (опять)", 0.6)
        c2 = capacity(conn)
        check("1: a REPEATED offense lowers it too (proportional to the severity it added)", c2 < c1 and abs(c1 - c2 - 1.8) < 1e-6, f"{c1} -> {c2}")
        rm.acknowledge_apology(conn, gid, 0.3)
        check("1: a plain low-sincerity sorry does not restore it", capacity(conn) == c2)
        rm.acknowledge_apology(conn, gid, 0.9)
        c3 = capacity(conn)
        check("1: an accepted apology restores it", abs(c3 - (c2 + 4.5)) < 1e-6, f"{c2} -> {c3}")
        rm.acknowledge_apology(conn, gid, 0.9)
        check("1: repeating the same apology restores nothing more (no farming of the state)", capacity(conn) == c3)
        clock.advance(3 * H)
        check("1: forgiveness completes", rm.progress_healing(conn, gid) is True)
        c4 = capacity(conn)
        check("1: forgiveness restores it further", abs(c4 - (c3 + 10)) < 1e-6, f"{c3} -> {c4}")

        # ── 2. STATE -> CONTEXT: the state is stated to the model as a fact, and follows the events ──
        ctx_low = None
        damaged = FakeConnection()
        for i, text in enumerate(("Ты дура", "Ты сломал мой велосипед", "Ты бесполезная")):
            rm.add_grievance(damaged, OWNER, "insult", text, 0.8)
        ctx_low = context(damaged)
        neutral = FakeConnection()
        ctx_neutral = context(neutral)
        check("2: the shadow context carries the continuous state", isinstance(ctx_low.get("forgiveness_capacity"), float)
              and ctx_low["forgiveness_capacity"] < ctx_neutral["forgiveness_capacity"] == 50.0,
              f"{ctx_low.get('forgiveness_capacity')} vs {ctx_neutral.get('forgiveness_capacity')}")
        msg_low = chat_local._memory_context_message(ctx_low)
        msg_neutral = chat_local._memory_context_message(ctx_neutral)
        check("2: COUNTERFACTUAL — same message, damaged vs neutral relationship: the model's context differs",
              msg_low != msg_neutral and "способность прощать этого человека: низкая" in msg_low
              and "способность прощать этого человека: средняя" in msg_neutral,
              f"{msg_low!r} | {msg_neutral!r}")
        high = FakeConnection()
        high.capacities[OWNER] = {"user_id": OWNER, "capacity": 92.0, "last_forgiveness": None, "updated_at": None}
        check("2: the third band follows the state as well (92 -> высокая)",
              "способность прощать этого человека: высокая" in chat_local._memory_context_message(context(high)))
        check("2: the state is a qualitative fact; no raw number is offered for the model to recite",
              not any(ch.isdigit() for ch in chat_local._capacity_fact(ctx_low)), chat_local._capacity_fact(ctx_low))
        # remove / change the persistent state -> the context returns to the neutral one
        damaged.capacities.clear()
        for g in damaged.grievances.values():
            g["status"] = "forgiven"
        msg_restored = chat_local._memory_context_message(context(damaged))
        check("2: COUNTERFACTUAL — removing the persistent state brings the context back to neutral", msg_restored == msg_neutral, msg_restored)
        check("2: the state is stated as a fact, not as an instruction how to feel",
              not any(w in msg_low.lower() for w in ("реагируй", "чувству", "тон должен", "покажи", "должна")), msg_low)

        # ── 3. STATE -> BEHAVIOUR: the state gates the next persistent transition ──
        def apology_outcome(prior_offenses):
            c = FakeConnection()
            clock.now = T0
            for text in prior_offenses:
                rm.add_grievance(c, OWNER, "insult", text, 0.9)
            target = rm.add_grievance(c, OWNER, "insult", "Ты сломал мой велосипед", 0.6)
            rm.apply_apology(c, OWNER, target, 0.9)
            clock.advance(3 * H)
            return rm.progress_healing(c, target), capacity(c)
        forgiven_neutral, cap_n = apology_outcome([])
        forgiven_damaged, cap_d = apology_outcome(["Ты дура " + "а" * 25, "Ты бесполезная " + "б" * 25, "Ты слабая " + "в" * 25])
        check("3: COUNTERFACTUAL — same apology, same elapsed time: neutral history forgives", forgiven_neutral is True, str(cap_n))
        check("3: COUNTERFACTUAL — same apology, same elapsed time: a damaged relationship does NOT",
              forgiven_damaged is False and cap_d < cap_n, f"capacity {cap_d}")

        # ── 4. direction: the state never creates events; stating it writes nothing ──
        c = FakeConnection()
        for text in ("Ты дура", "Ты сломал мой велосипед"):
            rm.add_grievance(c, OWNER, "insult", text, 0.9)
        before, cap_before = c.snapshot(), dict(c.capacities)
        ctx = context(c)
        chat_local._memory_context_message(ctx)
        check("4: reading the state and building the prompt writes nothing",
              c.changed(before) == [] and c.capacities == cap_before and len(c.grievances) == len(before))

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
