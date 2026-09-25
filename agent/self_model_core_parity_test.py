"""Модель себя на Rust (rustlib/yandi_core/src/self_model.rs) против agent/self_model.py на Python и НАСТОЯЩЕМ MySQL.
Одни и те же вызовы с управляемыми часами и идентификаторами; сравниваются результат каждого вызова и содержимое таблиц self_state / self_event. Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ."""
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
    import agent.self_model as sm
    from agent.db.sql.security_triggers import immutability_triggers

    os.environ["TZ"] = "UTC"
    import time as _time
    _time.tzset()
    host, port = target.rsplit(":", 1)
    kw = dict(host=host, port=int(port), user="root", password="", cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")
    admin = pymysql.connect(autocommit=True, **kw)
    ac = admin.cursor()
    DB = "yandi_self_parity"
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
    tables = ["self_state", "self_event"]

    clock = {"t": 1_770_000_000.0}
    idq: list[str] = []
    state = {"h": None}
    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    sm.uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=idq.pop(0)))

    @contextlib.contextmanager
    def fake_get_connection(autocommit=False):
        c = pymysql.connect(database=DB, autocommit=autocommit, **kw)
        try:
            yield c
        finally:
            c.close()

    sm.get_connection = fake_get_connection

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

    model = {"m": None}

    def py_call(name, a):
        try:
            if name == "init":
                model["m"] = sm.SelfModel()
                return {"ok": None}
            m = model["m"]
            if name == "declare_character_trait":
                return {"ok": norm(m.declare_character_trait(**a["traits"]))}
            if name == "wipe":
                cur.execute("DELETE FROM self_state")
                return {"ok": None}
            if name == "summary":
                return {"ok": m.summary()}
            if name == "repr":
                return {"ok": repr(m)}
            return {"ok": norm(getattr(m, name)(**a))}
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], list(idq))
        if name == "wipe":
            r = json.loads(yandi_core.call(state["h"], "query_exec", json.dumps({"sql": "DELETE FROM self_state"})))
            return {"error": "OperationalError"} if "error" in r else {"ok": None}
        r = json.loads(yandi_core.call(state["h"], "sm_" + name, json.dumps(a, ensure_ascii=False)))
        return {"ok": r["ok"]} if "ok" in r else {"error": str(r["error"]).split(":")[0]}

    n = 0

    def scenario(label, steps, ids=40):
        nonlocal n
        n += 1
        reset()
        idq.extend(f"{i:08x}{i * 7919 % 65536:04x}" for i in range(ids))
        model["m"] = None
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
            clock["t"] += 1  # порядок событий ОДНОЙ секунды в оригинале не определён (ORDER BY created_at DESC): различаем секундами
            check(f"{label} · шаг {i} {name}", ok, f"\n args={json.dumps(args, ensure_ascii=False)[:300]}\n py={json.dumps(p, ensure_ascii=False)[:700]}\n rs={json.dumps(r, ensure_ascii=False)[:700]}")
            if not ok:
                return
        dp, dr = dump_py(), dump_rs()
        for t in tables:
            same = canon(dp[t]) == canon(dr[t])
            diff = [(a, b) for a, b in zip(dp[t], dr[t]) if canon(a) != canon(b)]
            check(f"{label} · таблица {t}", same, f"\n строк py={len(dp[t])} rs={len(dr[t])}; первое отличие: {diff[:1]}")

    I = ("init", {})
    scenario("S1 инициализация и геттеры", [
        I, ("get_identity", {}), ("get_age", {}), ("get_metadata", {}), ("get_goals", {}), ("get_capabilities", {}), ("get_limitations", {}), ("get_uncertainties", {}),
        ("reflect", {}), ("check_health", {}), ("get_timeline", {}), ("summary", {}), ("repr", {}), I, ("get_recent_decisions", {}), ("get_lessons", {}), ("get_belief_history", {}),
    ])
    scenario("S2 события и счётчики", [
        I, ("t", 5),
        ("add_decision", {"decision": {"query": "Что такое сознание? " * 10, "domain": "philosophical", "answer_mode": "pluralistic_contextual", "trust": "VALUE_FRAMEWORK", "confidence": 0.4, "reason": "интерпретативный вопрос"}}),
        ("add_decision", {"decision": {}}), ("add_decision", {"decision": {"query": "x", "confidence": 1}}), ("add_decision", {"decision": {"query": None}}), ("add_decision", {"decision": {"query": 5}}),
        ("add_decision", {"decision": {"query": "y", "confidence": "high"}}), ("add_decision", {"decision": {"query": "z", "confidence": None}}), ("add_decision", {"decision": [1]}),
        ("t", 60), ("add_learning", {"lesson": "Интерпретативные вопросы не должны ходить в web " * 3, "context": "query: сознание", "importance": 0.8}), ("add_learning", {"lesson": "коротко", "context": ""}),
        ("t", 60), ("add_reflection", {"reflection": {"summary": "Итоги дня " * 20, "x": [1, 2, {"y": None}]}}), ("add_reflection", {"reflection": {}}), ("add_reflection", {"reflection": {"summary": 5}}),
        ("add_error", {"error": "сбой " * 30, "context": {"where": "тут", "n": 1}}), ("add_error", {"error": "мало", "context": {}, "severity": 0.2}), ("add_error", {"error": "", "context": None, "severity": 1}),
        ("add_belief_update", {"topic": "consciousness", "old_confidence": 0.5, "new_confidence": 0.7, "reason": "Новые данные"}), ("add_belief_update", {"topic": "тема", "old_confidence": 0.125, "new_confidence": 0.375, "reason": ""}),
        ("add_belief_update", {"topic": "t", "old_confidence": 0.005, "new_confidence": 0.995, "reason": "границы"}), ("add_belief_update", {"topic": "t", "old_confidence": 1, "new_confidence": 0, "reason": "целые"}),
        ("add_change", {"what_changed": "цель " * 20, "before": [1], "after": {"a": 1}, "reason": "потому"}), ("add_change", {"what_changed": "x", "before": None, "after": None, "reason": ""}),
        ("add_event", {"event_type": "custom", "description": "Своё событие", "details": {"k": "v", "ё": [1, 2.5, None]}, "importance": 0.9}), ("add_event", {"event_type": "custom", "description": "без важности", "details": {}}),
        ("increment_cycle", {}), ("increment_cycle", {}), ("increment_queries", {}), ("increment_errors", {}), ("increment_reflections", {}),
        ("get_recent_decisions", {"limit": 3}), ("get_recent_decisions", {"limit": 0}), ("get_lessons", {"limit": 1}), ("get_belief_history", {}), ("get_timeline", {"limit": 5}), ("get_timeline", {"limit": 100}),
        ("reflect", {}), ("check_health", {}), ("summary", {}), ("repr", {}),
    ])
    scenario("S3 характеристики и цели", [
        I, ("add_capability", {"capability": "новая"}), ("add_capability", {"capability": "новая"}), ("add_capability", {"capability": "reasoning"}),
        ("add_limitation", {"limitation": "новое ограничение"}), ("add_limitation", {"limitation": "new"}), ("add_limitation", {"limitation": "new"}),
        ("add_uncertainty", {"uncertainty": "неясно А"}), ("add_uncertainty", {"uncertainty": "неясно Б"}), ("add_uncertainty", {"uncertainty": "неясно А"}),
        ("remove_uncertainty", {"uncertainty": "неясно А"}), ("remove_uncertainty", {"uncertainty": "нет такой"}), ("remove_uncertainty", {"uncertainty": "неясно Б"}),
        ("get_uncertainties", {}), ("get_capabilities", {}), ("get_limitations", {}),
        ("set_goals", {"goals": ["понять", "помогать"]}), ("add_goal", {"goal": "учиться"}), ("add_goal", {"goal": "учиться"}), ("get_goals", {}),
        ("set_metadata_value", {"key": "mood", "value": {"a": [1, 2]}}), ("set_metadata_value", {"key": "goals", "value": 5}), ("add_goal", {"goal": "x"}), ("get_goals", {}), ("get_metadata", {}),
        ("declare_character_trait", {"traits": {"hair": "тёмные", "gender": "female"}}), ("declare_character_trait", {"traits": {}}), ("declare_character_trait", {"traits": {"website": None}}), ("get_metadata", {}),
        ("reflect", {}), ("summary", {}),
    ])
    scenario("S4 здоровье", [I] + [("increment_errors", {})] * 20 + [("check_health", {})] + [("increment_errors", {}), ("check_health", {}), ("reflect", {})] + [("increment_cycle", {})] * 19 + [("check_health", {}), ("increment_cycle", {}), ("check_health", {}), ("increment_cycle", {}), ("check_health", {}), ("increment_queries", {}), ("check_health", {}), ("summary", {})])
    scenario("S4b здоровье: много ошибок и мало запросов вместе", [I] + [("increment_errors", {})] * 25 + [("increment_cycle", {})] * 22 + [("check_health", {}), ("summary", {}), ("reflect", {})])
    scenario("S4c граничные значения", [
        I, ("add_belief_update", {"topic": "x", "old_confidence": "a", "new_confidence": 1, "reason": ""}), ("add_belief_update", {"topic": "x", "old_confidence": True, "new_confidence": 0.005, "reason": ""}),
        ("add_belief_update", {"topic": "x", "old_confidence": 0.015, "new_confidence": 0.025, "reason": ""}), ("add_belief_update", {"topic": "x", "old_confidence": 2.675, "new_confidence": 1.005, "reason": ""}),
        ("add_learning", {"lesson": "у" * 60, "context": "", "importance": 1}), ("add_learning", {"lesson": "у" * 61, "context": "", "importance": True}),
        ("add_error", {"error": "е" * 60, "context": [], "severity": 1}), ("add_error", {"error": "е" * 61, "context": [], "severity": 2}),
        ("add_change", {"what_changed": "ц" * 50, "before": 1, "after": 2, "reason": ""}), ("add_change", {"what_changed": "ц" * 51, "before": 1, "after": 2, "reason": ""}),
        ("add_decision", {"decision": {"query": "в" * 50}}), ("add_decision", {"decision": {"query": "в" * 51}}), ("add_decision", {"decision": {"query": "в" * 100}}), ("add_decision", {"decision": {"query": "в" * 101}}),
        ("add_reflection", {"reflection": {"summary": "р" * 60}}), ("add_reflection", {"reflection": {"summary": "р" * 61}}),
        ("add_event", {"event_type": "e", "description": "д" * 99, "details": {}}), ("add_event", {"event_type": "e", "description": "д" * 100, "details": {}}), ("add_event", {"event_type": "e", "description": "д" * 101, "details": {}}),
        ("get_timeline", {"limit": 30}), ("summary", {}), ("reflect", {}),
    ])
    scenario("S5 состояние стёрто (удаление запрещено триггером)", [I, ("add_event", {"event_type": "e0", "description": "до", "details": {}}), ("wipe", {}), ("reflect", {}), ("get_identity", {}), ("add_capability", {"capability": "x"}), ("check_health", {}), ("summary", {}), ("repr", {}), ("add_event", {"event_type": "e", "description": "d", "details": {}}), ("get_timeline", {})])
    scenario("S6 счётчики пустой истории", [I, ("summary", {}), ("get_timeline", {"limit": 3}), ("increment_cycle", {}), ("t", 86400), ("increment_cycle", {}), ("summary", {})])

    # ---- случайные сценарии ----
    rnd = random.Random(20260927)
    calls = [
        lambda: ("add_decision", {"decision": {"query": rnd.choice(["Что?", "ё" * 120, ""]), "confidence": rnd.choice([0.1, 0.99, 1, 0])}}),
        lambda: ("add_learning", {"lesson": rnd.choice(["урок", "у" * 80]), "context": rnd.choice(["", "ctx"]), "importance": rnd.choice([0.1, 0.6, 1.0])}),
        lambda: ("add_reflection", {"reflection": {"summary": rnd.choice(["итог", "и" * 90]), "n": rnd.randint(0, 3)}}),
        lambda: ("add_error", {"error": rnd.choice(["e", "е" * 70]), "context": {"k": rnd.randint(0, 9)}, "severity": rnd.choice([0.1, 0.7, 1])}),
        lambda: ("add_belief_update", {"topic": "t", "old_confidence": rnd.choice([0.0, 0.125, 0.5, 0.995]), "new_confidence": rnd.choice([0.375, 0.7, 1.0]), "reason": "r"}),
        lambda: ("add_change", {"what_changed": rnd.choice(["c", "ц" * 60]), "before": rnd.choice([None, 1, [1]]), "after": rnd.choice([None, {"a": 1}]), "reason": "r"}),
        lambda: ("add_capability", {"capability": rnd.choice(["a", "b", "reasoning"])}), lambda: ("add_limitation", {"limitation": rnd.choice(["a", "b"])}),
        lambda: ("add_uncertainty", {"uncertainty": rnd.choice(["a", "b", "c"])}), lambda: ("remove_uncertainty", {"uncertainty": rnd.choice(["a", "b", "c"])}),
        lambda: ("add_goal", {"goal": rnd.choice(["g1", "g2"])}), lambda: ("increment_cycle", {}), lambda: ("increment_queries", {}), lambda: ("increment_errors", {}), lambda: ("increment_reflections", {}),
        lambda: ("reflect", {}), lambda: ("check_health", {}), lambda: ("summary", {}), lambda: ("get_timeline", {"limit": rnd.choice([1, 3, 50])}),
        lambda: ("get_recent_decisions", {"limit": rnd.choice([1, 5])}), lambda: ("get_lessons", {"limit": rnd.choice([1, 5])}), lambda: ("get_belief_history", {"limit": rnd.choice([1, 5])}),
        lambda: ("t", rnd.choice([1, 60, 86400])),
    ]
    for k in range(40):
        steps = [I]
        for _ in range(rnd.randint(5, 25)):
            steps.append(rnd.choice(calls)())
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
