"""Рефлексивный цикл на Rust (rustlib/yandi_core/src/reflection_loop.rs) против agent/reflection_loop.py на Python и НАСТОЯЩЕМ MySQL.
Те же вызовы с управляемыми часами и идентификаторами; сравниваются результат каждого вызова и содержимое таблиц reflection_policy / episode / self_state / self_event
(порядок расходования идентификаторов тоже должен совпасть). Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ."""
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
    import agent.memory_episodic as me
    import agent.reflection_loop as rl
    import agent.self_model as sm
    from agent.db.sql.security_triggers import immutability_triggers

    os.environ["TZ"] = "UTC"
    import time as _time
    _time.tzset()
    host, port = target.rsplit(":", 1)
    kw = dict(host=host, port=int(port), user="root", password="", cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")
    admin = pymysql.connect(autocommit=True, **kw)
    ac = admin.cursor()
    DB = "yandi_refl_parity"
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
    tables = ["reflection_policy", "episode", "self_state", "self_event"]

    clock = {"t": 1_770_000_000.0}
    idq: list[str] = []
    state = {"h": None, "rl_state": None}
    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    fake_uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=idq.pop(0)))
    for mod in (me, sm, rl):
        mod.uuid = fake_uuid
    rl.time = types.SimpleNamespace(time=lambda: clock["t"])

    @contextlib.contextmanager
    def fake_get_connection(autocommit=False):
        c = pymysql.connect(database=DB, autocommit=autocommit, **kw)
        try:
            yield c
        finally:
            c.close()

    for mod in (me, sm, rl):
        mod.get_connection = fake_get_connection

    loop = {"m": None}

    def reset(t0=1_770_000_000.0):
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            cur.execute(f"TRUNCATE TABLE {t}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if state["h"] is not None:
            yandi_core.close(state["h"])
        state["h"] = yandi_core.open_memory()
        state["rl_state"] = None
        clock["t"] = t0
        idq.clear()
        sm._self_model = None
        me._memory = None
        rl._reflection = None

    def dump_py():
        out = {}
        for t in tables:
            cur.execute(f"SELECT * FROM {t} ORDER BY 1")
            out[t] = [norm(r) for r in cur.fetchall()]
        return out

    def dump_rs():
        return {t: json.loads(yandi_core.query(state["h"], f"SELECT * FROM {t} ORDER BY 1")) for t in tables}

    def py_call(name, a):
        try:
            if name == "init":
                sm._self_model = None
                me._memory = None
                loop["m"] = rl.ReflectionLoop()
                return {"ok": None}
            m = loop["m"]
            if name == "reflect_on_query":
                res = m.reflect_on_query(
                    a["query"], "ответ", a["epistemic"], a["trust"], a["confidence"], errors=a.get("errors"),
                    validation_result=a.get("validation_result"), context=a.get("context"))
                d = norm(dataclasses.asdict(res))
                d["self_state_after"].pop("last_event", None)  # события одной секунды: какое «последнее» — в оригинале не определено
                return {"ok": d}
            if name == "get_policies":
                return {"ok": norm(m.get_policies())}
            if name == "get_summary":
                return {"ok": norm(m.get_summary())}
            if name == "summary_text":
                return {"ok": m.summary_text()}
            raise ValueError(name)
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], list(idq))
        payload = {"method": name, "args": a, "state": None if name == "init" else state["rl_state"]}
        r = json.loads(yandi_core.call(state["h"], "rl_call", json.dumps(payload, ensure_ascii=False)))
        if "ok" in r:
            state["rl_state"] = r["ok"]["state"]
            out = r["ok"]["out"]
            if name == "reflect_on_query" and isinstance(out, dict):
                out["self_state_after"].pop("last_event", None)
            return {"ok": out}
        return {"error": str(r["error"]).split(":")[0]}

    n = 0

    def scenario(label, steps, ids=200):
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
            keydiff = ""
            if not ok and "ok" in p and "ok" in r and isinstance(p["ok"], dict) and isinstance(r["ok"], dict):
                keydiff = "; отличаются поля: " + ", ".join(f"{k}: py={json.dumps(p['ok'].get(k), ensure_ascii=False)[:160]} rs={json.dumps(r['ok'].get(k), ensure_ascii=False)[:160]}" for k in p["ok"] if canon(p["ok"].get(k)) != canon(r["ok"].get(k)))
            check(f"{label} · шаг {i} {name}", ok, f"{keydiff}\n args={json.dumps(args, ensure_ascii=False)[:500]}\n py={json.dumps(p, ensure_ascii=False)[:900]}\n rs={json.dumps(r, ensure_ascii=False)[:900]}")
            if not ok:
                return
        dp, dr = dump_py(), dump_rs()
        for t in tables:
            same = canon(dp[t]) == canon(dr[t])
            diff = [(a, b) for a, b in zip(dp[t], dr[t]) if canon(a) != canon(b)]
            check(f"{label} · таблица {t}", same, f"\n строк py={len(dp[t])} rs={len(dr[t])}; первое отличие: {diff[:1]}")

    I = ("init", {})

    def R(query="Что такое сознание?", epistemic=None, trust="VALUE_FRAMEWORK", confidence=0.4, **kw2):
        return ("reflect_on_query", {"query": query, "epistemic": epistemic if epistemic is not None else {}, "trust": trust, "confidence": confidence, **kw2})

    PHIL = {"domain": "philosophical", "testability": "interpretive", "answer_mode": "pluralistic_contextual", "should_use_web": True, "reason": "интерпретативный вопрос", "evidence_count": 0}
    FACT = {"domain": "factual", "testability": "fully_testable", "answer_mode": "factual", "should_use_web": False, "confidence": 0.3}

    scenario("S1 политика: наблюдается, потом применяется на третьем повторении", [
        I, R(epistemic=PHIL), ("get_policies", {}), R(epistemic=PHIL), ("get_policies", {}), R(epistemic=PHIL), ("get_policies", {}), R(epistemic=PHIL), ("get_policies", {}),
        ("get_summary", {}), ("summary_text", {}),
    ])
    scenario("S2 разные ошибки и политики", [
        I, R(epistemic=FACT, confidence=0.3, trust="UNVERIFIED"), R(epistemic=FACT, confidence=0.3, trust="UNVERIFIED"), R(epistemic=FACT, confidence=0.3, trust="UNVERIFIED"),
        R(epistemic={"domain": "factual", "should_use_web": False}, confidence=0.9), R(epistemic={"domain": "philosophical", "should_use_web": 1}, confidence=0.9),
        R(epistemic={"testability": "interpretive", "evidence_count": 1, "need_clarification": True}, confidence=0.9),
        R(epistemic={"testability": "interpretive", "evidence_count": 2}, confidence=0.9), R(epistemic={"testability": "interpretive", "evidence_count": 1.5}, confidence=0.4),
        R(epistemic={}, errors=["Найдена ошибка про web"], confidence=0.6), R(epistemic={"domain": "interpretive"}, errors=["ошибка: слишком низкая уверенность", "второй интерпретативный сбой web"], confidence=0.6),
        R(epistemic={"domain": "philosophical"}, errors=["ошибка про интерпретативный вопрос"], confidence=0.6), R(epistemic={}, errors=[], confidence=0.49), R(epistemic={}, errors=None, confidence=0.5),
        ("get_policies", {}), ("get_summary", {}), ("summary_text", {}),
    ])
    vr = lambda **d: {"performed": True, **d}  # noqa: E731
    scenario("S3 уроки о валидации", [
        I, R(validation_result=vr(accepted=3, rejected=0, total=3)), R(validation_result=vr(accepted=2, rejected=1, total=3)), R(validation_result=vr(accepted=0, rejected=0, total=3)),
        R(validation_result=vr(accepted=1, rejected=0, total=3)), R(validation_result=vr(total=0)), R(validation_result=vr()), R(validation_result={"performed": False, "total": 5}),
        R(validation_result={}), R(validation_result=None), R(validation_result=vr(accepted=2.0, rejected=0, total=2.0)), R(validation_result=vr(accepted="2", rejected=0, total=2)),
        R(validation_result=vr(accepted=True, rejected=False, total=1)), R(validation_result={"performed": 1, "accepted": 5, "rejected": 2, "total": 8}),
        R(context={"entity_not_found": True}), R(context={"entity_not_found": False}), R(context={}), R(context={"x": 1}, epistemic=PHIL), R(context={"entity_not_found": "да"}, errors=["web"], epistemic=PHIL),
        R(epistemic=FACT, confidence=0.1, trust="UNVERIFIED", errors=["интерпретативный web уверенность"], context={"entity_not_found": 1}),
    ])
    scenario("S4 разбор `epistemic` и типов", [
        I, R(epistemic={"domain": "philosophical", "testability": "fully_testable", "answer_mode": None, "reason": 5}), R(epistemic={"reason": ["a"], "testability": "interpretive"}),
        R(epistemic={"reason": "", "testability": "fully_testable", "domain": "d"}), R(epistemic={"domain": 5, "testability": 7, "answer_mode": True}),
        R(epistemic={"confidence": None, "testability": "fully_testable"}), R(epistemic={"confidence": "x", "testability": "fully_testable"}), R(epistemic={"confidence": True, "testability": "fully_testable"}),
        R(epistemic={"evidence_count": None, "testability": "interpretive"}), R(epistemic={"evidence_count": False, "testability": "interpretive"}),
        R(epistemic={"domain": "philosophical", "should_use_web": "yes"}), R(epistemic={"domain": "philosophical", "should_use_web": None}), R(epistemic={"domain": "factual", "should_use_web": None}),
        R(epistemic=[1]), R(epistemic="строка"), R(epistemic=None), R(query="я" * 100, epistemic=PHIL, errors=["web " * 30]),
    ])
    scenario("S4b порог доверия к проверяемым вопросам", [I] + [R(epistemic={"testability": "fully_testable", "confidence": c}, confidence=0.9) for c in (0.59, 0.6, 0.61, 0.0, 1)])
    # 25 рефлексий подряд: окно «последние 20» и «последние 3»
    many = [I] + [R(query=f"Вопрос {k}", epistemic=random.Random(k).choice([PHIL, FACT, {}]), confidence=random.Random(k).choice([0.2, 0.55, 0.9]), trust=random.Random(k).choice(["UNVERIFIED", "SUPPORTED"])) for k in range(25)]
    scenario("S5 окно сводки", many + [("get_summary", {}), ("summary_text", {}), ("get_policies", {})])
    scenario("S6 пустой цикл", [I, ("get_summary", {}), ("summary_text", {}), ("get_policies", {})])

    rnd = random.Random(20260928)
    eps = [PHIL, FACT, {}, {"domain": "factual", "should_use_web": False, "testability": "fully_testable", "confidence": 0.55}, {"testability": "interpretive", "evidence_count": 0, "need_clarification": 1},
           {"domain": "interpretive", "should_use_web": True}]
    for k in range(40):
        steps = [I]
        for _ in range(rnd.randint(3, 12)):
            op = rnd.choice(["r", "r", "r", "sum", "txt", "pol"])
            if op == "r":
                kws = {}
                if rnd.random() < 0.3:
                    kws["errors"] = rnd.choice([[], ["web"], ["уверенность"], ["интерпретативный сбой", "web"]])
                if rnd.random() < 0.3:
                    kws["validation_result"] = rnd.choice([None, {}, vr(accepted=1, rejected=0, total=1), vr(accepted=0, rejected=2, total=2), vr(total=0)])
                if rnd.random() < 0.3:
                    kws["context"] = rnd.choice([None, {}, {"entity_not_found": True}])
                steps.append(R(query=rnd.choice(["Вопрос", "в" * 80]), epistemic=rnd.choice(eps), trust=rnd.choice(["UNVERIFIED", "SUPPORTED"]), confidence=rnd.choice([0.1, 0.29, 0.3, 0.49, 0.5, 0.95]), **kws))
            elif op == "sum":
                steps.append(("get_summary", {}))
            elif op == "txt":
                steps.append(("summary_text", {}))
            else:
                steps.append(("get_policies", {}))
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
