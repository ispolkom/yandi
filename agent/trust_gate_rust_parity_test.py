"""
agent/trust_gate_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/trust_gate.rs даёт ПОСТРОЧНО ТЕ ЖЕ
ответы, что и agent/orchestrator/epistemic/trust_gate.py (_apply_trust_cap, _calculate_delta_factors и вся
apply_epistemic_trust_adjustment сквозным вызовом: метка, trace.trust, trace.trust_reason, learning rules).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Двадцать третий шаг переноса Python -> Rust (2026-09-24). Методология — rustlib/README.md. Контракт модуля кроме этого
теста — существующие agent/trust_order_weakly_supported_regression_test.py, epistemic_canonical_trust_shadow_*,
orchestrator_modularization_*. Проверяется: таблица рангов (каждая метка + неизвестные), _apply_trust_cap на всех парах,
_calculate_delta_factors на сетке и фаззинге (округление `round(x,3)` на «ничьих», NaN/inf, питоновские min/max),
метка на ВСЕЙ сетке значимых измерений (домены, проверяемость, лимиты, границы покрытия 0.5/0.8 и поддержки 0.3/0.6,
форматирование `.2f` на «ничьих» 0.125/0.375…, убеждения: нет/пусто/низкие/высокие/исключение/не число), типы, на которых
Rust не участвует (строка вместо числа) — тот же исход, что у Python.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.trust_gate_rust_parity_test
"""
from __future__ import annotations

import itertools
import os
import random
import sys
import types
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


