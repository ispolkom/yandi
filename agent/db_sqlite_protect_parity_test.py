"""Утилита защиты личного журнала на Rust (rustlib/yandi_db/src/protect.rs) против agent/db/sql/protect.py на НАСТОЯЩЕМ MySQL.

Одинаковый журнал наполняется на обеих сторонах; затем seal / unseal / status / backup / restore выполняются обеими: результаты (число значений, режим, отпечаток содержимого),
строки журнала работы и отказы совпадают. Резервная копия одной стороны восстанавливается другой. Нужен личный MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ, иначе SKIP."""
from __future__ import annotations

import json
import os
import sys
import tempfile
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


def main() -> int:
    target = os.environ.get("YANDI_PARITY_MYSQL", "")
    if not target:
        print("SKIP: не задан YANDI_PARITY_MYSQL (личный тестовый MySQL)")
        return 0
    try:
        import yandi_db
    except ImportError as e:
        print(f"SKIP: yandi_db не собран ({e})")
        return 0
    import pymysql

    import agent.db.sql.protect as P
    import agent.db.sql.repositories as repo
    import agent.db.sql.schema as S
    from agent.db.sql import field_protection as fp
    from agent.db.sql.security_triggers import immutability_triggers

    host, port = target.rsplit(":", 1)
    kw = dict(host=host, port=int(port), user="root", password="", autocommit=True, cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")
    admin = pymysql.connect(**kw)
    ac = admin.cursor()
    ac.execute("DROP DATABASE IF EXISTS yandi_protect_parity")
    ac.execute("CREATE DATABASE yandi_protect_parity CHARACTER SET utf8mb4")
    conn = pymysql.connect(database="yandi_protect_parity", **kw)
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
    tables = [n for n, _ in S.ALL_TABLES_IN_ORDER]
    state = {"h": None}

    def reset():
        fp.clear_key(); fp.forget_mode(); yandi_db.fp_clear_key(); yandi_db.fp_forget_mode()
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            cur.execute(f"TRUNCATE TABLE {t}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if state["h"] is not None:
            yandi_db.close(state["h"])
        state["h"] = yandi_db.open_memory()

    def both_repo(func, **kwargs):
        p = getattr(repo, func)(conn, **kwargs)
        r = json.loads(yandi_db.call(state["h"], func, json.dumps(kwargs, ensure_ascii=False)))
        assert "ok" in r, r
        return p, r["ok"]

    def rs_protect(name, **args):
        return json.loads(yandi_db.protect_call(state["h"], name, json.dumps(args)))

    def py_protect(fn, *a):
        logs: list[str] = []
        try:
            return {"ok": fn(conn, *a, log=logs.append), "log": logs}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}", "log": logs}

    KEY = bytes(range(1, 33))
    T = [f"2026-03-01 10:0{i}:00" for i in range(8)]

    def populate():
        both_repo("record_interaction_turn", user_id="u1", source_turn_id="t1", turn_id_origin="client", user_text="Привет, я обещаю прийти", assistant_text="Хорошо 🌍", created_at=T[0])
        both_repo("record_interaction_turn", user_id="u1", source_turn_id="t2", turn_id_origin="client", user_text="yp1:как запечатанное", assistant_text=None, created_at=T[1])
        both_repo("record_interaction_turn", user_id="u1", source_turn_id="t3", turn_id_origin="client", user_text="yp0:ещё", assistant_text="", created_at=T[2])
        both_repo("record_interaction_turn", user_id="u2", source_turn_id="t1", turn_id_origin="client", user_text="я" * 700, assistant_text="ответ", created_at=T[2])
        both_repo("insert_personal_fact", fact_id="f1", user_id="u1", fact_class="identity", statement="Меня зовут Аня", polarity="affirm", temporality="current", evidence="Я Аня", span_start=0, span_end=5, source_turn_id="t1", created_at=T[0])
        both_repo("insert_personal_fact_event", fact_id="f1", user_id="u1", event_type="restated", by_fact_id=None, evidence="снова", span_start=None, span_end=None, source_turn_id="t2", created_at=T[1])
        both_repo("record_commitment", commitment_id="c1", user_id="u1", kind="promise", text="Приду", evidence="обещаю", due_at=T[5], created_at=T[0], source_turn_id="t1")
        both_repo("record_commitment_event", commitment_id="c1", user_id="u1", event_type="fulfilled", source="user", evidence="пришёл", created_at=T[2], source_turn_id="t2", span_start=0, span_end=3)
        both_repo("record_commitment_event", commitment_id="c1", user_id="u1", event_type="broken", source="system", evidence=None, created_at=T[3])
        both_repo("record_grievance", grievance_id="g1", user_id="u1", event_type="insult", description="Секретное слово", severity=0.5, context={"a": "б"}, created_at=T[0])
        both_repo("record_grievance", grievance_id="g2", user_id="u1", event_type="x", description="yp1:подделка", severity=0.5, created_at=T[1])

    def rd(func, **kwargs):
        return (lambda p, r: json.dumps(p, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(r, ensure_ascii=False, sort_keys=True, default=str))(*both_repo(func, **kwargs))

    def keyed():
        fp.install_key(KEY); yandi_db.fp_install_key(KEY.hex())

    def unkey():
        fp.clear_key(); fp.forget_mode(); yandi_db.fp_clear_key(); yandi_db.fp_forget_mode()

    def same_result(label, p, r):
        if "error" in p or "error" in r:
            ok = "error" in p and "error" in r and p["error"].split(":")[0] == r["error"].split(":")[0] and p["error"].split(": ", 1)[-1] == r["error"].split(": ", 1)[-1]
        else:
            ok = json.dumps(p["ok"], sort_keys=True, ensure_ascii=False) == json.dumps(r["ok"], sort_keys=True, ensure_ascii=False)
        check(f"{label}: результат", ok, f"\n py={json.dumps(p, ensure_ascii=False)[:600]}\n rs={json.dumps(r, ensure_ascii=False)[:600]}")
        check(f"{label}: журнал работы", p.get("log") == r.get("log"), f"\n py={p.get('log')}\n rs={r.get('log')}")

    def statuses():
        return P.status(conn), rs_protect("status")["ok"]

    def reads():
        return all([rd("list_personal_facts", user_id="u1"), rd("list_commitments", user_id="u1"), rd("list_commitment_events", user_id="u1"),
                    rd("list_active_grievances", user_id="u1"), rd("list_recent_interaction_turns", user_id="u1"), rd("list_recent_interaction_turns", user_id="u2"),
                    rd("get_interaction_turn_text", user_id="u1", source_turn_id="t2"), rd("get_interaction_turn_text", user_id="u1", source_turn_id="t3")])

    # ---- цикл seal / unseal ----
    reset(); populate()
    st_p, st_r = statuses()
    check("A0 состояние до: status", json.dumps(st_p, sort_keys=True) == json.dumps(st_r, sort_keys=True), f"{st_p} {st_r}")
    unkey()
    p = py_protect(P.seal_all, KEY); r = rs_protect("seal", key=KEY.hex())
    same_result("A1 seal", p, r)
    st_p, st_r = statuses()
    check("A2 после seal: status", json.dumps(st_p, sort_keys=True) == json.dumps(st_r, sort_keys=True), f"{st_p} {st_r}")
    keyed()
    check("A3 после seal: чтение через репозитории (значения равны)", reads())
    p = py_protect(P.seal_all, KEY); r = rs_protect("seal", key=KEY.hex())
    same_result("A4 повторный seal: ничего не меняется", p, r)
    # то, что запечатал Rust, открывает Python, и наоборот
    cur.execute("SELECT id AS k, description AS v FROM grievance")
    pv = {x["k"]: x["v"] for x in cur.fetchall()}
    rv = {x["k"]: x["v"] for x in json.loads(yandi_db.query(state["h"], "SELECT id AS k, description AS v FROM grievance"))}
    for k in pv:
        try:
            a_ = fp.open_with(fp.storage_key(), "grievance", "description", {"id": k}, pv[k])
            b_ = fp.open_with(fp.storage_key(), "grievance", "description", {"id": k}, rv[k])
            check(f"A5 перекрёстное открытие grievance.description[{k}]", a_ == b_, f"{a_!r} {b_!r}")
        except Exception as e:  # noqa: BLE001
            check(f"A5 перекрёстное открытие grievance.description[{k}]", False, repr(e))
    unkey()
    p = py_protect(P.unseal_all, KEY); r = rs_protect("unseal", key=KEY.hex())
    same_result("A6 unseal", p, r)
    st_p, st_r = statuses()
    check("A7 после unseal: status", json.dumps(st_p, sort_keys=True) == json.dumps(st_r, sort_keys=True), f"{st_p} {st_r}")
    check("A8 после unseal: чтение через репозитории", reads())
    p = py_protect(P.unseal_all, KEY); r = rs_protect("unseal", key=KEY.hex())
    same_result("A9 повторный unseal", p, r)

    # ---- отказы ----
    unkey()
    p = py_protect(P.seal_all, KEY); r = rs_protect("seal", key=KEY.hex())
    same_result("B0 seal (снова)", p, r)
    unkey()
    wrong = bytes(range(50, 82))
    p = py_protect(P.seal_all, wrong); r = rs_protect("seal", key=wrong.hex())
    same_result("B1 seal чужим ключом при включённой защите", p, r)
    p = py_protect(P.unseal_all, wrong); r = rs_protect("unseal", key=wrong.hex())
    same_result("B2 unseal чужим ключом", p, r)
    unkey()
    # чужой UPDATE-триггер: остановка до изменений
    cur.execute("CREATE TRIGGER trg_evil BEFORE UPDATE ON grievance FOR EACH ROW SET NEW.severity = NEW.severity")
    yandi_db.exec_sql(state["h"], "CREATE TRIGGER trg_evil BEFORE UPDATE ON grievance BEGIN SELECT 1; END", "[]")
    p = py_protect(P.unseal_all, KEY); r = rs_protect("unseal", key=KEY.hex())
    same_result("B3 незнакомый UPDATE-триггер", p, r)
    cur.execute("DROP TRIGGER trg_evil"); yandi_db.exec_sql(state["h"], "DROP TRIGGER trg_evil", "[]")
    unkey()

    # ---- резервная копия и восстановление (в т.ч. крест-накрест) ----
    reset(); populate()
    tmp = Path(tempfile.mkdtemp(prefix="yandi-protect-"))
    unkey()
    py_protect(P.seal_all, KEY); rs_protect("seal", key=KEY.hex())
    unkey()
    bp, br = tmp / "py.bk", tmp / "rs.bk"
    p = py_protect(lambda c, k, log: P.backup(c, k, str(bp), log=log), KEY)
    r = rs_protect("backup", key=KEY.hex(), path=str(br))
    same_result("C1 backup", p, r)
    check("C1 права файла 0600", (os.stat(bp).st_mode & 0o777) == 0o600 == (os.stat(br).st_mode & 0o777))
    check("C1 сигнатура файла", bp.read_bytes()[:8] == br.read_bytes()[:8] == b"YANDIBK1")
    # восстановление в непустое
    unkey()
    p = py_protect(lambda c, k, log: P.restore(c, k, str(bp), log=log), KEY)
    r = rs_protect("restore", key=KEY.hex(), path=str(br))
    same_result("C2 restore в непустые таблицы отказ", p, r)
    # крест-накрест в пустые таблицы: Rust читает копию Python, Python читает копию Rust
    for label, src_for_py, src_for_rs in (("C3 копии своей стороны", bp, br), ("C4 копии чужой стороны", br, bp)):
        reset()
        unkey()
        p = py_protect(lambda c, k, log: P.restore(c, k, str(src_for_py), log=log), KEY)
        r = rs_protect("restore", key=KEY.hex(), path=str(src_for_rs))
        same_result(label, p, r)
        st_p, st_r = statuses()
        check(f"{label}: status после восстановления (режим)", json.dumps(st_p, sort_keys=True) == json.dumps(st_r, sort_keys=True) and st_p["mode"] == "on", f"{st_p} {st_r}")
        keyed()
        check(f"{label}: чтение после восстановления", reads())
        unkey()
    # неверный ключ и не копия
    reset()
    unkey()
    p = py_protect(lambda c, k, log: P.restore(c, k, str(bp), log=log), wrong)
    r = rs_protect("restore", key=wrong.hex(), path=str(br))
    same_result("C5 restore чужим ключом", p, r)
    junk = tmp / "junk.bk"
    junk.write_bytes(b"not a backup at all, just some bytes here....")
    p = py_protect(lambda c, k, log: P.restore(c, k, str(junk), log=log), KEY)
    r = rs_protect("restore", key=KEY.hex(), path=str(junk))
    same_result("C6 restore: это не копия", p, r)

    print(f"\n(успешных проверок: {_OK} из {_OK + len(FAILURES)})")
    print("=" * 72)
    ac.execute("DROP DATABASE IF EXISTS yandi_protect_parity")
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
