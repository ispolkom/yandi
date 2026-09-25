"""Схема SQLite для родного слоя БД (rustlib/yandi_db) против настоящего MySQL — по единому источнику agent/db/sql/schema.py.

Нужен ЛИЧНЫЙ тестовый MySQL: задайте YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ (root без пароля, база создаётся и удаляется тестом); без него — SKIP.
Сверяется: состав таблиц и колонок (порядок, NOT NULL/первичный ключ), поведение триггеров неизменяемости на реальных строках
(UPDATE/DELETE принимается или отвергается — по каждой таблице классов A/B/C/D), страж verification_run, внешние ключи, UNIQUE и ENUM/CHECK.
SQLite здесь — тот же движок, что вшит в бинарник (rusqlite bundled); проверяется именно сгенерированный SQL (rustlib/gen_db_schema.py).
"""
from __future__ import annotations

import os
import sqlite3
import sys
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
    import pymysql

    import agent.db.sql.schema as S
    from agent.db.sql.security_triggers import immutability_triggers

    host, port = target.rsplit(":", 1)
    my = pymysql.connect(host=host, port=int(port), user="root", password="", autocommit=True)
    cur = my.cursor()
    cur.execute("DROP DATABASE IF EXISTS yandi_parity")
    cur.execute("CREATE DATABASE yandi_parity CHARACTER SET utf8mb4")
    cur.execute("USE yandi_parity")
    for _, ddl in S.ALL_TABLES_IN_ORDER:
        cur.execute(ddl)
    for _, alter in S.ALTER_STATEMENTS_IN_ORDER:
        try:
            cur.execute(alter)
        except pymysql.err.OperationalError as e:
            if e.args[0] not in (1060, 1061):  # колонка/индекс уже есть в CREATE — норма для свежей базы
                raise
    for _, trg in immutability_triggers():
        cur.execute(trg)

    lite = sqlite3.connect(":memory:", isolation_level=None)
    gen = ROOT / "rustlib" / "yandi_db" / "src"
    lite.executescript((gen / "schema_sqlite.sql").read_text())
    lite.executescript((gen / "triggers_sqlite.sql").read_text())

    # ---- 1. состав таблиц и колонок ----
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='yandi_parity'")
    my_tables = sorted(r[0] for r in cur.fetchall())
    lt_tables = sorted(r[0] for r in lite.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    check("A1 состав таблиц", my_tables == lt_tables, f"{set(my_tables) ^ set(lt_tables)}")
    coltypes: dict[str, list] = {}
    for t in my_tables:
        cur.execute("SELECT column_name, is_nullable, column_key, data_type, column_type FROM information_schema.columns WHERE table_schema='yandi_parity' AND table_name=%s ORDER BY ordinal_position", (t,))
        mycols = cur.fetchall()
        coltypes[t] = mycols
        info = list(lite.execute(f"PRAGMA table_info({t})"))
        litecols = [(r[1], r[3] != 0 or (r[5] != 0 and r[2] == "INTEGER")) for r in info]  # INTEGER PRIMARY KEY (rowid) в SQLite не бывает NULL
        want = [(c[0], c[1] == "NO") for c in mycols]
        aff = {"int": "INTEGER", "bigint": "INTEGER", "tinyint": "INTEGER", "smallint": "INTEGER", "float": "REAL", "double": "REAL", "varbinary": "BLOB", "blob": "BLOB"}
        check(f"A5 типы {t}", [(r[1], r[2]) for r in info] == [(c[0], aff.get(c[3], "TEXT")) for c in mycols], f"\n mysql={[(c[0], c[3]) for c in mycols]}\n sqlite={[(r[1], r[2]) for r in info]}")
        check(f"A2 колонки {t}", litecols == want, f"\n mysql={want}\n sqlite={litecols}")
    # индексы: имена и уникальность
    cur.execute("SELECT table_name, index_name, non_unique FROM information_schema.statistics WHERE table_schema='yandi_parity' AND index_name<>'PRIMARY' GROUP BY table_name, index_name, non_unique")
    my_idx = {(r[0], r[1], r[2] == 0) for r in cur.fetchall()}
    my_idx = {i for i in my_idx if not i[1].startswith("fk_")}
    lt_idx, lt_inline = set(), set()
    for t in lt_tables:
        for r in lite.execute(f"PRAGMA index_list({t})"):
            cols = tuple(x[2] for x in lite.execute(f"PRAGMA index_info({r[1]})"))
            if r[3] == "c":
                lt_idx.add((t, r[1], bool(r[2])))
            elif r[3] == "u":
                lt_inline.add((t, cols))
    # безымянные UNIQUE у колонок: MySQL называет индекс по колонке
    cur.execute("SELECT table_name, index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index) FROM information_schema.statistics WHERE table_schema='yandi_parity' AND non_unique=0 AND index_name<>'PRIMARY' GROUP BY table_name, index_name")
    my_inline = {(r[0], tuple(r[2].split(","))) for r in cur.fetchall() if not r[1].startswith(("uq_", "fk_"))}
    my_named = {i for i in my_idx}
    check("A3 индексы (имя, уникальность)", {i for i in my_named if i[1].startswith(("idx_", "uq_"))} == lt_idx, f"\n только mysql={sorted(my_named - lt_idx)[:8]}\n только sqlite={sorted(lt_idx - my_named)[:8]}")
    check("A4 безымянные UNIQUE колонок", my_inline == lt_inline, f"{my_inline ^ lt_inline}")

    # ---- 2. триггеры неизменяемости на реальных строках ----
    def dummy_values(t):
        names, vals = [], []
        for name, nullable, key, dtype, ctype in coltypes[t]:
            lcol = list(lite.execute(f"PRAGMA table_info({t})"))
            pk_auto = any(r[1] == name and r[5] != 0 and r[2] == "INTEGER" and "PRIMARY KEY AUTOINCREMENT" in lite.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0].split(name)[1][:40] for r in lcol)
            has_default = [r for r in lcol if r[1] == name][0][4] is not None
            if pk_auto or (nullable == "YES") or (has_default and key != "PRI"):
                continue
            if dtype == "enum":
                v = ctype[5:-1].split(",")[0].strip("'")
            elif dtype in ("int", "bigint", "tinyint", "smallint", "float", "double"):
                v = 1
            elif dtype == "json":
                v = '"x"'
            elif dtype == "datetime":
                v = "2026-01-01 00:00:00"
            else:
                v = "x"
            names.append(name)
            vals.append(v)
        return names, vals

    def my_try(sql, args=()):
        try:
            cur.execute(sql, args)
            return "ok"
        except pymysql.err.Error as e:
            return {1644: "reject", 1452: "fk", 1062: "unique", 3819: "check", 1265: "check"}.get(e.args[0], f"other:{e.args[0]}")

    def lt_try(sql, args=()):
        try:
            lite.execute(sql, args)
            return "ok"
        except sqlite3.DatabaseError as e:
            m = str(e)
            if "immutable" in m or "verification_run" in m:
                return "reject"
            for key, tag in (("FOREIGN KEY", "fk"), ("UNIQUE", "unique"), ("CHECK", "check")):
                if key in m:
                    return tag
            return f"other:{m}"

    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    lite.execute("PRAGMA foreign_keys=OFF")
    cls = S.TABLE_CLASSIFICATION
    for t in my_tables:
        names, vals = dummy_values(t)
        ph = ",".join(["%s"] * len(vals))
        if names:
            ins_my = f"INSERT INTO {t} ({','.join(names)}) VALUES ({ph})"
            ins_lt = f"INSERT INTO {t} ({','.join(names)}) VALUES ({','.join('?' * len(vals))})"
        else:
            ins_my = ins_lt = None
        a = my_try(ins_my, vals) if ins_my else "skip"
        b = lt_try(ins_lt, vals) if ins_lt else "skip"
        check(f"B1 вставка строки-заглушки {t}", a == b == "ok", f"mysql={a} sqlite={b} {names}")
        c0 = coltypes[t][0][0]
        for label, sql in (("UPDATE", f"UPDATE {t} SET {c0} = {c0}"), ("DELETE", f"DELETE FROM {t}")):
            ra, rb = my_try(sql), lt_try(sql)
            check(f"B2 {label} {t} (класс {cls.get(t)})", ra == rb, f"mysql={ra} sqlite={rb}")
    cur.execute("SET FOREIGN_KEY_CHECKS=1")
    lite.execute("PRAGMA foreign_keys=ON")

    # ---- 3. страж verification_run, внешние ключи, UNIQUE, ENUM ----
    def both(label, sql_list):
        for db, run, tag in ((cur, my_try, "mysql"), (lite, lt_try, "sqlite")):
            pass
        res_my = [my_try(s) for s in sql_list]
        res_lt = [lt_try(s.replace("%%", "%")) for s in sql_list]
        check(label, res_my == res_lt, f"\n mysql={res_my}\n sqlite={res_lt}")

    for t in ("verification_run", "answer_version", "question_occurrence", "question"):
        cur.execute(f"DELETE FROM {t}") if False else None
    setup = [
        "INSERT INTO question (canonical_hash, first_asked_at) VALUES ('g1','2026-01-01 00:00:00'), ('g2','2026-01-01 00:00:00')",
        "INSERT INTO question_occurrence (question_id, raw_text, asked_at) VALUES (LAST_INSERT_ID_1, 'q1','2026-01-01 00:00:00')",
    ]
    # id-ы берём явно, чтобы обе базы были одинаковы
    for db_try in (my_try, lt_try):
        pass
    my_try("SET FOREIGN_KEY_CHECKS=0"); cur.execute("SET FOREIGN_KEY_CHECKS=0")
    for s in ("TRUNCATE TABLE " + t for t in ()):
        pass
    # чистые таблицы для сценариев: старые заглушки удалить нельзя (триггеры) — берём заведомо неиспользованные идентификаторы
    both("C0 подготовка", [
        "INSERT INTO question (question_id, canonical_hash, first_asked_at) VALUES (9001,'g1','2026-01-01 00:00:00'), (9002,'g2','2026-01-01 00:00:00')",
        "INSERT INTO question_occurrence (occurrence_id, question_id, raw_text, asked_at) VALUES (9001, 9001, 'q1','2026-01-01 00:00:00'), (9002, 9002, 'q2','2026-01-01 00:00:00')",
        "INSERT INTO verification_run (run_id, occurrence_id, started_at, pipeline_version) VALUES ('rg1', 9001, '2026-01-01 00:00:00', 'abc')",
    ])
    cur.execute("SET FOREIGN_KEY_CHECKS=1"); lite.execute("PRAGMA foreign_keys=ON")
    cols_av = [c[0] for c in coltypes["answer_version"]]
    both("C1 страж: идентичность", ["UPDATE verification_run SET run_id='zz' WHERE run_id='rg1'", "UPDATE verification_run SET occurrence_id=9002 WHERE run_id='rg1'"])
    both("C2 страж: статус", ["UPDATE verification_run SET status='running' WHERE run_id='rg1'", "UPDATE verification_run SET status='completed', pipeline_version='evil' WHERE run_id='rg1'",
                              "UPDATE verification_run SET status='completed', web_enabled=1 WHERE run_id='rg1'", "UPDATE verification_run SET status='completed', validation_enabled=1 WHERE run_id='rg1'", "UPDATE verification_run SET status='completed', schema_version=7 WHERE run_id='rg1'"])
    # ответ ЧУЖОГО вопроса
    names, vals = dummy_values("answer_version")
    def lit(v):
        return str(v) if isinstance(v, int) else "'" + str(v).replace("'", "''") + "'"
    row = dict(zip(names, vals))
    row.update({"answer_id": 9100, "question_id": 9002})
    row["created_by_run_id"] = "rg1"
    ins = f"INSERT INTO answer_version ({','.join(row)}) VALUES ({','.join(lit(v) for v in row.values())})"
    both("C3 ответ чужого вопроса вставляется", [ins])
    both("C4 страж: чужой final_answer_id", ["UPDATE verification_run SET status='completed', final_answer_id=9100 WHERE run_id='rg1'", "UPDATE verification_run SET status='completed', final_answer_id=424242 WHERE run_id='rg1'"])
    row.update({"answer_id": 9101, "question_id": 9001})
    both("C5 ответ своего вопроса", [f"INSERT INTO answer_version ({','.join(row)}) VALUES ({','.join(lit(v) for v in row.values())})", "UPDATE verification_run SET status='completed', completed_at='2026-01-02 00:00:00', final_answer_id=9101 WHERE run_id='rg1'"])
    both("C6 повторный переход и удаление", ["UPDATE verification_run SET status='failed' WHERE run_id='rg1'", "DELETE FROM verification_run WHERE run_id='rg1'"])
    both("C7 внешние ключи", ["INSERT INTO question_occurrence (question_id, raw_text, asked_at) VALUES (777777, 'x', '2026-01-01 00:00:00')"])
    both("C8 UNIQUE", ["INSERT INTO question (canonical_hash, first_asked_at) VALUES ('g1', '2026-01-02 00:00:00')"])
    both("C9 ENUM/CHECK", ["INSERT INTO verification_run (run_id, occurrence_id, started_at, status) VALUES ('rg2', 9001, '2026-01-01 00:00:00', 'bogus')",
                           "INSERT INTO instance_identity (id, instance_uuid, created_at, created_by_host) VALUES (2, 'u', '2026-01-01 00:00:00', 'h')"])

    print(f"\n(успешных проверок: {_OK} из {_OK + len(FAILURES)})")
    print("=" * 72)
    cur.execute("DROP DATABASE IF EXISTS yandi_parity")
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
