"""
pet/pet_personal_facts_regression_test.py — the PERSONAL FACT LEDGER through the
personal chat (real chat_local, real shadow_write, real personal_facts over an
in-memory fake SQL; the models are scripted).

    USER REPORTED FACT != OBJECTIVE TRUTH.   ASSISTANT SAID X != USER FACT.
    RAW TURN != DERIVED FACT.  FACT MUST HAVE A SOURCE TURN.  SESSION != PERSON.
    OLD FACT IS HISTORY: A CORRECTION APPENDS.   SAME TURN RETRY != NEW FACT HISTORY.
    PERSONAL FACT MEMORY MAY AFFECT A REPLY; IT MUST NOT BECOME A CURRENT EVENT.
    MODEL MAY CHANGE. PERSONAL BIOGRAPHY MUST SURVIVE.

Nothing checks a verbatim reply: it checks the CONTEXT the model is given and what
is written. Mutants: each safety net is shown to catch its own removal.

Run: python -m pet.pet_personal_facts_regression_test
"""
from __future__ import annotations

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
    import agent.causal_events as causal_events
    import agent.db.sql.repositories as repo
    import agent.db.sql.shadow_write as shadow_write
    import agent.personal_facts as pf
    import agent.personal_memory as pm
    import pet.chat_local as chat_local
    from llm_gateway.types import SemanticCompletionResult
    from agent.relationship_apology_matching_regression_test import FakeConnection
    from pet.extraction_test_support import scripted_llm
    from pet.fact_test_support import scripted_fact_llm, scripted_router

    OWNER = chat_local._RELATIONSHIP_USER_ID

    class Clock:
        now = datetime(2026, 9, 1, 12, 0, 0)

        def __call__(self):
            return self.now

        def advance(self, **kw):
            self.now += timedelta(**kw)
    clock = Clock()

    def system_text(seen) -> str:
        return " ".join(str(s) for s in (seen.get("system") or []) if s)

    def chat_turn(conn, text, turn_id, *, facts=(), memory_query="none", blind=None, events=(), model_name="model-A",
                  history=None, reply="Хорошо."):
        seen: dict = {}

        def fake_semantic(**kwargs):
            seen.update(kwargs)
            return SemanticCompletionResult(reply=reply, state=None, reply_ok=True, state_ok=False, parse_ok=True, error=None,
                                            metadata={"_llm_gateway_trace": [{"result": "success", "resolved_model": model_name, "adapter_id": "llama_cpp"}]})
        router = scripted_router(scripted_llm(events), scripted_fact_llm(facts, memory_query=memory_query, blind=blind))
        msgs = list(history or []) + [{"role": "user", "content": text}]
        with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(conn)), \
             patch.object(pm, "_now", clock), patch.object(pf, "_now", clock), patch.object(repo, "_now", clock), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm", lambda model: router), \
             patch.object(llm_gateway, "complete_semantic", fake_semantic):
            out = chat_local._respond_with_character("heretic:q8", msgs, 0.7, turn_id)
        return out, seen, router

    def stored(conn, user=OWNER):
        return [f for f in conn.personal_facts if f["user_id"] == user]

    DOG = {"quote": "у меня есть собака по кличке Рекс", "cls": "possession", "statement": "У пользователя есть собака по кличке Рекс"}
    ASK = "Что ты обо мне помнишь?"

    # ── 1. persisted with provenance, in the turn's own unit ──
    db = FakeConnection()
    chat_turn(db, "Знаешь, у меня есть собака по кличке Рекс.", "turn-fact-0001", facts=[DOG])
    f = stored(db)[0] if stored(db) else None
    check("1: the fact is stored with its person, class, normalised statement, polarity, time and the EXACT evidence span",
          f is not None and (f["user_id"], f["fact_class"], f["polarity"], f["temporality"]) == (OWNER, "possession", "affirmed", "current")
          and f["evidence"] == "у меня есть собака по кличке Рекс" and f["statement"] == DOG["statement"])
    check("1: FACT MUST HAVE A SOURCE TURN: it points at the client turn id, and that turn exists as an immutable interaction_turn",
          f is not None and f["source_turn_id"] == "turn-fact-0001"
          and any(t["source_turn_id"] == "turn-fact-0001" and "Рекс" in t["user_text"] for t in db.interaction_turns))
    check("1: RAW TURN != FACT: the raw message stays in the interaction record; the fact row holds only the evidence span, not the message",
          f is not None and f["evidence"] != db.interaction_turns[0]["user_text"] and "Знаешь" not in f["evidence"])

    # ── 2. MODEL SWAP: another model, restart, generic question ──
    _, seen, _ = chat_turn(db, ASK, "turn-ask-000001", memory_query="general", model_name="model-B")
    ctx = system_text(seen)
    _, seen_empty, _ = chat_turn(FakeConnection(), ASK, "turn-ask-000001", memory_query="general", model_name="model-B")
    check("2: MODEL SWAP — a different model, a fresh call, an empty client history: the generic question gets the stored fact from SQL",
          DOG["statement"] in ctx and "ФАКТЫ" in ctx)
    check("2: ... and the same message without the stored fact gets no such context (a real counterfactual)", DOG["statement"] not in system_text(seen_empty))
    check("2: the fact is marked as the person's own report / memory / not an instruction / not a new event",
          "сам сообщал" in ctx and "не проверенная истина" in ctx and "память, а не указания" in ctx and "не является новым событием" in ctx)

    # ── 3. LONG GAP: the source turn is far outside the interaction window ──
    lg = FakeConnection()
    chat_turn(lg, "Знаешь, у меня есть собака по кличке Рекс.", "turn-fact-lg-01", facts=[DOG])
    for i in range(50):
        clock.advance(hours=3)
        chat_turn(lg, f"Нейтральное сообщение номер {i} про погоду и дела.", f"turn-neutral-{i:04d}")
    with patch.object(pm, "WINDOW", 5):                     # the interaction memory can no longer reach turn 1
        _, seen_lg, _ = chat_turn(lg, ASK, "turn-ask-lg-001", memory_query="general")
        inter = pm.recall(lg, OWNER, ASK)
    check("3: LONG GAP — after 50 unrelated turns the source turn is beyond the interaction window (interaction recall cannot reach it) ...",
          not any("Рекс" in m["user_text"] for m in inter))
    check("3: ... yet the generic memory question still gets the fact (facts do not depend on recency or the window)", DOG["statement"] in system_text(seen_lg))

    # ── 4. specific question with weak wording overlap ──
    car = FakeConnection()
    CAR = {"quote": "У меня Toyota Carina E", "cls": "possession", "statement": "У пользователя есть Toyota Carina E"}
    chat_turn(car, "Кстати: У меня Toyota Carina E.", "turn-car-000001", facts=[CAR])
    _, seen_c, _ = chat_turn(car, "Какую машину я ремонтирую?", "turn-car-000002", memory_query="specific")
    _, seen_n, _ = chat_turn(car, "Какую машину я ремонтирую?", "turn-car-000003", memory_query="none")
    _, seen_l, _ = chat_turn(car, "Расскажи про Toyota Carina.", "turn-car-000004", memory_query="none")
    check("4: a question about a thing in the person's own life (route 'specific') is given the profile although no words overlap", CAR["statement"] in system_text(seen_c))
    check("4: without that route and without shared words the fact is NOT forced into an unrelated reply", CAR["statement"] not in system_text(seen_n))
    check("4: the lexical route still finds a fact that shares content with the message", CAR["statement"] in system_text(seen_l))

    # ── 5. CORRECTION appends history ──
    co = FakeConnection()
    chat_turn(co, "Мою собаку зовут Рекс.", "turn-corr-a-001", facts=[{"quote": "Мою собаку зовут Рекс", "cls": "possession", "statement": "Собаку пользователя зовут Рекс"}])
    old_row = dict(stored(co)[0])
    chat_turn(co, "Нет, я ошибся, её зовут Макс.", "turn-corr-b-001", facts=[
        {"quote": "её зовут Макс", "cls": "possession", "statement": "Собаку пользователя зовут Макс", "relation": "replaces", "target": 0}])
    rows = stored(co)
    new_row = next(r for r in rows if r["fact_id"] != old_row["fact_id"])
    old_now = next(r for r in rows if r["fact_id"] == old_row["fact_id"])
    sup = [e for e in co.personal_fact_events if e["event_type"] == "superseded"]
    check("5: CORRECTION — Rex is NOT deleted or rewritten: the old row is exactly as written", old_now == old_row and old_now["statement"] == "Собаку пользователя зовут Рекс")
    check("5: ... Max is appended as a new fact, and a 'superseded' event records the correction with the CORRECTING turn as provenance",
          new_row["statement"] == "Собаку пользователя зовут Макс" and len(sup) == 1 and sup[0]["fact_id"] == old_row["fact_id"]
          and sup[0]["by_fact_id"] == new_row["fact_id"] and sup[0]["source_turn_id"] == "turn-corr-b-001" and sup[0]["evidence"] == "её зовут Макс")
    statuses = {f["statement"]: f["status"] for f in pf.list_facts(co, OWNER)}
    check("5: folded status: Max is current, Rex is superseded (they cannot both be current)",
          statuses == {"Собаку пользователя зовут Макс": "current", "Собаку пользователя зовут Рекс": "superseded"}, repr(statuses))
    _, seen_co, _ = chat_turn(co, ASK, "turn-corr-c-001", memory_query="general")
    check("5: the profile stated to the model has Max and never the corrected-away Rex", "Макс" in system_text(seen_co) and "Рекс" not in system_text(seen_co))

    # ── 6. HISTORICAL fact ──
    hi = FakeConnection()
    chat_turn(hi, "В детстве у меня была собака.", "turn-hist-00001", facts=[
        {"quote": "В детстве у меня была собака", "cls": "possession", "statement": "В детстве у пользователя была собака", "time": "past"}])
    _, seen_h, _ = chat_turn(hi, ASK, "turn-hist-00002", memory_query="general")
    hctx = system_text(seen_h)
    check("6: HISTORICAL — stored as a past fact", stored(hi)[0]["temporality"] == "past" and pf.list_facts(hi, OWNER)[0]["status"] == "historical")
    check("6: the profile presents it as PAST ('раньше'), never as a current possession",
          "раньше (сейчас может быть иначе) — " in hctx and "сейчас — \"В детстве" not in hctx)

    # ── 7. NEGATIVE fact ──
    ne = FakeConnection()
    chat_turn(ne, "Я не пью кофе.", "turn-neg-000001", facts=[{"quote": "Я не пью кофе", "cls": "preference", "statement": "Пользователь не пьёт кофе", "polarity": "negated"}])
    _, seen_ne, _ = chat_turn(ne, ASK, "turn-neg-000002", memory_query="general")
    check("7: NEGATIVE fact — kept with its polarity and told to the model as a negation", stored(ne)[0]["polarity"] == "negated" and "не пьёт кофе" in system_text(seen_ne))

    # ── 8. false-positive frames write NOTHING ──
    fp = FakeConnection()
    chat_turn(fp, "Мой брат сказал: у меня BMW.", "turn-quot-00001", facts=[{"quote": "у меня BMW", "cls": "possession", "statement": "У пользователя есть BMW"}], blind=[{"frame": "quotation"}])
    chat_turn(fp, "Если бы у меня была собака, я бы гулял.", "turn-hypo-00001", facts=[{"quote": "у меня была собака", "cls": "possession", "statement": "У пользователя есть собака"}], blind=[{"frame": "hypothetical"}])
    chat_turn(fp, "Может быть, куплю собаку.", "turn-plan-00001", facts=[{"quote": "куплю собаку", "cls": "possession", "statement": "У пользователя есть собака"}], blind=[{"frame": "uncertain"}])
    check("8: a QUOTED other person, a HYPOTHETICAL and a PLAN produce no fact (0 rows) — but each turn is still recorded as raw history",
          fp.personal_facts == [] and len(fp.interaction_turns) == 3)

    # ── 9. ASSISTANT SAID X != USER FACT ──
    def assistant_false_memory(mutant=False) -> tuple:
        conn = FakeConnection()
        history = [{"role": "user", "content": "Привет"}, {"role": "assistant", "content": "Наверное, у тебя есть собака Барсик."}]
        _, _, router = chat_turn(conn, "Нет.", "turn-asst-000001", history=history,
                                 facts=[{"quote": "у тебя есть собака Барсик", "cls": "possession", "statement": "У пользователя есть собака Барсик"}])
        return conn, router
    conn9, router9 = assistant_false_memory()
    seen_by_fact_llm = " ".join(m["content"] for call in router9.fact_inputs for m in call)
    check("9: ASSISTANT FALSE MEMORY — the assistant's guess followed by the user's 'Нет' yields NO fact", conn9.personal_facts == [])
    check("9: the assistant's words never reach the fact extractor (it is shown the current user message only)", "Барсик" not in seen_by_fact_llm and "Наверное" not in seen_by_fact_llm)

    # ── 10. PERSON ISOLATION ──
    iso = FakeConnection()
    with patch.object(pm, "_now", clock), patch.object(pf, "_now", clock), patch.object(repo, "_now", clock):
        pm.record_turn(iso, "person_a", "turn-iso-a-0001", "У меня есть собака Рекс", "ок")
        pf.record_turn_facts(iso, "person_a", "turn-iso-a-0001", [
            __import__("pet.fact_extraction", fromlist=["x"]).ExtractedFact("possession", DOG["statement"], "affirmed", "current", "У меня есть собака Рекс", 0, 22)])
    a = pf.select_for_prompt(pf.list_facts(iso, "person_a"), ASK, profile=True)
    b = pf.select_for_prompt(pf.list_facts(iso, "person_b"), ASK, profile=True)
    check("10: PERSON ISOLATION — A's fact is in A's profile, B (same question) gets none", len(a) == 1 and b == [])

    # ── 11. retry / same text ──
    rt = FakeConnection()
    chat_turn(rt, "Знаешь, у меня есть собака по кличке Рекс.", "turn-retry-0001", facts=[DOG])
    chat_turn(rt, "Знаешь, у меня есть собака по кличке Рекс.", "turn-retry-0001", facts=[DOG])
    once = (len(rt.personal_facts), len(rt.personal_fact_events), len(rt.interaction_turns))
    check("11: SAME TURN retried -> one fact, no fact event, one interaction (nothing applied twice)", once == (1, 0, 1), repr(once))
    clock.advance(days=7)
    chat_turn(rt, "Я же говорил: у меня есть собака по кличке Рекс.", "turn-retry-0002", facts=[DOG])
    check("11: the same fact a week later in ANOTHER turn is one more occurrence (a 'restated' event with THAT turn's evidence), not a second fact",
          len(rt.personal_facts) == 1 and [(e["event_type"], e["source_turn_id"]) for e in rt.personal_fact_events] == [("restated", "turn-retry-0002")])
    chat_turn(rt, "А ещё у меня есть кошка по кличке Мурка.", "turn-retry-0003", facts=[{"quote": "у меня есть кошка по кличке Мурка", "cls": "possession", "statement": "У пользователя есть кошка по кличке Мурка"}])
    check("11: a different proposition is a new fact (no dedupe by loose similarity)", len(rt.personal_facts) == 2)

    # ── 11b. a wrong "same" link must not hang a foreign statement on a known fact ──
    wl = FakeConnection()
    chat_turn(wl, "Знаешь, у меня есть собака по кличке Рекс.", "turn-wl-00000001", facts=[DOG])
    chat_turn(wl, "У меня есть кошка Мурка.", "turn-wl-00000002", facts=[
        {"quote": "У меня есть кошка Мурка", "cls": "possession", "statement": "У пользователя есть кошка Мурка", "relation": "same", "target": 0}])
    check("11b: a model that calls a NEW statement a restatement of a different fact gets no false 'restated' event: it becomes its own fact",
          len(wl.personal_facts) == 2 and wl.personal_fact_events == [])
    wl2 = FakeConnection()
    chat_turn(wl2, "Знаешь, у меня есть собака по кличке Рекс.", "turn-wl2-0000001", facts=[DOG])
    with patch.object(chat_local, "extract_personal_facts", lambda text, llm, known=None: __import__("pet.fact_extraction", fromlist=["x"]).extract_personal_facts(
            text, scripted_fact_llm([{"quote": "У меня есть кошка Мурка", "cls": "possession", "statement": "У пользователя есть кошка Мурка", "relation": "replaces", "target": 0}], link=[{"revises": False}, {"conflict": False}]), known)):
        chat_turn(wl2, "У меня есть кошка Мурка.", "turn-wl2-0000002")
    check("11b: a wrongly claimed CORRECTION is confirmed by its own judgement; when that says no, the cat does not replace the dog: both stay current, no 'superseded' event",
          len(wl2.personal_facts) == 2 and wl2.personal_fact_events == [] and all(f["status"] == "current" for f in pf.list_facts(wl2, OWNER)))

    # ── 12. FACT MEMORY MUST NOT BECOME AN EVENT; the event extractor never sees it ──
    ev = FakeConnection()
    chat_turn(ev, "Знаешь, у меня есть собака по кличке Рекс.", "turn-ev-0000001", facts=[DOG])
    grievances, causal = len(ev.grievances), list(ev.causal_events)
    _, seen_ev, router_ev = chat_turn(ev, "Как погода?", "turn-ev-0000002", memory_query="general")
    fed_to_events = " ".join(m["content"] for call in router_ev.event_inputs for m in call)
    check("12: the stored fact IS in the reply context, yet the EVENT extractor saw only the current message", "Рекс" in system_text(seen_ev)
          and "Рекс" not in fed_to_events and "Как погода" in fed_to_events)
    check("12: reading facts created no relationship event", len(ev.grievances) == grievances and [(c["source_turn_id"], c["event_type"]) for c in ev.causal_events if c["event_type"] != "personal_facts"]
          == [(c["source_turn_id"], c["event_type"]) for c in causal if c["event_type"] != "personal_facts"])
    check("12: the fact extractor's known-fact list is data for linking only, and never contains the assistant's words",
          all("Барсик" not in " ".join(m["content"] for m in call) for call in router_ev.fact_inputs))

    # ── 13. unidentified requests neither read nor write facts ──
    un = FakeConnection()
    chat_turn(un, "Знаешь, у меня есть собака по кличке Рекс.", "turn-un-0000001", facts=[DOG])
    n_before = len(un.personal_facts)
    _, seen_un, _ = chat_turn(un, "Знаешь, у меня есть кошка Мурка.", None, facts=[{"quote": "у меня есть кошка Мурка", "cls": "possession", "statement": "У пользователя есть кошка Мурка"}], memory_query="general")
    check("13: a request WITHOUT a client turn id (a tool) writes no fact and is given none", len(un.personal_facts) == n_before and "Рекс" not in system_text(seen_un))

    # ── 14. UNKNOWN != EMPTY ──
    class MissingTable(Exception):
        pass

    def boom(*a, **k):
        raise MissingTable(1146, "Table 'yandi_epistemic.personal_fact' doesn't exist")
    mt = FakeConnection()
    with patch.object(repo, "list_personal_facts", boom), patch.object(repo, "insert_personal_fact", boom):
        reply, seen_mt, _ = chat_turn(mt, "Знаешь, у меня есть собака по кличке Рекс.", "turn-nov17-0001", facts=[DOG], reply="Ответ без фактов.")
    check("14: with the fact tables missing (schema v17 not applied) the reply is produced, nothing is claimed about facts, and the raw turn is still recorded",
          reply == "Ответ без фактов." and "ФАКТЫ" not in system_text(seen_mt) and len(mt.interaction_turns) == 1)

    # ── 15. the interaction memory does not repeat what a fact already says ──
    dd = FakeConnection()
    chat_turn(dd, "Знаешь, у меня есть собака по кличке Рекс.", "turn-dd-00000001", facts=[DOG])
    _, seen_dd, _ = chat_turn(dd, ASK, "turn-dd-00000002", memory_query="general")
    check("15: the same content is not said twice: the turn a shown fact came from is not also quoted as conversation memory",
          system_text(seen_dd).count("Рекс") == 1, str(system_text(seen_dd).count("Рекс")))   # the statement once; never also the raw turn

    # ── 16. atomic unit: a failing fact write rolls the WHOLE turn back (engine-level proof: agent/personal_facts_sql_integration_test) ──
    src = __import__("inspect").getsource(chat_local._respond_with_character)
    check("16: the fact write is a step of the turn's single persistence unit (after the source record), not a separate transaction",
          "shadow_record_personal_facts" in src and src.index("shadow_record_interaction_turn") < src.index("shadow_record_personal_facts") < src.index("return {\"applied\": True}"))

    # ── 17. MUTANTS ──
    def scenario_assistant_text_not_fact() -> bool:
        return assistant_false_memory()[0].personal_facts == []

    real_extract = chat_local.extract_personal_facts

    def leaky_facts(text, llm, known=None):
        return real_extract(text + " Наверное, у тебя есть собака Барсик.", llm, known)
    with patch.object(chat_local, "extract_personal_facts", leaky_facts):
        check("17 M1: MUTANT the assistant's words are handed to the fact extractor -> the assistant-false-memory check FAILS", not scenario_assistant_text_not_fact())

    def scenario_source_turn() -> bool:
        conn = FakeConnection()
        chat_turn(conn, "Знаешь, у меня есть собака по кличке Рекс.", "turn-m2-00000001", facts=[DOG])
        return bool(conn.personal_facts) and all(f["source_turn_id"] == "turn-m2-00000001" for f in conn.personal_facts)

    def insert_without_source(conn, fact_id, user_id, fact_class, statement, polarity, temporality, evidence, ss, se, source_turn_id, created_at=None):
        conn.personal_facts.append({"fact_id": fact_id, "user_id": user_id, "fact_class": fact_class, "statement": statement, "polarity": polarity,
                                    "temporality": temporality, "evidence": evidence, "span_start": ss, "span_end": se, "source_turn_id": None, "created_at": created_at})
    check("17 M2 (baseline): every stored fact points at its source turn", scenario_source_turn())
    with patch.object(repo, "insert_personal_fact", insert_without_source):
        check("17 M2: MUTANT a fact loses its source_turn_id -> the provenance check FAILS", not scenario_source_turn())

    def scenario_correction_keeps_history() -> bool:
        conn = FakeConnection()
        chat_turn(conn, "Мою собаку зовут Рекс.", "turn-m3-a-000001", facts=[{"quote": "Мою собаку зовут Рекс", "cls": "possession", "statement": "Собаку пользователя зовут Рекс"}])
        first = dict(conn.personal_facts[0])
        chat_turn(conn, "Нет, я ошибся, её зовут Макс.", "turn-m3-b-000001", facts=[{"quote": "её зовут Макс", "cls": "possession", "statement": "Собаку пользователя зовут Макс", "relation": "replaces", "target": 0}])
        return first in conn.personal_facts and len(conn.personal_facts) == 2 and any(e["event_type"] == "superseded" for e in conn.personal_fact_events)

    def overwrite(conn, fact_id, user_id, event_type, by_fact_id, evidence, ss, se, source_turn_id, created_at=None):
        if event_type == "superseded":      # the forbidden way: rewrite the old row in place
            new = next(f for f in conn.personal_facts if f["fact_id"] == by_fact_id)
            old = next(f for f in conn.personal_facts if f["fact_id"] == fact_id)
            old["statement"] = new["statement"]
            conn.personal_facts.remove(new)
    check("17 M3 (baseline): a correction appends and leaves the old row untouched", scenario_correction_keeps_history())
    with patch.object(repo, "insert_personal_fact_event", overwrite):
        check("17 M3: MUTANT a correction overwrites the old fact -> the history check FAILS", not scenario_correction_keeps_history())

    def scenario_retry_once() -> bool:
        conn = FakeConnection()
        chat_turn(conn, "Знаешь, у меня есть собака по кличке Рекс.", "turn-m4-00000001", facts=[DOG])
        chat_turn(conn, "Знаешь, у меня есть собака по кличке Рекс.", "turn-m4-00000001", facts=[DOG])
        return (len(conn.personal_facts), len(conn.personal_fact_events)) == (1, 0)
    check("17 M4 (baseline): a retried turn applies its facts once", scenario_retry_once())
    with patch.object(causal_events, "claim", lambda *a, **k: causal_events.NEW):
        check("17 M4: MUTANT a retry re-applies the turn's facts -> the retry check FAILS", not scenario_retry_once())

    def scenario_long_gap() -> bool:
        conn = FakeConnection()
        chat_turn(conn, "Знаешь, у меня есть собака по кличке Рекс.", "turn-m5-00000001", facts=[DOG])
        for i in range(12):
            chat_turn(conn, f"Нейтральное сообщение номер {i} про погоду.", f"turn-m5-n{i:07d}")
        with patch.object(pm, "WINDOW", 5):
            _, s, _ = chat_turn(conn, ASK, "turn-m5-ask00001", memory_query="general")
        return DOG["statement"] in system_text(s)
    check("17 M5 (baseline): the old fact is recalled far outside the interaction window", scenario_long_gap())
    real_list = pf.list_facts

    def windowed(conn, user_id):
        recent = {t["source_turn_id"] for t in repo.list_recent_interaction_turns(conn, user_id, pm.WINDOW)}
        return [f for f in real_list(conn, user_id) if f["source_turn_id"] in recent]
    with patch.object(pf, "list_facts", windowed):
        check("17 M5: MUTANT facts only from the recent interaction window -> the long-gap check FAILS", not scenario_long_gap())

    def scenario_events_isolated() -> bool:
        conn = FakeConnection()
        chat_turn(conn, "Знаешь, у меня есть собака по кличке Рекс.", "turn-m6-00000001", facts=[DOG])
        _, _, r = chat_turn(conn, "Как погода?", "turn-m6-00000002", memory_query="general")
        return "Рекс" not in " ".join(m["content"] for call in r.event_inputs for m in call)
    check("17 M6 (baseline): the event extractor never sees the facts", scenario_events_isolated())
    real_events = chat_local.extract_relational_events

    def leaky_events(text, llm):
        return real_events(text + " У пользователя есть собака по кличке Рекс.", llm)
    with patch.object(chat_local, "extract_relational_events", leaky_events):
        check("17 M6: MUTANT fact memory fed to the event extractor -> the isolation check FAILS", not scenario_events_isolated())

    def scenario_isolation() -> bool:
        conn = FakeConnection()
        chat_turn(conn, "Знаешь, у меня есть собака по кличке Рекс.", "turn-m7-00000001", facts=[DOG])
        return pf.list_facts(conn, "person_b") == [] and len(pf.list_facts(conn, OWNER)) == 1
    check("17 M7 (baseline): person B has no facts", scenario_isolation())
    real_repo_list = repo.list_personal_facts
    with patch.object(repo, "list_personal_facts", lambda conn, user_id, limit=1000: real_repo_list(conn, OWNER, limit)):
        check("17 M7: MUTANT facts are read without the person filter -> the isolation check FAILS", not scenario_isolation())

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
