"""
agent/final_claim_coverage_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/final_claim_coverage.rs даёт
ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/final_claim_coverage.py: _content_words, _lexical_overlap, _has_negation,
_shares_number, _is_near_duplicate, _mandatory_routing_reason и весь _route_candidate_pairs (routing + stats).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Двадцать пятый шаг переноса Python -> Rust (2026-09-24). Контракт кроме этого теста — существующий
agent/candidate_routing_regression_test.py (с включённым переключателем — в полном прогоне). Проверяется: слова только
[а-яёa-z0-9] (Unicode-цифры/другие буквы не входят), длина слова в СИМВОЛАХ (граница 3/4), стоп-слова, Жаккар, отрицание
(`не{1,2}…`, `нет`, `отсутств`, `никак`, `ни один` с точными `\b`: ударение U+0301, ², цифры Unicode рядом), числа `\d+([.,]\d+)?`
(Unicode-цифры совпадают, «145» ≠ «145,5»), «почти дубликаты» (регистр/пробелы/0.8), все комбинации ролей, порядок правил,
None вместо строки, сквозной _route_candidate_pairs с детерминированным эмбеддингом и при «эмбеддинг недоступен».

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.final_claim_coverage_rust_parity_test
"""
from __future__ import annotations

import hashlib
import itertools
import os
import random
import sys
from pathlib import Path
from unittest import mock

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
        import yandi_rs.final_claim_coverage as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.final_claim_coverage").setLevel(logging.ERROR)
    import numpy as np
    import agent.final_claim_coverage as fcc

    os.environ.pop("YANDI_FINAL_CLAIM_COVERAGE_ENGINE", None)
    fcc._rust_fcc = None
    rng = random.Random(20260924)

    def run(engine, fn, *a):
        saved = os.environ.get("YANDI_FINAL_CLAIM_COVERAGE_ENGINE")
        try:
            if engine:
                os.environ["YANDI_FINAL_CLAIM_COVERAGE_ENGINE"] = "rust"
            else:
                os.environ.pop("YANDI_FINAL_CLAIM_COVERAGE_ENGINE", None)
            fcc._rust_fcc = None
            try:
                r = fn(*a)
                return sorted(r) if isinstance(r, set) else r
            except Exception as e:                                # noqa: BLE001
                return f"EXC:{type(e).__name__}"
        finally:
            if saved is None:
                os.environ.pop("YANDI_FINAL_CLAIM_COVERAGE_ENGINE", None)
            else:
                os.environ["YANDI_FINAL_CLAIM_COVERAGE_ENGINE"] = saved
            fcc._rust_fcc = None

    words = ["Юпитер", "юпитер", "планета", "газовый", "гигант", "жизнь", "не", "нее", "неее", "нет", "нет.", "отсутствует", "никак", "ни один", "ни  один",
             "не обнаружена", "необнаружена", "неизвестно", "145", "145,5", "145.5", "3,14", "12", "٣٤", "²", "½", "ёлка", "Ёлка", "йод", "abc", "abcd", "abcde",
             "что", "это", "как", "для", "быть", "уже", "всё", "температура", "около", "градусов", "и", "в", "на", "İstanbul", "ſmall", "K", "K",
             "😀", "мама", "папа"]
    tricky = ["́", "​", "\x1c", " ", "　", "\t", "\n", ".", ",", "-", "'", "_", "²", "٣", "😀", "İ", "ı"]

    def rt():
        parts = []
        for _ in range(rng.randint(0, 9)):
            w = rng.choice(words)
            if rng.random() < 0.2:
                w = w.upper() if rng.random() < 0.5 else w.capitalize()
            if rng.random() < 0.15:
                i = rng.randint(0, len(w))
                w = w[:i] + rng.choice(tricky) + w[i:]
            parts.append(w)
        return "".join(p + rng.choice([" ", " ", " ", "  ", ", ", "\x1c", ". "]) for p in parts)

    texts = ["", " ", "a", "abcd", "abc", "абв", "абвг", "юпитер", "Юпитер", "ЮПИТЕР", "не", "не́", "неа", "нее", "необнаружена", "нет", "нет́", "отсутств", "ни  один",
             "ни один", "никак", "145", "145,5", "٣٤٥", "1,2,3", "1..2"] + [rt() for _ in range(9000)]
    n = 0
    for t in texts:
        for lbl, pf, rf in (("content_words", fcc._content_words, rs.content_words), ("has_negation", fcc._has_negation, rs.has_negation)):
            n += 1
            a, b = run(False, pf, t), run(True, pf, t)
            check(f"{lbl} {t[:40]!r}", a == b, f"python={a} rust={b}")
    for _ in range(9000):
        a_, b_ = rng.choice(texts), rng.choice(texts)
        if rng.random() < 0.3:
            b_ = a_.upper() if rng.random() < 0.5 else " " + a_ + "  "
        for lbl, pf in (("lexical_overlap", fcc._lexical_overlap), ("shares_number", fcc._shares_number), ("is_near_duplicate", fcc._is_near_duplicate)):
            n += 1
            a, b = run(False, pf, a_, b_), run(True, pf, a_, b_)
            check(f"{lbl} {a_[:25]!r}/{b_[:25]!r}", a == b, f"python={a} rust={b}")
    # Пограничный Жаккар для порога «почти дубликат» 0.8 и пола 0.15: наборы k и k+1 слов (доля k/(k+1): 0.8, 0.833, …, 0.9),
    # плюс тексты БЕЗ отрицания/чисел (иначе причина определяется раньше порога).
    vocab = ["альфа", "бета", "гамма", "дельта", "омега", "сигма", "тау", "ипсилон", "лямбда", "кси", "пси", "эта", "йота", "каппа", "мю", "ню"]
    for k in range(1, 15):
        wa = vocab[:k]
        wb = vocab[:k + 1]
        for extra in ("", " не", " 145"):
            a_, b_ = " ".join(wa), " ".join(wb) + extra
            for pf in (fcc._lexical_overlap, fcc._is_near_duplicate):
                n += 1
                x, y = run(False, pf, a_, b_), run(True, pf, a_, b_)
                check(f"jaccard-граница k={k} {pf.__name__} extra={extra!r}", x == y, f"python={x} rust={y}")
            for fr, pr in ((None, None), ("CORE", "CORE")):
                x, y = run(False, fcc._mandatory_routing_reason, a_, b_, fr, pr), run(True, fcc._mandatory_routing_reason, a_, b_, fr, pr)
                check(f"reason jaccard-граница k={k} extra={extra!r} {fr}", x == y, f"python={x} rust={y}")
    # доля около пола 0.15: |∩|/|∪| = 1/6 (0.1667), 1/7 (0.1428), 2/13 (0.1538), 3/20 (0.15 ровно)
    for inter, ua, ub in ((1, 3, 4), (1, 3, 5), (2, 7, 8), (3, 8, 9), (3, 10, 10), (3, 11, 12), (3, 12, 11), (6, 20, 26), (2, 8, 7)):
        common, oa, ob = vocab[:inter], [f"аа{i}бв" for i in range(ua - inter)], [f"бб{i}гд" for i in range(ub - inter)]
        for extra in (" не", " 145", ""):
            a_, b_ = " ".join(common + oa) + extra, " ".join(common + ob) + extra
            x, y = run(False, fcc._mandatory_routing_reason, a_, b_, None, None), run(True, fcc._mandatory_routing_reason, a_, b_, None, None)
            n += 1
            check(f"reason пол 0.15 inter={inter} {ua}/{ub} extra={extra!r}", x == y, f"python={x} rust={y}")

    # None вместо строки
    for x in (None, ""):
        for pf in (fcc._content_words, fcc._has_negation):
            check(f"None {pf.__name__} {x!r}", run(False, pf, x) == run(True, pf, x))
        for pf in (fcc._lexical_overlap, fcc._shares_number, fcc._is_near_duplicate):
            check(f"None {pf.__name__}", run(False, pf, x, "Юпитер") == run(True, pf, x, "Юпитер") and run(False, pf, "Юпитер", x) == run(True, pf, "Юпитер", x))
    # не-строки: тот же исход (Python-путь)
    for bad in (5, ["a"], b"x"):
        for pf in (fcc._content_words, fcc._has_negation):
            check(f"тип {pf.__name__} {bad!r}", run(False, pf, bad) == run(True, pf, bad))

    # ── _mandatory_routing_reason: все комбинации ролей ──
    roles = [None, "CORE", "BACKGROUND", "DIRECT_DECISION_EVIDENCE", "EXPLANATORY"]
    base = ["Разумная жизнь на Юпитере не обнаружена", "На Юпитере около 145 градусов", "Юпитер газовый гигант планета", "Марс красная планета холодная",
            "Разумная жизнь на Юпитере обнаружена", "  разумная ЖИЗНЬ на юпитере не обнаружена  ", "температура около 145 градусов на Юпитере", "", "Ни один аппарат не нашёл жизнь"]
    pairs = list(itertools.product(base + texts[:60], base + texts[:60]))
    rng.shuffle(pairs)
    for a_, b_ in pairs[:3000]:
        fr, pr = rng.choice(roles), rng.choice(roles)
        n += 1
        a, b = run(False, fcc._mandatory_routing_reason, a_, b_, fr, pr), run(True, fcc._mandatory_routing_reason, a_, b_, fr, pr)
        check(f"reason {a_[:25]!r}/{b_[:25]!r} {fr}/{pr}", a == b, f"python={a} rust={b}")
    for fr, pr in itertools.product(roles, roles):
        a, b = run(False, fcc._mandatory_routing_reason, "вода", "лёд", fr, pr), run(True, fcc._mandatory_routing_reason, "вода", "лёд", fr, pr)
        check(f"reason роли {fr}/{pr}", a == b, f"python={a} rust={b}")

    # ── _route_candidate_pairs сквозной ──
    def fake_embed(texts):
        out = {}
        for t in dict.fromkeys(x for x in texts if x):
            h = hashlib.md5(t.encode("utf-8")).digest()
            v = np.array([(b - 127.5) for b in h[:8]], dtype=np.float32)
            out[t] = v / (np.linalg.norm(v) or 1.0)
        return out

    def route(engine, finals, pipes, query, embed):
        saved = os.environ.get("YANDI_FINAL_CLAIM_COVERAGE_ENGINE")
        try:
            if engine:
                os.environ["YANDI_FINAL_CLAIM_COVERAGE_ENGINE"] = "rust"
            else:
                os.environ.pop("YANDI_FINAL_CLAIM_COVERAGE_ENGINE", None)
            fcc._rust_fcc = None
            with mock.patch.object(fcc, "_embed_texts_batch", embed):
                return fcc._route_candidate_pairs(finals, pipes, query)
        finally:
            if saved is None:
                os.environ.pop("YANDI_FINAL_CLAIM_COVERAGE_ENGINE", None)
            else:
                os.environ["YANDI_FINAL_CLAIM_COVERAGE_ENGINE"] = saved
            fcc._rust_fcc = None

    queries = ["", "Есть ли разумная жизнь на Юпитере?", "Расскажи о Юпитере", "Существует ли бозон Хиггса?"]
    for _ in range(500):
        finals = [rng.choice(base + texts[:80]) for _ in range(rng.randint(0, 6))]
        pipes = [rng.choice(base + texts[:80]) for _ in range(rng.randint(0, 9))]
        q = rng.choice(queries)
        for name, emb in (("fake", fake_embed), ("нет эмбеддинга", lambda t: {})):
            a, b = route(False, finals, pipes, q, emb), route(True, finals, pipes, q, emb)
            check(f"route {name} F={len(finals)} P={len(pipes)} q={q[:12]!r}", a == b, f"python={a} rust={b}")
    check("route пустые списки", route(False, [], [], "", fake_embed) == route(True, [], [], "", fake_embed))

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_FINAL_CLAIM_COVERAGE_ENGINE")
    try:
        os.environ.pop("YANDI_FINAL_CLAIM_COVERAGE_ENGINE", None)
        fcc._rust_fcc = None
        check("G0 по умолчанию активен Python-движок", fcc._get_rust_fcc() is None)
        os.environ["YANDI_FINAL_CLAIM_COVERAGE_ENGINE"] = "rust"
        fcc._rust_fcc = None
        check("G1 переменная окружения переключает на Rust", fcc._get_rust_fcc() is rs)
        os.environ.pop("YANDI_FINAL_CLAIM_COVERAGE_ENGINE", None)
        fcc._rust_fcc = None
        check("G3 выключение возвращает Python (кэш не залипает)", fcc._get_rust_fcc() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_FINAL_CLAIM_COVERAGE_ENGINE", None)
        else:
            os.environ["YANDI_FINAL_CLAIM_COVERAGE_ENGINE"] = saved
        fcc._rust_fcc = None
    src = (ROOT / "agent" / "final_claim_coverage.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(сравнений функций: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
