"""Граф личности на Rust (rustlib/yandi_core/src/personality_graph.rs) против agent/personality_graph.py на Python и НАСТОЯЩЕМ MySQL.
Те же вызовы с управляемыми часами; сравниваются результат каждого вызова и содержимое таблиц trait_graph / trait_change / trait_edge_change / internal_question / internal_question_answer.
Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ."""
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


def _round_floats(v):
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
    if isinstance(v, float):
        return 0.0 if v == 0.0 else float(f"{v:.12g}")
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
    import agent.personality_graph as pg
    from agent.db.sql.security_triggers import immutability_triggers

    os.environ["TZ"] = "UTC"
    import time as _time
    _time.tzset()
    host, port = target.rsplit(":", 1)
    kw = dict(host=host, port=int(port), user="root", password="", cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")
    admin = pymysql.connect(autocommit=True, **kw)
    ac = admin.cursor()
    DB = "yandi_pg_parity"
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
    tables = ["trait_graph", "trait_change", "trait_edge_change", "internal_question", "internal_question_answer"]

    clock = {"t": 1_770_000_000.0}
    state = {"h": None}
    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    pg.time = types.SimpleNamespace(time=lambda: clock["t"])

    @contextlib.contextmanager
    def fake_get_connection(autocommit=False):
        c = pymysql.connect(database=DB, autocommit=autocommit, **kw)
        try:
            yield c
        finally:
            c.close()

    pg.get_connection = fake_get_connection
    graph = {"g": None}

    def reset(t0=1_770_000_000.0):
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            cur.execute(f"TRUNCATE TABLE {t}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if state["h"] is not None:
            yandi_core.close(state["h"])
        state["h"] = yandi_core.open_memory()
        clock["t"] = t0

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
                graph["g"] = pg.PersonalityGraph()
                return {"ok": None}
            g = graph["g"]
            out = getattr(g, name.replace("_full", ""))(**a)
            if name == "get_evolution" and isinstance(out, dict):
                out = {k: v for k, v in out.items() if k != "recent_changes"}  # порядок изменений ОДНОЙ секунды в оригинале не определён
            return {"ok": norm(out)}
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], [])
        r = json.loads(yandi_core.call(state["h"], "pg_" + name.replace("_full", ""), json.dumps(a, ensure_ascii=False)))
        if "ok" in r and name == "get_evolution" and isinstance(r["ok"], dict):
            r["ok"] = {k: v for k, v in r["ok"].items() if k != "recent_changes"}
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
            p = py_call(name, args)
            r = rs_call(name, args)
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
            detail = diff[:1]
            if diff and t == "trait_graph":
                a_, b_ = diff[0]
                detail = {k: (a_[k], b_[k]) for k in a_ if canon(a_[k]) != canon(b_[k])}
            check(f"{label} · таблица {t}", same, f"\n строк py={len(dp[t])} rs={len(dr[t])}; первое отличие: {detail}")

    I = ("init", {})
    ALL = [("get_traits", {}), ("get_all_traits_data", {}), ("get_high_traits", {}), ("get_low_traits", {}), ("get_evolving_traits", {}), ("get_edges", {}), ("get_conflicts", {}), ("get_internal_questions", {}), ("get_evolution", {})]
    events = ["good_conversation", "bad_conversation", "deep_question", "insult", "apology", "self_reflection", "success", "failure", "нет_такого"]

    scenario("S1 начальный граф", [I] + ALL + [("get_trait_value", {"name": "curiosity"}), ("get_trait_value", {"name": "нет"}), ("get_trait_description", {"name": "honesty"}), ("get_trait_description", {"name": "нет"}),
                                              ("get_edge_weight", {"source": "curiosity", "target": "desire_to_understand"}), ("get_edge_weight", {"source": "curiosity", "target": "нет"}), ("get_edge_weight", {"source": "нет", "target": "x"}),
                                              ("get_edges_for_node", {"node": "caution"}), ("get_edges_for_node", {"node": "нет"}), ("get_high_traits", {"threshold": 0.5}), ("get_low_traits", {"threshold": 0.6}), I] + ALL)
    scenario("S2 события", [I] + [s for ev in events for s in (("reflect", {"event": ev, "intensity": 1.0}), ("t", 60), ("get_traits", {}))] + ALL + [("get_evolution", {"days": 1}), ("get_evolution", {"days": 0})])
    scenario("S3 качества и границы", [I, ("set_trait", {"name": "curiosity", "value": 1.5}), ("set_trait", {"name": "curiosity", "value": -1}), ("set_trait", {"name": "нет", "value": 0.5}), ("set_trait", {"name": "patience", "value": 0.3}),
                                        ("set_trait", {"name": "patience", "value": 0.7}), ("set_trait", {"name": "confidence", "value": 0.3000001}),
                                        ("change_trait", {"name": "honesty", "delta": 0.5}), ("change_trait", {"name": "honesty", "delta": -3}), ("change_trait", {"name": "нет", "delta": 1}),
                                        ("change_trait", {"name": "caution", "delta": 0.4, "source": "тест"}), ("change_trait", {"name": "respect", "delta": 0.001}), ("change_trait", {"name": "respect", "delta": 0.02, "source": "мелко"})] + ALL)
    scenario("S4 связи", [I, ("set_edge_weight", {"source": "curiosity", "target": "desire_to_understand", "weight": 5}), ("set_edge_weight", {"source": "caution", "target": "confidence", "weight": -5}),
                          ("set_edge_weight", {"source": "curiosity", "target": "нет", "weight": 0.5}), ("set_edge_weight", {"source": "нет", "target": "x", "weight": 0.5}),
                          ("learn_edge", {"source": "honesty", "target": "confidence", "success": True}), ("learn_edge", {"source": "honesty", "target": "confidence", "success": False}), ("learn_edge", {"source": "нет", "target": "x", "success": True}),
                          ("learn_edge", {"source": "caution", "target": "curiosity", "success": False})] + [("learn_edge", {"source": "honesty", "target": "desire_to_help", "success": True})] * 12 + [("get_edges", {})])
    scenario("S5 конфликты", [I, ("set_trait", {"name": "desire_to_help", "value": 0.9}), ("set_trait", {"name": "caution", "value": 0.2}), ("get_conflicts", {}), ("set_trait", {"name": "honesty", "value": 0.9}), ("get_conflicts", {}),
                              ("set_trait", {"name": "curiosity", "value": 0.8}), ("set_trait", {"name": "patience", "value": 0.3}), ("get_conflicts", {}), ("set_trait", {"name": "desire_to_help", "value": 0.7}), ("get_conflicts", {}),
                              ("set_trait", {"name": "caution", "value": 0.3}), ("get_conflicts", {})])
    scenario("S6 внутренние вопросы", [I, ("answer_internal_question", {"question_idx": 0, "answer": "Потому что разные"}), ("answer_internal_question", {"question_idx": 0, "answer": "Второй ответ"}), ("answer_internal_question", {"question_idx": 3, "answer": "Ответ"}),
                                        ("answer_internal_question", {"question_idx": 4, "answer": "нет"}), ("answer_internal_question", {"question_idx": -1, "answer": "нет"}), ("answer_internal_question", {"question_idx": 1, "answer": "я" * 300}), ("get_internal_questions", {}), I, ("get_internal_questions", {})])
    scenario("S7 эволюция во времени", [I, ("get_evolution", {}), ("change_trait", {"name": "curiosity", "delta": 0.3}), ("t", 86400 * 2), ("change_trait", {"name": "honesty", "delta": -0.4}), ("t", 86400 * 10), ("change_trait", {"name": "caution", "delta": 0.2}),
                                        ("get_evolution", {}), ("get_evolution", {"days": 3}), ("get_evolution", {"days": 30}), ("get_evolution", {"days": 0.5}), ("t", 86400 * 40), ("get_evolution", {"days": 1}), ("get_evolution", {"days": 7})]
             + [s for k in range(14) for s in (("change_trait", {"name": ["curiosity", "honesty", "caution", "respect"][k % 4], "delta": 0.05 * (k % 3 - 1)}), ("t", 30))] + [("get_evolution", {"days": 1}), ("get_evolution", {"days": 100})])

    ST = lambda n_, v: ("set_trait", {"name": n_, "value": v})  # noqa: E731
    scenario("S8 пороги конфликтов ровно", [I, ST("desire_to_help", 0.9), ST("caution", 0.3), ("get_conflicts", {}), ST("caution", 0.29), ("get_conflicts", {}), ST("caution", 0.5),
                                            ST("honesty", 0.8), ("get_conflicts", {}), ST("honesty", 0.81), ("get_conflicts", {}), ST("honesty", 0.5),
                                            ST("curiosity", 0.9), ST("patience", 0.4), ("get_conflicts", {}), ST("patience", 0.39), ("get_conflicts", {}),
                                            ST("desire_to_help", 0.7), ST("caution", 0.1), ST("honesty", 0.9), ("get_conflicts", {}), ST("curiosity", 0.7), ST("patience", 0.1), ("get_conflicts", {})])
    scenario("S9 затухание распространения ровно 0.005", [I, ("set_edge_weight", {"source": "honesty", "target": "desire_to_help", "weight": 1.0}), ("change_trait", {"name": "honesty", "delta": 0.016666666666666666}), ("get_traits", {}),
                                                        ("change_trait", {"name": "honesty", "delta": 0.0167}), ("get_traits", {}), ("change_trait", {"name": "honesty", "delta": 0.0166}), ("get_traits", {})])
    scenario("S10 последние 5 изменений", [I] + [s for k in range(9) for s in (("change_trait", {"name": ["curiosity", "honesty", "caution"][k % 3], "delta": 0.01 * (k + 1)}), ("t", 5), ("get_evolution_full", {"days": 1}))] + [("get_evolution_full", {"days": 0.00001})])

    rnd = random.Random(20260928)
    for k in range(40):
        steps = [I]
        names = list(json.loads(json.dumps({"curiosity": 1, "honesty": 1, "respect": 1, "patience": 1, "caution": 1, "confidence": 1})))
        for _ in range(rnd.randint(4, 25)):
            op = rnd.choice(["r", "r", "c", "s", "e", "l", "g", "t", "ev", "q"])
            if op == "r":
                steps.append(("reflect", {"event": rnd.choice(events), "intensity": rnd.choice([0.05, 0.5, 1.0, 2.0])}))
            elif op == "c":
                steps.append(("change_trait", {"name": rnd.choice(names), "delta": rnd.choice([-0.3, -0.05, 0.001, 0.05, 0.3])}))
            elif op == "s":
                steps.append(("set_trait", {"name": rnd.choice(names), "value": rnd.choice([0.0, 0.29, 0.31, 0.7, 0.71, 1.0])}))
            elif op == "e":
                steps.append(("set_edge_weight", {"source": "honesty", "target": rnd.choice(["confidence", "desire_to_help"]), "weight": rnd.choice([-1, 0.1, 0.9])}))
            elif op == "l":
                steps.append(("learn_edge", {"source": rnd.choice(["honesty", "caution"]), "target": rnd.choice(["confidence", "curiosity", "desire_to_help"]), "success": rnd.choice([True, False])}))
            elif op == "g":
                steps.append(rnd.choice(ALL))
            elif op == "ev":
                steps.append(("get_evolution", {"days": rnd.choice([0.1, 1, 7])}))
            elif op == "q":
                steps.append(("answer_internal_question", {"question_idx": rnd.randint(-1, 4), "answer": "ответ"}))
            else:
                steps.append(("t", rnd.choice([1, 3600, 86400])))
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
