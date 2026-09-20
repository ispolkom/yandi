"""
pet/pet_commitment_events_regression_test.py — PROMISES AND CLAIMS THROUGH THE
PERSONAL CHAT.

    USER SAID "I did it"  !=  YANDI KNOWS it was done.
    MEMORY MUST NOT BECOME A NEW USER EVENT.
    EVENTS WRITE STATE. STATE DOES NOT INVENT EVENTS. THE LLM DOES NOT WRITE TRUST.

A promise or a claim of having kept one is an event only if pet/event_extraction.py
confirms it from the CURRENT user message (evidence located by the model,
reconstructed by code). A claim is linked to one specific open promise chosen
before the reply; ambiguous -> no write. Neither moves a relationship
coordinate: only a verified outcome does.

Real chat_local + real shadow_write wrappers over an in-memory fake SQL
connection; only the model call is replaced (its prompt is captured).

Run: python -m pet.pet_commitment_events_regression_test
"""
from __future__ import annotations

import inspect
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
    import agent.relationship_commitments as rc
    import agent.relationship_state as rs
    import pet.chat_local as chat_local
    from llm_gateway.types import SemanticCompletionResult
    from agent.relationship_apology_matching_regression_test import FakeConnection, DAY
    from pet.extraction_test_support import scripted_llm

    OWNER = chat_local._RELATIONSHIP_USER_ID
    def turn(conn, text, extractor):
        captured = {}

        def fake_semantic(**kwargs):
            captured["system"] = kwargs["system"]
            return SemanticCompletionResult(reply="Ладно.", state=None, reply_ok=True, state_ok=False,
                                            parse_ok=True, error=None, metadata={})
        with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(conn)), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm", lambda model: extractor), \
             patch.object(llm_gateway, "complete_semantic", fake_semantic):
            chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": text}], 0.7)
        return next((p for p in captured["system"] if p and "памят" in p.lower()), "")

    def raw(text):
        return lambda messages: text if "Слова" in messages[-1]["content"] else '{"label": "exact_act"}'

    NEUTRAL_LLM = scripted_llm([])

    def coords(conn):
        s = rs.get_state(conn, OWNER)
        return (s["trust"], s["respect"], s["affection"], s["forgiveness_capacity"])

    REPORT = "Я пришлю тебе отчёт по проекту до пятницы"

    # ── 1. a promise: needs the extraction step to confirm it in the CURRENT message ──
    w = FakeConnection()
    turn(w, REPORT + ".", scripted_llm([("Я пришлю тебе отчёт", "promise", None)]))
    check("1: a confirmed promise is recorded with the person's own words and the code-reconstructed evidence",
          len(w.commitments) == 1 and next(iter(w.commitments.values()))["text"] == REPORT + "."
          and next(iter(w.commitments.values()))["evidence"] == "Я пришлю тебе отчёт")
    check("1: a promise moves no relationship coordinate", coords(w) == (50.0, 50.0, 30.0, 50.0) and w.inner_state_events == [])
    for label, extractor in {
        "the check refuses the span": scripted_llm([("Я пришлю тебе отчёт", "promise", None)], labels=["different_act"]),
        "reference out of range": raw('{"events": [{"span": [0, 99], "type": "promise"}]}'),
        "unknown event type": raw('{"events": [{"span": [0, 3], "type": "pledge"}]}'),
        "not JSON": raw("обещание"),
    }.items():
        w2 = FakeConnection()
        turn(w2, REPORT + ".", extractor)
        check(f"1: promise proposed but not confirmed ({label}) -> nothing recorded", w2.commitments == {}, repr(w2.commitments))

    # ── 2. memory must not become a new user event ──
    for label, extractor in {
        "nothing extracted": scripted_llm([]),
        "extractor points at words of the remembered claim, check refuses": scripted_llm([(REPORT, "fulfilment_claim", None)], labels=["different_act"]),
        "extractor re-proposes the remembered promise, check refuses": scripted_llm([("Я пришлю тебе отчёт", "promise", None)], labels=["different_act"]),
    }.items():
        h = FakeConnection()
        rc.create_commitment(h, OWNER, REPORT, "Я пришлю тебе отчёт")
        n_events = len(h.commitment_events); n_commit = len(h.commitments)
        memory = turn(h, "Как дела?", extractor)
        check(f"2: neutral current message + remembered promise ({label}) -> no event",
              len(h.commitment_events) == n_events and len(h.commitments) == n_commit)
    check("2: the remembered promise is still available to the reply as a fact (memory MAY affect the reply)",
          "не подтверждено" in memory or "отчёт" in memory, memory)

    # ── 3. a claim links to ONE specific open promise and is recorded as a REPORT ──
    c = FakeConnection()
    a = rc.create_commitment(c, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")["commitment_id"]
    b = rc.create_commitment(c, OWNER, "Я куплю билеты на концерт", "Я куплю билеты")["commitment_id"]
    before = coords(c)
    memory = turn(c, "Я отправил отчёт", scripted_llm([("Я отправил отчёт", "fulfilment_claim", None)]))
    check("3: the reply context is built around the promise the claim names",
          "отчёт по проекту" in memory and "билеты" not in memory and "не подтверждено" in memory, memory)
    check("3: the claim is recorded against THAT promise only",
          [(e["commitment_id"], e["event_type"]) for e in c.commitment_events] == [(a, rc.CLAIMED)])
    check("3: USER SAID != YANDI KNOWS — the claim changes no coordinate and no state event", coords(c) == before and c.inner_state_events == [])
    memory2 = turn(c, "Как дела?", NEUTRAL_LLM)
    check("3: afterwards she remembers the report as unverified", "заявлял" in memory2 and "не проверяла" in memory2 and "отчёт по проекту" in memory2, memory2)
    n = len(c.commitment_events)
    turn(c, "Я отправил отчёт", scripted_llm([("Я отправил отчёт", "fulfilment_claim", None)]))
    check("3: repeating the claim adds nothing", len(c.commitment_events) == n)

    # ── 4. ambiguity: a generic claim with several open promises writes nothing ──
    d = FakeConnection()
    rc.create_commitment(d, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")
    rc.create_commitment(d, OWNER, "Я куплю билеты на концерт", "Я куплю билеты")
    memory = turn(d, "Я всё сделал", scripted_llm([("Я всё сделал", "fulfilment_claim", None)]))
    check("4: generic claim + several open promises -> ambiguous: no event", d.commitment_events == [])
    check("4: the context states the ambiguity as a fact", "несколько" in memory and "не указывает" in memory, memory)

    # ── 5. COUNTERFACTUAL: same message, same model output, different history ──
    with_p, without_p = FakeConnection(), FakeConnection()
    rc.create_commitment(with_p, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")
    rc.create_commitment(without_p, OWNER, "Я куплю билеты на концерт", "Я куплю билеты")
    rc.create_commitment(without_p, OWNER, "Я помогу с переездом", "Я помогу")
    for conn in (with_p, without_p):
        turn(conn, "Я отправил отчёт", scripted_llm([("Я отправил отчёт", "fulfilment_claim", None)]))
    check("5: COUNTERFACTUAL — a claim is recorded only where the history holds the matching promise",
          len(with_p.commitment_events) == 1 and len(without_p.commitment_events) == 0)

    # ── 6. exclusivity: one confirmed event must not vouch for another ──
    e = FakeConnection()
    rc.create_commitment(e, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")
    e.add("g1", "Ты сломал мой велосипед", 0.4, age=DAY, user_id=OWNER)
    turn(e, "Извини, я всё исправлю завтра", scripted_llm([("Извини", "apology", {"sincerity": 0.9}), ("я всё исправлю завтра", "promise", None)]))
    check("6: an apology together with a promise in the same turn: the apology applies, the promise is dropped (a commitment must be the ONLY event)",
          e.grievances["g1"]["apology_at"] is not None and len(e.commitments) == 1)
    f = FakeConnection()
    rc.create_commitment(f, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")
    turn(f, "Я отправил отчёт и пришлю ещё", scripted_llm([("Я отправил отчёт", "fulfilment_claim", None), ("пришлю ещё", "promise", None)]))
    check("6: a promise and a claim in the same turn are contradictory: neither is recorded", f.commitment_events == [] and len(f.commitments) == 1)
    g = FakeConnection()
    rc.create_commitment(g, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")
    turn(g, "Я отправил отчёт", scripted_llm([("Я отправил отчёт", "fulfilment_claim", None), ("отправил отчёт", "promise", None)]))
    check("6: events whose evidence spans overlap are ambiguous: nothing is recorded", g.commitment_events == [] and len(g.commitments) == 1)

    # ── 7. a verified outcome is the only thing that moves trust, and only once ──
    v = FakeConnection()
    cid = rc.create_commitment(v, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")["commitment_id"]
    turn(v, "Я отправил отчёт", scripted_llm([("Я отправил отчёт", "fulfilment_claim", None)]))
    t0 = rs.get_state(v, OWNER)["trust"]
    rc.record_verification(v, OWNER, cid, True, source="independent_check")
    rc.record_verification(v, OWNER, cid, True, source="independent_check")
    check("7: after the claim, a verifier confirms it: trust rises exactly once", rs.get_state(v, OWNER)["trust"] == t0 + rs.KEPT_TRUST)

    # ── 8. an unreadable promise ledger never degrades the grievance memory ──
    u = FakeConnection()
    u.add("g1", "Ты сломал мой велосипед", 0.4, age=DAY, user_id=OWNER)
    with patch.object(rc, "commitment_statuses", side_effect=RuntimeError("table commitment does not exist")):
        memory = turn(u, "Извини за велосипед", scripted_llm([("Извини за велосипед", "apology", {"sincerity": 0.9})]))
    check("8: promise ledger unreadable -> grievance memory intact and the apology still applies",
          "велосипед" in memory and "Обещания" not in memory and u.grievances["g1"]["apology_at"] is not None, memory)

    # ── 9. structure: no keyword lists decide that a promise or a claim happened ──
    import ast, textwrap
    literals = []
    for fn in (chat_local._apply_current_turn_event, chat_local._respond_with_character):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        body = tree.body[0].body
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node is not body[0].value:
                literals.append(node.value.lower())
    check("9: PET decides nothing from words: no promise/claim vocabulary among the string literals of the event path",
          not any(w in lit for lit in literals for w in ("обещ", "выполнил", "сделал", "отправил", "promise", "fulfil")), repr(literals))
    check("9: neither model call has a path to write relationship coordinates: the reply schema is empty and events are references",
          chat_local._STATE_SCHEMA == {"type": "object", "properties": {}})

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
