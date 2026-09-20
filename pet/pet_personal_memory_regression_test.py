"""
pet/pet_personal_memory_regression_test.py — PERSONAL MEMORY CONTINUITY through
the personal chat.

    MODEL MAY CHANGE. MEMORY MUST SURVIVE.
    STORED -> READ LATER -> CAUSALLY CHANGES BEHAVIOUR (the reply's context).
    MEMORY MAY AFFECT A REPLY; MEMORY MUST NOT BECOME A NEW USER EVENT.
    SAME TURN RETRIED != NEW HISTORY.   SAME TEXT != SAME TURN.   SESSION != PERSON.

Real chat_local + real shadow_write wrappers + real personal_memory over an
in-memory fake SQL connection (which stands for the database that outlives every
process); the model is scripted. Nothing here checks a verbatim reply: it checks
the CONTEXT the model is given and what gets written.

Run: python -m pet.pet_personal_memory_regression_test
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
    import agent.db.sql.repositories as repo
    import agent.db.sql.shadow_write as shadow_write
    import agent.personal_memory as pm
    import pet.chat_local as chat_local
    from llm_gateway.types import SemanticCompletionResult
    from agent.relationship_apology_matching_regression_test import FakeConnection
    from pet.extraction_test_support import scripted_llm

    OWNER = chat_local._RELATIONSHIP_USER_ID
    T0 = datetime(2026, 9, 1, 12, 0, 0)

    class Clock:
        def __init__(self):
            self.now = T0

        def __call__(self):
            return self.now

        def advance(self, **kw):
            self.now += timedelta(**kw)

    clock = Clock()

    def system_text(seen) -> str:
        return " ".join(str(s) for s in (seen.get("system") or []) if s)

    def chat_turn(conn, text, turn_id, *, model_name="model-A", adapter="llama_cpp", history=None, events=(),
                  reply="Хорошо.", reply_ok=True, extractor_log=None):
        """One turn as the HTTP endpoint would run it, against `conn`. Returns (reply, what the model saw)."""
        seen: dict = {}

        def fake_semantic(**kwargs):
            seen.update(kwargs)
            return SemanticCompletionResult(
                reply=reply, state=None, reply_ok=reply_ok, state_ok=False, parse_ok=True, error=None,
                metadata={"_llm_gateway_trace": [
                    {"result": "success", "resolved_model": model_name, "adapter_id": adapter, "runtime": adapter}]})
        extractor = scripted_llm(events)

        def spying_extractor(messages):
            if extractor_log is not None:
                extractor_log.append(messages)
            return extractor(messages)
        msgs = list(history or []) + [{"role": "user", "content": text}]
        with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(conn)), \
             patch.object(pm, "_now", clock), patch.object(repo, "_now", clock), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm", lambda model: spying_extractor), \
             patch.object(llm_gateway, "complete_semantic", fake_semantic):
            out = chat_local._respond_with_character("heretic:q8", msgs, 0.7, turn_id)
        return out, seen

    def rows(conn, user=OWNER):
        return [t for t in conn.interaction_turns if t["user_id"] == user]

    PERSONAL = "Мой отец тяжело болеет, я всю неделю езжу к нему в больницу."
    LATER = "Как думаешь, что мне делать с отцом и больницей?"

    # ── 1. a turn leaves an immutable SOURCE record ──
    db = FakeConnection()
    out, _ = chat_turn(db, PERSONAL, "turn-000001-a", reply="Мне жаль, что так тяжело.")
    r = rows(db)
    check("1: the turn is persisted: person, client turn id, both sides, model, adapter",
          len(r) == 1 and r[0]["source_turn_id"] == "turn-000001-a" and r[0]["turn_id_origin"] == "client"
          and r[0]["user_text"] == PERSONAL and r[0]["assistant_text"] == out == "Мне жаль, что так тяжело."
          and r[0]["model"] == "model-A" and r[0]["adapter"] == "llama_cpp", repr(r))

    # ── 2. RESTART: a new process with the same database remembers ──
    clock.advance(days=2)
    del out
    _, seen = chat_turn(db, LATER, "turn-000002-a")     # fresh call, no in-process state, empty client history
    ctx = system_text(seen)
    check("2: RESTART — a fresh chat call (empty client history, no cache) is given the earlier exchange from SQL",
          PERSONAL in ctx and "ПРОШЛОЕ" in ctx, ctx[-300:])

    # ── 3. MODEL SWAP: another model, same person, same message; with vs. without the stored past ──
    _, seen_swapped = chat_turn(db, LATER, "turn-000003-a", model_name="model-B", adapter="remote_openai")
    with_memory = system_text(seen_swapped)
    _, seen_empty = chat_turn(FakeConnection(), LATER, "turn-000003-a", model_name="model-B", adapter="remote_openai")
    without_memory = system_text(seen_empty)
    check("3: MODEL SWAP — the new model receives the historical memory in its context ...", PERSONAL in with_memory)
    check("3: ... and that context DIFFERS from the same person / same message / same model without the stored memory",
          PERSONAL not in without_memory and with_memory != without_memory)
    swapped = rows(db)[-1]
    check("3: the record shows which model/adapter produced each reply (the history spans two models)",
          {t["model"] for t in rows(db)} == {"model-A", "model-B"} and swapped["adapter"] == "remote_openai")

    # ── 4. the memory is marked as PAST and is not the current message ──
    check("4: the recalled memory is stated as the PAST, not as the current user message, and not as a new event",
          "не сказано сейчас" in ctx and "не является новым событием" in ctx and "текущее сообщение" in ctx)

    # ── 5. MEMORY MUST NOT BECOME A NEW USER EVENT ──
    ex = FakeConnection()
    chat_turn(ex, "Ты просто ржавая консерва, от тебя никакого толку.", "turn-insult-001",
              events=[("ржавая консерва", "insult", {"severity": 0.7})])
    clock.advance(days=1)
    grievances_before = {g["id"]: dict(g) for g in ex.grievances.values()}
    events_before = list(ex.inner_state_events)
    causal_before = list(ex.causal_events)
    log: list = []
    _, seen = chat_turn(ex, "Как погода сегодня?", "turn-neutral-01", extractor_log=log)
    check("5: a past insult IS in the reply's memory context (memory affects the reply) ...",
          "ржавая консерва" in system_text(seen))
    fed = " ".join(m["content"] for call in log for m in call)
    check("5: ... yet the event extractor never saw any of it: only the current message", "ржавая консерва" not in fed and "Как погода" in fed)
    check("5: ... and reading it created 0 new events: no grievance recurrence, no state move, no new causal event",
          {g["id"]: dict(g) for g in ex.grievances.values()} == grievances_before
          and ex.inner_state_events == events_before and ex.causal_events == causal_before)

    # ── 6. READING MEMORY != EXPERIENCING A NEW EVENT (memory does not write memory) ──
    before = len(rows(ex))
    chat_turn(ex, "Как погода сегодня?", "turn-neutral-02")
    added = rows(ex)[before:]
    check("6: recalling writes exactly one new SOURCE row (this turn), never a copy of the recalled one",
          len(added) == 1 and added[0]["user_text"] == "Как погода сегодня?", repr([t["user_text"] for t in added]))
    check("6: that row records which earlier turns were shown to the model as memory (auditable provenance)",
          added[0]["recalled_turn_ids"] and "turn-insult-001" in added[0]["recalled_turn_ids"])

    # ── 7. RETRY: same turn id -> one source record; the turn is not its own memory ──
    rt = FakeConnection()
    chat_turn(rt, PERSONAL, "turn-retry-0001", reply="Мне жаль.")
    chat_turn(rt, PERSONAL, "turn-retry-0001", reply="Другой ответ на повтор.")
    check("7: SAME TURN retried -> ONE source record (the first delivery is the record; nothing merged or updated)",
          len(rows(rt)) == 1 and rows(rt)[0]["assistant_text"] == "Мне жаль.")
    _, seen = chat_turn(rt, PERSONAL, "turn-retry-0001")
    check("7: a retry never recalls itself as memory", PERSONAL not in system_text(seen))

    # ── 8. SAME TEXT, DIFFERENT TURN ──
    st = FakeConnection()
    chat_turn(st, "одинаковый текст сообщения", "turn-same-0001")
    chat_turn(st, "одинаковый текст сообщения", "turn-same-0002")
    check("8: the same words in two turns -> two historical turns (no de-duplication by text)", len(rows(st)) == 2)

    # ── 9. no turn id: recorded, honestly without a retry guarantee ──
    nd = FakeConnection()
    chat_turn(nd, "сообщение без идентификатора хода", None)
    chat_turn(nd, "сообщение без идентификатора хода", None)
    check("9: a request without a turn id is still remembered (server-minted id, origin 'server'); no retry guarantee is claimed",
          len(rows(nd)) == 2 and all(t["turn_id_origin"] == "server" and t["source_turn_id"].startswith("srv-") for t in rows(nd)))

    # ── 10. PERSON ISOLATION ──
    iso = FakeConnection()
    with patch.object(pm, "_now", clock), patch.object(repo, "_now", clock):
        pm.record_turn(iso, "person_a", "turn-iso-a-0001", PERSONAL, "Мне жаль.")
        a = pm.recall(iso, "person_a", LATER)
        b = pm.recall(iso, "person_b", LATER)
    check("10: person A has the memory, person B (same message) gets none — no leakage between people",
          any(PERSONAL in m["user_text"] for m in a) and b == [])

    # ── 11. bounded, and no double-showing of what the caller already has ──
    many = FakeConnection()
    with patch.object(pm, "_now", clock), patch.object(repo, "_now", clock):
        for i in range(60):
            pm.record_turn(many, OWNER, f"turn-many-{i:05d}", f"разговор про больницу и отца номер {i}", "ответ")
            clock.advance(minutes=5)
        recalled = pm.recall(many, OWNER, LATER)
    check(f"11: at most {pm.MAX_RECALLED} memories are recalled out of 60 (never the whole history)", 0 < len(recalled) <= pm.MAX_RECALLED, str(len(recalled)))
    with patch.object(pm, "_now", clock), patch.object(repo, "_now", clock):
        in_ctx = [t["user_text"] for t in many.interaction_turns[-3:]]
        recalled2 = pm.recall(many, OWNER, LATER, in_context_texts=in_ctx)
    check("11: turns already present in the caller's own context window are not recalled a second time",
          not any(m["user_text"] in in_ctx for m in recalled2))

    # ── 12. RELEVANCE, not just recency ──
    rel = FakeConnection()
    with patch.object(pm, "_now", clock), patch.object(repo, "_now", clock):
        pm.record_turn(rel, OWNER, "turn-topic-0001", "Я начала учить итальянский язык и записалась на курсы", "Здорово!")
        clock.advance(days=60)
        pm.record_turn(rel, OWNER, "turn-topic-0002", "Купила сегодня новые ботинки", "Красивые?")
        clock.advance(days=40)
        pm.record_turn(rel, OWNER, "turn-topic-0003", "Вчера смотрела сериал про врачей", "Интересно.")
        clock.advance(days=40)
        on_topic = pm.recall(rel, OWNER, "Как продвигается мой итальянский язык, стоит ли идти на курсы дальше?")
        off_topic = pm.recall(rel, OWNER, "Расскажи что-нибудь про космос и планеты")
    check("12: an old exchange on the same topic is recalled by relevance", any("итальянск" in m["user_text"] for m in on_topic) and
          [m["basis"] for m in on_topic if "итальянск" in m["user_text"]] == ["relevant"])
    check("12: with an unrelated topic and nothing recent, nothing is recalled", off_topic == [])

    # ── 13. YANDI's own earlier words never make a turn relevant (no memory laundering) ──
    lau = FakeConnection()
    with patch.object(pm, "_now", clock), patch.object(repo, "_now", clock):
        pm.record_turn(lau, OWNER, "turn-laund-0001", "Просто привет", "Я помню про твой итальянский язык и курсы.")
        clock.advance(days=90)
        out = pm.recall(lau, OWNER, "Как мой итальянский язык, курсы?")
    check("13: relevance comes from what the PERSON said; a turn whose reply merely quoted a memory is not pulled in by it", out == [])

    # ── 14. UNKNOWN != EMPTY: schema v16 not applied / unreadable ──
    class MissingTable(Exception):
        pass

    def boom(*a, **k):
        raise MissingTable(1146, "Table 'yandi_epistemic.interaction_turn' doesn't exist")
    ex2 = FakeConnection()
    with patch.object(repo, "list_recent_interaction_turns", boom), patch.object(repo, "record_interaction_turn", boom):
        reply, seen = chat_turn(ex2, LATER, "turn-nov16-0001", reply="Ответ без памяти.")
    check("14: with the table missing the reply is still produced, and no memory is claimed to be empty or present",
          reply == "Ответ без памяти." and "Память о прошлых разговорах" not in system_text(seen))

    # ── 15. a failed reply is recorded honestly ──
    fr = FakeConnection()
    chat_turn(fr, "сообщение, на которое не смогли ответить", "turn-fail-0001", reply="", reply_ok=False)
    check("15: a turn whose reply failed keeps the person's words and records NO assistant text (the failure line is not YANDI's speech)",
          len(rows(fr)) == 1 and rows(fr)[0]["assistant_text"] is None)

    # ── 16. legacy `episode` is neither read nor written by the personal path ──
    import inspect
    src = inspect.getsource(pm) + inspect.getsource(shadow_write.shadow_get_personal_memory) + inspect.getsource(shadow_write.shadow_record_interaction_turn)
    check("16: the personal memory path does not touch the legacy system-wide `episode` table",
          "episode" not in src.lower().replace("episodic", ""))

    # ── 17. MUTANTS: each safety net catches its own removal ──
    def scenario_context_has_memory() -> bool:
        conn = FakeConnection()
        chat_turn(conn, PERSONAL, "turn-mut-a-0001")
        clock.advance(days=1)
        _, s = chat_turn(conn, LATER, "turn-mut-a-0002", model_name="model-Z")
        return PERSONAL in system_text(s)

    def scenario_extractor_isolated() -> bool:
        conn = FakeConnection()
        chat_turn(conn, "Ты просто ржавая консерва, от тебя никакого толку.", "turn-mut-b-0001",
                  events=[("ржавая консерва", "insult", {"severity": 0.7})])
        log2: list = []
        chat_turn(conn, "Как погода сегодня?", "turn-mut-b-0002", extractor_log=log2)
        return "ржавая консерва" not in " ".join(m["content"] for call in log2 for m in call)

    def scenario_retry_single_record() -> bool:
        conn = FakeConnection()
        chat_turn(conn, PERSONAL, "turn-mut-c-0001")
        chat_turn(conn, PERSONAL, "turn-mut-c-0001")
        return len(rows(conn)) == 1

    check("17: baseline — the three scenarios hold on the real code",
          scenario_context_has_memory() and scenario_extractor_isolated() and scenario_retry_single_record())
    with patch.object(chat_local, "_past_conversation_message", lambda memories: None):
        check("17: MUTANT memory retrieval removed from the prompt -> the model-swap counterfactual FAILS", not scenario_context_has_memory())

    real_extract = chat_local.extract_relational_events

    def scenario_extractor_isolated_mutant() -> bool:
        conn = FakeConnection()
        chat_turn(conn, "Ты просто ржавая консерва, от тебя никакого толку.", "turn-mut-b-0001",
                  events=[("ржавая консерва", "insult", {"severity": 0.7})])
        log2: list = []

        def feeds_history(text, llm):
            history = " ".join(t["user_text"] for t in conn.interaction_turns)
            return real_extract(history + " " + text, llm)
        with patch.object(chat_local, "extract_relational_events", feeds_history):
            chat_turn(conn, "Как погода сегодня?", "turn-mut-b-0002", extractor_log=log2)
        return "ржавая консерва" not in " ".join(m["content"] for call in log2 for m in call)
    check("17: MUTANT retrieved history fed into the current-event extractor -> the extractor-isolation check FAILS",
          not scenario_extractor_isolated_mutant())

    def append_always(conn, user_id, source_turn_id, origin, user_text, assistant_text, **kw):
        conn.interaction_turns.append({
            "interaction_id": len(conn.interaction_turns) + 1, "user_id": user_id, "source_turn_id": source_turn_id,
            "turn_id_origin": origin, "user_text": user_text, "assistant_text": assistant_text, "model": kw.get("model"),
            "adapter": kw.get("adapter"), "recalled_turn_ids": None, "created_at": clock()})
        return True
    with patch.object(repo, "record_interaction_turn", append_always):
        check("17: MUTANT retry appends a second record -> the retry check FAILS", not scenario_retry_single_record())

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
