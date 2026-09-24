"""
agent/intent_router_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/intent_router.rs
даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/intent_router.py, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Шестнадцатый шаг переноса Python -> Rust (2026-09-24). Методология — rustlib/README.md (включая правило
«трудные входы»). Своего теста у модуля не было. Проверяется: КАЖДЫЙ паттерн отдельно (в трёх регистрах,
с префиксом/суффиксом), граница `len(q) < 10` (символы vs байты), пробелы U+001C..1F по краям и внутри,
якорь `$` (факты?$), фаззинг случайных фраз из фрагментов паттернов, справочные функции на всех типах
и на неизвестных значениях.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.intent_router_rust_parity_test
"""
from __future__ import annotations

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
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    try:
        import yandi_rs.intent_router as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.intent_router as ir

    os.environ.pop("YANDI_INTENT_ROUTER_ENGINE", None)
    ir._rust_ir = None

    rng = random.Random(20260924)
    all_patterns = [p for d in ir.INTENT_PATTERNS.values() for p in d["patterns"]]
    # «литерал» паттерна: для `факты?$` берём «факты»
    def literal(p):
        return p.replace("?$", "").replace("$", "")

    queries = ["", "   ", "привет", "Привет!", "ПРИВЕТ", "как дела?", "xyz", "a", "здра", "дела", "как ты", "ё", "Ёжик",
               "Расскажи про YANDI", "yandi", "Янди", "ЯНДИ", "янди?", "ты цифровая", "Как работает DHT?",
               "Пойдёшь за меня замуж?", "Твоё видение песни Арктик и Асти", "Придумай историю про дракона",
               "Помоги мне выбрать ноутбук", "факты", "факт", "Факты!", "факты\n", "факты \x1c", "\x1cфакты", "факты дальше",
               "  привет  ", "\x1f\x1fпривет\x1e", "приве\x1cт", "как\x1cдела", "как  дела",
               "😀", "привет😀", "​привет", "İyi", "ſ", "K"]
    for p in all_patterns:
        lit = literal(p)
        for q in (lit, lit.upper(), lit.capitalize(), "вот " + lit + " тут", lit + "!", "\x1c" + lit + "\x1f", lit + " " * 3):
            queries.append(q)
    # граница len(q) < 10: символы vs байты (кириллица 2 байта/символ)
    for n in (8, 9, 10, 11):
        queries.append("д" * n)
        queries.append("х" * (n - 5) + "дела")       # содержит "дела"
        queries.append("ы" * (n - 6) + "привет") if n >= 6 else None
    frag = sorted({w for p in all_patterns for w in literal(p).split()}) + ["xyz", "и", "в", "а", "?", "!", "..."]
    for _ in range(4000):
        queries.append(" ".join(rng.choice(frag) for _ in range(rng.randint(1, 7))))
    queries = [q for q in queries if q is not None]

    for q in queries:
        py_r, rs_r = ir.detect_intent(q), tuple(rs.detect_intent(q)) if q else ("unknown", 0.0, "empty")
        check(f"detect_intent {q[:50]!r}", py_r == rs_r, f"python={py_r} rust={rs_r}")

    types = list(ir.INTENT_PATTERNS) + ["unknown", "nope", "", "SOCIAL_DIALOG", "objective_information ", "\x1c"]
    for t in types:
        check(f"should_use_rag {t!r}", ir.should_use_rag(t) == rs.should_use_rag(t))
        check(f"get_intent_action {t!r}", ir.get_intent_action(t) == rs.get_intent_action(t))
        check(f"get_intent_description {t!r}", ir.get_intent_description(t) == rs.get_intent_description(t))
        check(f"get_intent_explanation {t!r}", ir.get_intent_explanation(t) == rs.get_intent_explanation(t))

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_INTENT_ROUTER_ENGINE")
    try:
        import logging
        logging.getLogger("yandi.intent_router").setLevel(logging.ERROR)
        os.environ.pop("YANDI_INTENT_ROUTER_ENGINE", None)
        ir._rust_ir = None
        check("G0 по умолчанию активен Python-движок", ir._get_rust_ir() is None)
        os.environ["YANDI_INTENT_ROUTER_ENGINE"] = "rust"
        ir._rust_ir = None
        check("G1 переменная окружения переключает на Rust", ir._get_rust_ir() is rs)
        for q in queries[:300]:
            direct = tuple(rs.detect_intent(q)) if q else ("unknown", 0.0, "empty")
            check(f"G2 переключённый detect_intent {q[:30]!r}", ir.detect_intent(q) == direct)
        check("G2 None/нестрока не падает и идёт как в Python",
              ir.detect_intent(None) == ("unknown", 0.0, "empty") and ir.should_use_rag(None) is False
              and ir.get_intent_action(None) == "unknown")
        os.environ.pop("YANDI_INTENT_ROUTER_ENGINE", None)
        ir._rust_ir = None
        check("G3 выключение возвращает Python (кэш не залипает)", ir._get_rust_ir() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_INTENT_ROUTER_ENGINE", None)
        else:
            os.environ["YANDI_INTENT_ROUTER_ENGINE"] = saved
        ir._rust_ir = None

    src = (ROOT / "agent" / "intent_router.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)

    print(f"\n(успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:8]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
