"""Убеждения на Rust (rustlib/yandi_core/src/belief_manager.rs) против agent/belief_manager.py на Python и НАСТОЯЩЕМ MySQL.
Одни и те же вызовы с управляемыми часами и идентификаторами; «вложения» и «судья» заменены сценарием (одинаковым для обеих сторон): сравниваются результат каждого вызова, все обращения к
вложениям и судье (тексты подсказок) и содержимое таблиц belief / belief_assessment_history. Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ."""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import math
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
    import decimal
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

    import agent.belief_manager as bm
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
    DB = "yandi_belief_parity"
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
    state = {"h": None}
    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    bm.time = types.SimpleNamespace(time=lambda: clock["t"])
    bm.uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=idq.pop(0)))

    @contextlib.contextmanager
    def fake_get_connection(autocommit=False):
        c = pymysql.connect(database=DB, autocommit=autocommit, **kw)
        try:
            yield c
        finally:
            c.close()

    bm.get_connection = fake_get_connection

    # ---- подмены шлюза ----
    embed_state = {"map": None, "calls": []}
    judge_state = {"queue": [], "prompts": []}

    def fake_embed(texts, model=None, **_):
        embed_state["calls"].append(list(texts))
        if embed_state["map"] is None:
            raise RuntimeError("вложения недоступны")
        return types.SimpleNamespace(vectors=[embed_state["map"].get(t, [1.0, 0.0, 0.0]) for t in texts])

    def fake_complete(prompt, **_):
        judge_state["prompts"].append(prompt)
        r = judge_state["queue"].pop(0) if judge_state["queue"] else ""
        if isinstance(r, str) and r.startswith("__raise__:"):
            raise type(r.split(":", 1)[1], (Exception,), {})()
        return r

    llm_gateway.embed = fake_embed
    llm_gateway.complete = fake_complete

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

    def dump_py():
        out = {}
        for t in tables:
            cur.execute(f"SELECT * FROM {t} ORDER BY 1")
            out[t] = [norm(r) for r in cur.fetchall()]
        return out

    def dump_rs():
        return {t: json.loads(yandi_core.query(state["h"], f"SELECT * FROM {t} ORDER BY 1")) for t in tables}

    manager = {"m": None}

    def conv(v):
        if dataclasses.is_dataclass(v):
            return dataclasses.asdict(v)
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v

    def py_call(name, a):
        embed_state["map"] = a.get("embed_map")
        embed_state["calls"] = []
        judge_state["queue"] = list(a.get("judge_responses") or [])
        judge_state["prompts"] = []
        try:
            m = manager["m"]
            if name == "apply_decay":
                m._apply_decay()
                out = None
            elif name == "add_belief":
                out = m.add_belief(a["topic"], a["statement"], a["confidence"], evidence_for=a.get("evidence_for"), evidence_against=a.get("evidence_against"), claim_ids=a.get("claim_ids"), prior=a.get("prior", 0.5))
            elif name == "challenge_belief":
                out = m.challenge_belief(a["belief_id"], a["counter_evidence"], a["new_confidence"], a["reason"])
            elif name == "supersede_belief":
                out = m.supersede_belief(a["old_belief_id"], a["new_belief_id"])
            elif name == "get_beliefs_by_topic":
                out = m.get_beliefs_by_topic(a["topic"])
            elif name == "get_belief":
                out = m.get_belief(a["belief_id"])
            elif name == "get_belief_history":
                out = m.get_belief_history(a["belief_id"])
            elif name == "get_contradictory":
                out = m.get_contradictory(a.get("min_score", 0.5))
            elif name in ("get_all_active", "get_all", "get_stats", "summary"):
                out = getattr(m, name)()
            else:
                raise ValueError(name)
            return {"ok": {"out": norm(conv(out)), "embed_calls": embed_state["calls"], "prompts": judge_state["prompts"]}}
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], list(idq))
        r = json.loads(yandi_core.call(state["h"], "bm_" + name, json.dumps(a, ensure_ascii=False)))
        return {"ok": r["ok"]} if "ok" in r else {"error": str(r["error"]).split(":")[0]}

    n = 0

    def scenario(label, steps, ids=40):
        nonlocal n
        n += 1
        reset()
        idq.extend(f"{i:08x}{i * 7919 % 65536:04x}" for i in range(ids))
        manager["m"] = bm.BeliefManager()
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
            check(f"{label} · шаг {i} {name}", ok, f"\n args={json.dumps(args, ensure_ascii=False)[:400]}\n py={json.dumps(p, ensure_ascii=False)[:900]}\n rs={json.dumps(r, ensure_ascii=False)[:900]}")
            if not ok:
                return
        dp, dr = dump_py(), dump_rs()
        for t in tables:
            same = canon(dp[t]) == canon(dr[t])
            diff = [(a, b) for a, b in zip(dp[t], dr[t]) if canon(a) != canon(b)]
            check(f"{label} · таблица {t}", same, f"\n строк py={len(dp[t])} rs={len(dr[t])}; первое отличие: {diff[:1]}")

    def add(topic, statement, conf, **kw2):
        return ("add_belief", {"topic": topic, "statement": statement, "confidence": conf, **kw2})

    scenario("S1 создание, точное совпадение, свидетельства", [
        add("тема", "Земля круглая", 0.8, evidence_for=["e1"], claim_ids=["c1"]),
        add("тема", "  ЗЕМЛЯ   круглая ", 0.7, evidence_for=["e2", "e3", "", "e2"]),
        add("тема", "Земля круглая", 0.6, evidence_against=["x1", "e1", "x1"]),
        add("тема", "Земля круглая", 0.9, evidence_against=["x2", "x3", "x4"]),
        add("тема", "Земля круглая", 0.05, evidence_for=["p1"]), add("тема", "Земля круглая", 0.99, evidence_against=["q1"]),
        add("тема", "Земля круглая", 0.5, evidence_for=["Ё1"], evidence_against=["Ё2"]),
        add("тема", "Земля круглая", 0.6, evidence_for=["x1", "x9"]),
        ("get_all_active", {}), ("get_all", {}), ("get_stats", {}), ("summary", {}), ("get_contradictory", {}), ("get_contradictory", {"min_score": 0.9}),
        ("get_beliefs_by_topic", {"topic": "тема"}), ("get_beliefs_by_topic", {"topic": "нет"}),
        ("t", 100), add("тема", "", 0.5), add("иная", "Совсем другое", 0.3, prior=0.2), add("иная", "Совсем другое", 0.3, prior=0.2),
    ])
    # идентификаторы известны заранее: idq формируется как f"{i:08x}{i*7919%65536:04x}", берутся первые 8 знаков
    bid = lambda i: f"bel_{i:08x}"  # noqa: E731
    scenario("S2b оспаривание", [
        add("т", "А", 0.8), add("т", "Б", 0.4), ("t", 50),
        ("challenge_belief", {"belief_id": bid(0), "counter_evidence": "против-1", "new_confidence": 0.9, "reason": "довод"}),
        ("challenge_belief", {"belief_id": bid(0), "counter_evidence": "против-1", "new_confidence": 0.9, "reason": "повтор"}),
        ("challenge_belief", {"belief_id": bid(0), "counter_evidence": "против-2", "new_confidence": 0.95, "reason": "ещё"}),
        ("challenge_belief", {"belief_id": bid(0), "counter_evidence": "против-3", "new_confidence": 0.95, "reason": "и ещё"}),
        ("challenge_belief", {"belief_id": bid(1), "counter_evidence": "", "new_confidence": 0.5, "reason": "пустое"}),
        ("challenge_belief", {"belief_id": bid(1), "counter_evidence": "сильный", "new_confidence": 0.0, "reason": "минимум"}),
        ("challenge_belief", {"belief_id": bid(1), "counter_evidence": "сильнее", "new_confidence": 5, "reason": "максимум"}),
        ("challenge_belief", {"belief_id": "нет", "counter_evidence": "x", "new_confidence": 0.5, "reason": "r"}),
        ("get_belief", {"belief_id": bid(0)}), ("get_belief", {"belief_id": "нет"}), ("get_belief_history", {"belief_id": bid(0)}), ("get_belief_history", {"belief_id": bid(1)}),
        ("supersede_belief", {"old_belief_id": bid(0), "new_belief_id": bid(1)}), ("supersede_belief", {"old_belief_id": bid(0), "new_belief_id": "нет"}), ("supersede_belief", {"old_belief_id": "нет", "new_belief_id": bid(1)}),
        ("get_all_active", {}), ("get_all", {}), ("get_stats", {}), ("summary", {}), ("get_belief_history", {"belief_id": bid(0)}),
    ])

    # ---- похожие по смыслу: вложения и судья ----
    def unit(x):
        return [x, math.sqrt(max(0.0, 1 - x * x)), 0.0]

    E = {"Новое утверждение": unit(1.0), "Старое утверждение": unit(0.9), "Третье утверждение": unit(0.75), "Далёкое утверждение": unit(0.3), "Чуть ниже": unit(0.69), "Чуть выше": unit(0.71)}
    eq = json.dumps({"relation": "equivalent"})
    scenario("S3 вложения и судья", [
        add("с", "Старое утверждение", 0.7), add("с", "Третье утверждение", 0.7), add("с", "Далёкое утверждение", 0.7), ("t", 10),
        add("с", "Новое утверждение", 0.8, embed_map=E, judge_responses=[eq]),
        add("с", "Новое утверждение", 0.8, embed_map=E, judge_responses=[json.dumps({"relation": "contradicts"}), json.dumps({"relation": " Equivalent "})]),
        add("с", "Новое утверждение", 0.8, embed_map=E, judge_responses=["не json", eq]),
        add("с", "Новое утверждение", 0.8, embed_map=E, judge_responses=["__raise__:OSError", eq]),
        add("с", "Новое утверждение", 0.8, embed_map=E, judge_responses=["[1, 2]", '{"relation": ["equivalent"]}']),
        add("с", "Новое утверждение", 0.8, embed_map=E, judge_responses=["", "{}", '{"relation": null}']),
        add("с", "Новое утверждение", 0.8, embed_map=None, judge_responses=[eq]),
        add("с", "Чуть ниже", 0.8, embed_map=E, judge_responses=[eq, eq, eq]), add("с", "Чуть выше", 0.8, embed_map=E, judge_responses=[eq, eq, eq]),
        add("с", "Чуть выше 0.705", 0.8, embed_map={**E, "Чуть выше 0.705": [1.0, 0.0, 0.0], "Старое утверждение": unit(0.705)}, judge_responses=[eq, eq]),
        add("с", "Чуть ниже 0.695", 0.8, embed_map={**E, "Чуть ниже 0.695": [1.0, 0.0, 0.0], "Старое утверждение": unit(0.695), "Третье утверждение": unit(0.1), "Далёкое утверждение": unit(0.1)}, judge_responses=[eq, eq]),
        add("с", "Текст, которого нет в карте", 0.8, embed_map=E, judge_responses=[eq, eq]),
        add("с", "Нулевой вектор", 0.8, embed_map={"Нулевой вектор": [0, 0, 0]}, judge_responses=[eq]),
        add("с", "Вектор длиннее единицы", 0.8, embed_map={"Вектор длиннее единицы": [3, 4]}, judge_responses=[eq]),
        add("с", "у" * 2500, 0.5, embed_map=E, judge_responses=[]),
        ("get_all_active", {}), ("get_stats", {}), ("summary", {}),
    ])

    # ---- затухание ----
    ages = sorted([0.5, 1.0, 1.0001, 1.5, 2.25, 2.35, 3.0, 30.0, 365.0, 1000.0, 0.05, 7.05, 12.15], reverse=True)
    steps = []
    for i, age in enumerate(ages):
        steps.append(add("з", f"Убеждение {i}", 0.9 - 0.05 * (i % 10)))
        if i + 1 < len(ages):
            steps.append(("t", (age - ages[i + 1]) * 86400))
    steps += [("t", ages[-1] * 86400), ("apply_decay", {}), ("get_all_active", {})] + [("get_belief_history", {"belief_id": bid(i)}) for i in range(len(ages))]
    steps += [("t", 86400 * 40), ("apply_decay", {}), ("apply_decay", {}), ("get_all", {}), ("get_stats", {}), ("summary", {})]
    scenario("S4 затухание", steps)
    scenario("S4b затухание и границы возраста", [
        add("з", "Один", 0.9), ("t", 86400), ("apply_decay", {}), ("get_belief_history", {"belief_id": bid(0)}), ("t", 1), ("apply_decay", {}), ("get_belief_history", {"belief_id": bid(0)}),
        add("з", "Два", 0.5), ("t", 86400 * 0.05), ("apply_decay", {}), ("t", 86400 * 2), ("apply_decay", {}), ("get_all", {}),
    ])

    # ---- случайные сценарии ----
    rnd = random.Random(20260927)
    topics = ["а", "б"]
    texts = ["Земля круглая", "Небо голубое", "Вода мокрая", "Огонь горячий", "Земля  КРУГЛАЯ", "небо голубое "]
    E2 = {t: unit(rnd.choice([0.2, 0.5, 0.72, 0.8, 0.95])) for t in texts}
    for k in range(40):
        steps = []
        made = 0
        for _ in range(rnd.randint(5, 22)):
            op = rnd.choice(["add", "add", "add", "chal", "sup", "decay", "get", "t"])
            if op == "add":
                steps.append(add(rnd.choice(topics), rnd.choice(texts), rnd.choice([0.05, 0.3, 0.6, 0.95, 1]), evidence_for=rnd.choice([[], ["e1"], ["e1", "e2"]]), evidence_against=rnd.choice([[], ["x1"], ["x1", "x2"]]),
                                 embed_map=rnd.choice([None, E2]), judge_responses=rnd.choice([[], [eq], [json.dumps({"relation": "different"}), eq], ["bad"]])))
                made += 1
            elif op == "chal":
                steps.append(("challenge_belief", {"belief_id": bid(rnd.randint(0, max(made, 1))), "counter_evidence": rnd.choice(["", "c1", "c2"]), "new_confidence": rnd.choice([0.0, 0.5, 0.95, 2]), "reason": "r"}))
            elif op == "sup":
                steps.append(("supersede_belief", {"old_belief_id": bid(rnd.randint(0, max(made, 1))), "new_belief_id": bid(rnd.randint(0, max(made, 1)))}))
            elif op == "decay":
                steps.append(("apply_decay", {}))
            elif op == "get":
                steps.append(rnd.choice([("get_all_active", {}), ("get_all", {}), ("get_stats", {}), ("summary", {}), ("get_contradictory", {"min_score": rnd.choice([0.0, 0.5, 1.0])}), ("get_beliefs_by_topic", {"topic": rnd.choice(topics)})]))
            else:
                steps.append(("t", rnd.choice([60, 3600, 86400, 86400 * 5, 86400 * 50])))
        scenario(f"R{k} случайный", steps, ids=60)

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
