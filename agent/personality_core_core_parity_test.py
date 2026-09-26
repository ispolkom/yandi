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
    import agent.personality_core as de_mod
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
    DB = "yandi_pc_parity"
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
    tables = ["personality", "personality_change"]

    clock = {"t": 1_770_000_000.0}
    idq: list[str] = []
    state = {"h": None}
    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    fake_uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=idq.pop(0)))
    fake_time = types.SimpleNamespace(time=lambda: clock["t"])
    for mod in (bm, de_mod):
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
    de_mod.get_connection = fake_get_connection

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
        clock["t"] = t0
        idq.clear()
        bm._inst = None
        de_mod._inst = None

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
                de_mod._inst = None
                engine["e"] = de_mod.get_personality_core()
                return {"ok": None}
            e = engine["e"]
            if name in ("add_trait", "add_goal", "add_principle", "add_limitation"):
                getattr(e, name)(a["item"])
                return {"ok": None}
            if name == "record_change":
                e.record_change(a["what_changed"], a["reason"])
                return {"ok": None}
            if name == "summary":
                return {"ok": e.summary()}
            return {"ok": norm(getattr(e, name)())}
        except Exception as ex:  # noqa: BLE001
            return {"error": type(ex).__name__}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], list(idq))
        r = json.loads(yandi_core.call(state["h"], "pc_call", json.dumps({"method": name, **a}, ensure_ascii=False)))
        return {"ok": r["ok"]} if "ok" in r else {"error": str(r["error"]).split(":")[0]}

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
    A = lambda kind, item: (f"add_{kind}", {"item": item})  # noqa: E731
    G = [("get_name", {}), ("get_traits", {}), ("get_goals", {}), ("get_principles", {}), ("get_summary", {}), ("summary", {})]

    scenario("S1 начальное", [I] + G)
    scenario("S2 добавления и повторы", [I, A("trait", "patient"), A("trait", "patient"), A("trait", "curious"), A("goal", "improve"), A("goal", "improve"), A("principle", "be kind"), A("limitation", "slow"), A("limitation", "slow")] + G)
    scenario("S3 счётчики и журнал", [I, ("increment_cycles", {}), ("increment_cycles", {}), ("increment_decisions", {}), ("increment_learnings", {}), ("record_change", {"what_changed": "a", "reason": "b"}), ("t", 5),
                                      ("record_change", {"what_changed": "в", "reason": ""}), ("record_change", {"what_changed": "", "reason": "г"})] + G)
    scenario("S4 повторный init не сбрасывает", [I, A("trait", "x"), ("increment_cycles", {}), ("record_change", {"what_changed": "a", "reason": "b"}), I] + G)
    scenario("S5 много целей — в сводке три", [I] + [A("goal", f"цель {k}") for k in range(6)] + [A("principle", f"п{k}") for k in range(5)] + [A("limitation", f"о{k}") for k in range(5)] + G)
    scenario("S6 значения разных видов", [I, A("trait", ""), A("trait", "русское слово"), A("trait", "Curious"), A("goal", "понять мир"), A("goal", "understand the world")] + G)
    rnd = random.Random(20260928)
    for k in range(40):
        steps = [I]
        for _ in range(rnd.randint(3, 22)):
            op = rnd.choice(["a", "a", "c", "r", "g", "t", "i"])
            if op == "a":
                steps.append(A(rnd.choice(["trait", "goal", "principle", "limitation"]), rnd.choice(["curious", "x", "y", "понять", "z" * 5, "understand the world", "never lie"])))
            elif op == "c":
                steps.append((rnd.choice(["increment_cycles", "increment_decisions", "increment_learnings"]), {}))
            elif op == "r":
                steps.append(("record_change", {"what_changed": rnd.choice(["a", "б"]), "reason": rnd.choice(["", "r"])}))
            elif op == "g":
                steps.append(rnd.choice(G))
            elif op == "i":
                steps.append(I)
            else:
                steps.append(("t", rnd.choice([1, 3600])))
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
