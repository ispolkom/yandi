"""Главный цикл жизни на Rust (rustlib/yandi_core/src/core_loop.rs) против agent/core_loop.py на Python и НАСТОЯЩЕМ MySQL.
Те же входы с управляемыми часами и идентификаторами; сравниваются результат каждого шага и содержимое таблиц episode / self_state / self_event / reflection_policy. Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ."""
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


def _round_floats(v):
    """MySQL разбирает/печатает double в JSON с точностью до последнего разряда не всегда так же, как Python и SQLite (0.9500000000000001 ↔ 0.95): сверяем до 12 значащих цифр."""
    if isinstance(v, float):
        return float(f"{v:.12g}")
    if isinstance(v, dict):
        return {k: _round_floats(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_round_floats(x) for x in v]
    return v


def norm(v):
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.dumps(_round_floats(json.loads(v)), ensure_ascii=False, sort_keys=True)
        except ValueError:
            return v
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


def scrub(v):
    """Убрать то, что в оригинале не определено: «последнее событие» секунды и текст исключения."""
    if isinstance(v, dict):
        return {k: scrub(x) for k, x in v.items() if k != "last_event"}
    if isinstance(v, list):
        return [scrub(x) for x in v]
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

    import agent.core_loop as cl
    import agent.db.sql.repositories as repo
    import agent.db.sql.schema as S
    import agent.memory_episodic as me
    import agent.motivation as mo
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
    DB = "yandi_core_loop_parity"
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
    tables = ["episode", "self_state", "self_event", "reflection_policy"]

    clock = {"t": 1_770_000_000.0}
    idq: list[str] = []
    state = {"h": None, "cl_state": None}
    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    fake_uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=idq.pop(0)))
    fake_time = types.SimpleNamespace(time=lambda: clock["t"])
    for mod in (me, sm, rl):
        mod.uuid = fake_uuid
    for mod in (cl, mo, rl):
        mod.time = fake_time

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
        state["cl_state"] = None
        clock["t"] = t0
        idq.clear()
        sm._self_model = None
        me._memory = None
        rl._reflection = None
        mo._motivation = None
        cl._core = None

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
        return v

    def py_call(name, a):
        try:
            if name == "init":
                sm._self_model = None
                me._memory = None
                rl._reflection = None
                mo._motivation = None
                loop["m"] = cl.CoreLoop()
                return {"ok": None}
            if name.startswith("sm:"):
                model = sm.get_self_model()
                if name == "sm:set_goals":
                    model.set_goals(a["goals"])
                elif name == "sm:add_uncertainty":
                    model.add_uncertainty(a["uncertainty"])
                elif name == "sm:add_limitation":
                    model.add_limitation(a["limitation"])
                else:
                    raise ValueError(name)
                return {"ok": None}
            c = loop["m"]
            if name == "run_cycle":
                out = c.run_cycle(a.get("input"))
                if out.get("status") == "failed":
                    out = {"status": "failed", "cycle": out["cycle"], "timestamp": out["timestamp"]}
                return {"ok": scrub(norm(out))}
            if name == "perceive":
                return {"ok": scrub(norm(c.perceive(a.get("input"))))}
            if name == "act":
                return {"ok": scrub(norm(c.act(a["action_type"], a["data"])))}
            if name == "remember":
                return {"ok": scrub(norm(c.remember(a["action_result"])))}
            if name == "reflect":
                # evaluate_goals-шаг не нужен: reflect(evaluation) не читает свой аргумент
                return {"ok": scrub(norm(c.reflect({})))}
            if name == "evaluate_goals":
                return {"ok": scrub(norm(c.evaluate_goals({})))}
            if name == "update_self_model":
                return {"ok": scrub(norm(c.update_self_model({})))}
            if name == "update_world_model":
                return {"ok": scrub(norm(c.update_world_model(a["perception"])))}
            if name == "get_status":
                st = c.get_status()
                return {"ok": scrub(norm(st))}
            if name == "summary_text":
                return {"ok": c.summary_text()}
            raise ValueError(name)
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], list(idq))
        if name.startswith("sm:"):
            r = json.loads(yandi_core.call(state["h"], "sm_" + name[3:], json.dumps(a, ensure_ascii=False)))
            return {"ok": None} if "ok" in r else {"error": str(r["error"]).split(":")[0]}
        payload = {"method": name, "args": a, "state": None if name == "init" else state["cl_state"]}
        r = json.loads(yandi_core.call(state["h"], "cl_call", json.dumps(payload, ensure_ascii=False)))
        if "ok" in r:
            state["cl_state"] = r["ok"]["state"]
            out = r["ok"]["out"]
            if name == "run_cycle" and isinstance(out, dict) and out.get("status") == "failed":
                out = {"status": "failed", "cycle": out["cycle"], "timestamp": out["timestamp"]}
            return {"ok": scrub(out)}
        return {"error": str(r["error"]).split(":")[0]}

    n = 0

    def scenario(label, steps, ids=400):
        nonlocal n
        only = os.environ.get("YANDI_ONLY")
        if only and not label.startswith(only):
            return
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
                keydiff = "; отличаются поля: " + ", ".join(f"{k}: py={json.dumps(p['ok'].get(k), ensure_ascii=False)[:200]} rs={json.dumps(r['ok'].get(k), ensure_ascii=False)[:200]}" for k in set(p["ok"]) | set(r["ok"]) if canon(p["ok"].get(k)) != canon(r["ok"].get(k)))
            check(f"{label} · шаг {i} {name}", ok, f"{keydiff}\n args={json.dumps(args, ensure_ascii=False)[:400]}\n py={json.dumps(p, ensure_ascii=False)[:600]}\n rs={json.dumps(r, ensure_ascii=False)[:600]}")
            if not ok:
                return
        dp, dr = dump_py(), dump_rs()
        for t in tables:
            same = canon(dp[t]) == canon(dr[t])
            diff = [(a, b) for a, b in zip(dp[t], dr[t]) if canon(a) != canon(b)]
            check(f"{label} · таблица {t}", same, f"\n строк py={len(dp[t])} rs={len(dr[t])}; первое отличие: {diff[:1] if not os.environ.get('YANDI_ONLY') else [(a.get('metadata'), b.get('metadata')) for a, b in diff[:1]]}")

    I = ("init", {})
    PHIL = {"domain": "philosophical", "testability": "interpretive", "answer_mode": "pluralistic_contextual", "trust": "VALUE_FRAMEWORK", "confidence": 0.4, "reason": "интерпретативный вопрос"}
    FACT = {"domain": "factual", "testability": "fully_testable", "answer_mode": "factual", "trust": "UNVERIFIED", "confidence": 0.2, "should_use_web": False}
    Q = lambda q, ep: ("run_cycle", {"input": {"query": q, "epistemic": ep}})  # noqa: E731

    scenario("C1 холостые циклы", [I, ("get_status", {}), ("summary_text", {}), ("run_cycle", {"input": None}), ("run_cycle", {"input": None}), ("run_cycle", {"input": None}), ("get_status", {}), ("summary_text", {})])
    scenario("C2 циклы с запросами", [
        I, Q("Что такое сознание?", PHIL), Q("Что такое сознание?", PHIL), Q("Как полететь на Марс?", FACT), Q("Как полететь на Марс?", FACT), ("run_cycle", {"input": {"query": "без эпистемики"}}),
        ("run_cycle", {"input": {"epistemic": PHIL}}), ("run_cycle", {"input": {}}), ("run_cycle", {"input": None}), Q("Ещё", {"confidence": 0.9}), Q("Ещё", {"confidence": 0.9}), Q("Другое", {}),
        ("get_status", {}), ("summary_text", {}),
    ])
    scenario("C3 шаги по отдельности", [
        I, ("perceive", {"input": {"query": "q", "epistemic": {"trust": "T"}}}), ("perceive", {"input": None}), ("perceive", {"input": {}}),
        ("update_world_model", {"perception": {"input": {"a": 1}}}), ("update_world_model", {"perception": {}}), ("update_self_model", {}), ("evaluate_goals", {}), ("evaluate_goals", {}),
        ("reflect", {}), ("reflect", {}),
        ("act", {"action_type": "explore", "data": {"confidence": 0.9, "uncertainty": 0.0}}), ("act", {"action_type": "explore", "data": {"confidence": 0.1, "uncertainty": 0.9}}),
        ("act", {"action_type": "explore", "data": {}}), ("act", {"action_type": "verify", "data": {"trust": "UNVERIFIED", "confidence": 0.2}}), ("act", {"action_type": "verify", "data": {"trust": "SUPPORTED", "confidence": 0.95}}),
        ("act", {"action_type": "verify", "data": {}}), ("act", {"action_type": "idle", "data": {"reason": "нет"}}), ("act", {"action_type": "query", "data": {"query": "q"}}),
        ("remember", {"action_result": {"type": "query", "data": {"query": "Вопрос?", "domain": "d", "answer_mode": "m", "trust": "t", "confidence": 0.7}, "status": "executed"}}),
        ("remember", {"action_result": {"type": "query", "data": {}, "status": "executed"}}),
        ("remember", {"action_result": {"type": "idle", "data": {"reason": "нет"}, "status": "executed"}}), ("remember", {"action_result": {"type": "custom", "status": "failed"}}),
        ("remember", {"action_result": {"status": "skipped"}}), ("remember", {"action_result": {"type": "query", "status": "skipped"}}),
        ("get_status", {}), ("summary_text", {}),
    ])
    scenario("C4 сбои внутри цикла", [
        I, ("run_cycle", {"input": "строка"}), ("run_cycle", {"input": {"query": "q", "epistemic": [1, 2]}}), ("run_cycle", {"input": {"query": "q", "epistemic": "x"}}), ("run_cycle", {"input": [1]}),
        ("run_cycle", {"input": {"query": "q2", "epistemic": {"domain": 5}}}), ("run_cycle", {"input": {"query": 5, "epistemic": {}}}), ("run_cycle", {"input": {"query": "q3", "epistemic": {"confidence": "x"}}}),
        ("run_cycle", {"input": None}), ("get_status", {}), ("summary_text", {}),
    ])

    scenario("C5 цели и неопределённости", [
        I, ("sm:set_goals", {"goals": ["одна"]}), ("evaluate_goals", {}), ("run_cycle", {"input": None}),
        ("sm:set_goals", {"goals": ["а", "б"]}), ("evaluate_goals", {}), ("sm:set_goals", {"goals": ["а", "б", "в"]}), ("evaluate_goals", {}), ("sm:set_goals", {"goals": []}), ("run_cycle", {"input": None}),
        ("sm:add_uncertainty", {"uncertainty": "н1"}), ("sm:add_uncertainty", {"uncertainty": "н2"}), ("sm:add_uncertainty", {"uncertainty": "н3"}), ("sm:add_uncertainty", {"uncertainty": "н4"}),
        ("update_world_model", {"perception": {"input": {"x": 1}}}), ("run_cycle", {"input": None}), ("get_status", {}), ("summary_text", {}),
    ])
    rnd = random.Random(20260928)
    eps = [PHIL, FACT, {}, {"confidence": 0.9, "trust": "SUPPORTED", "domain": "factual"}, {"confidence": 0.3, "testability": "fully_testable"}, {"domain": "interpretive", "should_use_web": True}]
    for k in range(40):
        steps = [I]
        for _ in range(rnd.randint(3, 14)):
            op = rnd.choice(["q", "q", "idle", "status", "text", "act", "rem", "t"])
            if op == "q":
                steps.append(Q(rnd.choice(["Вопрос А", "Вопрос Б", "в" * 70]), rnd.choice(eps)))
            elif op == "idle":
                steps.append(("run_cycle", {"input": None}))
            elif op == "status":
                steps.append(("get_status", {}))
            elif op == "text":
                steps.append(("summary_text", {}))
            elif op == "act":
                steps.append(("act", {"action_type": rnd.choice(["explore", "verify", "idle"]), "data": {"confidence": rnd.random(), "uncertainty": rnd.random(), "trust": rnd.choice(["UNVERIFIED", "SUPPORTED"])}}))
            elif op == "rem":
                steps.append(("remember", {"action_result": {"type": rnd.choice(["query", "idle", "x"]), "data": {"query": "Q?"}, "status": rnd.choice(["executed", "failed"])}}))
            else:
                steps.append(("t", rnd.choice([1, 60, 86400])))
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
