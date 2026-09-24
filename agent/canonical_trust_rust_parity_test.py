"""
agent/canonical_trust_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/canonical_trust.rs даёт
ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/orchestrator/epistemic/canonical_trust.py::compute_canonical_trust (значения + строки лога).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Двадцать четвёртый шаг переноса Python -> Rust (2026-09-24). Проверяется: ВСЕ пары меток из таблицы рангов + неизвестные,
None и пустая строка на каждой стороне, пробельные/регистровые варианты, оба режима verbose (строки лога), значения других
типов (число, список — идут прежним Python-путём с тем же исходом), стороны с равным рангом (SUPPORTED/VERIFIED).
Контракт кроме этого теста: agent/epistemic_canonical_trust_shadow_regression_test.py.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.canonical_trust_rust_parity_test
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
_OK = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _OK
    if condition:
        _OK += 1
    else:
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    try:
        import yandi_rs.canonical_trust as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.canonical_trust").setLevel(logging.ERROR)
    import agent.orchestrator.epistemic.canonical_trust as ct
    from agent.orchestrator.epistemic.trust_gate import _TRUST_ORDER

    os.environ.pop("YANDI_CANONICAL_TRUST_ENGINE", None)
    ct._rust_ct = None

    def run(engine, a, b, verbose):
        saved = os.environ.get("YANDI_CANONICAL_TRUST_ENGINE")
        lines = []
        try:
            if engine:
                os.environ["YANDI_CANONICAL_TRUST_ENGINE"] = "rust"
            else:
                os.environ.pop("YANDI_CANONICAL_TRUST_ENGINE", None)
            ct._rust_ct = None
            try:
                return ct.compute_canonical_trust(a, b, lines.append, verbose), lines
            except Exception as e:                                # noqa: BLE001
                return f"EXC:{type(e).__name__}", lines
        finally:
            if saved is None:
                os.environ.pop("YANDI_CANONICAL_TRUST_ENGINE", None)
            else:
                os.environ["YANDI_CANONICAL_TRUST_ENGINE"] = saved
            ct._rust_ct = None

    vals = list(_TRUST_ORDER) + [None, "", " ", "UNKNOWN", "supported", "SUPPORTED ", "СУПЕР", "WEAKLY_SUPPORTED", 0, 5, ["X"], b"x", False, True]
    n = 0
    for a, b in itertools.product(vals, vals):
        for verbose in (False, True):
            p, r = run(False, a, b, verbose), run(True, a, b, verbose)
            n += 1
            check(f"canonical {a!r} {b!r} verbose={verbose}", p == r, f"python={p} rust={r}")

    saved = os.environ.get("YANDI_CANONICAL_TRUST_ENGINE")
    try:
        os.environ.pop("YANDI_CANONICAL_TRUST_ENGINE", None)
        ct._rust_ct = None
        check("G0 по умолчанию активен Python-движок", ct._get_rust_ct() is None)
        os.environ["YANDI_CANONICAL_TRUST_ENGINE"] = "rust"
        ct._rust_ct = None
        check("G1 переменная окружения переключает на Rust", ct._get_rust_ct() is rs)
        os.environ.pop("YANDI_CANONICAL_TRUST_ENGINE", None)
        ct._rust_ct = None
        check("G3 выключение возвращает Python (кэш не залипает)", ct._get_rust_ct() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_CANONICAL_TRUST_ENGINE", None)
        else:
            os.environ["YANDI_CANONICAL_TRUST_ENGINE"] = saved
        ct._rust_ct = None
    src = (ROOT / "agent" / "orchestrator" / "epistemic" / "canonical_trust.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(сравнений: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
