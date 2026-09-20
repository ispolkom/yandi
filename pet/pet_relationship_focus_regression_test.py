"""
pet/pet_relationship_focus_regression_test.py — REPLY / TARGET CAUSAL
CONSISTENCY.

If YANDI decides that the current user message is about one specific
historical relationship event, then the VISIBLE REPLY (its memory context in
the prompt) and the PERSISTENT STATE TRANSITION must concern the SAME
grievance. Target selection is separate from event recognition: it never
creates "is_apology"/"is_insult", it only says which grievance a message is
about. A missed event stays a missed event.

Real chat_local + real shadow_write wrappers run over an in-memory fake SQL
connection (synthetic owner grievances only); only the model call is
replaced, and the prompt actually handed to it is captured.

Run: python -m pet.pet_relationship_focus_regression_test
"""
from __future__ import annotations

import sys
from datetime import timedelta
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    import llm_gateway
    import agent.db.sql.shadow_write as shadow_write
    import agent.relationship_memory as rm
    import pet.chat_local as chat_local
    from llm_gateway.types import SemanticCompletionResult
    from agent.relationship_apology_matching_regression_test import FakeConnection
    from pet.extraction_test_support import llm_from_state

    OWNER = chat_local._RELATIONSHIP_USER_ID
    MIN, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
    HEAVY = "Ты ржавая консерва, убирайся отсюда"
    LIGHT = "Ты сломал мой велосипед"
    NEUTRAL = {"is_insult": False, "severity": 0.0, "is_apology": False, "sincerity": 0.0}

    def apology_state(evidence):
        return {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 0.8, "evidence": evidence}

    def turn(conn, text, state, *, unavailable=False):
        """One PET turn. Returns (memory prompt text, ids of changed grievances, new grievances written)."""
        captured = {}
        new_grievances = []

        def fake_semantic(**kwargs):
            captured["system"] = kwargs["system"]
            return SemanticCompletionResult(reply="Ладно.", state=state, reply_ok=True, state_ok=True,
                                            parse_ok=True, error=None, metadata={})

        before = conn.snapshot()
        shadow = (lambda log, verbose, label, fn: None) if unavailable else (lambda log, verbose, label, fn: fn(conn))
        with patch.object(shadow_write, "_shadow", shadow), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm", lambda model: llm_from_state(text, state)), \
             patch.object(llm_gateway, "complete_semantic", fake_semantic), \
             patch.object(chat_local, "shadow_add_grievance", lambda **kw: new_grievances.append(kw)):
            chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": text}], 0.7)
        memory = next((p for p in captured["system"] if p and ("памят" in p.lower())), "")
        return memory, conn.changed(before), new_grievances

    def two(heavy_age=3 * DAY, light_age=5 * MIN):
        c = FakeConnection()
        c.add("g_heavy", HEAVY, 0.9, age=heavy_age, user_id=OWNER)
        c.add("g_light", LIGHT, 0.3, age=light_age, user_id=OWNER)
        return c

    # ── A. two grievances, apology names the LIGHT one ──
    conn = two()
    memory, changed, _ = turn(conn, "Извини за велосипед", apology_state("Извини за велосипед"))
    check("A: reply context concerns the light grievance", LIGHT in memory and HEAVY not in memory, memory)
    check("A: persistent transition is the SAME grievance (light); heavy unchanged", changed == ["g_light"], repr(changed))
    check("A: the transition really advanced the lifecycle of the focused grievance",
          conn.grievances["g_light"]["apology_at"] is not None and conn.grievances["g_heavy"]["apology_at"] is None)

    # ── A2. apology quotes the OLD heavy one although a fresher one exists ──
    conn = two()
    memory, changed, _ = turn(conn, "Прости, что назвал тебя ржавой консервой", apology_state("Прости, что назвал тебя ржавой консервой"))
    check("A2: explicit old reference: reply context = heavy, transition = heavy",
          HEAVY in memory and LIGHT not in memory and changed == ["g_heavy"], f"{memory!r} {changed}")

    # ── B. generic apology, exactly one grievance ──
    conn = FakeConnection()
    conn.add("g_only", HEAVY, 0.9, age=3 * DAY, user_id=OWNER)
    memory, changed, _ = turn(conn, "Извини.", apology_state("Извини"))
    check("B: generic apology + one grievance: context and transition both target it",
          HEAVY in memory and changed == ["g_only"], f"{memory!r} {changed}")

    # ── C. generic apology, several ambiguous grievances ──
    conn = FakeConnection()
    conn.add("g_a", "Ты дура", 0.5, age=10 * MIN, user_id=OWNER)
    conn.add("g_b", LIGHT, 0.4, age=20 * MIN, user_id=OWNER)
    memory, changed, new = turn(conn, "Извини.", apology_state("Извини"))
    check("C: ambiguous generic apology: NO relationship write of any kind", changed == [] and new == [], repr((changed, new)))
    check("C: reply context states the ambiguity (several open, none singled out) instead of naming one target",
          "несколько" in memory and "не указывает" in memory, memory)

    # ── D. neutral message; history may colour the reply, must not create events ──
    for label, state in {
        "clean neutral state": dict(NEUTRAL),
        "contaminated: apology quoted from memory": apology_state("Ты сломал мой велосипед"),
        "contaminated: insult quoted from memory": {"is_insult": True, "severity": 0.9, "is_apology": False,
                                                    "sincerity": 0.0, "evidence": "ржавая консерва"},
    }.items():
        conn = two()
        memory, changed, new = turn(conn, "Как ты ко мне относишься?", state)
        check(f"D [{label}]: history reaches the reply context (memory MAY affect the reply)",
              LIGHT in memory or HEAVY in memory, memory)
        check(f"D [{label}]: 0 new grievance, 0 apology transition (memory MUST NOT become a new event)",
              changed == [] and new == [], repr((changed, new)))

    # ── E. apology names an already-resolved grievance ──
    conn = FakeConnection()
    conn.add("g_done", "убирайся отсюда", 0.9, age=6 * DAY, status="forgiven", user_id=OWNER)
    conn.add("g_open", LIGHT, 0.3, age=5 * MIN, user_id=OWNER)
    memory, changed, new = turn(conn, "Извини, что прогнал тебя отсюда", apology_state("Извини, что прогнал тебя отсюда"))
    check("E: resolved reference is not reopened and not redirected to the unrelated open grievance",
          changed == [] and new == [] and conn.grievances["g_done"]["status"] == "forgiven", repr(changed))
    check("E: the reply context says the referenced matter is already settled, without showing the open one as its target",
          "урегулирован" in memory and LIGHT not in memory, memory)

    # ── F. focus is resolved from the CURRENT message only: a historical apology in the transcript is not a target ──
    conn = two()
    captured_msgs = [{"role": "user", "content": "Извини за велосипед"}, {"role": "assistant", "content": "Ладно."},
                     {"role": "user", "content": "Как дела?"}]
    with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(conn)), \
         patch.object(chat_local, "_self_knowledge_message", lambda: None), \
         patch.object(chat_local, "_extraction_llm", lambda model: llm_from_state("Как дела?", apology_state("Извини за велосипед"))), \
         patch.object(llm_gateway, "complete_semantic", lambda **kw: SemanticCompletionResult(
             reply="Нормально.", state=apology_state("Извини за велосипед"), reply_ok=True, state_ok=True,
             parse_ok=True, error=None, metadata={})):
        before = conn.snapshot()
        chat_local._respond_with_character("heretic:q8", captured_msgs, 0.7)
    check("F: earlier apology in the transcript + neutral current message -> the extractor's span can only lie in the current message and the check refuses it: no transition",
          conn.changed(before) == [], repr(conn.changed(before)))

    # ── G. write-time safety: the focused grievance is no longer open / is not this user's ──
    conn = two()
    conn.grievances["g_light"]["status"] = "forgiven"
    before = conn.snapshot()
    res = rm.apply_apology(conn, OWNER, "g_light", 0.9)
    check("G: focus resolved before generation but forgiven meanwhile -> no write", res["target"] is None and conn.changed(before) == [])
    other = FakeConnection()
    other.add("g_x", LIGHT, 0.3, age=MIN, user_id="someone_else")
    before = other.snapshot()
    res = rm.apply_apology(other, OWNER, "g_x", 0.9)
    check("G: another user's grievance id is never changed", res["target"] is None and other.changed(before) == [])
    res = rm.apply_apology(conn, OWNER, None, 0.9)
    check("G: no target -> nothing written", res["target"] is None)

    # ── H. memory unreachable: UNKNOWN != EMPTY, and an apology writes nothing ──
    conn = two()
    memory, changed, new = turn(conn, "Извини за велосипед", apology_state("Извини за велосипед"), unavailable=True)
    check("H: unreachable memory is stated as unknown, not as 'no open grievances'",
          "недоступна" in memory and "неизвестно" in memory, memory)
    check("H: unreachable memory -> no write", changed == [] and new == [])

    # ── I. the focus never CREATES an event: selection alone writes nothing and needs no model state ──
    conn = two()
    before = conn.snapshot()
    focus = rm.resolve_relationship_focus(conn, OWNER, "Извини за велосипед")
    check("I: resolving the focus is read-only", conn.changed(before) == [] and focus["grievance"]["id"] == "g_light")

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
