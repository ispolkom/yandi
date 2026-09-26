"""Внутреннее состояние личности на Rust (rustlib/yandi_core/src/inner_state.rs) против agent/inner_state.py на Python и НАСТОЯЩЕМ MySQL.
Те же события с управляемыми часами; сравниваются результат каждого вызова и содержимое таблиц inner_state / inner_state_event. Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ."""
from __future__ import annotations

import contextlib
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
    import agent.inner_state as ist
    from agent.db.sql.security_triggers import immutability_triggers

    os.environ["TZ"] = "UTC"
    import time as _time
    _time.tzset()
    host, port = target.rsplit(":", 1)
    kw = dict(host=host, port=int(port), user="root", password="", cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")
    admin = pymysql.connect(autocommit=True, **kw)
    ac = admin.cursor()
    DB = "yandi_inner_parity"
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
    tables = ["inner_state", "inner_state_event"]

    clock = {"t": 1_770_000_000.0}
    state = {"h": None}
    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    ist.time = types.SimpleNamespace(time=lambda: clock["t"])

    @contextlib.contextmanager
    def fake_get_connection(autocommit=False):
        c = pymysql.connect(database=DB, autocommit=autocommit, **kw)
        try:
            yield c
        finally:
            c.close()

    ist.get_connection = fake_get_connection
    managers: dict = {}

    def reset(t0=1_770_000_000.0):
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            cur.execute(f"TRUNCATE TABLE {t}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if state["h"] is not None:
            yandi_core.close(state["h"])
        state["h"] = yandi_core.open_memory()
        clock["t"] = t0
        managers.clear()

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
            uid = a["user_id"]
            if name == "init":
                managers[uid] = ist.InnerStateManager(uid)
                return {"ok": None}
            m = managers[uid]
            if name == "add_event":
                return {"ok": norm(m.add_event(a["event_type"], a["description"], sincerity=a["sincerity"]))}
            if name == "get_history":
                return {"ok": norm(m.get_history())}
            return {"ok": norm(getattr(m, name)())}
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], [])
        r = json.loads(yandi_core.call(state["h"], "is_" + name, json.dumps(a, ensure_ascii=False)))
        return {"ok": r["ok"]} if "ok" in r else {"error": str(r["error"]).split(":")[0]}

    n = 0

    def scenario(label, steps):
        nonlocal n
        n += 1
        reset()
        for i, st in enumerate(steps):
            if st[0] == "t":
                clock["t"] += st[1]
                continue
            name, args = st
            if name == "set":
                cur.execute(args["sql"])
                yandi_core.call(state["h"], "query_exec", json.dumps({"sql": args["sql"]}))
                continue
            p = py_call(name, args)
            r = rs_call(name, args)
            if "error" in p or "error" in r:
                ok = ("error" in p) == ("error" in r)
            else:
                ok = canon(p["ok"]) == canon(r["ok"])
            clock["t"] += 1
            check(f"{label} · шаг {i} {name}", ok, f"\n args={json.dumps(args, ensure_ascii=False)[:300]}\n py={json.dumps(p, ensure_ascii=False)[:800]}\n rs={json.dumps(r, ensure_ascii=False)[:800]}")
            if not ok:
                return
        dp, dr = dump_py(), dump_rs()
        for t in tables:
            same = canon(dp[t]) == canon(dr[t])
            diff = [(a, b) for a, b in zip(dp[t], dr[t]) if canon(a) != canon(b)]
            check(f"{label} · таблица {t}", same, f"\n строк py={len(dp[t])} rs={len(dr[t])}; первое отличие: {diff[:1]}")

    U = "anonymous"
    I = ("init", {"user_id": U})
    E = lambda et, sinc=0.5, uid=U: ("add_event", {"user_id": uid, "event_type": et, "description": f"описание {et}", "sincerity": sinc})  # noqa: E731
    G = lambda uid=U: [("get_summary", {"user_id": uid}), ("get_response_context", {"user_id": uid}), ("get_inner_monologue", {"user_id": uid}), ("get_history", {"user_id": uid})]  # noqa: E731
    types_ = ["severe_insult", "moderate_insult", "mild_insult", "insult", "sincere_apology", "formal_apology", "thanks", "help", "constructive_criticism", "honesty", "dishonesty", "provocation", "respect", "disrespect", "нечто"]

    scenario("S1 начальное состояние", [I] + G())
    scenario("S2 каждый тип события", [I] + [s for et in types_ for s in (E(et, 0.5), ("get_summary", {"user_id": U}))] + G())
    scenario("S3 искренность и границы", [I] + [E(et, sinc) for et in ("sincere_apology", "moderate_insult", "thanks", "нечто") for sinc in (0.0, 0.1, 0.5, 0.9, 1.0, 5.0, -1.0)] + G())
    scenario("S4 оскорбления подряд и шаблоны", [I] + [E("moderate_insult", 0.1)] * 6 + [E("sincere_apology", 0.9)] * 3 + G() + [E("thanks", 1.0)] * 5 + G() + [E("severe_insult", 0.0)] * 4 + G())
    scenario("S5 агрессия, восстановление, благодарность", [I, E("mild_insult"), E("mild_insult"), E("insult"), ("get_summary", {"user_id": U}), E("sincere_apology", 0.8), E("sincere_apology", 0.8), E("sincere_apology", 0.8)] + G()
             + [E("thanks"), E("thanks"), E("thanks"), E("thanks"), E("thanks")] + G())
    scenario("S6 время: прощение растёт со временем", [I, E("moderate_insult", 0.1), ("t", 86400 * 3), E("нечто"), ("t", 86400 * 0.5), E("нечто"), ("t", 86400 * 1.01), E("нечто"), ("t", 86400 * 30), E("thanks"), ("t", 86400 * 2), E("sincere_apology", 0.7)] + G())
    scenario("S7 два человека", [I, ("init", {"user_id": "u2"}), E("thanks", 1.0), E("severe_insult", 0.0, "u2"), E("help", 0.5, "u2"), E("moderate_insult", 0.3)] + G() + G("u2"))
    scenario("S8 много событий (окно 200)", [I] + [E(types_[k % len(types_)], 0.3 + (k % 7) / 10) for k in range(230)] + [("get_summary", {"user_id": U}), ("get_history", {"user_id": U}), ("get_inner_monologue", {"user_id": U})])

    # граничные значения порогов: состояние выставляется напрямую одинаково в обеих базах
    SET = lambda **kv: ("set", {"sql": "UPDATE inner_state SET " + ", ".join(f"{k}={v}" for k, v in kv.items())})  # noqa: E731
    for tr in (29.9, 30, 30.1, 40, 60, 70):
        for en in (30, 60):
            for af in (50, 51):
                for et in ("нечто", "help", "constructive_criticism", "moderate_insult", "thanks", "sincere_apology"):
                    scenario(f"B доверие={tr} энергия={en} привязанность={af} {et}", [I, SET(trust=tr, energy=en, affection=af, mood="'calm'"), E(et, 0.5), ("get_summary", {"user_id": U}), ("get_inner_monologue", {"user_id": U})] + G())
    for tr in (30, 29.99, 30.01, 40, 40.01, 39.99):
        scenario(f"B2 порог доверия {tr}", [I, SET(trust=tr), E("constructive_criticism"), E("insult"), E("moderate_insult", 0.3), E("нечто")] + G())
    for tr, et, sc in ((60, "нечто", 0.5), (60.01, "нечто", 0.5), (59.99, "нечто", 0.5), (38.5, "constructive_criticism", 1.0), (38.4, "constructive_criticism", 1.0), (38.6, "constructive_criticism", 1.0), (35, "mild_insult", 1.0), (34.9, "mild_insult", 1.0), (35.1, "mild_insult", 1.0)):
        scenario(f"B4 точный порог после события {tr} {et}", [I, SET(trust=tr, energy=80 if et == "нечто" else 35, curiosity=50, forgiveness=50, affection=50, mood="'calm'"), E(et, sc)] + G())
    for gap in (86399, 86400 - 1e-3, 86401):
        scenario(f"B3 ровно сутки {gap}", [I, E("thanks"), ("t", gap), E("нечто"), E("нечто")] + G())

    rnd = random.Random(20260928)
    for k in range(40):
        steps = [I]
        for _ in range(rnd.randint(4, 30)):
            op = rnd.choice(["e", "e", "e", "e", "s", "m", "t"])
            if op == "e":
                steps.append(E(rnd.choice(types_), rnd.choice([0.0, 0.2, 0.5, 0.8, 1.0, 1.5])))
            elif op == "s":
                steps.append(("get_summary", {"user_id": U}))
            elif op == "m":
                steps.append(("get_inner_monologue", {"user_id": U}))
            else:
                steps.append(("t", rnd.choice([60, 86400, 86400 * 5])))
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
