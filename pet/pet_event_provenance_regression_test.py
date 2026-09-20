"""
pet/pet_event_provenance_regression_test.py — CURRENT-EVENT PROVENANCE, end to
end through the personal chat.

    MEMORY MAY AFFECT THE REPLY.
    MEMORY MUST NOT BECOME A NEW USER EVENT.
    HISTORICAL EVENT != CURRENT EVENT.

A relationship event (insult / apology / promise / claim) is written only when
pet/event_extraction.py confirms it from the CURRENT user message. The
extractor never sees memory or history, and the reply generation is no longer a
source of events at all: whatever `state` the reply call returns is ignored.
Real chat_local + real shadow_write wrappers over an in-memory fake SQL
connection; the model is scripted (see pet/extraction_test_support.py).

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
    import agent.db.sql.shadow_write as shadow_write
    import pet.chat_local as chat_local
    from llm_gateway.types import SemanticCompletionResult
    from agent.relationship_apology_matching_regression_test import FakeConnection, DAY
    from pet.extraction_test_support import scripted_llm

    OWNER = chat_local._RELATIONSHIP_USER_ID
    OLD_INSULT = "Да ты вообще ржавая консерва, от тебя никакого толку."

    def world():
        c = FakeConnection()
        c.add("g_old", OLD_INSULT, 0.85, age=3 * DAY, user_id=OWNER)
        return c

    def turn(conn, messages, extractor, reply_state=None):
        """One PET turn. Returns the prompts of the reply call and of the extractor."""
        captured = {}

        def fake_semantic(**kwargs):
            captured["system"] = kwargs["system"]
            return SemanticCompletionResult(reply="Ага.", state=reply_state, reply_ok=True, state_ok=reply_state is not None,
                                            parse_ok=True, error=None, metadata={})
        with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(conn)), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm", lambda model: extractor), \
             patch.object(llm_gateway, "complete_semantic", fake_semantic):
            visible = chat_local._respond_with_character("heretic:q8", messages, 0.7)
        return visible, captured

    def snapshot(c):
        return (c.snapshot(), len(c.grievances), len(c.commitments), len(c.commitment_events), len(c.inner_state_events),
                dict((k, dict(v)) for k, v in c.capacities.items()))

    NEUTRAL_Q = "Как ты ко мне сейчас относишься?"
    q_msgs = [{"role": "user", "content": NEUTRAL_Q}]

    # ── 1. old insult in memory + neutral current message ──
    c = world(); before = snapshot(c)
    visible, cap = turn(c, q_msgs, scripted_llm([]))
    check("1: neutral current message writes NOTHING back (memory did not become an event)", snapshot(c) == before)
    check("1: the remembered insult still reaches the reply's memory context (memory MAY affect the reply)",
          any(OLD_INSULT in (p or "") for p in cap["system"]))

    # ── 2. the reply call is no longer a source of events ──
    forged = {"is_insult": True, "severity": 0.85, "is_apology": True, "sincerity": 0.9, "is_promise": True,
              "claims_fulfilled": False, "evidence": "ржавая консерва", "trust": 100, "respect": 100}
    c = world(); before = snapshot(c)
    turn(c, q_msgs, scripted_llm([]), reply_state=forged)
    check("2: a reply call that asserts events (even with a quote from memory) changes nothing", snapshot(c) == before)

    # ── 3. old apology in the transcript + neutral current message ──
    hist = [{"role": "user", "content": "Извини, я зря это сказал."}, {"role": "assistant", "content": "Ладно, проехали."},
            {"role": "user", "content": "Как ты?"}]
    c = world(); before = snapshot(c)
    turn(c, hist, scripted_llm([]))
    check("3: an old apology earlier in the history + neutral current message -> no acknowledgement", snapshot(c) == before)
    c = world(); before = snapshot(c)
    ex = scripted_llm([("Извини, я зря это сказал.", "apology", {"sincerity": 0.9})], labels=["different_act"])
    turn(c, hist, ex)
    check("3: an extractor that points at the OLD apology can only point at words of the CURRENT message ('Как ты?'); "
          "the check on that span refuses -> no write", snapshot(c) == before)
    check("3: the extractor was shown the current message only, never the history or memory",
          all("Ладно, проехали" not in m["content"] and OLD_INSULT not in m["content"] for call in ex.calls for m in call))

    # ── 4. real current events go through, with code-reconstructed evidence ──
    real_insult = "Да ты ржавая консерва."
    c = world()
    turn(c, [{"role": "user", "content": real_insult}], scripted_llm([("ржавая консерва", "insult", {"severity": 0.85})]))
    new = [g for gid, g in c.grievances.items() if gid != "g_old"]
    check("4: a real current insult -> exactly one new grievance holding the CURRENT message",
          len(new) == 1 and new[0]["description"] == real_insult and new[0]["severity"] > 0, repr(new))
    c = world()
    turn(c, [{"role": "user", "content": "Извини, я зря это сказал."}], scripted_llm([("Извини", "apology", {"sincerity": 0.8})]))
    check("4: a real current apology advances the focused grievance", c.grievances["g_old"]["apology_at"] is not None)

    # ── 5. extractor hallucination / disagreement -> no event ──
    c = world(); before = snapshot(c)
    turn(c, [{"role": "user", "content": "Как дела?"}], scripted_llm([("этого текста нет в сообщении", "insult", {"severity": 0.9})], labels=["different_act"]))
    check("5: an insult the words do not constitute (check says different_act) -> no write", snapshot(c) == before)
    c = world(); before = snapshot(c)
    turn(c, [{"role": "user", "content": "Он мне написал: «ты ржавая консерва»."}],
         scripted_llm([("ты ржавая консерва", "insult", {"severity": 0.9})], labels=["quotation"]))
    check("5: a quotation of somebody else's insult -> no write", snapshot(c) == before)

    # ── 6. numbers outside 0..1 are a malformed event, not a weak one ──
    for label, events, msg in (
        ("severity above 1", [("ржавая консерва", "insult", {"severity": 1.7})], "Да ты ржавая консерва."),
        ("severity negative", [("ржавая консерва", "insult", {"severity": -0.25})], "Да ты ржавая консерва."),
        ("sincerity outside 0..1", [("Извини", "apology", {"sincerity": 4})], "Извини, я зря это сказал."),
        ("sincerity as text", [("Извини", "apology", {"sincerity": "high"})], "Извини, я зря это сказал."),
    ):
        c = world(); before = snapshot(c)
        turn(c, [{"role": "user", "content": msg}], scripted_llm(events))
        check(f"6: {label} -> dropped, nothing written", snapshot(c) == before)
    c = world()
    turn(c, [{"role": "user", "content": "Извини, я зря это сказал."}], scripted_llm([("Извини", "apology", {"sincerity": 1})]))
    check("6: boundary value (sincerity == 1) is valid", c.grievances["g_old"]["apology_at"] is not None)
    c = world(); before = snapshot(c)
    turn(c, [{"role": "user", "content": "Да ты ржавая консерва."}], scripted_llm([("ржавая консерва", "insult", {"severity": 0})]))
    check("6: an insult with severity 0 is below the grievance threshold -> no grievance", len(c.grievances) == 1)

    # ── 7. the extractor failing must not break the reply, and writes nothing ──
    def broken(messages):
        raise RuntimeError("model unavailable")
    c = world(); before = snapshot(c)
    visible, _ = turn(c, [{"role": "user", "content": "Извини"}], broken)
    check("7: extraction failure -> the reply is still delivered, nothing is written", visible == "Ага." and snapshot(c) == before)

    # ── 8. structure ──
    check("8: the reply generation is no longer asked for relationship events",
          chat_local._STATE_SCHEMA == {"type": "object", "properties": {}})
    check("8: the old state-based guard is gone (one source of events, not two)",
          not hasattr(chat_local, "_require_current_turn_provenance") and not hasattr(chat_local, "intensity_from_state"))
    n = {"reply": 0, "extract": 0}
    def counting_reply(**kw):
        n["reply"] += 1
        return SemanticCompletionResult(reply="ok", state=None, reply_ok=True, state_ok=False, parse_ok=True, error=None, metadata={})
    def counting_extractor(messages):
        n["extract"] += 1
        return '{"events": []}'
    c = world()
    with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(c)), \
         patch.object(chat_local, "_self_knowledge_message", lambda: None), \
         patch.object(chat_local, "_extraction_llm", lambda model: counting_extractor), \
         patch.object(llm_gateway, "complete_semantic", counting_reply):
        chat_local._respond_with_character("heretic:q8", q_msgs, 0.7)
    check("8: one neutral turn = one extraction generation + one reply generation (each its own attempt)", n == {"reply": 1, "extract": 1}, str(n))

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
