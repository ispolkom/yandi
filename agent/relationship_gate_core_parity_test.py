"""Шлюз отношений на Rust (rustlib/yandi_core/src/relationship_gate.rs) против agent/relationship_gate.py: решение, причина (форматирование чисел), мета, тексты ответов, запись в тайный архив. Базы не нужно."""
from __future__ import annotations

import itertools
import json
import random
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


def canon(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def main() -> int:
    try:
        import yandi_core
    except ImportError as e:
        print(f"SKIP: yandi_core не собран ({e})")
        return 0
    import agent.relationship_gate as rg

    h = yandi_core.open_memory()

    def rs(method, args):
        r = json.loads(yandi_core.call(h, "rg_call", json.dumps({"method": method, **args}, ensure_ascii=False)))
        return r["ok"] if "ok" in r else {"error": str(r["error"]).split(":")[0]}

    n = 0

    def decide(ctx, is_self=False):
        nonlocal n
        n += 1
        try:
            d, c, r, m = rg.decide_response(ctx, "q", is_self)
            p = [d, c, r, m]
        except Exception as e:  # noqa: BLE001
            p = {"error": type(e).__name__}
        r_ = rs("decide", {"context": ctx, "is_self_query": is_self})
        check(f"решение {json.dumps(ctx, ensure_ascii=False)[:120]} self={is_self}", canon(p) == canon(r_), f"\n py={canon(p)}\n rs={canon(r_)}")
        return p

    def apply(ctx, answer, decision, idx=0, archive=True):
        nonlocal n
        n += 1

        class Arch:
            def __init__(self):
                self.calls = []

            def archive_question(self, query, reason, context):
                self.calls.append({"query": query, "reason": reason, "context": context})

        a = Arch() if archive else None
        orig = rg.random.choice
        rg.random.choice = lambda seq: seq[idx]
        try:
            t, m = rg.apply_gate(ctx, answer, decision, a)
            p = {"text": t, "meta": m, "archive": (a.calls[0] if a and a.calls else None)}
        except Exception as e:  # noqa: BLE001
            p = {"error": type(e).__name__}
        finally:
            rg.random.choice = orig
        r_ = rs("apply", {"context": ctx, "answer": answer, "decision": decision, "pick": idx, "has_archive": archive})
        check(f"ответ {decision} idx={idx} archive={archive} len={len(answer)}", canon(p) == canon(r_), f"\n py={canon(p)[:600]}\n rs={canon(r_)[:600]}")

    irr = [0, 10, 29.99, 30, 55, 55.01, 60, 60.01, 70, 70.01, 75, 75.01, 85, 85.01, 100, 84.95, 55.25, 70.75, 0.05, 99.999]
    tr = [0, 29.99, 30, 50, 60, 60.01, 100]
    rp = [0, 29.99, 30, 60, 60.01, 100]
    ins = [0, 5, 6]
    for i, t, r, s in itertools.product(irr, tr, rp, ins):
        if random.Random(hash((i, t, r, s)) & 0xFFFF).random() < 0.55:
            continue
        decide({"irritation": i, "trust": t, "respect": r, "total_insults": s})
    for i in irr:
        decide({"irritation": i, "trust": 60.01, "respect": 60.01})
        decide({"irritation": i}, True)
    for ctx in ({}, {"trust": 10}, {"respect": 10}, {"irritation": True}, {"irritation": False, "trust": True}, {"irritation": None}, {"irritation": "10"}, {"irritation": 70, "total_insults": None},
                {"irritation": 65, "total_insults": "5"}, {"irritation": 65, "total_insults": 6}, {"irritation": 61, "total_insults": 5}, {"irritation": 10, "trust": "5"}, {"irritation": 10, "trust": 50, "respect": None},
                {"irritation": 10, "trust": 61, "respect": 61, "extra": [1, 2]}, {"irritation": 0.1 + 0.2}, {"irritation": 1e3}, {"irritation": -5, "trust": -1}, {"irritation": 10, "trust": 70, "respect": 70}, [1], "x", None):
        decide(ctx)
        decide(ctx, True)
    ctx = {"irritation": 72.5, "trust": 12, "extra": {"a": [1, "б"]}}
    for dec in ("answer_fully", "break", "know_but_not_tell", "answer_with_warning", "answer_guarded", "answer_brief", "нечто", ""):
        for ans in ("Ответ", "", "я" * 200, "я" * 201, "я" * 199, "Длинный " * 60):
            for idx in (0, 1, 2):
                for archive in (True, False):
                    if dec != "break" and idx > 0:
                        continue
                    apply(ctx, ans, dec, idx, archive)
    apply({}, "x", "know_but_not_tell")
    apply([1], "x", "answer_fully")

    print(f"\n(вызовов: {n}; успешных проверок: {_OK} из {_OK + len(FAILURES)})")
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
