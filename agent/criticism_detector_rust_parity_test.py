"""
agent/criticism_detector_rust_parity_test.py — доказательство, что
rustlib/yandi_rs/src/criticism_detector.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и
agent/criticism_detector.py::CriticismDetector.analyze/get_response_template, на одних и тех же
примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Седьмой шаг переноса Python -> Rust (2026-09-23). Методология — rustlib/README.md.

Этот модуль не имел выделенного regression-теста ДО этого переноса — сценарии собраны из
собственного примера в __main__ этого модуля (не как готовые ожидания: перепроверены напрямую
через реальный Python перед тем, как стать проверками здесь — один такой пример в __main__ на
самом деле НЕ даёт "mixed", как можно было бы подумать на глаз, см. коммит) плюс прочтением
самой логики (границы score, контекстная коррекция, приоритет веток).

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.criticism_detector_rust_parity_test
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


ANALYZE_TEXTS = [
    "",
    "привет, как дела",
    "ты глупая",
    "ты ничего не понимаешь",
    "ты ошиблась в расчётах, попробуй перепроверить",
    "этот подход не работает, давай попробуем другой",
    "ты не учла важный фактор",
    "как у тебя вообще язык поворачивается такое говорить",
    "ты дура, но стоит перепроверить расчёты",
    "ты дура, но перепроверь расчёты",  # НЕ mixed на самом деле — проверено напрямую
    "это неправильно, нужно исправить",
    "ошибка в коде",
    "ЭТО ВООБЩЕ НЕ РАБОТАЕТ!!!",  # капс + восклицание + агрессивный маркер
    "стоит попробовать другой подход",
    "давайте начнём заново",
    "я бы предложил другой вариант",
]

# (trust, history_insults)
CONTEXTS = [
    (50, 0),
    (20, 3),
    (80, 0),
    (10, 10),
    (50, 100),  # repeat_offender_penalty упирается в потолок 0.3
]

# (is_insult, target, is_constructive, is_criticism, is_feedback, trust, irritation, forgiveness)
RESPONSE_CASES = [
    (True, "personality", False, False, False, 20, 70, 50),
    (True, "personality", False, False, False, 50, 10, 50),
    (True, "personality", False, False, False, 50, 10, 20),
    (True, "mixed", True, True, False, 50, 10, 50),
    (False, "action", True, True, True, 70, 10, 50),
    (False, "action", True, True, True, 30, 10, 50),
    (False, "work", False, True, True, 50, 10, 50),
    (False, "work", False, False, True, 50, 10, 50),
    (False, "unknown", False, False, False, 50, 10, 50),
]


def result_to_dict(r) -> dict:
    return {
        "is_criticism": r.is_criticism, "is_insult": r.is_insult, "is_constructive": r.is_constructive,
        "is_feedback": r.is_feedback, "specificity": r.specificity, "constructiveness": r.constructiveness,
        "severity": r.severity, "target": r.target, "suggested_improvement": r.suggested_improvement,
        "confidence": r.confidence, "reason": r.reason, "context_adjustment": r.context_adjustment,
    }


def main() -> int:
    try:
        import yandi_rs.criticism_detector as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    from agent.criticism_detector import CriticismDetector

    detector = CriticismDetector()

    for i, text in enumerate(ANALYZE_TEXTS):
        for j, (trust, history_insults) in enumerate(CONTEXTS):
            ctx = {"trust": trust, "history_insults": history_insults}
            py_result = result_to_dict(detector.analyze(text, ctx))
            rs_result = dict(rs.analyze(text, ctx))
            check(f"A{i}.{j} analyze({text!r}, trust={trust}, hist={history_insults}) совпадает",
                  py_result == rs_result, f"python={py_result!r} rust={rs_result!r}")

    # context=None (использует дефолты) — отдельная проверка, что дефолты совпадают
    for i, text in enumerate(ANALYZE_TEXTS):
        py_result = result_to_dict(detector.analyze(text, None))
        rs_result = dict(rs.analyze(text, None))
        check(f"AN{i} analyze({text!r}, context=None) совпадает", py_result == rs_result,
              f"python={py_result!r} rust={rs_result!r}")

    for i, (is_insult, target, is_c, is_crit, is_fb, trust, irritation, forgiveness) in enumerate(RESPONSE_CASES):
        from agent.criticism_detector import CriticismAnalysis
        analysis = CriticismAnalysis(is_insult=is_insult, target=target, is_constructive=is_c,
                                      is_criticism=is_crit, is_feedback=is_fb)
        ctx = {"trust": trust, "irritation": irritation, "forgiveness": forgiveness}
        py_result = detector.get_response_template(analysis, ctx)
        rs_result = dict(rs.get_response_template(is_insult, target, is_c, is_crit, is_fb, ctx))
        check(f"R{i} get_response_template(...) совпадает", py_result == rs_result,
              f"python={py_result!r} rust={rs_result!r}")

    # ── G: сам переключатель ──
    saved_env = os.environ.get("YANDI_CRITICISM_ENGINE")
    try:
        import agent.criticism_detector as cd_mod

        os.environ.pop("YANDI_CRITICISM_ENGINE", None)
        cd_mod._rust_cd = None
        check("G0 по умолчанию (без переменной) активен Python-движок", cd_mod._get_rust_cd() is None)

        os.environ["YANDI_CRITICISM_ENGINE"] = "rust"
        cd_mod._rust_cd = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", cd_mod._get_rust_cd() is rs)

        for i, text in enumerate(ANALYZE_TEXTS[:5]):
            via_switch = result_to_dict(detector.analyze(text, {"trust": 30, "history_insults": 2}))
            direct_rust = dict(rs.analyze(text, {"trust": 30, "history_insults": 2}))
            check(f"G2.{i} переключённый analyze совпадает с прямым вызовом Rust", via_switch == direct_rust)

        os.environ.pop("YANDI_CRITICISM_ENGINE", None)
        cd_mod._rust_cd = None
        check("G3 выключение переменной возвращает Python-движок (кэш не залипает)", cd_mod._get_rust_cd() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_CRITICISM_ENGINE", None)
        else:
            os.environ["YANDI_CRITICISM_ENGINE"] = saved_env
        cd_mod._rust_cd = None

    src = (ROOT / "agent" / "criticism_detector.py").read_text(encoding="utf-8")
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
