"""
agent/personal_boundary_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/personal_boundary.rs
даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/personal_boundary.py::PersonalBoundary.analyze/get_response_template.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Восемнадцатый шаг переноса Python -> Rust (2026-09-24). Методология — rustlib/README.md (правило «трудные
входы»). Своего теста у модуля не было. Проверяется: КАЖДЫЙ паттерн всех шести списков в 8 формах (регистр,
префикс/суффикс, разделители U+001C..1F — в `[, ]*` они НЕ пробел), приоритеты при пересечении категорий
(фаззинг фраз из фрагментов разных списков), перезапись reason/response_type последующими ветками,
`max(conf, 0.6)`, шаблоны ответа на границах доверия/раздражения (29.999/30/30.0001/60/61/40/40.0001, NaN,
inf, bool, огромное целое -> Python-путь, строка -> тот же TypeError), изменённые списки паттернов экземпляра
(делегирование выключается).

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.personal_boundary_rust_parity_test
"""
from __future__ import annotations

import dataclasses
import itertools
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
        import yandi_rs.personal_boundary as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.personal_boundary as pb

    os.environ.pop("YANDI_PERSONAL_BOUNDARY_ENGINE", None)
    pb._rust_pb = None
    py_pb = pb.PersonalBoundary()
    rng = random.Random(20260924)

    def rs_analyze(q):
        return pb.BoundaryAnalysis(**rs.analyze(q))

    lits = []
    for lst in (py_pb.sincere_apology_patterns, py_pb.provocation_patterns, py_pb.deep_question_patterns):
        lits += lst
    lits += [p for p, _ in py_pb.personal_patterns]
    lits += ["извини но", "извини, но", "извини,но", "извини , но", "извини,, ,но", "прости но", "прости,но", "я не хотел но", "я не хотел, но",
             "просто", "случайно", "не со зла", "как дела", "привет", "здравствуй", "как ты"]
    queries = ["", " ", "?", "xyz", "Привет!", "ПРИВЕТ", "как дела", "\x1cпривет", "İ", "ſ"]
    for lit in lits:
        for q in (lit, lit.upper(), lit.capitalize(), "вот " + lit + " тут", lit + "!", "\x1c" + lit + "\x1f", lit.replace(" ", "\x1c"),
                  lit.replace(" ", "  ")):
            queries.append(q)
    for sep in ("", " ", ",", ", ", " ,", ",,", "\x1c", "\t", " ", "　", ".", "-"):
        queries += [f"извини{sep}но", f"прости{sep}но", f"я не хотел{sep}но", f"ИЗВИНИ{sep}НО"]
    frags = sorted(set(lits)) + ["мне", "тебе", "и", "не", "а", "?", "!", "...", "очень"]
    for _ in range(6000):
        queries.append(" ".join(rng.choice(frags) for _ in range(rng.randint(1, 6))))
    # приоритеты пар из разных категорий
    for a, b in itertools.product(rng.sample(lits, 25), rng.sample(lits, 25)):
        queries.append(f"{a} {b}")

    def norm(x):
        return dataclasses.asdict(x)

    for q in queries:
        p, r = norm(py_pb.analyze(q)), norm(rs_analyze(q))
        check(f"analyze {q[:50]!r}", p == r, f"python={p} rust={r}")

    # ── шаблоны ответа ──
    flag_combos = list(itertools.product([False, True], repeat=6))
    trusts = [50, 0, 29.999, 30, 30.0001, 60, 61, 60.0000001, -1, 100, float("nan"), float("inf"), float("-inf"), True, False, 10 ** 400, 0.0, -0.0]
    irrs = [10, 0, 40, 40.0001, 39.9999, 100, float("nan"), float("inf"), True, 10 ** 400]
    n_t = 0
    for flags in flag_combos:
        analysis = pb.BoundaryAnalysis(is_personal=flags[0], is_apology=flags[1], is_sincere=flags[2], is_provocation=flags[3],
                                       is_deep_question=flags[4], is_social=flags[5])
        for t in trusts:
            for i in (10, 40.0001):
                state = {"trust": t, "irritation": i}
                pb._rust_pb = False
                py_r = py_pb.get_response_template(analysis, state)
                os.environ["YANDI_PERSONAL_BOUNDARY_ENGINE"] = "rust"
                pb._rust_pb = None
                rs_r = py_pb.get_response_template(analysis, state)
                os.environ.pop("YANDI_PERSONAL_BOUNDARY_ENGINE", None)
                n_t += 1
                check(f"template flags={flags} trust={str(t)[:12]} irr={i}", py_r == rs_r, f"python={py_r} rust={rs_r}")
    for i in irrs:
        analysis = pb.BoundaryAnalysis(is_social=True)
        state = {"trust": 50, "irritation": i}
        pb._rust_pb = False
        py_r = py_pb.get_response_template(analysis, state)
        os.environ["YANDI_PERSONAL_BOUNDARY_ENGINE"] = "rust"
        pb._rust_pb = None
        rs_r = py_pb.get_response_template(analysis, state)
        os.environ.pop("YANDI_PERSONAL_BOUNDARY_ENGINE", None)
        check(f"template social irr={str(i)[:12]}", py_r == rs_r)
    # None / без state / нечисловые значения -> те же исключения/результаты, что у Python
    for state in (None, {}, {"trust": None}, {"trust": "abc"}, {"irritation": "x"}, {"trust": [1]}):
        for engine in (False, True):
            pass
        analysis = pb.BoundaryAnalysis(is_apology=True, is_sincere=True)
        outs = []
        for eng in (False, "rust"):
            if eng == "rust":
                os.environ["YANDI_PERSONAL_BOUNDARY_ENGINE"] = "rust"
                pb._rust_pb = None
            else:
                pb._rust_pb = False
            try:
                outs.append(py_pb.get_response_template(analysis, state))
            except Exception as e:                      # noqa: BLE001
                outs.append(f"EXC:{type(e).__name__}")
            os.environ.pop("YANDI_PERSONAL_BOUNDARY_ENGINE", None)
        check(f"template state={state!r}", outs[0] == outs[1], f"{outs}")

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_PERSONAL_BOUNDARY_ENGINE")
    try:
        import logging
        logging.getLogger("yandi.personal_boundary").setLevel(logging.ERROR)
        os.environ.pop("YANDI_PERSONAL_BOUNDARY_ENGINE", None)
        pb._rust_pb = None
        check("G0 по умолчанию активен Python-движок", pb._get_rust_pb() is None)
        os.environ["YANDI_PERSONAL_BOUNDARY_ENGINE"] = "rust"
        pb._rust_pb = None
        check("G1 переменная окружения переключает на Rust", pb._get_rust_pb() is rs)
        for q in queries[:300]:
            check(f"G2 переключённый analyze {q[:30]!r}", norm(py_pb.analyze(q)) == norm(rs_analyze(q)))
        # изменённые списки паттернов экземпляра -> Rust не используется (результат по ИЗМЕНЁННЫМ спискам)
        custom = pb.PersonalBoundary()
        custom.provocation_patterns = ["мой_особый_паттерн"]
        pb._rust_pb = None
        r = custom.analyze("текст мой_особый_паттерн")
        check("G2b изменённые паттерны экземпляра уважаются (Python-путь)", r.is_provocation and "мой_особый_паттерн" in r.reason, f"{r}")
        r2 = custom.analyze("пофиг")
        check("G2c старый паттерн у изменённого экземпляра не срабатывает", not r2.is_provocation, f"{r2}")
        check("G2d возвращается настоящий BoundaryAnalysis", isinstance(py_pb.analyze("привет"), pb.BoundaryAnalysis))
        os.environ.pop("YANDI_PERSONAL_BOUNDARY_ENGINE", None)
        pb._rust_pb = None
        check("G3 выключение возвращает Python (кэш не залипает)", pb._get_rust_pb() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_PERSONAL_BOUNDARY_ENGINE", None)
        else:
            os.environ["YANDI_PERSONAL_BOUNDARY_ENGINE"] = saved
        pb._rust_pb = None

    src = (ROOT / "agent" / "personal_boundary.py").read_text(encoding="utf-8")
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
