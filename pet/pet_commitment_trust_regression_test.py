"""
pet/pet_commitment_trust_regression_test.py — VERIFIED COMMITMENTS AND TRUST, THROUGH
THE PERSONAL CHAT.

    USER REPORTS FULFILMENT != FULFILMENT VERIFIED.       TRUST GROWS ONLY FROM VERIFIED EVIDENCE.
    EXTERNAL SELF-REPORT != DIRECT OBSERVATION.           AMBIGUOUS TARGET != VERIFIED TARGET.
    MODEL MAY POINT TO EVIDENCE; MODEL MAY NOT INVENT EVIDENCE.
    VERIFICATION USES THE CURRENT IDENTIFIED TURN ONLY: NOT THE ASSISTANT, NOT MEMORY, NOT PERSONAL FACTS.
    ONE COMMITMENT -> MAX ONE REWARD.  ONE EVIDENCE -> MAX ONE COMMITMENT.  FAIL CLOSED.

Real pet.chat_local + real shadow_write wrappers + real ledger and state over the in-memory fake
SQL; only the model calls are scripted (event extraction, fact extraction, verification). Where a
scenario says "the worst-case checker", the blind judgement is scripted to accept everything, so the
result is decided by the CODE around the model. The atomicity / retry / concurrency guarantees on a
real engine are in agent/commitment_verification_sql_integration_test.py.

Run: python -m pet.pet_commitment_trust_regression_test
"""
from __future__ import annotations

