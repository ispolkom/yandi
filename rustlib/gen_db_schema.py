#!/usr/bin/env python3
"""Генератор схемы SQLite для yandi_db из ЕДИНСТВЕННОГО источника — Python-схемы MySQL (agent/db/sql/schema.py + security_triggers.py).
Ничего не переписывается руками: типы, ключи, ENUM→CHECK, ALTER'ы (сложены в итоговые CREATE, версия схемы v20), триггеры неизменяемости.
Запуск: python3 rustlib/gen_db_schema.py   (пишет rustlib/yandi_db/src/schema_sqlite.sql, triggers_sqlite.sql, schema_meta.json)"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent.db.sql.schema as S  # noqa: E402

OUT = ROOT / "rustlib" / "yandi_db" / "src"


def strip_comments(s: str) -> str:
    out = []
    for line in s.split("\n"):
        res, q, i = "", False, 0
        while i < len(line):
            ch = line[i]
            if ch == "'":
                q = not q
            if not q and line.startswith("--", i):
                break
            res += ch
            i += 1
        out.append(res.rstrip())
    return "\n".join(out)


def split_top(body: str):
    items, depth, cur, q = [], 0, "", False
    for ch in body:
        if ch == "'":
            q = not q
        if not q:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                items.append(cur.strip())
                cur = ""
                continue
        cur += ch
    if cur.strip():
        items.append(cur.strip())
    return items


class Table:
    def __init__(self, name):
        self.name = name
        self.cols = []          # (name, sqlite_def, notnull, pk)
        self.constraints = []   # текст табличных ограничений
        self.indexes = []       # (unique, name, cols)


def sqlite_type(t: str) -> str:
    t = t.upper()
    if t in ("BIGINT", "INT", "TINYINT", "SMALLINT", "BOOLEAN"):
        return "INTEGER"
    if t in ("FLOAT", "DOUBLE"):
        return "REAL"
    if t.startswith("VARBINARY") or t.startswith("BINARY") or "BLOB" in t:
        return "BLOB"
    return "TEXT"  # VARCHAR/CHAR/TEXT/MEDIUMTEXT/JSON/DATETIME (ISO-текст, как у MySQL)


def parse_column(item: str):
    m = re.match(r"(\w+)\s+(.*)$", item, re.S)
    name, rest = m.group(1), m.group(2).strip()
    rest = re.sub(r"\s+", " ", rest)
    enum = None
    em = re.match(r"ENUM\((.*?)\)(.*)$", rest)
    if em:
        enum, rest = em.group(1), "ENUM" + em.group(2)
    tm = re.match(r"(\w+)(\([\d, ]+\))?\s*(.*)$", rest)
    base, tail = tm.group(1), tm.group(3)
    tail = re.sub(r"CHARACTER SET \w+", "", tail)
    tail = re.sub(r"COLLATE \w+", "", tail)
    tail = re.sub(r"\bUNSIGNED\b", "", tail).strip()
    tail = re.sub(r"\s+", " ", tail)
    notnull = "NOT NULL" in tail
    pk = "PRIMARY KEY" in tail
    auto = "AUTO_INCREMENT" in tail
    typ = sqlite_type(base)
    if auto:
        assert pk and typ == "INTEGER", item
        return name, f"{name} INTEGER PRIMARY KEY AUTOINCREMENT", True, True
    if pk and not notnull:
        # в SQLite (в отличие от MySQL) первичный ключ не INTEGER допускает NULL — запрещаем явно
        tail = ("NOT NULL " + tail).strip()
        notnull = True
    d = f"{name} {typ} {tail}".strip()
    if enum:
        d += f" CHECK ({name} IN ({enum}))"
    return name, d, notnull or pk, pk


def build_tables():
    tables = {}
    for n, s in S.ALL_TABLES_IN_ORDER:
        s = strip_comments(s)
        m = re.search(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*)\)\s*ENGINE=", s, re.S)
        assert m, n
        t = Table(n)
        for it in split_top(m.group(2)):
            w = it.split()[0].upper()
            if w == "KEY":
                km = re.match(r"KEY (\w+) \((.*)\)$", it, re.S)
                t.indexes.append((False, km.group(1), re.sub(r"\s+", " ", km.group(2))))
            elif w == "UNIQUE":
                km = re.match(r"UNIQUE KEY (\w+) \((.*)\)$", it, re.S)
                t.indexes.append((True, km.group(1), re.sub(r"\s+", " ", km.group(2))))
            elif w in ("CONSTRAINT", "PRIMARY", "FOREIGN", "CHECK"):
                t.constraints.append(re.sub(r"\s+", " ", it))
            else:
                t.cols.append(parse_column(it))
        tables[n] = t
    # ALTER'ы складываем в итоговые определения (свежая установка сразу получает схему последней версии)
    for _, a in S.ALTER_STATEMENTS_IN_ORDER:
        a = re.sub(r"\s+", " ", a.strip().rstrip(";"))
        m = re.match(r"ALTER TABLE (\w+) (.*)$", a)
        tn, rest = m.group(1), m.group(2)
        t = tables[tn]
        if rest.startswith("ADD COLUMN "):
            col = parse_column(rest[len("ADD COLUMN "):])
            if col[0] not in [c[0] for c in t.cols]:  # в CREATE уже есть — ALTER нужен только старым базам
                t.cols.append(col)
        elif rest.startswith("ADD CONSTRAINT "):
            t.constraints.append(rest[len("ADD "):])
        elif rest.startswith("ADD KEY "):
            km = re.match(r"ADD KEY (\w+) \((.*)\)$", rest)
            t.indexes.append((False, km.group(1), km.group(2)))
        elif rest.startswith("MODIFY COLUMN "):
            pass  # MEDIUMTEXT → TEXT: у SQLite длина не ограничена, определение не меняется
        else:
            raise SystemExit(f"неизвестный ALTER: {a}")
    return tables


def gen_schema(tables) -> str:
    out = ["-- ГЕНЕРИРУЕТСЯ rustlib/gen_db_schema.py из agent/db/sql/schema.py (v%d). Руками не править." % S.SCHEMA_VERSION, ""]
    seen = set()
    for t in tables.values():
        body = [c[1] for c in t.cols] + t.constraints
        out.append(f"CREATE TABLE IF NOT EXISTS {t.name} (\n    " + ",\n    ".join(body) + "\n);")
        done = set()
        for uniq, iname, cols in t.indexes:
            if (iname, cols) in done:
                continue  # тот же индекс объявлен и в CREATE, и в ALTER (миграция v13 для старых баз)
            done.add((iname, cols))
            assert iname not in seen, f"повтор имени индекса {iname}"
            seen.add(iname)
            out.append(f"CREATE {'UNIQUE ' if uniq else ''}INDEX IF NOT EXISTS {iname} ON {t.name} ({cols});")
        out.append("")
    return "\n".join(out)


def sq(msg: str) -> str:
    return msg.replace("'", "''")


def gen_triggers() -> str:
    cls = S.TABLE_CLASSIFICATION
    names = [n for n, _ in S.ALL_TABLES_IN_ORDER]
    out = ["-- ГЕНЕРИРУЕТСЯ rustlib/gen_db_schema.py из agent/db/sql/security_triggers.py. Руками не править.", ""]

    def reject(table, op):
        return (f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{op.lower()}\nBEFORE {op} ON {table}\nBEGIN\n"
                f"  SELECT RAISE(ABORT, '{sq(table)} is immutable (YANDI SQL BASTION): {op} forbidden');\nEND;")

    for t in names:
        c = cls.get(t)
        if c in ("A", "B"):
            out += [reject(t, "UPDATE"), reject(t, "DELETE")]
        elif c == "C":
            out.append(reject(t, "DELETE"))
        elif c == "D":
            if t == "verification_run":
                out.append(VERIFICATION_RUN_GUARD)
                out.append(reject(t, "DELETE"))
            else:
                out += [reject(t, "UPDATE"), reject(t, "DELETE")]
    return "\n".join(out) + "\n"


VERIFICATION_RUN_GUARD = """CREATE TRIGGER IF NOT EXISTS trg_verification_run_guard_update
BEFORE UPDATE ON verification_run
BEGIN
  SELECT RAISE(ABORT, 'verification_run: run_id/occurrence_id/started_at are immutable')
    WHERE NEW.run_id <> OLD.run_id OR NEW.occurrence_id <> OLD.occurrence_id OR NEW.started_at <> OLD.started_at;
  SELECT RAISE(ABORT, 'verification_run: status is already terminal, no further transition allowed')
    WHERE OLD.status <> 'running';
  SELECT RAISE(ABORT, 'verification_run: only running -> a terminal status is an allowed transition')
    WHERE NEW.status NOT IN ('completed', 'aborted', 'failed');
  SELECT RAISE(ABORT, 'verification_run: pipeline_version/web_enabled/validation_enabled/schema_version are write-once, set only at start_run() time')
    WHERE NEW.pipeline_version IS NOT OLD.pipeline_version OR NEW.web_enabled IS NOT OLD.web_enabled
       OR NEW.validation_enabled IS NOT OLD.validation_enabled OR NEW.schema_version IS NOT OLD.schema_version;
  SELECT RAISE(ABORT, 'verification_run: final_answer_id must belong to THIS run''s own question')
    WHERE NEW.final_answer_id IS NOT NULL AND (
      (SELECT question_id FROM answer_version WHERE answer_id = NEW.final_answer_id) IS NULL
      OR (SELECT question_id FROM answer_version WHERE answer_id = NEW.final_answer_id)
         <> (SELECT question_id FROM question_occurrence WHERE occurrence_id = NEW.occurrence_id));
END;"""


def main():
    tables = build_tables()
    (OUT / "schema_sqlite.sql").write_text(gen_schema(tables), encoding="utf-8")
    (OUT / "triggers_sqlite.sql").write_text(gen_triggers(), encoding="utf-8")
    meta = {
        "schema_version": S.SCHEMA_VERSION,
        "tables": {t.name: {"class": S.TABLE_CLASSIFICATION.get(t.name),
                            "columns": [{"name": c[0], "notnull": c[2], "pk": c[3]} for c in t.cols]} for t in tables.values()},
    }
    (OUT / "schema_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"таблиц: {len(tables)}, колонок: {sum(len(t.cols) for t in tables.values())}, индексов: {sum(len(t.indexes) for t in tables.values())}")


if __name__ == "__main__":
    main()
