"""
agent/claim_status_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/claim_status.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что
agent/orchestrator/claims/status.py::classify_claim_epistemic_status — ЭПИСТЕМИЧЕСКОЕ ЯДРО (отношения «доказательство → утверждение» → статус).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Тридцать седьмой шаг переноса Python -> Rust (2026-09-24). Инвариант «trust never truth»: `verified` не выставляется; N перепечаток одной истории
(один source_cluster_id) = ОДИН независимый источник; путь авторитета (role=direct + eligible is True) и путь directness (>= 0.60, не из «жёстко
заблокированных» классов, не из local_registry). Дифференциальный фаззинг: случайные утверждения/отношения/доказательства из «родных» типов JSON
и посторонних (bool как число, NaN/inf/огромные числа, `float("abc")`, нехэшируемые evidence_id/source_cluster_id, кортеж вместо списка, подкласс
dict…); сравнивается ВСЁ наблюдаемое: возвращённая сводка, каждое утверждение после вызова (`repr` — включая ПОРЯДОК ключей и `counted_via` у
отношений), все строки журнала (verbose True/False), а при ошибке — тип исключения и то, что успело измениться. Отдельно: границы directness
0.59/0.6/0.6000001, дубликаты evidence_id, «unclustered» singleton-ключи, сходство по кластерам, статус rejected.
Также подсчитывается, СКОЛЬКО случаев реально прошло по Rust-пути (а не откатилось) — иначе тест ничего не доказывал бы.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.claim_status_rust_parity_test
"""
from __future__ import annotations

import copy
import os
import random
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
        if len(FAILURES) < 25:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


class MyDict(dict):
    pass


