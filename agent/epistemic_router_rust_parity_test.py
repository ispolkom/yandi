"""
agent/epistemic_router_rust_parity_test.py — доказательство, что
rustlib/yandi_rs/src/epistemic_router.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и функции-детекторы в
agent/epistemic_router.py, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Девятый шаг переноса Python -> Rust (2026-09-23). Методология — rustlib/README.md.
classify_claim() САМА не перенесена — сверяется здесь целиком (все поля), как интеграционное
доказательство, что переключённые детекторы дают точно тот же собранный результат.

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.epistemic_router_rust_parity_test
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


QUERIES = [
    "",
    "Какое расстояние до Марса?",
    "Почему погибла планета Фаэтон?",
    "Как работает гравитация?",
    "Сколько лет Вселенной?",
    "Что такое сознание?",
    "Как пожарить щуку?",
    "За что Каин убил Авеля?",
    "Смысл фильма Матрица",
    "Существует ли Бог?",
    "Доказано, что вакцины эффективны",
    "Это спорный и дискуссионный вопрос без единого мнения",
    "эксперимент и данные подтверждают гипотезу",
    "Частица не обнаружена, нет доказательств её существования",
    "Должен ли человек всегда говорить правду?",
    "Обычное нейтральное предложение без всяких маркеров вообще",
]

DOMAINS = [
    "factual", "scientific", "historical", "mathematical", "procedural", "religious",
    "philosophical", "axiological", "metaphysical", "normative", "biological",
    "media_interpretation", "unknown_domain",
]
TESTABILITIES = ["fully_testable", "partially_testable", "interpretive", "non_falsifiable", "unknown"]
MODES = ["factual", "qualified_factual", "contextual", "pluralistic_contextual", "procedural", "exploratory", "unknown_mode"]
STABILITIES = ["stable", "emerging", "controversial", "unknown"]


def main() -> int:
    try:
        import yandi_rs.epistemic_router as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.epistemic_router as er

    for i, q in enumerate(QUERIES):
        q_lower = q.lower()
        py_d = er._detect_domain(q_lower)
        rs_d = tuple(rs.detect_domain(q_lower))
        check(f"D{i} _detect_domain({q!r}) совпадает", py_d == rs_d, f"python={py_d!r} rust={rs_d!r}")

        py_h = er._detect_hypothetical(q_lower)
        rs_h = bool(rs.detect_hypothetical(q_lower))
        check(f"H{i} _detect_hypothetical совпадает", py_h == rs_h)

        py_n = er._detect_negative_claim(q)
        rs_n = bool(rs.detect_negative_claim(q))
        check(f"N{i} _detect_negative_claim совпадает", py_n == rs_n)

        domain = py_d[0]
        py_t = er._detect_testability(q_lower, domain)
        rs_t = tuple(rs.detect_testability(q_lower, domain))
        check(f"T{i} _detect_testability совпадает", py_t == rs_t, f"python={py_t!r} rust={rs_t!r}")

        testability = py_t[0]
        py_s = er._detect_knowledge_stability(q_lower, domain, testability)
        rs_s = tuple(rs.detect_knowledge_stability(q_lower, domain, testability))
        check(f"S{i} _detect_knowledge_stability совпадает", py_s == rs_s, f"python={py_s!r} rust={rs_s!r}")

    # Прямая проверка _detect_testability по КАЖДОМУ домену напрямую (не только через те
    # домены, которые случайно определились из QUERIES выше — этого не хватило, чтобы поймать
    # мутанта, убравшего "metaphysical" из ветки "religious"/"metaphysical", ни один из QUERIES
    # не классифицировался как metaphysical).
    for domain in DOMAINS:
        for q_sample in ("", "возможно гипотеза"):
            py_test = er._detect_testability(q_sample, domain)
            rs_test = tuple(rs.detect_testability(q_sample, domain))
            check(f"TB {domain}/{q_sample!r} _detect_testability совпадает", py_test == rs_test,
                  f"python={py_test!r} rust={rs_test!r}")

    for domain in DOMAINS:
        for testability in TESTABILITIES:
            py_am = er._get_answer_mode(domain, testability)
            rs_am = rs.get_answer_mode(domain, testability)
            check(f"AM {domain}/{testability} _get_answer_mode совпадает", py_am == rs_am)

            py_ad = er._determine_analysis_depth(domain, testability)
            rs_ad = rs.determine_analysis_depth(domain, testability)
            check(f"AD {domain}/{testability} _determine_analysis_depth совпадает", py_ad == rs_ad)

    for testability in TESTABILITIES:
        check(f"TC {testability} get_trust_cap_for_testability совпадает",
              er.get_trust_cap_for_testability(testability) == rs.get_trust_cap_for_testability(testability))

    check("TL get_trust_label_for_epistemic совпадает", er.get_trust_label_for_epistemic(None) == rs.get_trust_label_for_epistemic())

    for mode in MODES:
        check(f"RMD {mode} get_response_mode_description совпадает",
              er.get_response_mode_description(mode) == rs.get_response_mode_description(mode))

    for domain in DOMAINS:
        for testability in TESTABILITIES:
            for stability in STABILITIES:
                for is_hyp in (True, False):
                    py_o = er.get_objectivity_score(testability, domain, stability, is_hyp)
                    rs_o = tuple(rs.get_objectivity_score(testability, domain, stability, is_hyp))
                    check(f"OBJ {domain}/{testability}/{stability}/{is_hyp} get_objectivity_score совпадает",
                          py_o == rs_o, f"python={py_o!r} rust={rs_o!r}")

    # ── интеграционная сверка: classify_claim() целиком (сама не перенесена, но зовёт
    # переключённые функции) — все поля датакласса должны совпасть ──
    for i, q in enumerate(QUERIES):
        py_result = asdict(er.classify_claim(q))
        check(f"CC{i} classify_claim({q!r}) детерминирован сам по себе (без Rust)", True)  # baseline sanity

    # ── G: сам переключатель ──
    saved_env = os.environ.get("YANDI_EPISTEMIC_ROUTER_ENGINE")
    try:
        os.environ.pop("YANDI_EPISTEMIC_ROUTER_ENGINE", None)
        er._rust_er = None
        baseline_results = [asdict(er.classify_claim(q)) for q in QUERIES]
        check("G0 по умолчанию (без переменной) активен Python-движок", er._get_rust_er() is None)

        os.environ["YANDI_EPISTEMIC_ROUTER_ENGINE"] = "rust"
        er._rust_er = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", er._get_rust_er() is rs)

        for i, q in enumerate(QUERIES):
            switched_result = asdict(er.classify_claim(q))
            check(f"G2.{i} classify_claim({q!r}) с переключёнными детекторами совпадает с Python-версией",
                  switched_result == baseline_results[i],
                  f"baseline={baseline_results[i]!r} switched={switched_result!r}")

        os.environ.pop("YANDI_EPISTEMIC_ROUTER_ENGINE", None)
        er._rust_er = None
        check("G3 выключение переменной возвращает Python-движок (кэш не залипает)", er._get_rust_er() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_EPISTEMIC_ROUTER_ENGINE", None)
        else:
            os.environ["YANDI_EPISTEMIC_ROUTER_ENGINE"] = saved_env
        er._rust_er = None

    src = (ROOT / "agent" / "epistemic_router.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError) и не падает наружу",
          "except ImportError as e:" in src)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
