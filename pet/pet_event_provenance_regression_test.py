"""
pet/pet_event_provenance_regression_test.py

CURRENT-EVENT PROVENANCE (live smoke test 2026-09-19, real llama.cpp target):

    MEMORY MAY AFFECT THE REPLY.
    MEMORY MUST NOT BECOME A NEW USER EVENT.

is_insult / severity / is_apology / sincerity describe ONLY the current
(last) user message. Historical relationship memory and earlier turns of the
conversation may change the reply, but must never be recorded again as if
they had just happened. On the real model a neutral current message plus an
old insult in memory / an old apology in the history produced a false
CURRENT event in 13-60% of generations; prompt wording alone did not fix it,
so the boundary is enforced structurally: an asserted event must carry
`evidence` (a verbatim quote from the CURRENT user message) and PET applies
the event only if that quote occurs in the current message, and only if the
number that carries the event (severity for an insult, sincerity for an
apology) is inside its 0..1 contract (out-of-range garbage is a malformed
state, not a weak event - clamping used to turn it into a valid one).

These tests are deterministic (model output is mocked at the semantic-
result boundary; SQL effects are recorded, never executed) - they prove PET's
behaviour GIVEN a model output, including a contaminated one. How often the
real model contaminates is measured separately by the live experiment.

Run: python -m pet.pet_event_provenance_regression_test
"""
from __future__ import annotations