def main() -> int:
    try:
        import yandi_rs.claim_status as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.claim_status").setLevel(logging.ERROR)
    import agent.orchestrator.claims.status as st

    rnd = random.Random(20260924)
    stats = {"rust": 0, "fallback": 0}
    real = rs.classify_claim

    def counting(*a):
        r = real(*a)
        stats["rust" if r is not None else "fallback"] += 1
        return r

    class RsProxy:
        classify_claim = staticmethod(counting)

    def run(mode, claims, evidence, verbose):
        st._rust_cs = RsProxy if mode == "rs" else False
        cl = copy.deepcopy(claims)
        logs = []
        try:
            res = st.classify_claim_epistemic_status(cl, logs.append, verbose, copy.deepcopy(evidence) if evidence is not None else None)
            return ("ok", repr(res), repr(cl), logs)
        except Exception as e:  # noqa: BLE001
            return ("exc", type(e).__name__, repr(cl), logs)

    saved = os.environ.get("YANDI_CLAIM_STATUS_ENGINE")
    n = 0
    try:
        ROLE = ["direct", "direct", "direct", "secondary", "context", "other", None, "DIRECT", ""]
        ROLE_W = [None, 1, ["x"], True]
        ELIG = [True, True, False, None, 1, "yes", 0]
        BLOCKED = ["generated_pipeline", "social", "forum", "blog_opinion", "speculative", "news", "popular_article"]
        SRC = BLOCKED + ["academic", "unknown", "gov", None, "", "wikipedia"]
        SRC_W = [5, ["x"], {"a": 1}, 1.5, True]
        ORIGIN = ["web", "web", "local_registry", None, "", "Local_Registry"]
        ORIGIN_W = [5, ["local_registry"]]
        DIRN = [0.0, 0.3, 0.59, 0.6, 0.6000001, 0.61, 0.9, 1.0, -1, 0, 1, None, True, False, "0.7", " 0.65 ", "0.59", float("nan"), float("inf"), -float("inf")]
        DIRN_W = ["abc", "", [0.7], {"a": 1}, 10 ** 400, "1e999", "nan", "١٢", b"0.7"]
        REL = ["supports", "supports", "contradicts", "contradicts", "uncertain", "unrelated", None, "SUPPORTS", ""]
        REL_W = [5, ["supports"], {"a": 1}, True]
        EID = ["e1", "e2", "e3", "e4", "e5", "", None, 1, 2]
        EID_W = [2.5, True, ["e1"], (1,), {"a": 1}]
        CLUST = ["c1", "c2", "c1", None, "", 0, 1, "e1", "__unclustered__e1", "__unclustered__e2"]
        CLUST_W = [["c1"], {"a": 1}, 2.5, True]
        STATUS = ["candidate", "candidate", "candidate", "rejected", "verified", "supported", None, "", "unverified"]

        def pick(clean, weird, p_weird):
            return rnd.choice(weird) if weird and rnd.random() < p_weird else rnd.choice(clean)

        def gen_rel(pw):
            r = {}
            if rnd.random() < 0.95:
                r["evidence_id"] = pick(EID, EID_W, pw)
            if rnd.random() < 0.95:
                r["relation"] = pick(REL, REL_W, pw)
            if rnd.random() < 0.95:
                r["evidence_role"] = pick(ROLE, ROLE_W, pw)
            if rnd.random() < 0.8:
                r["evidence_eligible"] = rnd.choice(ELIG)
            if rnd.random() < 0.8:
                r["source_class"] = pick(SRC, SRC_W, pw)
            if rnd.random() < 0.6:
                r["retrieval_origin"] = pick(ORIGIN, ORIGIN_W, pw)
            if rnd.random() < 0.8:
                r["directness"] = pick(DIRN, DIRN_W, pw)
            if rnd.random() < 0.2:
                r["counted_via"] = rnd.choice(["old", None])       # уже есть: позиция ключа сохраняется
            if rnd.random() < 0.15:
                r["extra"] = rnd.choice([1, "x", None])
            return r

        def gen_ev(pw):
            e = {}
            if rnd.random() < 0.93:
                e["evidence_id"] = pick(EID, EID_W, pw * 0.3)
            if rnd.random() < 0.85:
                e["source_cluster_id"] = pick(CLUST, CLUST_W, pw)
            return e

        def gen_claim(pw, i):
            c = {}
            if rnd.random() < 0.95:
                c["claim_id"] = f"c{i}"
            if rnd.random() < 0.95:
                c["verification_status"] = rnd.choice(STATUS)
            r = rnd.random()
            if r < 0.04:
                pass
            elif r < 0.08:
                c["evidence_relations"] = None
            elif r < 0.12:
                c["evidence_relations"] = []
            elif r < 0.13 and pw > 0:
                c["evidence_relations"] = rnd.choice(["x", {}, 0, "", (gen_rel(0),), {"a": 1}, [None], ["x"], [gen_rel(0), 5], False, 5])
            else:
                c["evidence_relations"] = [gen_rel(pw) for _ in range(rnd.randrange(0, 7))]
            if rnd.random() < 0.1:
                c["support_count"] = 99                   # уже есть: позиция ключа сохраняется
            return c

        for it in range(30000):
            pw = 0.0 if it % 5 else rnd.choice([0.05, 0.15, 0.4])
            claims = [gen_claim(pw, i) for i in range(rnd.randrange(0, 5))]
            if pw > 0 and rnd.random() < 0.03:
                claims.append(rnd.choice([MyDict(evidence_relations=[]), None, 5, "x"]))
            evid = None
            r = rnd.random()
            if r < 0.15:
                evid = None
            elif r < 0.2:
                evid = []
            else:
                evid = [gen_ev(pw) for _ in range(rnd.randrange(0, 7))]
            for verbose in (False, True):
                p = run("py", claims, evid, verbose)
                rr = run("rs", claims, evid, verbose)
                n += 1
                check("A1 classify_claim_epistemic_status", p == rr, f"verbose={verbose}\n claims={claims!r}\n ev={evid!r}\n py={p}\n rs={rr}")

        # ---- B. направленные границы -----------------------------------------------------------------------------------------
        def rel(ev_id, relation="supports", role="direct", elig=True, sc="academic", origin="web", dirn=None):
            r = {"evidence_id": ev_id, "relation": relation, "evidence_role": role, "evidence_eligible": elig, "source_class": sc, "retrieval_origin": origin}
            if dirn is not None:
                r["directness"] = dirn
            return r

        cases = []
        for dirn in (0.59, 0.6, 0.6000000001, 0.5999999999, 1.0, 0.0):
            cases.append(([{"claim_id": "a", "evidence_relations": [rel("e1", role="secondary", elig=False, dirn=dirn)]}], None))
        for sc in BLOCKED + ["unknown"]:
            cases.append(([{"claim_id": "a", "evidence_relations": [rel("e1", role="context", elig=False, sc=sc, dirn=0.9)]}], None))
        cases.append(([{"claim_id": "a", "evidence_relations": [rel("e1", role="context", elig=False, origin="local_registry", dirn=0.9)]}], None))
        # три перепечатки одной истории = один источник; разные кластеры = разные; без кластера — каждый сам по себе
        ev = [{"evidence_id": "e1", "source_cluster_id": "c1"}, {"evidence_id": "e2", "source_cluster_id": "c1"}, {"evidence_id": "e3", "source_cluster_id": "c2"},
              {"evidence_id": "e4"}, {"evidence_id": "e5", "source_cluster_id": ""}]
        cases.append(([{"claim_id": "a", "evidence_relations": [rel("e1"), rel("e2"), rel("e3"), rel("e4"), rel("e5"), rel("e6"), rel("e6")]}], ev))
        cases.append(([{"claim_id": "a", "evidence_relations": [rel("e1"), rel("e2"), rel("e3", "contradicts")]}], ev))
        cases.append(([{"claim_id": "a", "evidence_relations": [rel("e1", "contradicts"), rel("e2", "contradicts")]}], ev))
        cases.append(([{"claim_id": "a", "evidence_relations": [rel("e1", "uncertain"), rel("e2", "unrelated")]}], ev))
        cases.append(([{"claim_id": "a", "evidence_relations": []}, {"claim_id": "b", "verification_status": "rejected"}, {"claim_id": "c"}], ev))
        cases.append(([{"claim_id": "a", "evidence_relations": [rel(None), rel(None)]}], [{"evidence_id": None, "source_cluster_id": "c1"}]))
        cases.append(([{"claim_id": "a", "evidence_relations": [rel("__unclustered__e4")]}], ev))
        for claims, evid in cases:
            for verbose in (False, True):
                p = run("py", claims, evid, verbose)
                rr = run("rs", claims, evid, verbose)
                n += 1
                check("B1 направленные случаи", p == rr, f"{claims!r}: {p} vs {rr}")

        # ---- B2. evaluate_claim_status_gate (ШЛЮЗ СТАТУСОВ: потолок доверия + предупреждение в теле ответа) ------------------------------
        import dataclasses
        import decimal
        import types as _types

        @dataclasses.dataclass
        class Synth:
            answer: object = "Ответ модели."
            trust_level: object = "STRONGLY_SUPPORTED"
            confidence: object = 0.9

        @dataclasses.dataclass(frozen=True)
        class FrozenSynth:
            answer: object = "Ответ модели."
            trust_level: object = "STRONGLY_SUPPORTED"
            confidence: object = 0.9

        class Slots:
            __slots__ = ("answer", "trust_level", "confidence")

            def __init__(self, a, t, c):
                self.answer, self.trust_level, self.confidence = a, t, c

        class Prop:
            def __init__(self, a, t, c):
                self._a, self.trust_level, self.confidence = a, t, c

            @property
            def answer(self):
                return self._a

            @answer.setter
            def answer(self, v):
                self._a = v

        def state(o):
            if hasattr(o, "__slots__") and not hasattr(o, "__dict__"):
                return {k: getattr(o, k, "<нет>") for k in o.__slots__}
            return {k: v for k, v in vars(o).items()}

        def run_gate(mode, claims, synth):
            st._rust_cs = RsProxy2 if mode == "rs" else False
            cl = copy.deepcopy(claims)
            sy = copy.deepcopy(synth)
            logs = []
            try:
                res = st.evaluate_claim_status_gate(cl, sy, logs.append)
                return ("ok", res, repr(state(sy)), logs, repr(cl))
            except Exception as e:  # noqa: BLE001
                return ("exc", type(e).__name__, repr(state(sy)), logs, repr(cl))

        real_gate = rs.evaluate_gate
        gstats = {"rust": 0, "fallback": 0}

        def counting_gate(*a):
            r = real_gate(*a)
            gstats["rust" if r is not None else "fallback"] += 1
            return r

        class RsProxy2:
            evaluate_gate = staticmethod(counting_gate)
            classify_claim = staticmethod(real)

        GST = ["verified", "supported", "disputed", "contradicted", "candidate", "rejected", "unverified", "weak", "", None, "VERIFIED", "Supported"]
        GST_W = [5, ["supported"], True, 1.5]
        ANS = ["Ответ модели.", "", "⚠️ уже помечено", "⚠️", "\u26a0 без селектора", "текст ⚠️ внутри", "многострочный\nтекст\n"]
        ANS_W = [None, b"x", 5, ["a"], MyDict()]
        TRUST = ["UNVERIFIED", "WEAKLY_SUPPORTED", "PARTIALLY_SUPPORTED", "SUPPORTED", "STRONGLY_SUPPORTED", "VERIFIED", "unknown", "", None, 0, 7]
        TRUST_W = [["x"], {"a": 1}, 2.5, True]
        CONF = [0.0, 0.1, 0.24, 0.25, 0.26, 0.4, 0.44, 0.45, 0.46, 0.59, 0.6, 0.61, 0.9, 1.0, 1, 0, -1, True, False, float("nan"), float("inf"), -float("inf"), 10 ** 30]
        CONF_W = ["0.5", None, decimal.Decimal("0.3"), [0.3], b"1"]
        gn = 0
        for it in range(40000):
            pw = 0.0 if it % 4 else rnd.choice([0.1, 0.3])
            nclaims = rnd.choice([0, 0, 1, 1, 2, 3, 4, 6])
            kind = rnd.random()
            claims = []
            for i in range(nclaims):
                c = {"claim_id": f"c{i}"}
                if kind < 0.15:
                    c["verification_status"] = "rejected"
                elif kind < 0.30:
                    c["verification_status"] = rnd.choice(["contradicted", "unverified", "candidate", "rejected", "contradicted"])
                elif kind < 0.45:
                    c["verification_status"] = rnd.choice(["disputed", "supported", "unverified", "verified"])
                elif kind < 0.6:
                    c["verification_status"] = rnd.choice(["supported", "unverified", "candidate", "weak", None, ""])
                elif rnd.random() < 0.95:
                    c["verification_status"] = rnd.choice(GST_W) if rnd.random() < pw else rnd.choice(GST)
                if rnd.random() < 0.05:
                    c.pop("verification_status", None)
                claims.append(c)
            if pw and rnd.random() < 0.1 and claims:
                claims[rnd.randrange(len(claims))] = rnd.choice([MyDict(verification_status="supported"), None, 5, "x"])
            if pw and rnd.random() < 0.05:
                claims = tuple(claims)
            a = rnd.choice(ANS_W) if rnd.random() < pw else rnd.choice(ANS)
            t = rnd.choice(TRUST_W) if rnd.random() < pw else rnd.choice(TRUST)
            c_ = rnd.choice(CONF_W) if rnd.random() < pw else rnd.choice(CONF)
            kindsy = rnd.random()
            if kindsy < 0.7:
                sy = Synth(a, t, c_)
            elif kindsy < 0.78:
                sy = _types.SimpleNamespace(answer=a, trust_level=t, confidence=c_)
            elif kindsy < 0.84:
                sy = Slots(a, t, c_)
            elif kindsy < 0.9:
                sy = Prop(a, t, c_)
            elif kindsy < 0.95:
                sy = FrozenSynth(a, t, c_)
            else:
                sy = _types.SimpleNamespace(answer=a)          # нет trust_level/confidence
            p = run_gate("py", claims, sy)
            rr = run_gate("rs", claims, sy)
            gn += 1
            check("B2 evaluate_claim_status_gate", p == rr, f"claims={claims!r}\n synth={state(sy)!r}\n py={p}\n rs={rr}")
        n += gn
        check("B3 шлюз: Rust-путь реально использован (>= 60%)", gstats["rust"] >= 0.6 * (gstats["rust"] + gstats["fallback"]) and gstats["rust"] > 20000, str(gstats))
        # пять веток по очереди, точные тексты
        for statuses in ([], ["rejected", "rejected"], ["contradicted", "unverified"], ["contradicted", "supported"], ["disputed", "supported"], ["supported", "unverified"],
                         ["supported"], ["unverified", "candidate"], ["verified", "unverified"], ["verified"], ["contradicted", "rejected", "candidate"]):
            cl = [{"claim_id": f"c{i}", "verification_status": s_} for i, s_ in enumerate(statuses)]
            for tl in ("STRONGLY_SUPPORTED", "VERIFIED", "SUPPORTED", "PARTIALLY_SUPPORTED", "WEAKLY_SUPPORTED", "UNVERIFIED"):
                for ans in ("Ответ.", "⚠️ уже"):
                    sy = Synth(ans, tl, 0.95)
                    p = run_gate("py", cl, sy)
                    rr = run_gate("rs", cl, sy)
                    n += 1
                    check("B4 ветки шлюза", p == rr, f"{statuses} {tl}: {p} vs {rr}")

        # ---- C. охват: сколько прошло по Rust-пути ------------------------------------------------------------------------------------
        check("C1 Rust-путь реально использован (>= 60% вызовов)", stats["rust"] >= 0.6 * (stats["rust"] + stats["fallback"]) and stats["rust"] > 20000,
              str(stats))

        # ---- D. переключатель / константы ---------------------------------------------------------------------------------------------
        os.environ.pop("YANDI_CLAIM_STATUS_ENGINE", None)
        st._rust_cs = None
        check("D1 по умолчанию выключен", st._get_rust_cs() is None)
        os.environ["YANDI_CLAIM_STATUS_ENGINE"] = "rust"
        st._rust_cs = None
        check("D2 включается переменной", st._get_rust_cs() is not None)
        # изменённые константы уважаются: frozenset вместо set → Python-путь; пороги
        st._rust_cs = rs
        claims = [{"claim_id": "a", "evidence_relations": [rel("e1", role="secondary", elig=False, sc="forum", dirn=0.9)]}]
        old_hb, old_th = st.HARD_BLOCKED_SOURCE_CLASSES, st.DIRECTNESS_SUPPORT_THRESHOLD
        try:
            st.HARD_BLOCKED_SOURCE_CLASSES = set()
            a = copy.deepcopy(claims)
            st.classify_claim_epistemic_status(a, lambda m: None, False)
            check("D3 изменённое множество заблокированных уважается", a[0]["verification_status"] == "supported", repr(a))
            st.HARD_BLOCKED_SOURCE_CLASSES = frozenset(old_hb)
            a = copy.deepcopy(claims)
            st.classify_claim_epistemic_status(a, lambda m: None, False)
            check("D4 frozenset → Python-путь без ошибок", a[0]["verification_status"] == "unverified", repr(a))
            st.HARD_BLOCKED_SOURCE_CLASSES = old_hb
            st.DIRECTNESS_SUPPORT_THRESHOLD = 0.95
            a = copy.deepcopy(claims[:1])
            a[0]["evidence_relations"] = [rel("e1", role="secondary", elig=False, dirn=0.9)]
            st.classify_claim_epistemic_status(a, lambda m: None, False)
            check("D5 изменённый порог уважается", a[0]["verification_status"] == "unverified", repr(a))
        finally:
            st.HARD_BLOCKED_SOURCE_CLASSES, st.DIRECTNESS_SUPPORT_THRESHOLD = old_hb, old_th
        os.environ.pop("YANDI_CLAIM_STATUS_ENGINE", None)
        st._rust_cs = None
        check("D6 выключение возвращает Python", st._get_rust_cs() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_CLAIM_STATUS_ENGINE", None)
        else:
            os.environ["YANDI_CLAIM_STATUS_ENGINE"] = saved
        st._rust_cs = None
    src = (ROOT / "agent" / "orchestrator" / "claims" / "status.py").read_text(encoding="utf-8")
    check("D7 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(сравнений: {n}; по Rust-пути: {stats['rust']}, откат на Python: {stats['fallback']}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
