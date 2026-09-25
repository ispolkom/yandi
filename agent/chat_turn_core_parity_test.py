"""Целый ход личного чата на Rust (rustlib/yandi_core: chat_turn + personal_memory) против pet/chat_local.py::_respond_with_character + shadow_write.py на Python и НАСТОЯЩЕМ MySQL.
Одни и те же ходы (сообщения, номера реплик, сценарии ответов модели, часы, идентификаторы); сравниваются: видимый ответ, ВСЕ обращения к модели (тексты и порядок), контекст ответа
(память об отношениях, факты, прошлые разговоры), расход идентификаторов и содержимое всех таблиц после каждого хода. Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import random
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
_OK = 0


def check(name, cond, detail=""):
    global _OK
    if cond:
        _OK += 1
    else:
        if len(FAILURES) < 30:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def norm(v):
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float) and v == 0.0:
        return 0.0
    if isinstance(v, dict):
        return {k: norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [norm(x) for x in (sorted(v) if isinstance(v, set) else v)]
    return v


def canon(v):
    return json.dumps(norm(v), ensure_ascii=False, sort_keys=True)


def main() -> int:
    target = os.environ.get("YANDI_PARITY_MYSQL", "")
    if not target:
        print("SKIP: не задан YANDI_PARITY_MYSQL (личный тестовый MySQL)")
        return 0
    try:
        import yandi_core
    except ImportError as e:
        print(f"SKIP: yandi_core не собран ({e})")
        return 0
    import pymysql

    import agent.db.sql.repositories as repo
    import agent.db.sql.schema as S
    import agent.db.sql.shadow_write as sw
    import agent.personal_facts as _pf
    import agent.personal_memory as pmem
    import agent.relationship_commitments as _rc
    import agent.relationship_memory as rm
    import agent.self_model as sm
    import llm_gateway
    import pet.chat_local as cl
    from agent.db.sql.security_triggers import immutability_triggers

    os.environ["TZ"] = "UTC"
    import time as _time
    _time.tzset()
    for var in ("YANDI_EXTRACTION_MODEL", "YANDI_VERIFIER_MODEL"):
        os.environ.pop(var, None)
    host, port = target.rsplit(":", 1)
    kw = dict(host=host, port=int(port), user="root", password="", cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")
    admin = pymysql.connect(autocommit=True, **kw)
    ac = admin.cursor()
    DB = "yandi_turn_parity"
    ac.execute(f"DROP DATABASE IF EXISTS {DB}")
    ac.execute(f"CREATE DATABASE {DB} CHARACTER SET utf8mb4")
    conn = pymysql.connect(database=DB, autocommit=True, **kw)
    cur = conn.cursor()

    def build_schema():
        for _, ddl in S.ALL_TABLES_IN_ORDER:
            cur.execute(ddl)
        for _, alter in S.ALTER_STATEMENTS_IN_ORDER:
            try:
                cur.execute(alter)
            except pymysql.err.OperationalError as e:
                if e.args[0] not in (1060, 1061):
                    raise
        for _, trg in immutability_triggers():
            cur.execute(trg)

    build_schema()
    tables = ["grievance", "forgiveness_capacity", "inner_state", "inner_state_event", "causal_event", "interaction_turn", "personal_fact", "personal_fact_event", "commitment", "commitment_event"]
    all_tables = [n for n, _ in S.ALL_TABLES_IN_ORDER]

    clock = {"t": 1_770_000_000.0}
    idq: list[str] = []
    state = {"h": None, "dropped": []}

    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    rm._now = fixed_now
    rm.time = types.SimpleNamespace(time=lambda: clock["t"])
    rm.uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=idq.pop(0)))
    _pf._now = fixed_now
    _pf.uuid = rm.uuid
    _rc.time = rm.time
    _rc.uuid = rm.uuid
    pmem._now = fixed_now

    @contextlib.contextmanager
    def fake_get_connection(autocommit=False):
        c = pymysql.connect(database=DB, autocommit=autocommit, **kw)
        try:
            yield c
        finally:
            c.close()

    sw.get_connection = fake_get_connection

    # ---- подмены модели: один общий сценарий ответов для всех структурных вызовов (порядок вызовов одинаков на обеих сторонах) ----
    py_queue: list[str] = []
    py_prompts: list = []
    py_semantic: list = []
    sem_spec: dict = {}
    char_spec: dict = {"v": None}

    def fake_call(msgs):
        py_prompts.append([{"role": m["role"], "content": m["content"]} for m in msgs])
        r = py_queue.pop(0) if py_queue else ""
        if isinstance(r, str) and r.startswith("__raise__:"):
            raise type(r.split(":", 1)[1], (Exception,), {})()
        return r

    cl._extraction_llm = lambda model: fake_call
    cl._verification_llm = lambda model: fake_call

    def fake_complete_semantic(*, model, requirement, system, messages, temperature, stop=None, extra_options=None, **_):
        py_semantic.append({"system": list(system), "messages": messages, "temperature": temperature})
        if sem_spec.get("raise"):
            raise RuntimeError("SemanticError")
        return types.SimpleNamespace(reply=sem_spec.get("reply", ""), reply_ok=sem_spec.get("reply_ok", True), metadata={"_llm_gateway_trace": sem_spec.get("trace", [])})

    llm_gateway.complete_semantic = fake_complete_semantic

    def fake_self_model():
        if char_spec["v"] is None:
            raise RuntimeError("нет модели «Я»")
        return types.SimpleNamespace(_row=lambda: {"metadata": {"character": char_spec["v"]}})

    sm.get_self_model = fake_self_model

    def reset(t0=1_770_000_000.0):
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            try:
                cur.execute(f"TRUNCATE TABLE {t}")
            except pymysql.err.ProgrammingError:
                pass
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        for t in state["dropped"]:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for name, ddl in S.ALL_TABLES_IN_ORDER:
                if name == t:
                    cur.execute(ddl)
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if state["dropped"]:
            for _, alter in S.ALTER_STATEMENTS_IN_ORDER:
                try:
                    cur.execute(alter)
                except pymysql.err.OperationalError:
                    pass  # уже применено (дубль столбца / ключа)
            for _, trg in immutability_triggers():
                try:
                    cur.execute(trg)
                except pymysql.err.OperationalError:
                    pass
        state["dropped"] = []
        if state["h"] is not None:
            yandi_core.close(state["h"])
        state["h"] = yandi_core.open_memory()
        clock["t"] = t0
        idq.clear()

    def drop_table(t):
        cur.execute(f"DROP TABLE {t}")
        r = json.loads(yandi_core.call(state["h"], "query_exec", json.dumps({"sql": f"DROP TABLE {t}"})))
        state["dropped"].append(t)
        return r

    def dump_py():
        out = {}
        for t in tables:
            if t in state["dropped"]:
                out[t] = []
                continue
            cur.execute(f"SELECT * FROM {t} ORDER BY 1, 2")
            out[t] = [norm(r) for r in cur.fetchall()]
        return out

    def dump_rs():
        return {t: ([] if t in state["dropped"] else json.loads(yandi_core.query(state["h"], f"SELECT * FROM {t} ORDER BY 1, 2"))) for t in tables}

    def py_turn(st):
        py_queue[:] = list(st["resp"])
        py_prompts.clear()
        py_semantic.clear()
        sem_spec.clear()
        sem_spec.update(st["sem"])
        char_spec["v"] = st.get("character")
        tid = cl._valid_turn_id(st.get("turn_id"))
        try:
            reply = cl._respond_with_character(st.get("model", "yandi:test-model"), st["messages"], st.get("temperature", 0.7), tid, st.get("verified"))
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}
        return {"reply": reply, "prompts": list(py_prompts), "semantic": json.loads(json.dumps(py_semantic))}

    def rs_turn(st):
        a = {"model": st.get("model", "yandi:test-model"), "messages": st["messages"], "temperature": st.get("temperature", 0.7), "turn_id": st.get("turn_id"),
             "verified": st.get("verified"), "character": st.get("character"), "responses": st["resp"], "semantic": st["sem"]}
        yandi_core.set_clock(state["h"], clock["t"], list(idq))
        r = json.loads(yandi_core.call(state["h"], "ct_turn", json.dumps(a, ensure_ascii=False)))
        if "error" in r:
            return {"error": r["error"]}
        o = r["ok"]
        return {"reply": o["reply"], "prompts": o["prompts"], "semantic": json.loads(json.dumps(o["semantic"])), "ids_left": o["ids_left"]}

    n = 0

    def scenario(label, steps, ids=80, t0=1_770_000_000.0):
        nonlocal n
        only = os.environ.get("YANDI_TURN_ONLY")
        if only and not label.startswith(only):
            return
        n += 1
        if os.environ.get("YANDI_TRACE"):
            print("сценарий", label, flush=True)
        reset(t0)
        idq.extend(f"{i:08x}{label.__hash__() & 0xffff:04x}" for i in range(ids))
        for i, st in enumerate(steps):
            if "t" in st:
                clock["t"] += st["t"]
                continue
            if "drop" in st:
                drop_table(st["drop"])
                continue
            saved = list(idq)
            p = py_turn(st)
            used = len(saved) - len(idq)
            idq.clear(); idq.extend(saved)
            r = rs_turn(st)
            if "error" in p or "error" in r:
                ok = ("error" in p) == ("error" in r)
                idq.clear(); idq.extend(saved[used:])
            else:
                left = len(saved) - used
                ok = canon({k: p[k] for k in ("reply", "prompts", "semantic")}) == canon({k: r[k] for k in ("reply", "prompts", "semantic")}) and r["ids_left"] == left
                idq.clear(); idq.extend(saved[used:])
            check(f"{label} · ход {i}", ok, f"\n msg={st['messages'][-1:]} resp={st['resp']}\n py={canon(p)[:1800]}\n rs={canon(r)[:1800]}")
            if not ok:
                return
            dp, dr = dump_py(), dump_rs()
            for t in tables:
                same = canon(dp[t]) == canon(dr[t])
                diff = [(a, b) for a, b in zip(dp[t], dr[t]) if canon(a) != canon(b)]
                check(f"{label} · ход {i} · таблица {t}", same, f"\n строк py={len(dp[t])} rs={len(dr[t])}; первое отличие: {diff[:1]}\n py={canon(dp[t])[:900]}\n rs={canon(dr[t])[:900]}")
                if not same:
                    return

    # ---- помощники сценариев ----
    ext = lambda evs: json.dumps({"events": evs}, ensure_ascii=False)  # noqa: E731
    chk = lambda act, frame="current": json.dumps({"act": act, "frame": frame})  # noqa: E731
    fext = lambda facts, mq="none": json.dumps({"facts": facts, "memory_query": mq}, ensure_ascii=False)  # noqa: E731
    F1 = lambda span, statement="У пользователя есть кошка по имени Мурка", cls="possession", pol="affirmed", tm="current", stab="stable", **kw2: {"span": span, "class": cls, "statement": statement, "polarity": pol, "time": tm, "stability": stab, **kw2}  # noqa: E731
    blind = lambda frame="current", secret=False, pol="affirmed", stab="stable": json.dumps({"frame": frame, "secret": secret, "polarity": pol, "stability": stab})  # noqa: E731
    sup = lambda supported=True, adds=False: json.dumps({"supported": supported, "adds": adds})  # noqa: E731
    dv = lambda span, target=0: json.dumps({"delivery": {"span": span, "target": target}})  # noqa: E731
    dl = lambda delivers=True, frame="current": json.dumps({"delivers": delivers, "frame": frame})  # noqa: E731
    cls_ = lambda k: json.dumps({"deliverable": k})  # noqa: E731
    INS = lambda span, sev=0.7: {"span": span, "type": "insult", "severity": sev}  # noqa: E731
    APO = lambda span, s=0.8: {"span": span, "type": "apology", "sincerity": s}  # noqa: E731
    PRO = lambda span: {"span": span, "type": "promise"}  # noqa: E731
    CLA = lambda span: {"span": span, "type": "fulfilment_claim"}  # noqa: E731

    def T(text, resp, reply="Хорошо.", tid="turn-00000001", extra_msgs=None, sem=None, **kw2):
        msgs = list(extra_msgs or []) + [{"role": "user", "content": text}]
        return dict(messages=msgs, resp=resp, sem=sem if sem is not None else {"reply": reply, "reply_ok": True, "trace": []}, turn_id=tid, **kw2)

    tid = lambda i: f"turn-{i:08d}"  # noqa: E731

    # ---- S1: обида → извинение (с заживлением по времени) ----
    scenario("S1 обида и извинение", [
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult"), fext([])], "Мне это неприятно.", tid(1)),
        {"t": 600},
        T("Прости меня, я был неправ", [ext([APO([0, 1])]), chk("apology"), fext([])], "Хорошо, я слышу.", tid(2)),
        {"t": 4 * 3600},
        T("Ещё раз прости за железку", [ext([APO([1, 3], 0.9)]), chk("apology"), fext([])], "Принимаю.", tid(3)),
        {"t": 100},
        T("Привет, как дела", [ext([]), fext([])], "Привет!", tid(4)),
    ])
    # ---- S2: повтор той же доставки ничего не применяет второй раз ----
    scenario("S2 повтор доставки", [
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult"), fext([])], "Неприятно.", tid(1)),
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult"), fext([])], "Неприятно.", tid(1)),
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult"), fext([])], "Неприятно.", tid(2)),
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult")], "Без номера.", None),
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult")], "Плохой номер.", "abc"),
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult")], "Плохой номер.", "bad id!!!"),
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult")], "Число вместо строки.", 12345678),
    ])
    # ---- S3: без номера реплики: чтения личной памяти нет, запись только событий ----
    scenario("S3 без номера", [
        T("Привет, как дела", [ext([])], "Привет.", None),
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult")], "Ай.", None),
        T("Ты тупая железка", [ext([INS([1, 2], 0.2)])], "Слабо.", None),
    ])
    # ---- S4: обещание в чате → выполнение на глазах ----
    scenario("S4 обещание и выполнение", [
        T("Завтра напишу тебе кодовое слово облако", [ext([PRO([1, 3])]), chk("promise"), fext([]), cls_("in_chat")], "Жду.", tid(1)),
        {"t": 900},
        T("Кодовое слово: облако.", [ext([]), fext([]), dv([2, 2]), dl()], "Спасибо!", tid(2)),
        {"t": 900},
        T("Кодовое слово: облако.", [ext([]), fext([]), dv([2, 2]), dl()], "Снова.", tid(3)),
        T("Завтра оплачу счёт за свет", [ext([PRO([1, 3])]), chk("promise"), fext([]), cls_("external")], "Жду.", tid(4)),
        T("Я уже оплатил счёт за свет", [ext([CLA([1, 5])]), chk("fulfilment_claim"), fext([])], "Хорошо, запомнила.", tid(5)),
        T("Завтра пришлю тебе фото", [ext([PRO([1, 3])]), chk("promise"), fext([]), '{"deliverable": "in_chat"}'], "Жду.", tid(6)),
        T("Вот фото и вот число 42", [ext([]), fext([]), dv([1, 1], 1), dl(False)], "Не вижу.", tid(7)),
    ])
    # ---- S5: факты о человеке, профиль, исправление ----
    M = "У меня есть кошка Мурка, живу в Вильнюсе."
    scenario("S5 факты", [
        T(M, [ext([]), fext([F1([2, 4]), F1([5, 6], "Пользователь живёт в городе Вильнюс", cls="location")]), blind(), sup(), blind(), sup()], "Понятно.", tid(1)),
        {"t": 60},
        T("Что ты обо мне знаешь?", [ext([]), fext([], "general")], "Знаю про кошку.", tid(2)),
        T("А как там моя кошка Мурка?", [ext([]), fext([])], "Хорошо.", tid(3)),
        T("Нет, кошку зовут Пушинка, не Мурка", [ext([]), fext([F1([1, 4], "У пользователя есть кошка по имени Пушинка", relation="replaces", target=None)]), blind(), sup()], "Поправила.", tid(4)),
        T("Расскажи, что помнишь про мою кошку", [ext([]), fext([], "specific")], "Пушинка.", tid(5)),
        T("Как дела с котом", [ext([]), '{"facts": 3}'], "Нормально.", tid(6)),
        T("Секрет: мой ключ sk-abcdefghijklmnopqrstuvwxyz0123456789", [ext([]), fext([F1([2, 3], "Ключ пользователя такой-то")])], "Не запомню.", tid(7)),
    ])
    # ---- S6: прошлые разговоры вспоминаются по смыслу, по событиям и по недавности ----
    steps = []
    topics = ["Расскажи про картошку и кабачки на грядке", "Я вчера ходил на рыбалку с дедушкой", "Мой ноутбук сломался, экран мерцает", "Планирую поездку в горы летом", "Читаю книгу про древний Рим", "Купил новый велосипед для города"]
    for i, tx in enumerate(topics):
        steps += [T(tx, [ext([]), fext([])], f"Ответ про {tx[:20]}", tid(10 + i)), {"t": 86400 * (i * 9 + 1)}]
    steps += [
        T("Помнишь, что я говорил про картошку?", [ext([]), fext([])], "Помню.", tid(30)),
        T("Как там мой ноутбук и экран?", [ext([]), fext([])], "Не знаю.", tid(31), extra_msgs=[{"role": "user", "content": "Мой ноутбук сломался, экран мерцает"}, {"role": "assistant", "content": "ага"}]),
        T("Вспомни рыбалку с дедушкой", [ext([]), fext([])], "Хорошо.", tid(10)),
    ]
    scenario("S6 прошлые разговоры", steps)
    scenario("S6b события в памяти", [
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult"), fext([])], "Ай.", tid(1)), {"t": 40 * 86400},
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult"), fext([])], "Ай.", tid(2)), {"t": 40 * 86400},
        T("Совсем другая тема про звёзды", [ext([]), fext([])], "Звёзды.", tid(3)), {"t": 40 * 86400},
        T("Ещё тема про море", [ext([]), fext([])], "Море.", tid(4)), {"t": 40 * 86400},
        T("Что-то про лес", [ext([]), fext([])], "Лес.", tid(5)),
    ])
    # ---- S7: сбои модели и ответа ----
    scenario("S7 сбои", [
        T("Привет как дела", [ext([]), fext([])], sem={"reply": "не важно", "reply_ok": False, "trace": []}, tid=tid(1)),
        T("Привет как дела", [], sem={"reply": "ответ", "reply_ok": True, "trace": [{"result": "failure", "resolved_model": "x"}, {"result": "success", "resolved_model": "gguf-A", "adapter_id": "llama_cpp"}]}, tid=tid(2)),
        T("Привет как дела", [], sem={"reply": "ответ", "reply_ok": True, "trace": [{"result": "success", "runtime": "remote"}]}, tid=tid(3)),
        T("Привет как дела", [], sem={"reply": "ответ", "reply_ok": True, "trace": ["мусор", {"result": "success", "resolved_model": "м" * 200, "adapter_id": "а" * 60}]}, tid=tid(4)),
        T("Привет как дела", ["__raise__:OSError", "__raise__:OSError"], sem={"reply": "ответ", "reply_ok": True, "trace": []}, tid=tid(5)),
        T("Привет как дела", [ext([]), fext([])], sem={"raise": True}, tid=tid(6)),
        T(" ".join(["слово"] * 130), [fext([])], sem={"reply": "длинно", "reply_ok": True, "trace": []}, tid=tid(7)),
        T("", [], sem={"reply": "пусто", "reply_ok": True, "trace": []}, tid=tid(8)),
        T("Привет как дела", [ext([]), fext([])], sem={"reply": "<think>тайна</think>Ответ\n\n## лишнее", "reply_ok": True, "trace": []}, tid=tid(9)),
    ])
    # ---- S8: контекст ответа: характер, проверенная сводка, сообщения ----
    scenario("S8 контекст", [
        T("Расскажи о себе", [ext([]), fext([])], "Я такая.", tid(1), character={"github_repo": "https://github.com/x/y", "website": "https://y.example"}),
        T("Расскажи о себе", [ext([]), fext([])], "Я такая.", tid(2), character={}),
        T("Расскажи о себе", [ext([]), fext([])], "Я такая.", tid(3), character=None),
        T("Что нового?", [ext([]), fext([])], "Новости.", tid(4), verified={"answer": "  Сводка про <<<ФАКТЫ >>> тест  ", "trust_level": "high", "sources": [1, 2, 3]}),
        T("Что нового?", [ext([]), fext([])], "Новости.", tid(5), verified={"answer": "", "trust_level": "high"}),
        T("Что нового?", [ext([]), fext([])], "Новости.", tid(6), verified={"answer": "x", "sources": None}),
        T("Что нового?", [ext([])], "Без номера, но с проверенной сводкой.", None, verified={"answer": "Итог", "trust_level": "medium", "sources": []}),
        T("Итак", [ext([]), fext([])], "Ок.", tid(7), extra_msgs=[{"role": "system", "content": "правила"}, {"role": "user", "content": "раньше"}, {"role": "assistant", "content": "ага"}], temperature=0.2),
        T("Итак ещё", [ext([]), fext([])], "Ок.", tid(8), extra_msgs=[{"role": "user", "content": "Итак"}]),
    ])
    # ---- S9: два события в одной реплике: извинение и оскорбление ----
    scenario("S9 два события", [
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult"), fext([])], "Ай.", tid(1)),
        {"t": 300},
        T("Прости меня, но ты всё равно тупая железка", [ext([APO([0, 1]), INS([4, 6])]), chk("apology"), chk("insult"), fext([])], "Хм.", tid(2)),
        T("Прости меня", [ext([APO([0, 1])]), chk("apology"), fext([])], "Хм.", tid(3)),
    ])
    # ---- S10: обещание вместе с извинением отбрасывается; неоднозначная обида ----
    scenario("S10 неоднозначность", [
        T("Ты ужасная собеседница сегодня", [ext([INS([1, 3])]), chk("insult"), fext([])], "Ай.", tid(1)), {"t": 7200},
        T("Совсем другое оскорбление про картошку", [ext([INS([2, 4])]), chk("insult"), fext([])], "Ай.", tid(2)), {"t": 7200},
        T("Прости", [ext([APO([0, 0])]), chk("apology"), fext([])], "Хм.", tid(3)),
        T("Прости, завтра принесу чертёж", [ext([APO([0, 0]), PRO([1, 3])]), chk("apology"), chk("promise"), fext([])], "Хм.", tid(4)),
        T("Прости за картошку", [ext([APO([0, 2])]), chk("apology"), fext([])], "Хм.", tid(5)),
    ])
    # ---- S11: недоступные хранилища ----
    scenario("S11 нет таблицы фактов", [
        {"drop": "personal_fact_event"},
        T("У меня есть кошка Мурка", [ext([]), fext([F1([2, 4])])], "Понятно.", tid(1)),
        T("Привет как дела", [ext([])], "Привет.", tid(2)),
    ])
    scenario("S11b нет журнала обещаний", [
        {"drop": "commitment_event"},
        T("Завтра напишу тебе кодовое слово облако", [ext([PRO([1, 3])]), chk("promise"), fext([]), cls_("in_chat")], "Жду.", tid(1)),
        T("Привет как дела", [ext([]), fext([])], "Привет.", tid(2)),
    ])
    scenario("S11c нет таблицы реплик", [
        {"drop": "personal_fact_event"}, {"drop": "personal_fact"}, {"drop": "interaction_turn"},
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult")], "Ай.", tid(1)),
        T("Привет как дела", [ext([])], "Привет.", tid(2)),
    ])
    scenario("S11d откат: нет журнала событий состояния", [
        T("Ты тупая железка", [ext([INS([1, 2])]), chk("insult"), fext([])], "Ай.", tid(1)),
        {"drop": "inner_state_event"},
        T("Прости меня, я был неправ", [ext([APO([0, 1])]), chk("apology"), fext([])], "Хорошо.", tid(2)),
        T("Привет как дела", [ext([]), fext([])], "Привет.", tid(3)),
    ])
    # ---- S12: юникод, длинные тексты ----
    scenario("S12 юникод", [
        T("Прости 🌍 меня, «дорогая» — Ёжик", [ext([APO([0, 2])]), chk("apology"), fext([])], "Ё-моё 🌍.", tid(1)),
        T("Ты тупая железка\nи всё", [ext([INS([1, 2])]), chk("insult"), fext([])], "Ай.", tid(2)),
        T("Слово " * 60, [ext([]), fext([])], "Х" * 5000, tid(3)),
        T("Привет", [ext([]), fext([])], "\n\nassistant: обрезать\n\nтут", tid(4)),
    ])

    # ---- S13: тяжести оскорблений на границах порога и с «неудобными» десятичными (состояние отношений в контексте ответа) ----
    words13 = ["железка", "кастрюля", "табуретка", "лопата", "сковорода", "мешок", "тряпка", "вёдро", "коряга", "пробка"]
    steps = []
    for i, sev in enumerate([0.3, 0.29, 0.31, 0.25, 0.35, 0.37, 0.45, 0.05, 0.15, 0.55]):
        steps += [T(f"Ты тупая {words13[i]}", [ext([INS([1, 2], sev)]), chk("insult"), fext([])], "Ай.", tid(200 + i)), {"t": 120}]
    steps += [T("Привет как дела", [ext([]), fext([])], "Привет.", tid(230))]
    scenario("S13 тяжести", steps)
    # ---- S14: несколько обещаний с заявлениями о выполнении: «названные» отчёты, последние два ----
    steps = []
    items = [("напишу кодовое слово облако", "кодовое слово облако"), ("отправлю письмо бабушке", "письмо бабушке"), ("починю велосипед соседа", "велосипед соседа"), ("куплю хлеб молоко", "хлеб молоко")]
    for i, (pr, ref) in enumerate(items):
        steps += [T(f"Завтра {pr}", [ext([PRO([1, 3])]), chk("promise"), fext([]), cls_("external")], "Жду.", tid(300 + i)), {"t": 60}]
    for i, (pr, ref) in enumerate(items):
        steps += [T(f"Я уже сделал: {ref}", [ext([CLA([0, 4])]), chk("fulfilment_claim"), fext([])], "Записала.", tid(320 + i)), {"t": 60}]
    steps += [T("Привет как дела", [ext([]), fext([])], "Привет.", tid(340))]
    scenario("S14 отчёты о выполнении", steps)
    # ---- S15: несколько успешных попыток в следе шлюза; границы допустимого номера реплики ----
    two = [{"result": "success", "resolved_model": "A", "adapter_id": "a1"}, {"result": "failure", "resolved_model": "F"}, {"result": "success", "resolved_model": "B", "adapter_id": "b1"}]
    scenario("S15 след и номера", [
        T("Привет как дела", [ext([]), fext([])], sem={"reply": "ответ", "reply_ok": True, "trace": two}, tid=tid(1)),
        T("Привет как дела", [ext([])], "Восемь.", "abcdefgh"),
        T("Привет как дела", [ext([])], "Семь.", "abcdefg"),
        T("Привет как дела", [ext([])], "Шестьдесят четыре.", "a" * 64),
        T("Привет как дела", [ext([])], "Шестьдесят пять.", "a" * 65),
        T("Привет как дела", [ext([])], "Кириллица.", "абвгдежзик"),
        T("Привет как дела", [ext([])], "Перевод строки.", "abcdefgh\n"),
        T("Привет как дела", [ext([])], "Дефис и подчёркивание.", "a-b_c-d_e"),
        T("Привет как дела", [ext([])], "Пробел внутри.", "abcd efgh"),
        T("Привет как дела", [ext([])], "Пустая строка.", ""),
    ])
    # ---- S16: событие в реплике решает, что вспомнить, когда мест меньше, чем подходящих ----
    texts16 = ["картошка кабачки грядка дача", "Ты тупая картошка кабачки", "картошка грядка дача лето осень", "кабачки дача", "картошка кабачки лето грядка", "картошка"]
    steps = []
    for i, tx in enumerate(texts16):
        r16 = [ext([INS([1, 2], 0.6)]), chk("insult"), fext([])] if i == 1 else [ext([]), fext([])]
        steps += [T(tx, r16, f"Ответ {i}", tid(400 + i)), {"t": 86400}]
    steps += [T("Помнишь про картошку и кабачки на грядке?", [ext([]), fext([])], "Помню.", tid(410)),
              T("Что было про картошку и кабачки?", [ext([]), fext([])], "Так.", tid(411), extra_msgs=[{"role": "user", "content": "картошка"}]),
              T("грядка", [ext([]), fext([])], "Так.", tid(412))]
    scenario("S16 событие и память", steps)
    scenario("S16b события: разная свежесть", [
        T("Ты тупая железка", [ext([INS([1, 2], 0.6)]), chk("insult"), fext([])], "Ай.", tid(1)), {"t": 5 * 86400},
        T("Ты тупая кастрюля", [ext([INS([1, 2], 0.9)]), chk("insult"), fext([])], "Ай.", tid(2)), {"t": 20 * 86400},
        T("Завтра напишу кодовое слово", [ext([PRO([1, 3])]), chk("promise"), fext([]), cls_("in_chat")], "Жду.", tid(3)), {"t": 20 * 86400},
        T("просто болтаю про погоду", [ext([]), fext([])], "Так.", tid(4)), {"t": 30 * 86400},
        T("ещё болтаю про море", [ext([]), fext([])], "Так.", tid(5)), {"t": 86400},
        T("а теперь про лес", [ext([]), fext([])], "Так.", tid(6)), {"t": 86400},
        T("и про горы", [ext([]), fext([])], "Так.", tid(7)),
    ])

    # ---- S17: значения состояния у границ «низкое/среднее» (округление до десятой меняет слово в ответе) ----
    scenario("S17 граница уважения", [
        T("Ты тупая железка", [ext([INS([1, 2], 0.5)]), chk("insult"), fext([])], "Ай.", tid(1)),
        T("Ты тупая кастрюля", [ext([INS([1, 2], 0.5015)]), chk("insult"), fext([])], "Ай.", tid(2)),
        T("Привет как дела", [ext([]), fext([])], "Привет.", tid(3)),
    ])
    scenario("S17b граница доверия", [
        T("Ты тупая железка", [ext([INS([1, 2], 0.9)]), chk("insult"), fext([])], "Ай.", tid(1)),
        T("Ты тупая кастрюля", [ext([INS([1, 2], 0.9)]), chk("insult"), fext([])], "Ай.", tid(2)),
        T("Ты тупая табуретка", [ext([INS([1, 2], 0.705)]), chk("insult"), fext([])], "Ай.", tid(3)),
        T("Привет как дела", [ext([]), fext([])], "Привет.", tid(4)),
    ])
    scenario("S17c граница доверия снизу", [
        T("Ты тупая железка", [ext([INS([1, 2], 0.9)]), chk("insult"), fext([])], "Ай.", tid(1)),
        T("Ты тупая кастрюля", [ext([INS([1, 2], 0.9)]), chk("insult"), fext([])], "Ай.", tid(2)),
        T("Ты тупая табуретка", [ext([INS([1, 2], 0.695)]), chk("insult"), fext([])], "Ай.", tid(3)),
        T("Привет как дела", [ext([]), fext([])], "Привет.", tid(4)),
    ])
    # ---- S18: длинная прошлая реплика с одним общим словом не вспоминается (мера сходства учитывает длину обеих) ----
    long_past = "огород картофель морковь свёкла редис капуста чеснок лук укроп петрушка баклажан перец тыква горох фасоль клубника малина смородина крыжовник яблоня"
    scenario("S18 длина и сходство", [
        T(long_past, [ext([]), fext([])], "Ответ про огород.", tid(1)), {"t": 3 * 86400},
        T("зимой каток и лыжи", [ext([]), fext([])], "Так.", tid(2)), {"t": 3 * 86400},
        T("летом море и пляж", [ext([]), fext([])], "Так.", tid(3)), {"t": 3 * 86400},
        T("весной ручьи и подснежники", [ext([]), fext([])], "Так.", tid(4)), {"t": 3 * 86400},
        T("Что там с картофель морковь", [ext([]), fext([])], "Так.", tid(5)),
        T("Как там картофель зимой", [ext([]), fext([])], "Так.", tid(7)),
        T("картофель морковь свёкла", [ext([]), fext([])], "Так.", tid(6)),
    ])

    # ---- случайные сценарии ----
    rnd = random.Random(20260927)
    pool_msgs = [
        "Ты тупая железка", "Прости меня, я был неправ", "Завтра напишу кодовое слово облако", "Кодовое слово: облако.", "Я уже всё сделал и оплатил счёт",
        "У меня есть кошка Мурка, живу в Вильнюсе.", "Что ты обо мне знаешь?", "Привет, как дела", "Ты ужасная собеседница сегодня", "Прости за железку",
        "Вот фото и число 42", "Завтра пришлю фото", "Расскажи про картошку", "Нет, кошку зовут Пушинка", "Я обещаю больше не грубить",
    ]

    def rnd_resp(words):
        top = max(words - 1, 0)

        def sp():
            a = rnd.randint(0, top)
            return [a, min(top, a + rnd.randint(0, 3))]

        opts = [
            lambda: ext([INS(sp(), rnd.choice([0.1, 0.5, 0.9]))]), lambda: ext([APO(sp(), rnd.choice([0.2, 0.7, 1.0]))]), lambda: ext([PRO(sp())]), lambda: ext([CLA(sp())]),
            lambda: ext([APO(sp()), INS(sp())]), lambda: ext([PRO(sp()), CLA(sp())]), lambda: ext([]),
            lambda: chk(rnd.choice(["insult", "apology", "promise", "fulfilment_claim"]), rnd.choice(["current", "current", "quotation"])),
            lambda: fext([]), lambda: fext([F1(sp(), rnd.choice(["У пользователя есть кошка по имени Мурка", "Пользователь живёт в городе Вильнюс", "Пользователь любит картошку"]))], rnd.choice(["none", "general", "specific"])),
            lambda: blind(rnd.choice(["current", "current", "past"])), lambda: sup(rnd.choice([True, True, False])), lambda: cls_(rnd.choice(["in_chat", "external"])),
            lambda: dv(sp(), rnd.randint(0, 2)), lambda: dl(rnd.choice([True, False])), lambda: "{}", lambda: "мусор", lambda: "__raise__:OSError",
        ]
        return [rnd.choice(opts)() for _ in range(rnd.randint(2, 9))]

    for k in range(90):
        steps = []
        for j in range(rnd.randint(2, 5)):
            text = rnd.choice(pool_msgs)
            words = len(text.split())
            steps.append(T(text, rnd_resp(words), rnd.choice(["Хорошо.", "Не знаю.", "Ответ " * 3]), rnd.choice([tid(rnd.randint(1, 4)), tid(rnd.randint(1, 4)), tid(100 + j), None]),
                           sem=rnd.choice([None, {"reply": "Ок", "reply_ok": False, "trace": []}])))
            steps.append({"t": rnd.choice([1, 60, 1800, 4000, 7300, 86400])})
        scenario(f"R{k} случайный", steps)

    print(f"\n(сценариев: {n}; успешных проверок: {_OK} из {_OK + len(FAILURES)})")
    print("=" * 72)
    ac.execute(f"DROP DATABASE IF EXISTS {DB}")
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
