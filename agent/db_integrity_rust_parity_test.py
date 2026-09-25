"""Журнал целостности на Rust (rustlib/yandi_db/src/integrity.rs) против agent/db/sql/integrity.py: канонические байты, хеши, цепочки (в т.ч. подделанные),
контрольные точки (файлы, созданные одной реализацией, читает и проверяет другая), обнаружение отката. Не требует MySQL."""
from __future__ import annotations

import copy
import json
import os
import random
import sys
import tempfile
import time
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
    try:
        import yandi_db
    except ImportError as e:
        print(f"SKIP: yandi_db не собран ({e})")
        return 0
    from agent.db.sql import integrity as I

    def rs(name, **args):
        return json.loads(yandi_db.integrity_call(name, json.dumps(args, ensure_ascii=False)))

    def py(fn, *a, **kw):
        try:
            return {"ok": fn(*a, **kw)}
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def same(label, p, r):
        if "error" in p or "error" in r:
            ok = "error" in p and "error" in r and str(r["error"]).split(":")[0] == p["error"]
        else:
            ok = json.dumps(p["ok"], ensure_ascii=False, sort_keys=True) == json.dumps(r["ok"], ensure_ascii=False, sort_keys=True)
        check(label, ok, f"\n py={json.dumps(p, ensure_ascii=False)[:400]}\n rs={json.dumps(r, ensure_ascii=False)[:400]}")

    KEY = bytes(range(32))
    KH = KEY.hex()
    rnd = random.Random(20260925)

    # ---- значения ----
    vals = [None, True, False, 0, 1, -1, 2**40, "", "строка", "🌍", 0.0, -0.0, 0.1, 1.5, 1e-7, 123456789.123456789, 1e22, 2.5e-6, 0.0000005, 0.0000015, [1], {"a": 1}, [], {}]
    for v in vals:
        same(f"A1 canonicalize_value {v!r}", py(I.canonicalize_value, v), rs("canonicalize_value", value=v))

    # ---- записи ----
    strs = ["", "a", "Привет", "🌍 emoji", 'кавычки " и \\ слэш', "перевод\nстроки\tтаб\r", "\x00\x01\x1f\x7f", "  ", "é" + "́", "tab\u000b"]
    keys = ["a", "B", "б", "Z", "aa", "é", "🌍", "ÿ", "￿", "k k", ""]
    n_rec = 0
    for i in range(300):
        fields = {}
        for _ in range(rnd.randint(0, 6)):
            k = rnd.choice(keys)
            fields[k] = rnd.choice([None, True, False, rnd.randint(-10**12, 10**12), rnd.random() * rnd.choice([1, 1e6, 1e-6]), rnd.choice(strs)])
        eid = rnd.choice(["e1", 7, "юникод", "🌍"])
        et = rnd.choice(["question", "answer", "тип"])
        fv = rnd.choice([1, 1, 2])
        p = py(I.canonicalize_record, fields, entity_type=et, entity_id=eid, format_version=fv)
        if "ok" in p:
            p = {"ok": p["ok"].decode("utf-8")}
        same(f"B1 canonicalize_record #{i}", p, rs("canonicalize_record", fields=fields, entity_type=et, entity_id=eid, format_version=fv))
        n_rec += 1
    same("B2 запись с неподдерживаемым значением", py(I.canonicalize_record, {"a": [1]}, entity_type="t", entity_id=1), rs("canonicalize_record", fields={"a": [1]}, entity_type="t", entity_id=1))

    # ---- хеши ----
    for i in range(50):
        ph = I.payload_hash(f"x{i}".encode())
        prev = rnd.choice([I.GENESIS_HASH, ph])
        same(f"C1 compute_event_hash #{i}", py(I.compute_event_hash, KEY, 1, "t", i, ph, prev), rs("compute_event_hash", key=KH, format_version=1, entity_type="t", entity_id=i, payload_hash=ph, previous=prev))
    same("C2 пустой ключ", py(I.compute_event_hash, b"", 1, "t", 1, "a", "b"), rs("compute_event_hash", key="", format_version=1, entity_type="t", entity_id=1, payload_hash="a", previous="b"))

    # ---- цепочки ----
    chain_py: list = []
    chain_rs: list = []
    for i in range(12):
        f = {"i": i, "text": rnd.choice(strs), "score": rnd.random()}
        e_py = I.append_event(KEY, chain_py, entity_type="q", entity_id=i, fields=f)
        r = rs("append_event", key=KH, chain=chain_rs, entity_type="q", entity_id=i, fields=f)
        check(f"D1 append_event #{i}", r.get("ok") == e_py, f"\n py={e_py}\n rs={r}")
        chain_py.append(e_py)
        chain_rs.append(r.get("ok"))
    same("D2 verify_chain целая", py(I.verify_chain, KEY, chain_py), rs("verify_chain", key=KH, events=chain_py))
    tampered = []
    c = copy.deepcopy(chain_py); del c[5]; tampered.append(("удалено звено", c))
    c = copy.deepcopy(chain_py); c[2], c[3] = c[3], c[2]; tampered.append(("переставлены", c))
    c = copy.deepcopy(chain_py); c[4]["event_hash"] = "f" * 64; tampered.append(("подделан хеш", c))
    c = copy.deepcopy(chain_py); c[6]["entity_id"] = "иной"; tampered.append(("подделан entity_id", c))
    c = copy.deepcopy(chain_py); c[0]["payload_hash"] = "0" * 64; tampered.append(("подделан payload", c))
    c = copy.deepcopy(chain_py); c = c[:3] + c[4:] ; tampered.append(("вырезано звено 3", c))
    tampered.append(("пустая цепочка", []))
    for label, c in tampered:
        p = py(I.verify_chain, KEY, c)
        if "ok" in p:
            p = {"ok": [p["ok"][0], p["ok"][1]]}
        same(f"D3 verify_chain: {label}", p, rs("verify_chain", key=KH, events=c))
    same("D4 verify_chain чужим ключом", py(I.verify_chain, bytes(range(1, 33)), chain_py), rs("verify_chain", key=bytes(range(1, 33)).hex(), events=chain_py))
    for i in (0, 5, 11):
        f = {"i": i, "text": "иначе", "score": 0.5}
        same(f"D5 verify_record_against_event #{i}", py(I.verify_record_against_event, f, chain_py[i], entity_type="q", entity_id=i), rs("verify_record_against_event", fields=f, event=chain_py[i], entity_type="q", entity_id=i))

    # ---- контрольные точки: файлы обеих реализаций взаимно читаются ----
    tmp = Path(tempfile.mkdtemp(prefix="yandi-integrity-"))
    real_time = time.time
    time.time = lambda: 1767225600.7
    try:
        p1 = tmp / "py" / "cp.json"
        p2 = tmp / "rs" / "cp.json"
        cp_py = I.make_checkpoint("q", chain_py, str(p1))
        cp_rs = rs("make_checkpoint", entity_type="q", events=chain_py, path=str(p2), now=1767225600)
        check("E1 контрольная точка: содержимое", cp_rs.get("ok") == cp_py, f"{cp_py} {cp_rs}")
        check("E1 контрольная точка: текст файла", p1.read_text() == p2.read_text(), f"{p1.read_text()!r} {p2.read_text()!r}")
        check("E1 каталог 0700", (os.stat(p2.parent).st_mode & 0o777) == 0o700)
        same("E2 Rust читает точку Python", {"ok": I.load_checkpoint(str(p1))}, rs("load_checkpoint", path=str(p1)))
        same("E2 Rust читает точку Rust", {"ok": I.load_checkpoint(str(p2))}, rs("load_checkpoint", path=str(p2)))
        same("E2 нет файла", {"ok": I.load_checkpoint(str(tmp / "нет"))}, rs("load_checkpoint", path=str(tmp / "нет")))
        same("E3 пустая цепочка", py(I.make_checkpoint, "q", [], str(tmp / "x.json")), rs("make_checkpoint", entity_type="q", events=[], path=str(tmp / "x.json"), now=0))
        scen = [("ok", chain_py), ("ahead", chain_py + [I.append_event(KEY, chain_py, entity_type="q", entity_id=99, fields={"x": 1})]),
                ("отстаёт", chain_py[:-2]), ("пусто", []), ("расхождение", chain_py[:-1] + [dict(chain_py[-1], event_hash="a" * 64)])]
        for label, cur in scen:
            for path in (p1, p2):
                p = py(I.check_for_rollback, "q", cur, str(path))
                if "ok" in p:
                    p = {"ok": list(p["ok"])}
                same(f"E4 check_for_rollback: {label} ({path.parent.name})", p, rs("check_for_rollback", current_events=cur, path=str(path)))
        same("E4 без контрольной точки", {"ok": list(I.check_for_rollback("q", chain_py, str(tmp / "нет")))}, rs("check_for_rollback", current_events=chain_py, path=str(tmp / "нет")))
    finally:
        time.time = real_time

    print(f"\n(записей: {n_rec}; успешных проверок: {_OK} из {_OK + len(FAILURES)})")
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