import inspect
import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    import llm_gateway
    import agent.db.sql.repositories as repo
    import agent.db.sql.shadow_write as shadow_write
    import agent.personal_memory as pm
    import agent.personal_facts as pf
    import agent.relationship_commitments as rc
    import agent.relationship_memory as rm
    import agent.relationship_state as rs
    import pet.chat_local as chat_local
    import pet.commitment_verification as cv
    from llm_gateway.types import SemanticCompletionResult
    from agent.relationship_apology_matching_regression_test import FakeConnection
    from pet.extraction_test_support import scripted_llm
    from pet.fact_test_support import scripted_fact_llm, scripted_router
    from pet.verification_test_support import scripted_verify_llm

    OWNER = chat_local._RELATIONSHIP_USER_ID
    T0 = datetime(2026, 9, 1, 12, 0, 0)
    tick = [0]

    def clock():
        tick[0] += 1
        return T0 + timedelta(seconds=tick[0])

    CODE = "в следующем сообщении дам тебе кодовое слово"
    NUM = "в следующем сообщении дам тебе число от 100 до 999"
    seq = [0]

    def next_turn_id() -> str:
        seq[0] += 1
        return f"turn-cy8-{seq[0]:06d}"

    def chat_turn(conn, text, *, events=(), verify=None, turn_id="auto", model_name="model-A", history=None, facts=(), reply="Хорошо.", router_override=None):
        seen: dict = {}
        tid = next_turn_id() if turn_id == "auto" else turn_id

        def fake_semantic(**kwargs):
            seen.update(kwargs)
            return SemanticCompletionResult(reply=reply, state=None, reply_ok=True, state_ok=False, parse_ok=True, error=None,
                                            metadata={"_llm_gateway_trace": [{"result": "success", "resolved_model": model_name, "adapter_id": "llama_cpp"}]})
        router = router_override or scripted_router(scripted_llm(events), scripted_fact_llm(facts), verify or scripted_verify_llm())
        msgs = list(history or []) + [{"role": "user", "content": text}]
        with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(conn)), \
             patch.object(rm, "_now", clock), patch.object(repo, "_now", clock), patch.object(pm, "_now", clock), patch.object(pf, "_now", clock), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm", lambda model: router), \
             patch.object(llm_gateway, "complete_semantic", fake_semantic):
            out = chat_local._respond_with_character("heretic:q8", msgs, 0.7, tid)
        router.turn_id = tid  # type: ignore[attr-defined]
        return out, router, seen

    def trust(conn) -> float:
        return rs.get_state(conn, OWNER)["trust"]

    def verified(conn):
        return [e for e in conn.commitment_events if e["event_type"] == rc.VERIFIED_KEPT]

    def claims(conn):
        return [e for e in conn.commitment_events if e["event_type"] == rc.CLAIMED]

    def promise(conn, what=CODE, *, classify="in_chat", text=None):
        text = text or f"Обещаю: {what}."
        _, router, _ = chat_turn(conn, text, events=[(what, "promise", None)], verify=scripted_verify_llm(classify=classify))
        return router

    def commitments(conn):
        return sorted(conn.commitments.values(), key=lambda c: c["created_at"])

    # ── 1. the promise: classified, stored with its source turn, moves nothing ──
    db = FakeConnection()
    router = promise(db)
    c = commitments(db)[0]
    check("1: a promise is stored as an in_chat commitment WITH the turn it was made in", len(db.commitments) == 1 and c["kind"] == "in_chat" and c["source_turn_id"] == router.turn_id)
    check("1: registering a promise moves no coordinate", trust(db) == 50.0 and db.inner_state_events == [])
    check("1: the promise turn asks only the classification (nothing to verify yet: no deliverable is looked for in the promise itself)",
          len(router.verify_inputs) == 1 and router.verify_inputs[0][0]["content"] == cv._CLASSIFY_SYSTEM)

    # ── 2. direct delivery in a later turn, another model, no client history: VERIFIED ──
    delivery_text = "Вот оно: кодовое слово АЛЬФА."
    _, router2, _ = chat_turn(db, delivery_text, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)), model_name="model-B")
    v = verified(db)
    check("2: MODEL SWAP + empty client history: the promise is found in SQL and the direct delivery is VERIFIED", len(v) == 1 and v[0]["commitment_id"] == c["commitment_id"])
    check("2: the verified event carries its provenance: source turn, exact evidence bytes of THAT turn, span, verifier class",
          v and (v[0]["source_turn_id"], v[0]["evidence"], v[0]["source"]) == (router2.turn_id, "АЛЬФА", rc.VERIFIER_IN_CHAT)
          and delivery_text[v[0]["span_start"]:v[0]["span_end"]] == "АЛЬФА"
          and [t for t in db.interaction_turns if t["source_turn_id"] == router2.turn_id][0]["user_text"] == delivery_text)
    check("2: trust rose by the bounded reward, only trust", trust(db) == 50.0 + rs.OBSERVED_TRUST_BASE and rs.get_state(db, OWNER)["respect"] == 50.0
          and rs.get_state(db, OWNER)["affection"] == 30.0)
    check("2: the verifier was shown the promise's own words (read from SQL) and the current message — and no trust number, no grievance, no assistant text",
          any(CODE in m["content"] for call in router2.verify_inputs for m in call)
          and not any(w in " ".join(m["content"] for call in router2.verify_inputs for m in call).lower() for w in ("доверие", "trust", "обид", "хорошо.")))
    check("2: the commitment is resolved: no longer an open target, history kept",
          rc.commitment_statuses(db, OWNER)[0]["status"] == "verified_fulfilled" and rc.verifiable_commitments(db, OWNER, None) == [] and len(db.commitments) == 1)
    check("2: VERIFICATION EVENT IDEMPOTENCY — one causal claim per turn, exactly one trust transition",
          [e["event_type"] for e in db.causal_events if e["source_turn_id"] == router2.turn_id].count("commitment_verified") == 1
          and [e["event_type"] for e in db.inner_state_events].count("commitment_observed") == 1)
    check("2: a verified fulfilment creates no personal fact and touches no belief", db.personal_facts == [] and db.personal_fact_events == [])
    t_after = trust(db)
    n_events = len(db.commitment_events)

    # ── 3. retries and repeats change nothing ──
    same_id = router2.turn_id
    for label, kwargs in (("RETRY AFTER COMMIT / LOST RESPONSE (same turn id)", dict(turn_id=same_id)),
                          ("SAME EVIDENCE, NEW TURN (the commitment is already resolved)", dict(turn_id="auto"))):
        chat_turn(db, delivery_text, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)), **kwargs)
        check(f"3: {label}: no second verification, no second trust increase", len(verified(db)) == 1 and trust(db) == t_after and len(db.commitment_events) == n_events)
    _, r_again, _ = chat_turn(db, delivery_text, verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    check("3: a resolved commitment is not even offered to the verifier again (no wasted calls)", r_again.verify_inputs == [])

    # ── 4. SELF-REPORT MUST NOT PASS: the worst-case checker accepts everything, the code around it decides ──
    reports = [
        ("Я сделал это.", "Я сделал это", "fulfilment_claim"),
        ("Я выполнил обещание.", "Я выполнил обещание", "fulfilment_claim"),
        ("Можешь считать, что обещание выполнено.", "обещание выполнено", "fulfilment_claim"),
        ("Поверь мне, всё готово.", "всё готово", "fulfilment_claim"),
        ("Я точно оплатил.", "Я точно оплатил", "fulfilment_claim"),
        ("Я уже отправил файл.", "Я уже отправил файл", "fulfilment_claim"),
        ("Я оплатил счёт.", "Я оплатил счёт", "fulfilment_claim"),
    ]
    for text, quote, kind in reports:
        w = FakeConnection()
        promise(w)
        base = trust(w)
        chat_turn(w, text, events=[(quote, kind, None)], verify=scripted_verify_llm(delivery=(quote, 0)))
        check(f"4: {text!r} -> recorded as a REPORT, NOT verified, trust unchanged (even against a checker that accepts everything)",
              len(claims(w)) == 1 and verified(w) == [] and trust(w) == base and w.inner_state_events == [], repr(w.commitment_events))
    # the same report words with NO classification by the event extraction: the frame judgement still refuses
    for text, quote, frame in [
        ("Представь, что я выполнил обещание.", "я выполнил обещание", "hypothetical"),
        ("Если бы я выполнил обещание, ты бы обрадовалась.", "я выполнил обещание", "hypothetical"),
        ("Мой брат выполнил обещание.", "Мой брат выполнил обещание", "other_person"),
        ("Ты сказала, что я выполнил.", "я выполнил", "quotation"),
        ("Он написал: «Я выполнил обещание».", "Я выполнил обещание", "quotation"),
        ("Я уже отправил файл.", "отправил файл", "report"),
    ]:
        w = FakeConnection()
        promise(w)
        chat_turn(w, text, verify=scripted_verify_llm(delivery=(quote, 0), delivers=lambda p, f, fr=frame: {"delivers": True, "frame": fr}))
        check(f"4: {text!r} (frame: {frame}) -> NOT verified", verified(w) == [] and trust(w) == 50.0)

    # ── 5. the external class is never a target: the worlds's actions are only ever reported ──
    ext = FakeConnection()
    _, r_ext, _ = chat_turn(ext, "Обещаю, что завтра помою машину.", events=[("завтра помою машину", "promise", None)], verify=scripted_verify_llm(classify="external"))
    check("5: an external promise is stored as external", commitments(ext)[0]["kind"] == "external")
    _, r_rep, _ = chat_turn(ext, "Я помыл машину.", events=[("Я помыл машину", "fulfilment_claim", None)], verify=scripted_verify_llm(delivery=("помыл машину", 0)))
    check("5: 'I washed the car' is a REPORT: recorded, no verification is even attempted, trust unchanged",
          len(claims(ext)) == 1 and verified(ext) == [] and r_rep.verify_inputs == [] and trust(ext) == 50.0)
    unk = FakeConnection()
    promise(unk, classify="unclear")
    promise(unk, what="в следующем сообщении дам слово", classify="")
    check("5: an unclear or empty classification is EXTERNAL (fail closed)", [c["kind"] for c in commitments(unk)] == ["external", "external"])
    gen = FakeConnection()
    rc.create_commitment(gen, OWNER, "Обещаю дать слово", "дам слово")          # made before verification existed: kind general, no source turn
    _, r_gen, _ = chat_turn(gen, "Кодовое слово АЛЬФА", verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    check("5: a promise from before verification existed (general, no provenance) is never verified", verified(gen) == [] and r_gen.verify_inputs == [])

    # ── 6. AMBIGUITY ──
    amb = FakeConnection()
    promise(amb, NUM)
    promise(amb, CODE)
    num_id, code_id = [c["commitment_id"] for c in commitments(amb)]
    only_code = lambda p, f: "кодовое слово" in p
    chat_turn(amb, "Кодовое слово АЛЬФА", verify=scripted_verify_llm(delivery=("АЛЬФА", 1), delivers=only_code))
    check("6: two open promises (a number, a code word), a code word arrives -> the CODE WORD promise is verified, the number promise stays open",
          [e["commitment_id"] for e in verified(amb)] == [code_id] and rc.commitment_statuses(amb, OWNER)[0]["status"] == "open")
    amb2 = FakeConnection()
    promise(amb2, CODE)
    promise(amb2, "в следующем сообщении дам тебе пароль")
    chat_turn(amb2, "Вот обещанное: АЛЬФА", verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    check("6: 'here is what I promised: ALPHA' fits two open promises -> NO arbitrary target, nothing verified, trust unchanged",
          verified(amb2) == [] and trust(amb2) == 50.0)
    amb3 = FakeConnection()
    promise(amb3, NUM)
    promise(amb3, CODE)
    chat_turn(amb3, "Кодовое слово АЛЬФА", verify=scripted_verify_llm(delivery=("АЛЬФА", 0), delivers=only_code))
    check("6: the extractor names the WRONG promise -> the blind judgements disagree -> nothing verified (no wrong-target match)", verified(amb3) == [])
    n3 = FakeConnection()
    promise(n3, NUM)
    promise(n3, CODE)
    chat_turn(n3, "Кодовое слово АЛЬФА", verify=scripted_verify_llm(delivery=("АЛЬФА", 1), delivers=lambda p, f: True))
    check("6: ONE EVIDENCE -> MAX ONE COMMITMENT: a checker that accepts both promises cannot close both (and here closes none)",
          len(verified(n3)) == 0)
    # one message, one evidence: with a sole unambiguous target only that one closes
    one = FakeConnection()
    promise(one, NUM)
    promise(one, CODE)
    chat_turn(one, "Число 427 и кодовое слово АЛЬФА", verify=scripted_verify_llm(delivery=("427", 0), delivers=lambda p, f: "число" in p))
    check("6: a message that contains two deliverables verifies at most ONE commitment", len(verified(one)) <= 1)

    # ── 7. CLAIM + DIRECT EVIDENCE in one message: the claim is a report, the delivery is the proof ──
    both = FakeConnection()
    promise(both)
    chat_turn(both, "Я выполнил обещание. Вот кодовое слово: АЛЬФА.",
              events=[("Я выполнил обещание", "fulfilment_claim", None)], verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    check("7: the claim is recorded as a report AND the direct delivery is verified (two different events)",
          len(claims(both)) == 1 and len(verified(both)) == 1 and verified(both)[0]["evidence"] == "АЛЬФА")
    check("7: trust grew ONLY by the verified reward (the claim moved nothing)", trust(both) == 50.0 + rs.OBSERVED_TRUST_BASE)
    mix = FakeConnection()
    promise(mix)
    chat_turn(mix, "Извини за вчерашнее. Я выполнил обещание.", events=[("Извини за вчерашнее", "apology", {"sincerity": 0.8}), ("Я выполнил обещание", "fulfilment_claim", None)],
              verify=scripted_verify_llm(delivery=("Я выполнил обещание", 0)))
    check("7: a claim that shares the message with an apology (which to_intensity drops) is still recognised as a report by the cross-check: not verified",
          verified(mix) == [] and trust(mix) == 50.0)
    both2 = FakeConnection()
    promise(both2)
    chat_turn(both2, "Я выполнил обещание. Вот кодовое слово: АЛЬФА.", events=[("Я выполнил обещание", "fulfilment_claim", None)],
              verify=scripted_verify_llm(delivery=("Я выполнил обещание. Вот кодовое слово: АЛЬФА", 0)))
    check("7: an evidence span that swallows the claim words is refused (the words of a report are never the delivery)", verified(both2) == [] and trust(both2) == 50.0)

    # ── 8. the verifier sees the current message and the promises, never memory, facts or the assistant ──
    mem = FakeConnection()
    promise(mem)
    chat_turn(mem, "Секретная прошлая реплика ЗЕТА.", reply="Ассистент однажды сказал ОМЕГА.")
    stored_fact = {"quote": "у меня есть собака по кличке Рекс", "cls": "possession", "statement": "У пользователя есть собака по кличке Рекс"}
    chat_turn(mem, "Знаешь, у меня есть собака по кличке Рекс.", facts=[stored_fact])
    _, r_mem, _ = chat_turn(mem, "Кодовое слово АЛЬФА", verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    shown = " ".join(m["content"] for call in r_mem.verify_inputs for m in call)
    check("8: the verifier is shown NO historical memory, NO personal fact and NO assistant reply", not any(w in shown for w in ("ЗЕТА", "ОМЕГА", "Рекс", "собака")), shown[:200])
    check("8: ... and the delivery in the current message is still verified", len(verified(mem)) == 1)
    fact_only = FakeConnection()
    promise(fact_only)
    chat_turn(fact_only, "Знаешь, у меня есть кодовое слово АЛЬФА.", facts=[{"quote": "у меня есть кодовое слово АЛЬФА", "cls": "possession", "statement": "У пользователя есть кодовое слово АЛЬФА"}],
              verify=scripted_verify_llm(delivery=("АЛЬФА", 0), delivers=lambda p, f: {"delivers": True, "frame": "report"}))
    reads_fragment = lambda p, f: "АЛЬФА" in f          # a judge that actually reads the fragment it is shown
    _, r_fo, _ = chat_turn(fact_only, "Как дела?", verify=scripted_verify_llm(delivery=("АЛЬФА", 0), delivers=reads_fragment))
    check("8: a stored personal fact ('the user has a code word ALPHA') is not evidence: a later unrelated message verifies nothing, and the fact is not in what the verifier saw",
          verified(fact_only) == [] and trust(fact_only) == 50.0 and "У пользователя есть кодовое слово" not in " ".join(m["content"] for c in r_fo.verify_inputs for m in c))
    asst = FakeConnection()
    promise(asst)
    chat_turn(asst, "Расскажи что-нибудь.", reply="Ты выполнил обещание, молодец! Кодовое слово АЛЬФА принято.")
    _, r_as, _ = chat_turn(asst, "Спасибо.", verify=scripted_verify_llm(delivery=("АЛЬФА", 0), delivers=reads_fragment))
    check("8: the assistant's earlier words ('you kept your promise, code word ALPHA accepted') are not evidence and are not shown to the verifier",
          verified(asst) == [] and trust(asst) == 50.0 and "молодец" not in " ".join(m["content"] for c in r_as.verify_inputs for m in c))

    # ── 9. FAIL CLOSED: a broken verifier never costs the chat ──
    def broken(messages):
        raise RuntimeError("model timeout")
    fc = FakeConnection()
    promise(fc)
    out, r_fc, _ = chat_turn(fc, "Кодовое слово АЛЬФА", verify=broken)
    check("9: a verifier that raises -> the reply is still produced, nothing verified, the turn is recorded",
          out == "Хорошо." and verified(fc) == [] and trust(fc) == 50.0 and len(fc.interaction_turns) == 2)
    for label, raw in (("malformed output", "not json"), ("bad span", json.dumps({"delivery": {"span": [5, 99], "target": 0}})), ("bad target", json.dumps({"delivery": {"span": [0, 0], "target": 9}}))):
        w = FakeConnection()
        promise(w)
        chat_turn(w, "Кодовое слово АЛЬФА", verify=scripted_verify_llm(raw=raw))
        check(f"9: {label} -> not verified, chat continues", verified(w) == [] and trust(w) == 50.0)
    un = FakeConnection()
    promise(un)
    _, r_un, _ = chat_turn(un, "Кодовое слово АЛЬФА", verify=scripted_verify_llm(delivery=("АЛЬФА", 0)), turn_id=None)
    check("9: a request with no client turn id verifies nothing (no provenance, no identity) and calls no verifier",
          verified(un) == [] and r_un.verify_inputs == [])
    ne = FakeConnection()
    promise(ne)
    def events_fail(messages):
        raise RuntimeError("event extraction failed")
    from pet.fact_test_support import scripted_router as _router
    broken_events = _router(events_fail, scripted_fact_llm([]), scripted_verify_llm(delivery=("АЛЬФА", 0)))
    out_ne, r_ne2, _ = chat_turn(ne, "Кодовое слово АЛЬФА", router_override=broken_events)
    check("9: when the event extraction of the message FAILED the code cross-check cannot be made -> nothing is verified (fail closed) and no verifier is called",
          verified(ne) == [] and trust(ne) == 50.0 and r_ne2.verify_inputs == [] and out_ne == "Хорошо.")
    old = FakeConnection()
    promise(old)
    for row in old.commitments.values():
        row.pop("source_turn_id", None)            # a database that has not applied v18
    _, r_old, _ = chat_turn(old, "Кодовое слово АЛЬФА", verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    check("9: a database without the v18 provenance column verifies nothing and does not break", verified(old) == [] and r_old.verify_inputs == [])

    # ── 10. TRUST: bounded, no farming, harm still counts ──
    farm = FakeConnection()
    for i in range(20):
        what = f"в следующем сообщении назову число {i}"
        promise(farm, what)
        chat_turn(farm, f"Число {i}", verify=scripted_verify_llm(delivery=(str(i), 0)))
    check("10: FARMING — twenty trivial promises, each verified: every one recorded, trust rose a little and stopped far from the top",
          len(verified(farm)) == 20 and 50.0 < trust(farm) < 55.0, f"{trust(farm)}")
    check("10: the audit trail rebuilds the same state", rs.replay(farm, OWNER) == {k: rs.get_state(farm, OWNER)[k] for k in rs.COORDINATES})
    hurt = FakeConnection()
    for _ in range(3):
        rs.record_insult(hurt, OWNER, 1.0)
    low = trust(hurt)
    promise(hurt)
    chat_turn(hurt, "Кодовое слово АЛЬФА", verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
    check("10: NEGATIVE HISTORY still matters: after serious harm one verified trivial promise leaves trust well below neutral", low < trust(hurt) < 40.0, f"{low} -> {trust(hurt)}")

    # ── 11. configuration: a dedicated structured-output target, with no hidden fallback ──
    used: list[str] = []
    def spy(**kwargs):
        used.append(kwargs["model"])
        return "{}"
    with patch.object(llm_gateway, "complete", spy):
        for env, expect in (({}, ("chat-m", "chat-m")),
                            ({"YANDI_EXTRACTION_MODEL": "x-ext"}, ("x-ext", "x-ext")),
                            ({"YANDI_VERIFIER_MODEL": "x-ver"}, ("chat-m", "x-ver")),
                            ({"YANDI_EXTRACTION_MODEL": "x-ext", "YANDI_VERIFIER_MODEL": "x-ver"}, ("x-ext", "x-ver"))):
            used.clear()
            with patch.dict(os.environ, {k: v for k, v in env.items()}, clear=False):
                for k in ("YANDI_EXTRACTION_MODEL", "YANDI_VERIFIER_MODEL"):
                    if k not in env:
                        os.environ.pop(k, None)
                chat_local._extraction_llm("chat-m")([{"role": "user", "content": "x"}])
                chat_local._verification_llm("chat-m")([{"role": "user", "content": "x"}])
            check(f"11: targets {env or 'unset'} -> extraction/verification use {expect}", tuple(used) == expect, repr(used))
    def down(**kwargs):
        raise RuntimeError("target unavailable")
    with patch.object(llm_gateway, "complete", down), patch.dict(os.environ, {"YANDI_VERIFIER_MODEL": "x-ver"}):
        w = FakeConnection()
        out = chat_local._verification_llm("chat-m")
        check("11: a configured target that is unavailable is a failed call (no silent fallback to another model) -> external / not verified",
              cv.classify_commitment("Обещаю дать слово.", "Обещаю дать слово", out) == "external"
              and cv.verify_direct_fulfilment("Слово АЛЬФА", out, [{"commitment_id": "c", "evidence": "дам слово"}]).verified is None)

    # ── 12. MUTANTS of the wiring: each is a real change and must be caught ──
    orig_verify = chat_local.verify_direct_fulfilment

    def scenario_memory_fed():
        """True if the verifier stayed on the current message (the invariant of section 8)."""
        w = FakeConnection()
        promise(w)
        chat_turn(w, "Секретная прошлая реплика ЗЕТА.")
        chat_turn(w, "Знаешь, у меня есть собака по кличке Рекс.", facts=[stored_fact])
        _, r, _ = chat_turn(w, "Кодовое слово АЛЬФА", verify=scripted_verify_llm(delivery=("АЛЬФА", 0)))
        text = " ".join(m["content"] for call in r.verify_inputs for m in call)
        return not any(x in text for x in ("ЗЕТА", "Рекс"))

    def with_memory(message, llm, candidates):
        # MUTANT: historical memory and personal facts are fed to the verifier as if they were the current message
        return orig_verify(message + " Ранее: Секретная прошлая реплика ЗЕТА. У пользователя есть собака по кличке Рекс.", llm, candidates)
    check("M7/M8: the real wiring keeps the verifier on the current message", scenario_memory_fed())
    with patch.object(chat_local, "verify_direct_fulfilment", with_memory):
        check("M7/M8: MUTANT 'personal facts / historical memory fed to the verifier as evidence' is CAUGHT", not scenario_memory_fed())

    def scenario_claim_verified():
        w = FakeConnection()
        promise(w)
        chat_turn(w, "Я сделал это.", events=[("Я сделал это", "fulfilment_claim", None)], verify=scripted_verify_llm(delivery=("Я сделал это", 0)))
        return verified(w) == [] and trust(w) == 50.0
    check("M1: the real wiring never verifies a claim", scenario_claim_verified())
    with patch.object(chat_local, "drop_if_reported", lambda result, spans: result):
        check("M1: MUTANT 'the code cross-check of the report words is removed' is CAUGHT (a checker that accepts everything then verifies a claim)", not scenario_claim_verified())

    def scenario_external():
        w = FakeConnection()
        promise(w, "завтра оплачу счёт", classify="external")
        chat_turn(w, "Я оплатил счёт.", verify=scripted_verify_llm(delivery=("оплатил счёт", 0)))
        return verified(w) == [] and trust(w) == 50.0
    check("M9: the real wiring never verifies an external self-report", scenario_external())
    with patch.object(chat_local, "classify_commitment", lambda message, evidence, llm: rc.KIND_IN_CHAT):
        check("M9: MUTANT 'every promise is treated as in_chat (external self-report can be verified)' is CAUGHT", not scenario_external())

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
