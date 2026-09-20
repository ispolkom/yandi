"""
pet/pet_commitment_events_regression_test.py — PROMISES AND CLAIMS THROUGH THE
PERSONAL CHAT.

    USER SAID "I did it"  !=  YANDI KNOWS it was done.
    MEMORY MUST NOT BECOME A NEW USER EVENT.
    EVENTS WRITE STATE. STATE DOES NOT INVENT EVENTS. THE LLM DOES NOT WRITE TRUST.

A promise or a claim of having kept one is an event only if the model's state
asserts it AND quotes it verbatim from the CURRENT user message. A claim is
linked to one specific open promise chosen before the reply; ambiguous -> no
write. Neither moves a relationship coordinate: only a verified outcome does.

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

    OWNER = chat_local._RELATIONSHIP_USER_ID
    NEUTRAL = {"is_insult": False, "severity": 0.0, "is_apology": False, "sincerity": 0.0}

    def state(**kw):
        return {**NEUTRAL, **kw}

    def turn(conn, text, model_state):
        captured = {}

        def fake_semantic(**kwargs):
            captured["system"] = kwargs["system"]
            return SemanticCompletionResult(reply="Ладно.", state=model_state, reply_ok=True, state_ok=True,
                                            parse_ok=True, error=None, metadata={})
        with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(conn)), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(llm_gateway, "complete_semantic", fake_semantic):
            chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": text}], 0.7)
        return next((p for p in captured["system"] if p and "памят" in p.lower()), "")

    def coords(conn):
        s = rs.get_state(conn, OWNER)
        return (s["trust"], s["respect"], s["affection"], s["forgiveness_capacity"])

    REPORT = "Я пришлю тебе отчёт по проекту до пятницы"

    # ── 1. a promise: needs the model's assertion AND a verbatim quote from the CURRENT message ──
    w = FakeConnection()
    turn(w, REPORT + ".", state(is_promise=True, evidence="Я пришлю тебе отчёт"))
    check("1: a validated promise is recorded with the person's own words and evidence",
          len(w.commitments) == 1 and next(iter(w.commitments.values()))["text"] == REPORT + "."
          and next(iter(w.commitments.values()))["evidence"] == "Я пришлю тебе отчёт")
    check("1: a promise moves no relationship coordinate", coords(w) == (50.0, 50.0, 30.0, 50.0) and w.inner_state_events == [])
    for label, st in {
        "no evidence": state(is_promise=True),
        "empty evidence": state(is_promise=True, evidence=""),
        "evidence not in the message": state(is_promise=True, evidence="Я обещаю вернуть деньги"),
        "flag is not a strict boolean": state(is_promise="true", evidence="Я пришлю тебе отчёт"),
    }.items():
        w2 = FakeConnection()
        turn(w2, REPORT + ".", st)
        check(f"1: promise asserted but ungrounded ({label}) -> nothing recorded", w2.commitments == {}, repr(w2.commitments))

    # ── 2. memory must not become a new user event ──
    for label, st in {
        "claim quoted from the remembered promise": state(claims_fulfilled=True, evidence=REPORT),
        "promise re-asserted from memory": state(is_promise=True, evidence="Я пришлю тебе отчёт"),
    }.items():
        h = FakeConnection()
        rc.create_commitment(h, OWNER, REPORT, "Я пришлю тебе отчёт")
        n_events = len(h.commitment_events); n_commit = len(h.commitments)
        turn(h, "Как дела?", st)
        check(f"2: neutral current message + remembered promise + contaminated state ({label}) -> no event",
              len(h.commitment_events) == n_events and len(h.commitments) == n_commit)

    # ── 3. a claim links to ONE specific open promise and is recorded as a REPORT ──
    c = FakeConnection()
    a = rc.create_commitment(c, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")["commitment_id"]
    b = rc.create_commitment(c, OWNER, "Я куплю билеты на концерт", "Я куплю билеты")["commitment_id"]
    before = coords(c)
    memory = turn(c, "Я отправил отчёт", state(claims_fulfilled=True, evidence="Я отправил отчёт"))
    check("3: the reply context is built around the promise the claim names",
          "отчёт по проекту" in memory and "билеты" not in memory and "не подтверждено" in memory, memory)
    check("3: the claim is recorded against THAT promise only",
          [(e["commitment_id"], e["event_type"]) for e in c.commitment_events] == [(a, rc.CLAIMED)])
    check("3: USER SAID != YANDI KNOWS — the claim changes no coordinate and no state event", coords(c) == before and c.inner_state_events == [])
    memory2 = turn(c, "Как дела?", dict(NEUTRAL))
    check("3: afterwards she remembers the report as unverified", "заявлял" in memory2 and "не проверяла" in memory2 and "отчёт по проекту" in memory2, memory2)
    n = len(c.commitment_events)
    turn(c, "Я отправил отчёт", state(claims_fulfilled=True, evidence="Я отправил отчёт"))
    check("3: repeating the claim adds nothing", len(c.commitment_events) == n)

    # ── 4. ambiguity: a generic claim with several open promises writes nothing ──
    d = FakeConnection()
    rc.create_commitment(d, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")
    rc.create_commitment(d, OWNER, "Я куплю билеты на концерт", "Я куплю билеты")
    memory = turn(d, "Я всё сделал", state(claims_fulfilled=True, evidence="Я всё сделал"))
    check("4: generic claim + several open promises -> ambiguous: no event", d.commitment_events == [])
    check("4: the context states the ambiguity as a fact", "несколько" in memory and "не указывает" in memory, memory)

    # ── 5. COUNTERFACTUAL: same message, same model output, different history ──
    with_p, without_p = FakeConnection(), FakeConnection()
    rc.create_commitment(with_p, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")
    rc.create_commitment(without_p, OWNER, "Я куплю билеты на концерт", "Я куплю билеты")
    rc.create_commitment(without_p, OWNER, "Я помогу с переездом", "Я помогу")
    for conn in (with_p, without_p):
        turn(conn, "Я отправил отчёт", state(claims_fulfilled=True, evidence="Я отправил отчёт"))
    check("5: COUNTERFACTUAL — a claim is recorded only where the history holds the matching promise",
          len(with_p.commitment_events) == 1 and len(without_p.commitment_events) == 0)

    # ── 6. exclusivity: one grounded event must not vouch for a remembered one ──
    e = FakeConnection()
    rc.create_commitment(e, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")
    e.add("g1", "Ты сломал мой велосипед", 0.4, age=DAY, user_id=OWNER)
    turn(e, "Извини, я был неправ", state(is_apology=True, sincerity=0.9, is_promise=True, claims_fulfilled=False, evidence="Извини"))
    check("6: an apology together with a promise flag: the grounded apology applies, the promise is dropped",
          e.grievances["g1"]["apology_at"] is not None and len(e.commitments) == 1)
    f = FakeConnection()
    rc.create_commitment(f, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")
    turn(f, "Я отправил отчёт и пришлю ещё", state(is_promise=True, claims_fulfilled=True, evidence="Я отправил отчёт"))
    check("6: promise and claim asserted together are contradictory: neither is recorded", f.commitment_events == [] and len(f.commitments) == 1)

    # ── 7. a verified outcome is the only thing that moves trust, and only once ──
    v = FakeConnection()
    cid = rc.create_commitment(v, OWNER, "Я пришлю отчёт по проекту", "Я пришлю отчёт")["commitment_id"]
    turn(v, "Я отправил отчёт", state(claims_fulfilled=True, evidence="Я отправил отчёт"))
    t0 = rs.get_state(v, OWNER)["trust"]
    rc.record_verification(v, OWNER, cid, True, source="independent_check")
    rc.record_verification(v, OWNER, cid, True, source="independent_check")
    check("7: after the claim, a verifier confirms it: trust rises exactly once", rs.get_state(v, OWNER)["trust"] == t0 + rs.KEPT_TRUST)

    # ── 8. an unreadable promise ledger never degrades the grievance memory ──
    u = FakeConnection()
    u.add("g1", "Ты сломал мой велосипед", 0.4, age=DAY, user_id=OWNER)
    with patch.object(rc, "commitment_statuses", side_effect=RuntimeError("table commitment does not exist")):
        memory = turn(u, "Извини за велосипед", state(is_apology=True, sincerity=0.9, evidence="Извини за велосипед"))
    check("8: promise ledger unreadable -> grievance memory intact and the apology still applies",
          "велосипед" in memory and "Обещания" not in memory and u.grievances["g1"]["apology_at"] is not None, memory)

    # ── 9. structure: no keyword lists decide that a promise or a claim happened ──
    import ast, textwrap
    literals = []
    for fn in (chat_local._apply_current_turn_event, chat_local._require_current_turn_provenance):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        body = tree.body[0].body
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node is not body[0].value:
                literals.append(node.value.lower())
    check("9: PET decides nothing from words: no promise/claim vocabulary among the string literals of the event path",
          not any(w in lit for lit in literals for w in ("обещ", "выполнил", "сделал", "отправил", "promise", "fulfil")), repr(literals))
    check("9: the model's state schema still has no relationship coordinate",
          not ({"trust", "respect", "affection", "forgiveness_capacity"} & set(chat_local._STATE_SCHEMA["properties"])))
    check("9: the new schema fields are optional (older models/backends stay valid)",
          chat_local._STATE_SCHEMA["required"] == ["is_insult", "severity", "is_apology", "sincerity"]
          and {"is_promise", "claims_fulfilled"} <= set(chat_local._STATE_SCHEMA["properties"]))

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
