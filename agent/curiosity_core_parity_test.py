"""Движок любопытства на Rust (rustlib/yandi_core/src/curiosity.rs) против agent/curiosity.py на Python и НАСТОЯЩЕМ MySQL (убеждения читаются из базы).
Те же вызовы с управляемыми часами и идентификаторами; сравниваются результат каждого вызова и содержимое таблицы belief. Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ."""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import decimal
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
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return 0.0 if v == 0.0 else float(f"{v:.12g}")
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.dumps(norm(json.loads(v)), ensure_ascii=False, sort_keys=True)
        except ValueError:
            return v
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

    import agent.belief_manager as bm
    import agent.curiosity as cur_mod
    import agent.db.sql.repositories as repo
    import agent.db.sql.schema as S
    import llm_gateway
    from agent.db.sql.security_triggers import immutability_triggers

    os.environ["TZ"] = "UTC"
    import time as _time
    _time.tzset()
    host, port = target.rsplit(":", 1)
    kw = dict(host=host, port=int(port), user="root", password="", cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")
    admin = pymysql.connect(autocommit=True, **kw)
    ac = admin.cursor()
    DB = "yandi_cur_parity"
    ac.execute(f"DROP DATABASE IF EXISTS {DB}")
    ac.execute(f"CREATE DATABASE {DB} CHARACTER SET utf8mb4")
    conn = pymysql.connect(database=DB, autocommit=True, **kw)
    cur = conn.cursor()
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
    tables = ["belief", "belief_assessment_history"]

    clock = {"t": 1_770_000_000.0}
    idq: list[str] = []
    state = {"h": None, "cu_state": None}
    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    fake_uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=idq.pop(0)))
    fake_time = types.SimpleNamespace(time=lambda: clock["t"])
    for mod in (bm, cur_mod):
        mod.uuid = fake_uuid
        mod.time = fake_time

    @contextlib.contextmanager
    def fake_get_connection(autocommit=False):
        c = pymysql.connect(database=DB, autocommit=autocommit, **kw)
        try:
            yield c
        finally:
            c.close()

    bm.get_connection = fake_get_connection

    def fake_embed(texts, model=None, **_):
        raise RuntimeError("вложения недоступны")

    llm_gateway.embed = fake_embed
    engine = {"e": None, "bm": None}

    def reset(t0=1_770_000_000.0):
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            cur.execute(f"TRUNCATE TABLE {t}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if state["h"] is not None:
            yandi_core.close(state["h"])
        state["h"] = yandi_core.open_memory()
        state["cu_state"] = None
        clock["t"] = t0
        idq.clear()
        bm._inst = None
        cur_mod._curiosity = None

    def dump_py():
        out = {}
        for t in tables:
            cur.execute(f"SELECT * FROM {t} ORDER BY 1")
            out[t] = [norm(r) for r in cur.fetchall()]
        return out

    def dump_rs():
        return {t: json.loads(yandi_core.query(state["h"], f"SELECT * FROM {t} ORDER BY 1")) for t in tables}

    def conv(v):
        if dataclasses.is_dataclass(v):
            return dataclasses.asdict(v)
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v

    def py_call(name, a):
        try:
            if name == "init":
                bm._inst = None
                engine["bm"] = bm.get_belief_manager()
                engine["e"] = cur_mod.CuriosityEngine()
                return {"ok": None}
            if name == "bm:add":
                b = engine["bm"].add_belief(a["topic"], a["statement"], a["confidence"], evidence_for=a.get("evidence_for"), evidence_against=a.get("evidence_against"))
                return {"ok": norm(conv(b))}
            e = engine["e"]
            if name == "analyze_beliefs":
                return {"ok": norm(conv(e.analyze_beliefs()))}
            if name == "analyze_response":
                return {"ok": norm(conv(e.analyze_response(a["query"], a["epistemic"], a["confidence"], a["evidence_count"], a.get("claims", []), a["trust"])))}
            if name == "get_next_question":
                return {"ok": norm(conv(e.get_next_question()))}
            if name == "mark_resolved":
                e.mark_resolved(a["unknown_id"])
                return {"ok": None}
            if name in ("get_pending", "get_exploring", "unknowns"):
                return {"ok": norm(conv(e.unknowns if name == "unknowns" else getattr(e, name)()))}
            if name == "get_by_topic":
                return {"ok": norm(conv(e.get_by_topic(a["topic"])))}
            return {"ok": norm(getattr(e, name)())}
        except Exception as ex:  # noqa: BLE001
            return {"error": type(ex).__name__}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], list(idq))
        if name == "bm:add":
            r = json.loads(yandi_core.call(state["h"], "bm_add_belief", json.dumps(a, ensure_ascii=False)))
            return {"ok": r["ok"]["out"]} if "ok" in r else {"error": str(r["error"]).split(":")[0]}
        payload = {"method": name, "args": a, "state": None if name == "init" else state["cu_state"]}
        r = json.loads(yandi_core.call(state["h"], "cu_call", json.dumps(payload, ensure_ascii=False)))
        if "ok" in r:
            state["cu_state"] = r["ok"]["state"]
            return {"ok": r["ok"]["out"]}
        return {"error": str(r["error"]).split(":")[0]}

    n = 0

    def scenario(label, steps, ids=300):
        nonlocal n
        n += 1
        reset()
        idq.extend(f"{i:08x}{i * 7919 % 65536:04x}" for i in range(ids))
        for i, st in enumerate(steps):
            if st[0] == "t":
                clock["t"] += st[1]
                continue
            name, args = st
            saved = list(idq)
            p = py_call(name, args)
            used = len(saved) - len(idq)
            idq.clear(); idq.extend(saved)
            r = rs_call(name, args)
            idq.clear(); idq.extend(saved[used:])
            if "error" in p or "error" in r:
                ok = ("error" in p) == ("error" in r)
            else:
                ok = canon(p["ok"]) == canon(r["ok"])
            clock["t"] += 1
            check(f"{label} · шаг {i} {name}", ok, f"\n args={json.dumps(args, ensure_ascii=False)[:300]}\n py={json.dumps(p, ensure_ascii=False)[:900]}\n rs={json.dumps(r, ensure_ascii=False)[:900]}")
            if not ok:
                return
        dp, dr = dump_py(), dump_rs()
        for t in tables:
            same = canon(dp[t]) == canon(dr[t])
            diff = [(a, b) for a, b in zip(dp[t], dr[t]) if canon(a) != canon(b)]
            check(f"{label} · таблица {t}", same, f"\n строк py={len(dp[t])} rs={len(dr[t])}; первое отличие: {diff[:1]}")

    I = ("init", {})
    B = lambda topic, statement, conf, **kw2: ("bm:add", {"topic": topic, "statement": statement, "confidence": conf, **kw2})  # noqa: E731
    RS = lambda q, ep=None, conf=0.9, evid=5, trust="SUPPORTED": ("analyze_response", {"query": q, "epistemic": ep if ep is not None else {}, "confidence": conf, "evidence_count": evid, "trust": trust})  # noqa: E731
    READ = [("get_pending", {}), ("get_exploring", {}), ("get_summary", {}), ("to_dict", {}), ("unknowns", {})]

    scenario("S1 убеждения разной уверенности", [
        I, B("т1", "Земля круглая, как известно всем " * 3, 0.3), B("т2", "Небо голубое", 0.59), B("т3", "Вода мокрая", 0.6), B("т4", "Огонь горячий", 0.79), B("т5", "Лёд холодный", 0.8), B("т6", "Ветер дует", 0.99), B("т7", "Штиль", 0.05),
        ("analyze_beliefs", {}), ("analyze_beliefs", {}), ("get_summary", {}), ("to_dict", {}), ("unknowns", {}),
    ])
    scenario("S2 анализ ответа", [
        I, RS("Что такое сознание?", {"domain": "philosophical", "testability": "interpretive"}, 0.4, 1, "UNVERIFIED"), RS("Смысл жизни?", {"domain": "philosophical"}, 0.9, 5), RS("Как жить, в чём смысл?", {}, 0.59, 2, "PARTIALLY_SUPPORTED"),
        RS("Просто вопрос", {"domain": "factual"}, 0.6, 3), RS("Просто вопрос", {"domain": "factual"}, 0.59, 2, "UNVERIFIED"), RS("СОЗНАНИЕ и ЖИЗНЬ", {"domain": 5}, 0.1, 0, "UNVERIFIED"), RS("в" * 100, {}, 0.3, 2),
        RS("Запрос", None, 0.3, 4, "SUPPORTED"), RS("Запрос", {"testability": "interpretive"}, 0.95, 3, "VALUE_FRAMEWORK"), ("analyze_response", {"query": "q", "epistemic": [1], "confidence": 0.5, "evidence_count": 1, "trust": "X"}),
    ] + READ)
    scenario("S3 очередь вопросов и приоритеты", [
        I, RS("Сознание?", {}, 0.4, 1, "UNVERIFIED"), ("get_next_question", {}), ("get_next_question", {}), ("get_summary", {}),
        RS("Сознание?", {}, 0.4, 1, "UNVERIFIED"), B("т", "Утверждение А", 0.4), ("analyze_beliefs", {}), ("get_next_question", {}), ("get_next_question", {}), ("get_next_question", {}), ("get_next_question", {}),
        ("get_next_question", {}), ("get_next_question", {}), ("get_next_question", {}), ("get_next_question", {}), ("get_next_question", {}), ("get_next_question", {}),
    ] + READ + [("get_by_topic", {"topic": "philosophy_of_mind"}), ("get_by_topic", {"topic": "нет"})])
    scenario("S4 решённые и топ-5", [I] + [RS(f"Тема {k}", {"domain": f"d{k}"}, 0.1 + 0.05 * k, 1) for k in range(6)] + READ + [("get_next_question", {}), ("mark_resolved", {"unknown_id": "unk_00000000"}), ("mark_resolved", {"unknown_id": "нет"}), ("get_summary", {})] + READ)
    scenario("S5 повторы поднимают приоритет", [I, B("т", "Утверждение Б", 0.7), ("analyze_beliefs", {}), ("t", 100), ("bm:add", {"topic": "т", "statement": "Утверждение Б", "confidence": 0.2, "evidence_against": ["x1", "x2"]}), ("analyze_beliefs", {}), ("analyze_beliefs", {})] + READ)

    rnd = random.Random(20260928)
    for k in range(40):
        steps = [I]
        for _ in range(rnd.randint(4, 24)):
            op = rnd.choice(["b", "b", "ab", "ar", "ar", "n", "r", "rd", "t"])
            if op == "b":
                steps.append(B(rnd.choice(["а", "б"]), rnd.choice(["У1", "У2", "У3", "У4", "у" * 60]), rnd.choice([0.1, 0.4, 0.59, 0.6, 0.7, 0.79, 0.8, 0.95])))
            elif op == "ab":
                steps.append(("analyze_beliefs", {}))
            elif op == "ar":
                steps.append(RS(rnd.choice(["Сознание", "Смысл жизни", "Обычный", "в" * 70]), rnd.choice([{}, {"domain": "d"}, {"testability": "interpretive", "domain": "x"}]),
                                rnd.choice([0.1, 0.59, 0.6, 0.9]), rnd.choice([0, 2, 3, 6]), rnd.choice(["SUPPORTED", "UNVERIFIED", "PARTIALLY_SUPPORTED"])))
            elif op == "n":
                steps.append(("get_next_question", {}))
            elif op == "r":
                steps.append(("mark_resolved", {"unknown_id": f"unk_{rnd.randint(0, 9):08x}"}))
            elif op == "rd":
                steps.append(rnd.choice(READ))
            else:
                steps.append(("t", rnd.choice([1, 60, 86400 * 3])))
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
