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
    import agent.disagreement_engine as de_mod
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
    DB = "yandi_de_parity"
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
    tables = ["belief", "belief_assessment_history", "disagreement"]

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
                bm._inst = None
                de_mod._inst = None
                engine["e"] = de_mod.get_disagreement_engine()
                return {"ok": None}
            if name == "bm:add":
                engine["bm"] = bm.get_belief_manager()
                b = engine["bm"].add_belief(a["topic"], a["statement"], a["confidence"])
                return {"ok": norm(conv(b))}
            e = engine["e"]
            if name == "challenge":
                return {"ok": norm(e.challenge(**a))}
            if name == "summary":
                return {"ok": e.summary()}
            return {"ok": norm(getattr(e, name)(**a))}
        except Exception as ex:  # noqa: BLE001
            return {"error": type(ex).__name__}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], list(idq))
        if name == "bm:add":
            r = json.loads(yandi_core.call(state["h"], "bm_add_belief", json.dumps(a, ensure_ascii=False)))
            return {"ok": r["ok"]["out"]} if "ok" in r else {"error": str(r["error"]).split(":")[0]}
        r = json.loads(yandi_core.call(state["h"], "de_call", json.dumps({"method": name, **a}, ensure_ascii=False)))
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

    bids: list = []

    def sub(a):
        if isinstance(a, dict):
            return {k: sub(v) for k, v in a.items()}
        if isinstance(a, str) and a.startswith("$B"):
            return bids[int(a[2:])] if int(a[2:]) < len(bids) else "нет_такого"
        return a

    # scenario вызывает py_call/rs_call с готовыми аргументами; подстановку id убеждений делаем в обёртке
    _py, _rs = py_call, rs_call

    def py_call(name, a):  # noqa: F811
        a = sub(a)
        r = _py(name, a)
        if name == "bm:add" and "ok" in r:
            bids.append(r["ok"]["id"])
        return r

    def rs_call(name, a):  # noqa: F811
        return _rs(name, sub(a))

    _sc = scenario

    def scenario(label, steps, ids=300):  # noqa: F811
        bids.clear()
        _sc(label, steps, ids)

    I = ("init", {})
    B = lambda statement, conf: ("bm:add", {"topic": "т", "statement": statement, "confidence": conf})  # noqa: E731
    C = lambda topic="т", old="Старая позиция", ch="Контраргумент", an="Анализ спора", new="Новая позиция", cb=0.8, ca=0.55, rel=None: ("challenge", dict(topic=topic, old_position=old, challenge=ch, analysis=an, new_position=new, confidence_before=cb, confidence_after=ca, related_belief_id=rel))  # noqa: E731
    R = [("get_recent", {}), ("get_recent", {"limit": 1}), ("get_recent", {"limit": 3}), ("get_recent", {"limit": 0}), ("get_recent", {"limit": -1}), ("get_recent", {"limit": -2}), ("get_recent", {"limit": 100}), ("get_recent", {"limit": -100}),
         ("get_stats", {}), ("summary", {}), ("get_by_topic", {"topic": "т"}), ("get_by_topic", {"topic": "нет"})]

    scenario("S1 пусто", [I] + R)
    scenario("S2 без убеждения", [I, C(), C("ф", cb=0.5, ca=0.375), C("т", cb=0.3, ca=0.9), C("ф", cb=0.7, ca=0.7)] + R)
    scenario("S3 с убеждением", [I, B("Утверждение А", 0.9), B("Утверждение Б", 0.5), C(rel="$B0", ca=0.4), C(rel="$B0", ca=0.4), C(rel="$B0", ch="Другой довод", ca=0.1), C(rel="$B1", ca=0.99), C(rel="$B1", ch="", ca=0.5),
                                 C(rel="$B9"), C(rel=""), C(rel=None)] + R + [("get_recent", {"limit": 2})])
    scenario("S4 округление среднего", [I, C(cb=0.5, ca=0.375)] + R[8:10] + [C(cb=0.5, ca=0.4), C(cb=0.125, ca=0.5)] + R[8:10])
    scenario("S5 длинные тексты", [I, C(old="я" * 80, new="ю" * 80, an="а" * 200, topic="длинная тема " * 5)] + R)
    scenario("S6 время и порядок", [I, C("а"), ("t", 3600), C("б"), ("t", 86400 * 5), C("а"), C("в")] + R)
    rnd = random.Random(20260928)
    for k in range(40):
        steps = [I]
        for _ in range(rnd.randint(3, 20)):
            op = rnd.choice(["b", "c", "c", "c", "r", "t"])
            if op == "b":
                steps.append(B(rnd.choice(["У1", "У2", "У3"]), rnd.choice([0.2, 0.5, 0.9])))
            elif op == "c":
                steps.append(C(rnd.choice(["а", "б", "в"]), rnd.choice(["п1", "п2"]), rnd.choice(["д1", "д2", "д3"]), rnd.choice(["анализ", "а" * 60]), "н", rnd.choice([0.1, 0.5, 0.8, 0.95]), rnd.choice([0.05, 0.3, 0.55, 0.999]),
                               rnd.choice([None, None, "$B0", "$B1", "$B7"])))
            elif op == "r":
                steps.append(rnd.choice(R))
            else:
                steps.append(("t", rnd.choice([1, 3600, 86400 * 3])))
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