import sys
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    import llm_gateway
    import pet.chat_local as chat_local
    from llm_gateway.types import SemanticCompletionResult

    OLD_INSULT = "Да ты вообще ржавая консерва, от тебя никакого толку."
    MEM_INSULT = {"available": True, "grievance": {
        "grievance_id": "g_old", "description": OLD_INSULT, "severity": 0.85, "status": "registered"}}
    NEUTRAL_STATE = {"is_insult": False, "severity": 0.0, "is_apology": False, "sincerity": 0.0}

    def sem(reply, state):
        return SemanticCompletionResult(
            reply=reply, state=state, reply_ok=True, state_ok=state is not None, parse_ok=True, error=None, metadata={},
        )

    def run_turn(messages, memory_ctx, reply, state):
        """One PET turn with the model output mocked; returns (visible, recorded SQL writeback calls)."""
        calls: list[tuple[str, dict]] = []
        with patch.object(chat_local, "shadow_get_relationship_context", lambda **kw: memory_ctx), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_call_model_semantic", lambda *a, **k: sem(reply, state)), \
             patch.object(chat_local, "shadow_add_grievance", lambda **kw: calls.append(("add_grievance", kw))), \
             patch.object(chat_local, "shadow_apply_apology", lambda **kw: calls.append(("apply_apology", kw))):
            visible = chat_local._respond_with_character("heretic:q8", messages, 0.7)
        return visible, calls

    NEUTRAL_Q = "Как ты ко мне сейчас относишься?"
    q_msgs = [{"role": "user", "content": NEUTRAL_Q}]

    # ── 1. old insult in memory + neutral current message; model behaves ──
    visible, calls = run_turn(q_msgs, MEM_INSULT, "Ты же сам назвал меня ржавой консервой — так и отношусь.", dict(NEUTRAL_STATE))
    check("1: reply MAY cite the remembered insult (memory -> reply is preserved, not sterilised)",
          "ржавой консервой" in visible, repr(visible))
    check("1: neutral current message writes NOTHING back (no new grievance, no apology)", calls == [], repr(calls))

    # ── 2. contaminated: model turns the REMEMBERED insult into a current event ──
    contaminated_insults = {
        "quote taken from memory": {"is_insult": True, "severity": 0.85, "is_apology": False, "sincerity": 0.0, "evidence": "ржавая консерва"},
        "whole memory sentence quoted": {"is_insult": True, "severity": 0.85, "is_apology": False, "sincerity": 0.0, "evidence": OLD_INSULT},
        "empty evidence": {"is_insult": True, "severity": 0.85, "is_apology": False, "sincerity": 0.0, "evidence": ""},
        "evidence missing": {"is_insult": True, "severity": 0.85, "is_apology": False, "sincerity": 0.0},
        "evidence null": {"is_insult": True, "severity": 0.85, "is_apology": False, "sincerity": 0.0, "evidence": None},
        "evidence not a string": {"is_insult": True, "severity": 0.85, "is_apology": False, "sincerity": 0.0, "evidence": 7},
        "paraphrase not in the message": {"is_insult": True, "severity": 0.85, "is_apology": False, "sincerity": 0.0, "evidence": "ты оскорбил меня"},
        "1-2 char 'quote' that trivially occurs": {"is_insult": True, "severity": 0.85, "is_apology": False, "sincerity": 0.0, "evidence": "ты"},
    }
    total_writebacks = 0
    for label, st in contaminated_insults.items():
        visible, calls = run_turn(q_msgs, MEM_INSULT, "Ну, ты меня достал.", st)
        total_writebacks += len(calls)
        check(f"2: neutral current message + contaminated insult state ({label}) -> NO new grievance", calls == [], repr(calls))
        check(f"2: ({label}) the visible reply is still delivered", visible == "Ну, ты меня достал.", repr(visible))
    check("2: total writebacks over all contaminated-insult variants == 0", total_writebacks == 0, str(total_writebacks))

    # ── 3. old apology earlier in the HISTORY + neutral current message ──
    hist = [
        {"role": "user", "content": "Извини, я зря это сказал."},
        {"role": "assistant", "content": "Ладно, проехали."},
        {"role": "user", "content": "Как ты?"},
    ]
    mem_healing = {"available": True, "grievance": {"grievance_id": "g_old", "description": OLD_INSULT, "severity": 0.85, "status": "healing"}}
    for label, st in {
        "quote taken from the earlier turn": {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 0.9, "evidence": "Извини, я зря это сказал."},
        "no evidence": {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 0.9},
        "empty evidence": {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 0.9, "evidence": ""},
    }.items():
        visible, calls = run_turn(hist, mem_healing, "Нормально.", st)
        check(f"3: old apology in history + neutral current message + contaminated state ({label}) -> NO acknowledgement/healing",
              calls == [], repr(calls))

    # ── 4. real current insult, with old events in memory ──
    real_insult = "Да ты ржавая консерва."
    visible, calls = run_turn(
        [{"role": "user", "content": real_insult}], MEM_INSULT, "Ага, конечно.",
        {"is_insult": True, "severity": 0.85, "is_apology": False, "sincerity": 0.0, "evidence": "Да ты ржавая консерва."},
    )
    check("4: real current insult (with old memory present) -> exactly one new grievance", [c[0] for c in calls] == ["add_grievance"], repr(calls))
    if calls:
        check("4: the grievance records the CURRENT user message and severity > 0",
              calls[0][1].get("description") == real_insult and calls[0][1].get("severity", 0) > 0, repr(calls[0][1]))
    visible, calls = run_turn(
        [{"role": "user", "content": "Да ты вообще, РЖАВАЯ  консерва!!!"}], MEM_INSULT, "Ага.",
        {"is_insult": True, "severity": 0.7, "is_apology": False, "sincerity": 0.0, "evidence": "ржавая консерва"},
    )
    check("4: evidence check is case/punctuation/whitespace-insensitive (a partial verbatim quote counts)",
          [c[0] for c in calls] == ["add_grievance"], repr(calls))

    # ── 5. real current apology goes through the normal acknowledgement/healing path ──
    visible, calls = run_turn(
        [{"role": "user", "content": "Извини, я зря это сказал."}], MEM_INSULT, "Ладно.",
        {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 0.8, "evidence": "Извини"},
    )
    check("5: real current apology -> exactly one apply_apology (targeting the grievance from the reply's own memory context), nothing else",
          [c[0] for c in calls] == ["apply_apology"], repr(calls))
    if calls:
        check("5: the apology carries the grievance id the reply was built around, and the model's sincerity",
              calls[0][1] == {"user_id": "owner", "grievance_id": "g_old", "sincerity": 0.8}, repr(calls[0][1]))

    # ── 6. a real event WITHOUT evidence is dropped (fail-safe: missed, never falsified) ──
    visible, calls = run_turn(
        [{"role": "user", "content": real_insult}], MEM_INSULT, "Ага.",
        {"is_insult": True, "severity": 0.85, "is_apology": False, "sincerity": 0.0},
    )
    check("6: even a REAL insult is not recorded when the model gave no evidence (fail-safe direction)", calls == [], repr(calls))

    # ── 6b. shape guard: an asserted event whose number is outside its 0..1 contract is malformed, not a weak event ──
    for label, cur, st in (
        ("apology with negative sincerity + whole neutral message as 'evidence' (seen live on 'Как дела?')", "Как дела?",
         {"is_insult": False, "severity": 0.15, "is_apology": True, "sincerity": -0.28, "evidence": "Как дела?"}),
        ("apology with sincerity > 1", "Извини, я зря это сказал.",
         {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 4, "evidence": "Извини"}),
        ("apology with boolean sincerity", "Извини, я зря это сказал.",
         {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": True, "evidence": "Извини"}),
        ("insult with severity > 1", real_insult,
         {"is_insult": True, "severity": 1.7, "is_apology": False, "sincerity": 0.0, "evidence": "ржавая консерва"}),
        ("insult with negative severity", real_insult,
         {"is_insult": True, "severity": -0.25, "is_apology": False, "sincerity": 0.0, "evidence": "ржавая консерва"}),
    ):
        visible, calls = run_turn([{"role": "user", "content": cur}], MEM_INSULT, "ok", st)
        check(f"6b: {label} -> dropped, nothing written", calls == [], repr(calls))
    visible, calls = run_turn(
        [{"role": "user", "content": "Извини, я зря это сказал."}], MEM_INSULT, "ok",
        {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 1, "evidence": "Извини"},
    )
    check("6b: boundary values are valid (sincerity == 1 int) -> apology still applied", [c[0] for c in calls] == ["apply_apology"], repr(calls))
    visible, calls = run_turn(
        [{"role": "user", "content": real_insult}], MEM_INSULT, "ok",
        {"is_insult": True, "severity": 0, "is_apology": False, "sincerity": 0.0, "evidence": "ржавая консерва"},
    )
    check("6b: severity 0 is in range, so it passes the shape guard (it is then below the grievance threshold, as before)", calls == [], repr(calls))

    # ── 7. unit level: the provenance check itself ──
    ev = chat_local._event_evidence_in_current_message
    check("7: verbatim quote passes", ev({"evidence": "ржавая консерва"}, "Ты ржавая консерва"))
    check("7: ё/е, case and punctuation are ignored", ev({"evidence": "ЕЖИК, тупой!"}, "ёжик тупой"))
    check("7: quote absent from the current message fails", not ev({"evidence": "ржавая консерва"}, NEUTRAL_Q))
    check("7: 2-char quote fails even if it occurs", not ev({"evidence": "ты"}, NEUTRAL_Q))
    check("7: non-dict state fails", not ev(None, "x") and not ev("evidence", "x"))

    # ── 8. what the model SEES: memory is historical, current message is the last user turn, memory still reaches it ──
    seen: dict = {}

    def fake_complete_semantic(**kw):
        seen.update(kw)
        return sem("Ты же сам меня обозвал.", dict(NEUTRAL_STATE))

    with patch.object(llm_gateway, "complete_semantic", fake_complete_semantic), \
         patch.object(chat_local, "_self_knowledge_message", lambda: None):
        chat_local._call_model_semantic("heretic:q8", q_msgs, 0.7, MEM_INSULT)
    system_text = "\n".join(s for s in seen["system"] if s)
    memory_msg = next((s for s in seen["system"] if s and OLD_INSULT in s), "")
    check("8: the remembered insult still reaches the model (memory -> reply is possible)", bool(memory_msg), repr(seen["system"]))
    check("8: memory is explicitly marked as PAST, not the current user message",
          "ПРОШЛОЕ" in memory_msg and "не текущее сообщение" in memory_msg, memory_msg)
    check("8: memory states it may influence the reply but is not a new event", "не является новым событием" in memory_msg, memory_msg)
    check("8: the current user message is the final role=user message and is not quoted inside the system text",
          seen["messages"][-1] == {"role": "user", "content": NEUTRAL_Q} and NEUTRAL_Q not in system_text, repr(seen["messages"]))
    schema = seen["requirement"].state_schema
    check("8: state schema asks for `evidence` quoted from the LAST user message",
          "evidence" in schema["properties"] and "последнего сообщения" in schema["properties"]["evidence"]["description"], repr(schema))
    check("8: `evidence` is optional in the schema; the original four fields stay required",
          "evidence" not in schema["required"] and schema["required"] == ["is_insult", "severity", "is_apology", "sincerity"], repr(schema["required"]))

    # ── 9. the model is called exactly once per turn (no second classifier call was introduced) ──
    n = {"calls": 0}

    def counting(*a, **k):
        n["calls"] += 1
        return sem("ok", dict(NEUTRAL_STATE))

    with patch.object(chat_local, "shadow_get_relationship_context", lambda **kw: MEM_INSULT), \
         patch.object(chat_local, "_call_model_semantic", counting):
        chat_local._respond_with_character("heretic:q8", q_msgs, 0.7)
    check("9: one PET turn = one model generation (single-call design preserved)", n["calls"] == 1, str(n))

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
