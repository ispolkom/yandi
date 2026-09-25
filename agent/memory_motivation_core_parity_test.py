"""Эпизодическая память и мотивация на Rust (rustlib/yandi_core: memory_episodic, motivation) против agent/memory_episodic.py и agent/motivation.py на Python и НАСТОЯЩЕМ MySQL.
Одни и те же вызовы с управляемыми часами и идентификаторами; сравниваются результат каждого вызова и содержимое таблиц episode / self_state. Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ."""
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
    import agent.motivation as mo
    import agent.self_model as sm
    from agent.db.sql.security_triggers import immutability_triggers

    os.environ["TZ"] = "UTC"
    import time as _time
    _time.tzset()
    host, port = target.rsplit(":", 1)
    kw = dict(host=host, port=int(port), user="root", password="", cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")
    admin = pymysql.connect(autocommit=True, **kw)
    ac = admin.cursor()
    DB = "yandi_epi_parity"
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
    tables = ["episode", "self_state"]

    clock = {"t": 1_770_000_000.0}
    idq: list[str] = []
    state = {"h": None, "mo_state": None}
    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    fake_uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=idq.pop(0)))
    me.uuid = fake_uuid
    sm.uuid = fake_uuid
    fake_time = types.SimpleNamespace(time=lambda: clock["t"], strftime=_time.strftime, localtime=_time.localtime)
    mo.time = fake_time

    @contextlib.contextmanager
    def fake_get_connection(autocommit=False):
        c = pymysql.connect(database=DB, autocommit=autocommit, **kw)
        try:
            yield c
        finally:
            c.close()

    me.get_connection = fake_get_connection
    sm.get_connection = fake_get_connection

    def reset(t0=1_770_000_000.0):
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            cur.execute(f"TRUNCATE TABLE {t}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if state["h"] is not None:
            yandi_core.close(state["h"])
        state["h"] = yandi_core.open_memory()
        state["mo_state"] = None
        clock["t"] = t0
        idq.clear()
        sm._self_model = None
        mo._motivation = None

    def dump_py():
        out = {}
        for t in tables:
            cur.execute(f"SELECT * FROM {t} ORDER BY 1")
            out[t] = [norm(r) for r in cur.fetchall()]
        return out

    def dump_rs():
        return {t: json.loads(yandi_core.query(state["h"], f"SELECT * FROM {t} ORDER BY 1")) for t in tables}

    memory = {"m": me.EpisodicMemory()}
    system = {"m": None}

    def conv(v):
        if dataclasses.is_dataclass(v):
            return dataclasses.asdict(v)
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v

    def py_call(name, a):
        try:
            dom, meth = name.split(":", 1)
            if dom == "me":
                m = memory["m"]
                if meth == "add_learning":
                    out = m.add_learning(a["lesson"], a["context"], a.get("importance", 0.6))
                elif meth == "get_timeline":
                    out = m.get_timeline(a.get("limit", 50))
                elif meth in ("get_stats", "summary"):
                    out = getattr(m, meth)()
                else:
                    out = getattr(m, meth)(**a)
                return {"ok": norm(conv(out))}
            if dom == "sm":
                model = sm.get_self_model()
                if meth == "set_metadata_value":
                    model.set_metadata_value(a["key"], a["value"])
                    return {"ok": None}
                raise ValueError(name)
            # мотивация
            if meth == "init":
                sm._self_model = None
                system["m"] = mo.MotivationSystem()
                return {"ok": None}
            s = system["m"]
            if meth == "set":
                getattr(s, "set_" + a["which"])(a["value"])
                return {"ok": None}
            if meth == "update_from_experience":
                s.update_from_experience(a["result"])
                return {"ok": None}
            if meth == "get_answer_mode_preference":
                return {"ok": s.get_answer_mode_preference(a["domain"], a["testability"])}
            if meth == "should_explore":
                return {"ok": s.should_explore(a.get("confidence", 0.5), a.get("uncertainty", 0.5))}
            if meth == "should_verify":
                return {"ok": s.should_verify(a["trust"], a["confidence"])}
            if meth == "should_ask_clarification":
                return {"ok": s.should_ask_clarification(a["uncertainty"])}
            if meth == "should_use_web":
                return {"ok": s.should_use_web(a["testability"], a["confidence"])}
            if meth == "get_summary":
                return {"ok": s.get_summary()}
            if meth == "summary_text":
                return {"ok": s.summary_text()}
            raise ValueError(name)
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], list(idq))
        dom, meth = name.split(":", 1)
        if dom == "me":
            r = json.loads(yandi_core.call(state["h"], "me_" + meth, json.dumps(a, ensure_ascii=False)))
        elif dom == "sm":
            r = json.loads(yandi_core.call(state["h"], "sm_" + meth, json.dumps(a, ensure_ascii=False)))
        else:
            payload = {"method": meth, "args": a, "state": None if meth == "init" else state["mo_state"]}
            r = json.loads(yandi_core.call(state["h"], "mo_call", json.dumps(payload, ensure_ascii=False)))
            if "ok" in r:
                state["mo_state"] = r["ok"]["state"]
                return {"ok": r["ok"]["out"]}
        return {"ok": r["ok"]} if "ok" in r else {"error": str(r["error"]).split(":")[0]}

    n = 0

    def scenario(label, steps, ids=60):
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
            clock["t"] += 1  # порядок событий одной секунды в оригинале не определён (ORDER BY created_at DESC)
            check(f"{label} · шаг {i} {name}", ok, f"\n args={json.dumps(args, ensure_ascii=False)[:300]}\n py={json.dumps(p, ensure_ascii=False)[:800]}\n rs={json.dumps(r, ensure_ascii=False)[:800]}")
            if not ok:
                return
        dp, dr = dump_py(), dump_rs()
        for t in tables:
            same = canon(dp[t]) == canon(dr[t])
            diff = [(a, b) for a, b in zip(dp[t], dr[t]) if canon(a) != canon(b)]
            check(f"{label} · таблица {t}", same, f"\n строк py={len(dp[t])} rs={len(dr[t])}; первое отличие: {diff[:1]}")

    # ---------------- эпизодическая память ----------------
    scenario("E1 добавление и выборки", [
        ("me:add_query", {"query": "Что такое сознание? " * 8, "domain": "philosophical", "answer_mode": "pluralistic_contextual", "trust": "VALUE_FRAMEWORK", "confidence": 0.4}),
        ("me:add_query", {"query": "Как полететь на Марс?", "domain": "factual", "answer_mode": "factual", "trust": "EMPIRICALLY_SUPPORTED", "confidence": 0.7}),
        ("me:add_decision", {"decision_type": "epistemic_route", "reason": "Выбран pluralistic_contextual " * 3, "details": {"domain": "philosophical"}, "importance": 0.6}),
        ("me:add_error", {"error": "web search timeout " * 5, "context": {"query": "Yandi"}, "severity": 0.55}),
        ("me:add_error", {"error": "мелкая", "context": {}, "severity": 5}),
        ("me:add_error", {"error": "отрицательная", "context": {}, "severity": -3}),
        ("me:add_reflection", {"reflection": {"summary": "Итоги дня", "n": [1, 2]}}),
        ("me:add_reflection", {"reflection": {"x": 1}}),
        ("me:add_reflection", {"reflection": {"summary": 5}}),
        ("me:add_learning", {"lesson": "Интерпретативные вопросы не должны ходить в web " * 2, "context": {"domain": "philosophical"}, "importance": 0.81}),
        ("me:add_learning", {"lesson": "урок", "context": {"lesson": "старый", "k": None}, "importance": 0.72}),
        ("me:add_learning", {"lesson": "урок", "context": [1], "importance": 0.72}),
        ("me:add", {"event_type": "custom", "summary": "Своё", "details": {"a": {"б": [1, 2.5, None]}}, "importance": 0.33, "tags": ["один", "два"]}),
        ("me:add", {"event_type": "custom", "summary": "Без деталей", "details": None, "importance": 0.34}),
        ("me:get_by_type", {"event_type": "error"}), ("me:get_by_type", {"event_type": "error", "limit": 1}), ("me:get_by_type", {"event_type": "нет"}),
        ("me:get_by_tag", {"tag": "error"}), ("me:get_by_tag", {"tag": "philosophical"}), ("me:get_by_tag", {"tag": "один", "limit": 5}), ("me:get_by_tag", {"tag": "нет"}),
        ("me:get_recent", {}), ("me:get_recent", {"limit": 2}), ("me:get_recent", {"limit": 0}),
        ("me:get_timeline", {}), ("me:get_timeline", {"limit": 3}), ("me:get_stats", {}), ("me:summary", {}),
    ])
    # (порядок при РАВНОЙ важности в оригинале не определён — здесь только различные значения; значение РОВНО на пороге тоже не берём: MySQL сравнивает FLOAT (0.69999999) с 0.7 и
    #  отбрасывает его, SQLite хранит те же 6 значащих цифр и включает — осознанное отличие, намерение автора — «не меньше порога»)
    scenario("E3 по важности", [
        ("me:add_error", {"error": "a", "context": {}, "severity": 0.11}), ("me:add_error", {"error": "b", "context": {}, "severity": 0.92}), ("me:add_error", {"error": "c", "context": {}, "severity": 0.73}),
        ("me:add_error", {"error": "d", "context": {}, "severity": 0.55}), ("me:add_error", {"error": "e", "context": {}, "severity": 0.71}), ("me:add_error", {"error": "f", "context": {}, "severity": 0.61}),
        ("me:get_by_importance", {}), ("me:get_by_importance", {"min_importance": 0.5, "limit": 3}), ("me:get_by_importance", {"min_importance": 0.7}), ("me:get_by_importance", {"min_importance": 2}), ("me:get_by_importance", {"min_importance": 0.0, "limit": 100}),
    ])
    scenario("E2 пустая память", [("me:get_stats", {}), ("me:summary", {}), ("me:get_recent", {}), ("me:get_timeline", {}), ("me:get_by_type", {"event_type": "query"}), ("me:get_by_importance", {})])

    # ---------------- мотивация ----------------
    I = ("mo:init", {})
    grid = [(0.0, 0.0), (0.3, 0.7), (0.8, 0.2), (0.5, 0.5), (0.99, 1.0), (0.1, 0.9), (0.0, 1.0), (1.0, 0.0), (0.2, 0.75)]
    dec = []
    for conf, unc in grid:
        dec += [("mo:should_explore", {"confidence": conf, "uncertainty": unc}), ("mo:should_ask_clarification", {"uncertainty": unc}), ("mo:should_verify", {"trust": "UNVERIFIED", "confidence": conf}),
                ("mo:should_verify", {"trust": "SUPPORTED", "confidence": conf}), ("mo:should_use_web", {"testability": "fully_testable", "confidence": conf}), ("mo:should_use_web", {"testability": "interpretive", "confidence": conf})]
    modes = [("mo:get_answer_mode_preference", {"domain": d, "testability": t}) for d in ("procedural", "philosophical", "factual") for t in ("fully_testable", "partially_testable", "interpretive", "untestable")]
    scenario("M1 значения по умолчанию и решения", [I, ("mo:get_summary", {}), ("mo:summary_text", {})] + dec + modes)
    # граничные значения решений: сравнение «строго больше/меньше» на точных порогах (accuracy/curiosity/usefulness/safety ровно на 0.7/0.8/0.6/0.7)
    def boundary_block():
        steps = []
        for conf in (0.3, 0.4, 0.5, 0.6, 0.7):
            for unc in (0.0, 0.5, 0.7, 1.0):
                steps += [("mo:should_explore", {"confidence": conf, "uncertainty": unc}), ("mo:should_ask_clarification", {"uncertainty": unc}), ("mo:should_verify", {"trust": "UNVERIFIED", "confidence": conf}),
                          ("mo:should_verify", {"trust": "SUPPORTED", "confidence": conf}), ("mo:should_use_web", {"testability": "fully_testable", "confidence": conf}), ("mo:should_use_web", {"testability": "interpretive", "confidence": conf})]
        steps += [("mo:get_answer_mode_preference", {"domain": d, "testability": t}) for d in ("procedural", "philosophical") for t in ("fully_testable", "partially_testable", "interpretive")]
        return steps
    bsteps = [I]
    for which, values in (("accuracy", (0.7, 0.8, 0.6)), ("safety", (0.6, 0.7)), ("usefulness", (0.8, 0.5)), ("curiosity", (0.7, 0.3))):
        for v in values:
            bsteps += [("mo:set", {"which": which, "value": v})] + boundary_block()
    scenario("M5 точные пороги решений", bsteps)
    scenario("M2 сеттеры и границы", [I] + [("mo:set", {"which": w, "value": v}) for w in ("accuracy", "curiosity", "coherence", "usefulness", "safety", "exploration", "caution") for v in (-1, 0, 0.005, 0.555, 0.995, 1, 7)] + [("mo:get_summary", {}), ("mo:summary_text", {})])
    scenario("M3 сдвиг от опыта", [I, ("mo:update_from_experience", {"result": {"was_useful": True}}), ("mo:update_from_experience", {"result": {"error": "мало данных"}}),
                                     ("mo:update_from_experience", {"result": {"had_conflict": 1}}), ("mo:update_from_experience", {"result": {"was_correct": "да"}}),
                                     ("mo:update_from_experience", {"result": {"was_useful": 0, "error": "", "had_conflict": [], "was_correct": {}}}),
                                     ("mo:update_from_experience", {"result": {"was_useful": [0], "error": {"a": 1}, "had_conflict": True, "was_correct": True}}),
                                     ("mo:update_from_experience", {"result": {}}), ("mo:update_from_experience", {"result": [1]}), ("mo:update_from_experience", {"result": None}),
                                     ("mo:get_summary", {}), ("mo:summary_text", {})] + [("mo:update_from_experience", {"result": {"was_useful": True, "error": "e", "had_conflict": True, "was_correct": True}})] * 12 + [("mo:get_summary", {}), ("mo:should_explore", {"confidence": 0.3, "uncertainty": 0.7})])
    scenario("M4 загрузка из сохранённого", [
        I, ("mo:set", {"which": "accuracy", "value": 0.33}), ("mo:set", {"which": "caution", "value": 0.11}), I, ("mo:get_summary", {}), ("mo:should_verify", {"trust": "SUPPORTED", "confidence": 0.55}),
        ("sm:set_metadata_value", {"key": "motivation", "value": {"accuracy": 0.9, "curiosity": 0.1}}), I, ("mo:get_summary", {}),
        ("sm:set_metadata_value", {"key": "motivation", "value": {"accuracy": 0.9, "лишний": 1}}), I, ("mo:get_summary", {}),
        ("sm:set_metadata_value", {"key": "motivation", "value": "мусор"}), I, ("mo:get_summary", {}),
        ("sm:set_metadata_value", {"key": "motivation", "value": {"history": [1, 2], "last_update": 5}}), I, ("mo:get_summary", {}), ("mo:set", {"which": "safety", "value": 0.4}), I, ("mo:summary_text", {}),
    ])

    # ---------------- случайные сценарии ----------------
    rnd = random.Random(20260928)
    ops = [
        lambda: ("me:add_query", {"query": rnd.choice(["Вопрос?", "в" * 90, ""]), "domain": rnd.choice(["a", "b"]), "answer_mode": rnd.choice(["x", "y"]), "trust": "T", "confidence": rnd.choice([0.1, 0.51, 0.93])}),
        lambda: ("me:add_error", {"error": rnd.choice(["e", "е" * 80]), "context": {"k": rnd.randint(0, 9)}, "severity": rnd.choice([0.21, 0.66, 0.99])}),
        lambda: ("me:add_learning", {"lesson": rnd.choice(["l", "л" * 70]), "context": {"c": 1}, "importance": rnd.choice([0.31, 0.52, 0.94])}),
        lambda: ("me:add_decision", {"decision_type": rnd.choice(["r1", "r2"]), "reason": rnd.choice(["why", "п" * 50]), "details": {"z": 1}, "importance": rnd.choice([0.41, 0.77])}),
        lambda: ("me:get_recent", {"limit": rnd.choice([1, 3, 20])}), lambda: ("me:get_by_type", {"event_type": rnd.choice(["query", "error", "learning"])}),
        lambda: ("me:get_by_tag", {"tag": rnd.choice(["a", "error", "learning", "r1"])}), lambda: ("me:get_stats", {}), lambda: ("me:summary", {}), lambda: ("me:get_timeline", {"limit": rnd.choice([2, 10])}),
        lambda: ("t", rnd.choice([1, 60, 86400])),
        lambda: ("mo:set", {"which": rnd.choice(["accuracy", "curiosity", "coherence", "usefulness", "safety", "exploration", "caution"]), "value": rnd.choice([-1, 0.2, 0.61, 1.5])}),
        lambda: ("mo:update_from_experience", {"result": {k: rnd.choice([True, False, "x", 0]) for k in rnd.sample(["was_useful", "error", "had_conflict", "was_correct"], rnd.randint(0, 4))}}),
        lambda: ("mo:should_explore", {"confidence": rnd.random(), "uncertainty": rnd.random()}), lambda: ("mo:get_summary", {}), lambda: ("mo:summary_text", {}),
        lambda: ("mo:get_answer_mode_preference", {"domain": rnd.choice(["procedural", "x"]), "testability": rnd.choice(["fully_testable", "partially_testable", "interpretive", "z"])}),
    ]
    for k in range(40):
        steps = [I]
        for _ in range(rnd.randint(6, 26)):
            steps.append(rnd.choice(ops)())
        scenario(f"R{k} случайный", steps, ids=80)

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
