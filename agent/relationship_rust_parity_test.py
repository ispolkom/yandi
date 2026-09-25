"""Ядро отношений на Rust (rustlib/yandi_core: causal_events, relationship_state, relationship_memory) против агента на Python + НАСТОЯЩИЙ MySQL.
Те же последовательности вызовов с управляемыми часами и идентификаторами; сравниваются результат каждого вызова и содержимое таблиц. Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ."""
from __future__ import annotations

import datetime as dt
import json
import os
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
        return 0.0      # SQLite не хранит «минус ноль» (MySQL хранил): значение то же
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

    import agent.causal_events as ce
    import agent.db.sql.repositories as repo
    import agent.db.sql.schema as S
    import agent.relationship_memory as rm
    import agent.relationship_state as rs
    from agent.db.sql.security_triggers import immutability_triggers

    host, port = target.rsplit(":", 1)
    kw = dict(host=host, port=int(port), user="root", password="", autocommit=True, cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")
    admin = pymysql.connect(**kw)
    ac = admin.cursor()
    ac.execute("DROP DATABASE IF EXISTS yandi_rel_parity")
    ac.execute("CREATE DATABASE yandi_rel_parity CHARACTER SET utf8mb4")
    conn = pymysql.connect(database="yandi_rel_parity", **kw)
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
    tables = ["grievance", "forgiveness_capacity", "inner_state", "inner_state_event", "causal_event"]
    all_tables = [n for n, _ in S.ALL_TABLES_IN_ORDER]

    clock = {"t": 1_770_000_000.0}
    idq: list[str] = []
    state = {"h": None}

    fixed_now = lambda: dt.datetime.utcfromtimestamp(clock["t"])  # noqa: E731
    repo._now = fixed_now
    rm._now = fixed_now
    rm.time = types.SimpleNamespace(time=lambda: clock["t"])
    rm.uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=idq.pop(0)))

    def reset(t0=1_770_000_000.0):
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in all_tables:
            cur.execute(f"TRUNCATE TABLE {t}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if state["h"] is not None:
            yandi_core.close(state["h"])
        state["h"] = yandi_core.open_memory()
        clock["t"] = t0
        idq.clear()

    def py_call(name, a):
        uid = a.get("user_id")
        try:
            if name == "causal_claim":
                return {"ok": ce.claim(conn, uid, a.get("source_turn_id"), a["event_type"], tuple(a["span"]) if a.get("span") else None)}
            if name.startswith("rs_"):
                fn = name[3:]
                if fn == "observed_trust_reward":
                    return {"ok": rs.observed_trust_reward(a["prior_observed"])}
                return {"ok": norm(getattr(rs, fn)(conn, uid, **{k: v for k, v in a.items() if k != "user_id"}))}
            if name == "rm_match_grievance_target":
                now = dt.datetime.utcfromtimestamp(a["now"])
                m = rm.match_grievance_target(a["text"], a["active"], a["resolved"], now)
                return {"ok": norm({"grievance": m.grievance, "basis": m.basis, "candidates": m.candidates})}
            if name.startswith("rm_"):
                fn = name[3:]
                kwargs = {k: v for k, v in a.items() if k not in ("user_id", "span")}
                if a.get("span"):
                    kwargs["span"] = tuple(a["span"])
                if fn in ("acknowledge_apology", "progress_healing"):
                    return {"ok": getattr(rm, fn)(conn, **kwargs)}
                return {"ok": norm(getattr(rm, fn)(conn, uid, **kwargs)) if uid is not None else norm(getattr(rm, fn)(conn, **kwargs))}
            return {"ok": norm(getattr(repo, name)(conn, **a))}
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__, "msg": str(e)[:200]}

    def rs_call(name, a):
        yandi_core.set_clock(state["h"], clock["t"], list(idq))
        r = json.loads(yandi_core.call(state["h"], name, json.dumps(a, ensure_ascii=False)))
        return r

    def dump_py():
        out = {}
        for t in tables:
            cur.execute(f"SELECT * FROM {t} ORDER BY 1, 2")
            out[t] = [norm(r) for r in cur.fetchall()]
        return out

    def dump_rs():
        return {t: json.loads(yandi_core.query(state["h"], f"SELECT * FROM {t} ORDER BY 1, 2")) for t in tables}

    n = 0

    def scenario(label, steps):
        """steps: ("t", секунды) — сдвинуть часы; ("ids", [...]) — добавить идентификаторы; (имя, аргументы) — вызов."""
        nonlocal n
        reset()
        n += 1
        for i, st in enumerate(steps):
            if st[0] == "t":
                clock["t"] += st[1]
                continue
            if st[0] == "ids":
                idq.extend(st[1])
                continue
            name, args = st
            saved = list(idq)
            p = py_call(name, args)
            idq.clear(); idq.extend(saved)
            r = rs_call(name, args)
            # Rust израсходовал те же идентификаторы: синхронизируем очередь Python (он израсходовал свои при вызове)
            if "error" in p or "error" in r:
                ok = ("error" in p) == ("error" in r)
            else:
                ok = canon(p["ok"]) == canon(r["ok"])
            if os.environ.get("YANDI_TRACE") and label.startswith("B5"):
                print("TRACE", i, name, json.dumps(p, ensure_ascii=False)[:200])
            check(f"{label} · шаг {i} {name}", ok, f"\n args={json.dumps(args, ensure_ascii=False)[:300]}\n py={json.dumps(p, ensure_ascii=False)[:600]}\n rs={json.dumps(r, ensure_ascii=False)[:600]}")
            if not ok:
                return
            # очередь идентификаторов после вызова: Python уже сделал pop-ы на первой копии — берём остаток из неё
            idq.clear(); idq.extend(saved[len(saved) - len(rest_after(saved, p)):] if False else consume(saved, name, args, p))
        dp, dr = dump_py(), dump_rs()
        for t in tables:
            diff = [(a, b) for a, b in zip(dp[t], dr[t]) if canon(a) != canon(b)]
            check(f"{label} · таблица {t}", canon(dp[t]) == canon(dr[t]), f"\n строк py={len(dp[t])} rs={len(dr[t])}; первое отличие: {diff[:1]}")

    def rest_after(saved, p):
        return []

    def consume(saved, name, args, p):
        """Сколько идентификаторов израсходовал вызов: только rm_add_grievance при создании НОВОЙ обиды."""
        if name == "rm_add_grievance" and "ok" in p and p["ok"] and str(p["ok"]).startswith("g_") and saved and str(p["ok"]).endswith(saved[0][:8]):
            return saved[1:]
        return saved

    U = "owner"

    def ins(desc, sev, turn=None, **extra):
        a = {"user_id": U, "event_type": "insult", "description": desc, "severity": sev}
        if turn:
            a["source_turn_id"] = turn
        a.update(extra)
        return ("rm_add_grievance", a)

    # ---- S1: обида → извинение → заживление → прощение ----
    scenario("S1 полный цикл", [
        ("ids", ["aaaaaaaa11111111"]), ins("Ты глупая и бесполезная железка", 0.6, "t1", context={"turn": "t1", "текст": "🌍"}),
        ("rm_get_summary", {"user_id": U}), ("rs_get_state", {"user_id": U}),
        ("t", 600), ("rm_resolve_relationship_focus", {"user_id": U, "current_text": "Прости, что назвал тебя глупой железкой"}),
        ("rm_apply_apology", {"user_id": U, "grievance_id": "g_1770000000_aaaaaaaa", "sincerity": 0.9, "source_turn_id": "t2", "span": [0, 5]}),
        ("rm_apply_apology", {"user_id": U, "grievance_id": "g_1770000000_aaaaaaaa", "sincerity": 0.9, "source_turn_id": "t2"}),
        ("rm_progress_healing", {"grievance_id": "g_1770000000_aaaaaaaa"}),
        ("t", 3600), ("rm_progress_healing", {"grievance_id": "g_1770000000_aaaaaaaa"}),
        ("t", 7200), ("rm_progress_healing", {"grievance_id": "g_1770000000_aaaaaaaa"}), ("rm_progress_healing", {"grievance_id": "g_1770000000_aaaaaaaa"}),
        ("rm_get_summary", {"user_id": U}), ("rs_get_state", {"user_id": U}), ("rs_replay", {"user_id": U}),
        ("rm_progress_healing", {"grievance_id": "нет-такой"}),
    ])
    # ---- S2: дубли доставки, нестабильные ---
    scenario("S2 идемпотентность", [
        ("ids", ["bbbbbbbb", "cccccccc", "dddddddd"]),
        ins("Ты ужасная собеседница сегодня вечером", 0.5, "tA"), ins("Ты ужасная собеседница сегодня вечером", 0.5, "tA"),
        ins("Ты ужасная собеседница сегодня вечером", 0.5, "tB"), ins("Совсем другое оскорбление ночью", 0.4),
        ins("Ещё одно оскорбление без номера доставки", 0.9),
        ("causal_claim", {"user_id": U, "source_turn_id": "tA", "event_type": "insult"}), ("causal_claim", {"user_id": U, "source_turn_id": None, "event_type": "insult"}),
        ("causal_claim", {"user_id": U, "source_turn_id": "", "event_type": "apology", "span": [1, 2]}), ("causal_claim", {"user_id": U, "source_turn_id": "tZ", "event_type": "apology", "span": [1, 2]}),
        ("rm_get_summary", {"user_id": U}), ("rs_get_state", {"user_id": U}),
    ])
    # ---- S3: повтор усиливает, ёмкость зажата в 0..100 ----
    steps = [("ids", [f"{i:08x}" for i in range(30)])]
    for i in range(14):
        steps += [ins("Повторяющееся оскорбление одинаковое каждый раз", 0.7 + 0.02 * (i % 4), f"r{i}"), ("t", 30)]
    steps += [("rm_get_summary", {"user_id": U}), ("rs_get_state", {"user_id": U}), ("rs_replay", {"user_id": U}), ("rm_most_severe_active_grievance", {"user_id": U})]
    scenario("S3 повторы и границы", steps)
    # ---- S4: искренность ----
    scenario("S4 искренность", [
        ("ids", ["11112222", "33334444"]),
        ins("Первое оскорбление про интеллект собеседницы", 0.8, "a1"), ins("Второе оскорбление про внешность собеседницы", 0.6, "a2"),
        ("rm_acknowledge_apology", {"grievance_id": "g_1770000000_11112222", "sincerity": 0.5}),
        ("rm_acknowledge_apology", {"grievance_id": "g_1770000000_11112222", "sincerity": 0.61}),
        ("rm_acknowledge_apology", {"grievance_id": "g_1770000000_11112222", "sincerity": 0.95}),
        ("rm_acknowledge_apology", {"grievance_id": "g_1770000000_33334444", "sincerity": 0.6}),
        ("rm_acknowledge_apology", {"grievance_id": "нет", "sincerity": 0.9}),
        ("rm_progress_healing", {"grievance_id": "g_1770000000_33334444"}), ("rm_progress_healing", {"grievance_id": "g_1770000000_11112222"}),
        ("t", 8000), ("rm_progress_healing", {"grievance_id": "g_1770000000_11112222"}), ("rm_progress_healing", {"grievance_id": "g_1770000000_33334444"}),
        ("rm_get_summary", {"user_id": U}), ("rs_get_state", {"user_id": U}),
    ])
    # ---- S5: выбор цели извинения ----
    scenario("S5 выбор цели", [
        ("ids", ["aa000001", "aa000002", "aa000003"]),
        ins("Ты вечно путаешь даты и факты", 0.5, "x1"), ("t", 4000), ins("Ты медленно отвечаешь на вопросы", 0.4, "x2"), ("t", 4000), ins("Ты скучная и однообразная собеседница", 0.6, "x3"),
        ("rm_resolve_relationship_focus", {"user_id": U, "current_text": "Прости, что сказал про скуку и однообразие"}),
        ("rm_resolve_relationship_focus", {"user_id": U, "current_text": "Извини за всё, что наговорил"}),
        ("rm_resolve_relationship_focus", {"user_id": U, "current_text": ""}),
        ("t", 100000), ("rm_resolve_relationship_focus", {"user_id": U, "current_text": "Извини"}),
        ("rm_apply_apology", {"user_id": U, "grievance_id": "g_1770000000_aa000001", "sincerity": 0.7, "source_turn_id": "z1"}),
        ("rm_apply_apology", {"user_id": "чужой", "grievance_id": "g_1770004000_aa000002", "sincerity": 0.7, "source_turn_id": "z2"}),
        ("rm_apply_apology", {"user_id": U, "grievance_id": None, "sincerity": 0.7}),
        ("rm_apply_apology", {"user_id": U, "grievance_id": "g_1770008000_aa000003", "sincerity": 0.2, "source_turn_id": "z3"}),
        ("rm_get_summary", {"user_id": U}),
    ])
    # ---- S6: обещания и доверие ----
    steps = []
    for k in range(0, 16):
        steps.append(("rs_observed_trust_reward", {"prior_observed": k}))
    steps += [("rs_observed_trust_reward", {"prior_observed": -3}), ("rs_get_state", {"user_id": U})]
    for k in range(14):
        steps += [("rs_record_observed_commitment", {"user_id": U, "prior_observed": k}), ("t", 5)]
    steps += [("rs_record_verified_commitment", {"user_id": U, "kept": True}), ("rs_record_verified_commitment", {"user_id": U, "kept": False}),
              ("rs_record_verified_commitment", {"user_id": U, "kept": False}), ("rs_record_verified_commitment", {"user_id": U, "kept": False}),
              ("rs_record_verified_commitment", {"user_id": U, "kept": False}), ("rs_record_insult", {"user_id": U, "severity": 1.7}), ("rs_record_insult", {"user_id": U, "severity": -0.5}),
              ("rs_record_accepted_apology", {"user_id": U, "offense_severity": 0.9, "sincerity": 0.7}),
              ("rs_get_state", {"user_id": U}), ("rs_replay", {"user_id": U}), ("rs_get_state", {"user_id": "другой"}), ("rs_replay", {"user_id": "другой"})]
    scenario("S6 обещания и доверие", steps)
    # ---- S7: предел «непрощённых» ----
    scenario("S7 непрощённые блокируют прощение", [
        ("ids", ["b0000001", "b0000002", "b0000003", "b0000004"]),
        ins("Оскорбление номер один очень обидное", 0.5, "u1"), ins("Оскорбление номер два тоже обидное", 0.5, "u2"), ins("Оскорбление номер три снова обидное", 0.5, "u3"), ins("Оскорбление номер четыре опять обидное", 0.5, "u4"),
        ("update_grievance_status", {"grievance_id": "g_1770000000_b0000001", "status": "unforgiven", "timestamp": "2026-03-01 10:00:00"}),
        ("update_grievance_status", {"grievance_id": "g_1770000000_b0000002", "status": "unforgiven", "timestamp": "2026-03-01 10:00:00"}),
        ("update_grievance_status", {"grievance_id": "g_1770000000_b0000003", "status": "unforgiven", "timestamp": "2026-03-01 10:00:00"}),
        ("rm_acknowledge_apology", {"grievance_id": "g_1770000000_b0000004", "sincerity": 0.9}), ("t", 9000),
        ("rm_progress_healing", {"grievance_id": "g_1770000000_b0000004"}), ("rm_get_summary", {"user_id": U}),
    ])
    def boundary(label, sincerity, capacity, unforgiven, wait):
        st = [("ids", ["c0000001"] + [f"c00000{i+2:02x}" for i in range(unforgiven)]),
              ins("Граничное оскорбление для проверки порогов", 0.5, "bd1")]
        for i in range(unforgiven):
            st.append(ins(f"Вариант {i} — про погоду", 0.3, f"bd{i+2}"))
        for i in range(unforgiven):
            st.append(("update_grievance_status", {"grievance_id": f"g_1770000000_c00000{i+2:02x}", "status": "unforgiven", "timestamp": "2026-03-01 10:00:00"}))
        st += [("update_grievance_status", {"grievance_id": "g_1770000000_c0000001", "status": "understood", "apology_sincerity": sincerity, "apology_at": "2026-02-02 02:40:00", "understood_at": "2026-02-02 02:40:00", "timestamp": "2026-02-02 02:40:00"}),
               ("set_forgiveness_capacity", {"user_id": U, "capacity": capacity, "timestamp": "2026-02-02 02:40:00"}), ("t", wait),
               ("rm_progress_healing", {"grievance_id": "g_1770000000_c0000001"}), ("rm_get_summary", {"user_id": U})]
        scenario(label, st)
    boundary("B1 искренность ровно 0.4", 0.4, 60.0, 0, 7200)
    boundary("B2 искренность 0.39", 0.39, 60.0, 0, 7200)
    boundary("B3 ёмкость ровно 30", 0.9, 30.0, 0, 7200)
    boundary("B4 ёмкость 29.9", 0.9, 29.9, 0, 7200)
    boundary("B5 ровно два непрощённых", 0.9, 60.0, 2, 7200)
    boundary("B6 три непрощённых", 0.9, 60.0, 3, 7200)
    boundary("B7 ровно два часа заживления", 0.9, 60.0, 0, 0)
    scenario("B8 потолок доверия за наблюдаемые выполнения", [
        ("rs_record_verified_commitment", {"user_id": U, "kept": True}), ("rs_record_verified_commitment", {"user_id": U, "kept": True}),
        ("rs_record_insult", {"user_id": U, "severity": 0.8}), ("rs_get_state", {"user_id": U}),
        ("rs_record_observed_commitment", {"user_id": U, "prior_observed": 0}), ("rs_get_state", {"user_id": U}), ("rs_replay", {"user_id": U}),
    ])

    # ---- S8: чистый выбор цели ----
    reset()
    base = {"id": "g1", "severity": 0.5, "status": "registered", "description": "Ты глупая железка", "created_at": "2026-03-01 10:00:00", "updated_at": "2026-03-01 10:00:00"}
    mk = lambda i, d, s=0.5, st="registered", c="2026-03-01 10:00:00", u="2026-03-01 10:00:00": {"id": i, "severity": s, "status": st, "description": d, "created_at": c, "updated_at": u}  # noqa: E731
    cases = [
        ("пусто", "Прости", [], []),
        ("единственная", "Извини меня", [base], []),
        ("явная ссылка", "прости, что назвал глупой", [mk("a", "Ты глупая железка"), mk("b", "Ты медленная")], []),
        ("ничья: новее", "прости за картошку", [mk("a", "про картошку и кабачки", u="2026-03-01 09:00:00"), mk("b", "про картошку и кабачки", u="2026-03-01 10:30:00")], []),
        ("ничья: тяжелее", "прости за картошку", [mk("a", "про картошку и кабачки", 0.3), mk("b", "про картошку и кабачки", 0.9)], []),
        ("ничья: id", "прости за картошку", [mk("b", "про картошку и кабачки"), mk("a", "про картошку и кабачки")], []),
        ("названа решённая", "прости за глупость железки", [mk("a", "совсем другое")], [mk("r", "глупость железки", 0.5, "forgiven")]),
        ("одна недавняя", "извини", [mk("a", "первое", c="2026-03-01 06:00:00", u="2026-03-01 06:00:00"), mk("b", "второе", c="2026-03-01 11:40:00", u="2026-03-01 11:40:00")], []),
        ("двусмысленно", "извини", [mk("a", "первое", c="2026-03-01 11:40:00", u="2026-03-01 11:40:00"), mk("b", "второе", c="2026-03-01 11:50:00", u="2026-03-01 11:50:00")], []),
        ("ровно час назад", "извини", [mk("a", "первое", c="2026-03-01 11:00:00", u="2026-03-01 11:00:00"), mk("b", "второе", c="2026-03-01 05:00:00", u="2026-03-01 05:00:00")], []),
        ("healing: время по created_at", "извини", [mk("a", "первое", st="healing", c="2026-03-01 06:00:00", u="2026-03-01 11:55:00"), mk("b", "второе", c="2026-03-01 05:00:00", u="2026-03-01 05:00:00")], []),
        ("не registered → created_at", "извини", [mk("a", "первое", st="healing", c="2026-03-01 06:00:00", u="2026-03-01 11:55:00")], []),
        ("служебные слова", "извини прости сожалею", [mk("a", "первое"), mk("b", "второе")], []),
        ("латиница и ё", "sorry за Ёжика", [mk("a", "Ёжика обидел"), mk("b", "иное")], []),
    ]
    now = dt.datetime(2026, 3, 1, 12, 0, 0).replace(tzinfo=dt.timezone.utc).timestamp()
    for label, text, act, res in cases:
        a = {"text": text, "active": act, "resolved": res, "now": now}
        p = py_call("rm_match_grievance_target", a)
        r = rs_call("rm_match_grievance_target", a)
        n += 1
        check(f"S8 {label}", canon(p.get("ok")) == canon(r.get("ok")), f"\n py={p}\n rs={r}")

    print(f"\n(сценариев: {n}; успешных проверок: {_OK} из {_OK + len(FAILURES)})")
    print("=" * 72)
    ac.execute("DROP DATABASE IF EXISTS yandi_rel_parity")
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
