"""
agent/claim_evidence_retriever_rust_parity_test.py — доказательство, что
rustlib/yandi_rs/src/claim_evidence_retriever.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и
agent/claim_evidence_retriever.py::{_is_absence_claim, _is_existence_question,
_extract_existence_target, _target_overlap, _classify_claim_role}, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Тринадцатый шаг переноса Python -> Rust (2026-09-24). Методология — rustlib/README.md.
Сценарии — реальные примеры из agent/claim_priority_regression_test.py (уже существующего
regression-контракта этих функций), плюс отдельный граничный случай на char-vs-byte расхождение
в _target_overlap (новый вариант ловушки, не встречавшийся в прежних срезах: стем
word[:max(3, len(word)-2)] считается по символам, не байтам).

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.claim_evidence_retriever_rust_parity_test
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── Реальные claim/query из agent/claim_priority_regression_test.py ──
JUPITER_QUERY = "Есть ли разумная жизнь на Юпитере?"
MARS_QUERY = "Есть ли вода на Марсе?"
HIGGS_QUERY = "Существует ли бозон Хиггса?"

ABSENCE_TRUE_CASES = [
    "разумная жизнь не обнаружена",
    "разумная жизнь не была обнаружена",
    "разумная жизнь пока не обнаружена",
    "признаки жизни не были обнаружены",
    "нет доказательств существования жизни",
    "доказательства жизни отсутствуют",
    "ни один аппарат не обнаружил признаков жизни",
    "не выявлено признаков разумной деятельности",
    "не найдено подтверждений",
    "сигналы не зафиксированы",
    "По имеющимся данным разумная жизнь на Юпитере не была обнаружена.",
    "На Юпитере отсутствует жидкая вода на поверхности.",
]
ABSENCE_FALSE_CASES = [
    "разумная жизнь обнаружена",
    "аппарат обнаружил сигнал",
    "доказательства существуют",
    "температура не превышает -145°C",
    "нет сомнений, что жизнь существует",  # исключение "нет сомнений"
]

ROLE_CASES = [
    # (query, claim_text)
    (JUPITER_QUERY, "Согласно имеющейся информации, разумная жизнь на Юпитере считается крайне маловероятной."),
    (JUPITER_QUERY, "Атмосфера Юпитера состоит преимущественно из водорода и гелия."),
    (JUPITER_QUERY, "На Юпитере отсутствует жидкая вода на поверхности."),
    (JUPITER_QUERY, "Нет разумной жизни на Юпитере."),
    (JUPITER_QUERY, "Разумная жизнь на Юпитере не была обнаружена."),
    (JUPITER_QUERY, "Телескопические наблюдения не зафиксировали признаков жизни на Юпитере."),
    (JUPITER_QUERY, "Космические миссии не зафиксировали признаков жизни на Юпитере."),
    (JUPITER_QUERY, "Зонды не обнаружили признаков жизни на Юпитере."),
    (JUPITER_QUERY, "На Юпитере нет жидкой воды."),
    (JUPITER_QUERY, "Температура на Юпитере не превышает -145°C."),
    (JUPITER_QUERY, "Условия на Юпитере исключают развитие цивилизации."),
    (MARS_QUERY, "Нет воды на Марсе."),
    (MARS_QUERY, "Марс не имеет глобального магнитного поля."),
    (MARS_QUERY, "Вода на Марсе не была обнаружена в жидком виде."),
    (HIGGS_QUERY, "Бозон Хиггса был обнаружен в экспериментах на LHC."),
    ("Расскажи об атмосфере Юпитера.", "Атмосфера Юпитера состоит преимущественно из водорода и гелия."),  # не existence question
    ("Расскажи о Юпитере", "что угодно"),
    ("", ""),
    (JUPITER_QUERY, ""),
    ("", "Разумная жизнь на Юпитере не была обнаружена."),
]

EXISTENCE_QUERIES = [
    JUPITER_QUERY,
    MARS_QUERY,
    HIGGS_QUERY,
    "Расскажи о Юпитере",
    "Почему небо голубое?",
    "Какие условия на Марсе?",
    "",
    "есть ли хоть какая жизнь на Юпитере около звезды?",
    "Обнаружена ли вода на спутнике Европа?",
]

# Граничный случай на char-vs-byte в фильтре len(w)>=4 (_extract_existence_target): "лёд" — 3
# символа, но каждый кириллический символ — 2 байта в UTF-8, значит 6 байт. Символьная длина (3)
# < 4 — правильно ДОЛЖНО отсеяться; байтовая (6) >= 4 — неверно прошло бы фильтр, если бы длина
# считалась в байтах вместо символов.
CHAR_BYTE_TARGET_QUERY = "Есть ли лёд на Европе?"


def main() -> int:
    try:
        import yandi_rs.claim_evidence_retriever as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    from agent.claim_evidence_retriever import (
        _is_absence_claim,
        _is_existence_question,
        _extract_existence_target,
        _target_overlap,
        _classify_claim_role,
    )

    # ── A: _is_absence_claim ──
    for i, text in enumerate(ABSENCE_TRUE_CASES + ABSENCE_FALSE_CASES):
        py_r = _is_absence_claim(text)
        rs_r = rs.is_absence_claim(text)
        check(f"A{i} is_absence_claim совпадает — {text[:50]!r}", py_r == rs_r, f"python={py_r} rust={rs_r}")

    # ── B: _is_existence_question ──
    for i, q in enumerate(EXISTENCE_QUERIES):
        py_r = _is_existence_question(q)
        rs_r = rs.is_existence_question(q)
        check(f"B{i} is_existence_question совпадает — {q[:50]!r}", py_r == rs_r, f"python={py_r} rust={rs_r}")

    # ── C: _extract_existence_target ──
    for i, q in enumerate(EXISTENCE_QUERIES + [CHAR_BYTE_TARGET_QUERY]):
        py_r = _extract_existence_target(q)
        rs_r = list(rs.extract_existence_target(q))
        check(f"C{i} extract_existence_target совпадает — {q[:50]!r}", py_r == rs_r, f"python={py_r} rust={rs_r}")

    # ── D: _target_overlap (используем target_words из C-фикстур, применяем к каждому claim) ──
    all_claim_texts = [c for _, c in ROLE_CASES] + ABSENCE_TRUE_CASES + ABSENCE_FALSE_CASES
    for i, q in enumerate(EXISTENCE_QUERIES):
        target_words = _extract_existence_target(q)
        if not target_words:
            continue
        for j, claim_text in enumerate(all_claim_texts):
            lower = (claim_text or "").lower()
            py_r = _target_overlap(lower, target_words)
            rs_r = rs.target_overlap(lower, list(target_words))
            check(f"D{i}.{j} target_overlap совпадает", py_r == rs_r, f"python={py_r} rust={rs_r} words={target_words}")

    # ── E: _classify_claim_role ──
    for i, (q, claim_text) in enumerate(ROLE_CASES):
        py_r = _classify_claim_role(claim_text, q)
        rs_r = dict(rs.classify_claim_role(claim_text, q))
        check(f"E{i} classify_claim_role совпадает — {claim_text[:50]!r}", py_r == rs_r, f"python={py_r!r} rust={rs_r!r}")

    # ── H: якоря субъекта (_anchor_hit, _subject_anchor_matches) — кусок 26 ──
    import agent.claim_evidence_retriever as cer_mod2
    rng = random.Random(20260924)
    _h_ok = [0]

    def check_h(name, cond, detail=""):
        """Массовые проверки раздела H: успехи считаем, печатаем только провалы."""
        if cond:
            _h_ok[0] += 1
        else:
            check(name, cond, detail)
    anchors_pool = ["sun", "юпитер", "европейский союз", "c++", "++", "--", "a", "aa", "ы", "x-y", "а.б", "не", "ёж", "\u0301", "²", "_", "юпитер\u0301",
                    "nato", "e=mc²", "(x)", "[y]", "a b", "\x1c", "😀", "İstanbul", "ı", "ss", "ß", "\u0345", "\u093e", "\u0e31", "ª"]
    hay_atoms = ["sun", "sunlight", "sunday", "юпитер", "юпитере", "Юпитер", "европейский союз", "европейский  союз", "c++", "c++x", "xc++", "a", "aa", "aaa", "aaaa", "ы",
                 "x-y", "x-yz", "а.б", "не", "нее", "ёж", "ёжик", "\u0301", "²", "_", "юпитер\u0301", "nato", "e=mc²", "(x)", "[y]", "a b", "😀", "İstanbul", "ı", "ss", "ß",
                 "\u0345", "\u093e", "\u0e31", "\u0903", "ª", "sun\u0345", "sun\u093e", "\u0345sun", "क", "कि",
                 " ", "  ", ",", ".", "-", "\n", "\x1c", "1", "٣", "http://example.com/sun/jupiter"]
    def rnd_hay():
        return "".join(rng.choice(hay_atoms) + rng.choice(["", "", " ", "-", ",", "\u0301"]) for _ in range(rng.randint(0, 8)))
    def _run_h(engine, fn, *a, **k):
        saved = os.environ.get("YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE")
        try:
            if engine:
                os.environ["YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE"] = "rust"
            else:
                os.environ.pop("YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE", None)
            cer_mod2._rust_cer = None
            try:
                return fn(*a, **k)
            except Exception as e:                                    # noqa: BLE001
                return f"EXC:{type(e).__name__}"
        finally:
            if saved is None:
                os.environ.pop("YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE", None)
            else:
                os.environ["YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE"] = saved
            cer_mod2._rust_cer = None
    for anchor in anchors_pool + [""]:
        for hay in ["", anchor, " " + anchor + " ", anchor + anchor, "x" + anchor, anchor + "x", "-" + anchor + "-", anchor.upper()] + [rnd_hay() for _ in range(40)]:
            p_, r_ = _run_h(False, cer_mod2._anchor_hit, anchor, hay), _run_h(True, cer_mod2._anchor_hit, anchor, hay)
            check_h(f" anchor_hit {anchor!r} in {hay[:30]!r}", p_ == r_, f"python={p_} rust={r_}")
    for _ in range(3000):
        anchors = [rng.choice(anchors_pool + [""]) for _ in range(rng.randint(0, 3))]
        named = anchors if rng.random() < 0.6 else None
        content = anchors if named is None else rng.choice([None, [], ["юпитер"]])
        title, url, passage = rng.choice([None, "", rnd_hay()]), rng.choice([None, "", rnd_hay()]), rng.choice([None, "", rnd_hay()])
        claim = rng.choice(["", "Юпитер газовый гигант", "NATO и Европейский союз", "sun"])
        kw = dict(title=title, url=url, named_anchors=named, content_anchors=content)
        p_, r_ = _run_h(False, cer_mod2._subject_anchor_matches, claim, passage, **kw), _run_h(True, cer_mod2._subject_anchor_matches, claim, passage, **kw)
        check_h(f" subject_anchor_matches {anchors} t={str(title)[:15]!r}", p_ == r_, f"python={p_} rust={r_}")
    for bad in ([5], [None], ["ok", 3]):
        p_, r_ = _run_h(False, cer_mod2._subject_anchor_matches, "x", "юпитер", named_anchors=bad), _run_h(True, cer_mod2._subject_anchor_matches, "x", "юпитер", named_anchors=bad)
        check_h(f" типы якорей {bad}", p_ == r_, f"python={p_} rust={r_}")

    print(f"(раздел H: успешных массовых проверок {_h_ok[0]})")

    # ── G: сам переключатель ──
    saved_env = os.environ.get("YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE")
    try:
        import agent.claim_evidence_retriever as cer_mod

        os.environ.pop("YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE", None)
        cer_mod._rust_cer = None
        check("G0 по умолчанию (без переменной) активен Python-движок", cer_mod._get_rust_cer() is None)

        os.environ["YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE"] = "rust"
        cer_mod._rust_cer = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", cer_mod._get_rust_cer() is rs)

        for i, (q, claim_text) in enumerate(ROLE_CASES):
            switch_result = cer_mod._classify_claim_role(claim_text, q)
            direct_result = dict(rs.classify_claim_role(claim_text, q))
            check(f"G2.{i} переключённый classify_claim_role совпадает с прямым вызовом Rust",
                  switch_result == direct_result)

        for text in ABSENCE_TRUE_CASES[:3]:
            switch_result = cer_mod._is_absence_claim(text)
            direct_result = rs.is_absence_claim(text)
            check("G3 переключённый is_absence_claim совпадает с прямым вызовом Rust",
                  switch_result == direct_result)

        os.environ.pop("YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE", None)
        cer_mod._rust_cer = None
        check("G4 выключение переменной возвращает Python-движок (кэш не залипает)", cer_mod._get_rust_cer() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE", None)
        else:
            os.environ["YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE"] = saved_env
        cer_mod._rust_cer = None

    src = (ROOT / "agent" / "claim_evidence_retriever.py").read_text(encoding="utf-8")
    check("G5 сбой импорта yandi_rs пойман (ImportError) и не падает наружу",
          "except ImportError as e:" in src)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