def norm(x):
    if isinstance(x, float) and x != x:
        return "NaN"
    if isinstance(x, dict):
        return {k: norm(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [norm(v) for v in x]
    return x


def main() -> int:
    try:
        import yandi_rs.trust_gate as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.trust_gate").setLevel(logging.ERROR)
    import agent.orchestrator.epistemic.trust_gate as tg

    os.environ.pop("YANDI_TRUST_GATE_ENGINE", None)
    tg._rust_tg = None
    rng = random.Random(20260924)

    def run(fn, engine, *a, **k):
        saved = os.environ.get("YANDI_TRUST_GATE_ENGINE")
        try:
            if engine:
                os.environ["YANDI_TRUST_GATE_ENGINE"] = "rust"
            else:
                os.environ.pop("YANDI_TRUST_GATE_ENGINE", None)
            tg._rust_tg = None
            try:
                return norm(fn(*a, **k))
            except Exception as e:                            # noqa: BLE001
                return f"EXC:{type(e).__name__}"
        finally:
            if saved is None:
                os.environ.pop("YANDI_TRUST_GATE_ENGINE", None)
            else:
                os.environ["YANDI_TRUST_GATE_ENGINE"] = saved
            tg._rust_tg = None

    labels = list(tg._TRUST_ORDER) + ["UNKNOWN", "", "weakly_supported", "СУПЕР", "SUPPORTED ", "None"]

    # ── A. таблица рангов ──
    for lab in labels:
        check(f"A order {lab!r}", tg._TRUST_ORDER.get(lab, 0) == rs.trust_order(lab))
    # ── B. _apply_trust_cap на всех парах ──
    for a, b in itertools.product(labels, labels):
        check(f"B cap {a!r},{b!r}", run(tg._apply_trust_cap, False, a, b) == run(tg._apply_trust_cap, True, a, b))

    # ── C. _calculate_delta_factors ──
    verdicts = ["VERIFIED", "PARTIALLY_VERIFIED", "CONFLICT", "REJECTED", "TIMEOUT", "", "OTHER", "verified"]
    confs = [0, 1, 0.5, -1, 2, 0.0, -0.0, 0.0625, 0.125, 0.375, 0.6249999, float("nan"), float("inf"), float("-inf"), True, False, 0.333333, 0.7]
    for v, c, hs, (ag, tn) in itertools.product(verdicts, confs, [True, False, 0, 1, None, "x"], [(0, 0), (1, 2), (3, 7), (2, 3), (5, 5), (7, 0), (1, 1000), (0, 3), (1, 8), (True, 2)]):
        a = run(tg._calculate_delta_factors, False, v, c, hs, ag, tn)
        b = run(tg._calculate_delta_factors, True, v, c, hs, ag, tn)
        check(f"C delta {v!r} {c!r} {hs!r} {ag!r}/{tn!r}", a == b, f"python={a} rust={b}")
    for _ in range(6000):
        v, c = rng.choice(verdicts), rng.choice([rng.random(), rng.uniform(-2, 3), rng.randint(-3, 3), rng.choice(confs)])
        tn = rng.choice([0, rng.randint(1, 50)])
        ag = rng.randint(0, 60)
        hs = rng.random() < 0.5
        a = run(tg._calculate_delta_factors, False, v, c, hs, ag, tn)
        b = run(tg._calculate_delta_factors, True, v, c, hs, ag, tn)
        check(f"C delta fuzz {v!r} {c!r} {ag}/{tn}", a == b, f"python={a} rust={b}")
    for bad in [("VERIFIED", "abc", True), (None, 0.5, True), ("VERIFIED", None, True), ("VERIFIED", [1], True)]:
        check(f"C delta типы {bad!r}", run(tg._calculate_delta_factors, False, *bad) == run(tg._calculate_delta_factors, True, *bad))
    check("C delta огромные целые", run(tg._calculate_delta_factors, False, "VERIFIED", 0.5, True, 10 ** 400, 10 ** 400) ==
          run(tg._calculate_delta_factors, True, "VERIFIED", 0.5, True, 10 ** 400, 10 ** 400))

    # ── D. apply_epistemic_trust_adjustment сквозным вызовом ──
    class Trace:
        def __init__(self):
            self.rules, self.trust, self.trust_reason, self._coverage = [], None, None, None

        def add_learning_rule(self, *a):
            self.rules.append(a)

    class BM:
        def __init__(self, confs=None, raises=False):
            self.confs, self.raises = confs, raises

        def get_all_active(self):
            if self.raises:
                raise RuntimeError("boom")
            return None if self.confs is None else [types.SimpleNamespace(confidence=c) for c in self.confs]

    domains = ["axiological", "normative", "philosophical", "media_interpretation", "scientific", "other"]
    testabs = ["interpretive", "non_falsifiable", "empirical", "other"]
    covs = [1.0, 0.9, 0.8, 0.7999999, 0.79, 0.5, 0.4999999, 0.49, 0.0, 0.125, 0.375, 0.625, 0.875, 0.005, 0.995, 0.6, 0.3, 0.29, 0.605, 0.995, float("nan"), float("inf"), -0.5, 1, 0]
    beliefs = [None, BM(None), BM([]), BM([0.2, 0.3]), BM([0.5]), BM([0.49999]), BM([0.9, 0.8]), BM(raises=True), BM(["x"]), BM([0.125, 0.375])]
    entities = [None, {}, {"title": "Фильм"}]

    def one(subj, lab, dom, tst, cap, sci, ent, cov, sup, bm, engine):
        trace = Trace()
        er = types.SimpleNamespace(domain=dom, testability=tst, max_trust_cap=cap, is_science_as_model=sci, trust_score=0.8,
                                   need_clarification=True, needs_frame_split=True)
        args = dict(is_subjective_answer=subj, epistemic_trust_label=lab, epistemic_result=er, entity=ent,
                    final_claim_coverage_score=cov, support_grounding_score=sup, belief_manager=bm, trace=trace, web_used=True,
                    claims_data=[1, 2], search_result=types.SimpleNamespace(confidence=0.1), epistemic_grounding_score=0.7,
                    clarification_answered=True, is_media_query=True, supporting_ids=["a"], coverage_report_data={"x": 1},
                    intent_result=types.SimpleNamespace(intent="q"))
        res = run(tg.apply_epistemic_trust_adjustment, engine, **args)
        return res, norm((trace.trust, trace.trust_reason, trace.rules, trace._coverage))

    n = 0
    # (1) сетка по главным измерениям
    for subj, lab, dom, tst, cap, sci in itertools.product([False, True], labels[:14] + ["UNKNOWN"], domains, testabs, labels[:14], [False, True]):
        cov, sup, bm, ent = rng.choice(covs), rng.choice(covs), rng.choice(beliefs), rng.choice(entities)
        a, b = one(subj, lab, dom, tst, cap, sci, ent, cov, sup, bm, False), one(subj, lab, dom, tst, cap, sci, ent, cov, sup, bm, True)
        n += 1
        check(f"D сетка subj={subj} {lab} {dom}/{tst} cap={cap} sci={sci} cov={cov} sup={sup}", a == b, f"python={a} rust={b}")
    # (2) случайные комбинации с упором на пороги и убеждения
    for _ in range(12000):
        subj, lab = rng.random() < 0.3, rng.choice(labels)
        dom, tst, cap, sci = rng.choice(domains), rng.choice(testabs), rng.choice(labels), rng.random() < 0.3
        cov, sup, bm, ent = rng.choice(covs), rng.choice(covs), rng.choice(beliefs), rng.choice(entities)
        a, b = one(subj, lab, dom, tst, cap, sci, ent, cov, sup, bm, False), one(subj, lab, dom, tst, cap, sci, ent, cov, sup, bm, True)
        n += 1
        check(f"D fuzz subj={subj} {lab} {dom}/{tst} cap={cap} sci={sci} cov={cov} sup={sup}", a == b, f"python={a} rust={b}")
    # (3) типы вне области Rust — тот же исход (обычно то же исключение)
    for cov, sup in [("0.9", 0.9), (0.9, "x"), (None, 0.9), (0.9, None), ([0.9], 0.9), (10 ** 400, 0.9), (0.9, -10 ** 400)]:
        a, b = one(False, "VERIFIED", "scientific", "empirical", "VERIFIED", False, {}, cov, sup, None, False), one(False, "VERIFIED", "scientific", "empirical", "VERIFIED", False, {}, cov, sup, None, True)
        check(f"D типы cov={str(cov)[:8]} sup={str(sup)[:8]}", a == b, f"python={a} rust={b}")
    for lab in [None, 5, ["VERIFIED"], b"x"]:
        a, b = one(False, lab, "scientific", "empirical", "VERIFIED", False, {}, 0.9, 0.9, None, False), one(False, lab, "scientific", "empirical", "VERIFIED", False, {}, 0.9, 0.9, None, True)
        check(f"D тип метки {lab!r}", a == b, f"python={a} rust={b}")

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_TRUST_GATE_ENGINE")
    try:
        os.environ.pop("YANDI_TRUST_GATE_ENGINE", None)
        tg._rust_tg = None
        check("G0 по умолчанию активен Python-движок", tg._get_rust_tg() is None)
        os.environ["YANDI_TRUST_GATE_ENGINE"] = "rust"
        tg._rust_tg = None
        check("G1 переменная окружения переключает на Rust", tg._get_rust_tg() is rs)
        os.environ.pop("YANDI_TRUST_GATE_ENGINE", None)
        tg._rust_tg = None
        check("G3 выключение возвращает Python (кэш не залипает)", tg._get_rust_tg() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_TRUST_GATE_ENGINE", None)
        else:
            os.environ["YANDI_TRUST_GATE_ENGINE"] = saved
        tg._rust_tg = None

    src = (ROOT / "agent" / "orchestrator" / "epistemic" / "trust_gate.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(сквозных вызовов apply_epistemic_trust_adjustment: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
