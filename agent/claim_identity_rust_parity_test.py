"""
agent/claim_identity_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/claim_identity.rs
даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/claim_identity.py, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Третий шаг переноса Python -> Rust (2026-09-23; первые два — pet/local_guard.py,
pet/web_login.py). Методология — rustlib/README.md.

Сценарии собраны из agent/epistemic_claim_identity_regression_test.py и
agent/claim_evidence_bilingual_subject_gate_regression_test.py плюс граничные случаи, которые
эти файлы прямым текстом не перечисляют (символьная vs байтовая длина, порядок alias-групп).

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.claim_identity_rust_parity_test
"""
from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


CANONICALIZE_TEXTS = [
    "",
    "   \n\t  ",
    "Юпитер   имеет    кольца.",
    "  Юпитер\nимеет\tкольца.  ",
    unicodedata.normalize("NFC", "Café was founded in 1976."),
    unicodedata.normalize("NFD", "Café was founded in 1976."),
    "ЮПИТЕР", "юпитер",
    "Jupiter", "JUPITER",
    "Aspartame is safe.", "ASPARTAME IS SAFE.",
    "Аспартам безопасен.", "АСПАРТАМ БЕЗОПАСЕН.",
    "Aspartame is safe.", "Aspartame is safe",
    "Aspartame is safe!",
    "Aspartame, is safe.",
    "Привет, мир!", "Привет, мир",
    "Что?!", "Что",
    "А, Б", "А Б",
    "Юпитер — крупнейшая планета.", "Jupiter is the largest planet.",
    "У Юпитера 95 спутников.", "У Юпитера 96 спутников.",
    "...", "!!!", "…", "  .  !  ?  ",
    "Слово-с-дефисом.",
    "Mixed РУССКИЙ and English text.",
    "emoji 🚀 stays as-is? maybe not stripped",
    "многоточие…",
    "line1\nline2\r\nline3",
]

SUBJECT_ANCHOR_TEXTS = [
    "",
    "   ",
    "На Юпитере разумная жизнь не обнаружена",
    "Если посмотреть на это, естественно, есть нюанс.",
    "Европейский союз обсуждает бюджет.",
    "ЕС и НАТО провели встречу.",
    "Еврозона столкнулась с инфляцией.",
    "На Марсе и Венере искали воду.",
    "Натощак нельзя есть.",  # regression: "нато" не должен false-positive внутри "Натощак"
    "Наточить нож перед готовкой.",
    "Просто обычное предложение без заглавных слов внутри",
    "NASA launched a mission to Jupiter and Saturn.",
    "Юпитера юпитером юпитере — разные падежи одного слова",
    "Sun is not in the alias table at all",
]

CONTENT_ANCHOR_TEXTS = [
    "",
    "   ",
    "Есть ли жизнь на Солнце?",
    "Is there life on the Sun?",
    "primary evidence research study data observations confirmed detection",
    "Если посмотреть на это, естественно, есть нюанс.",
    "уфа большой город",  # короткий кириллический токен ровно 3 символа (6 байт)
    "их дом стоит на горе",  # РОВНО тот случай, где символьная и байтовая длина расходятся:
                              # "их" = 2 символа (должно отфильтроваться), но 4 байта в UTF-8
                              # (byte-length штамп НЕ отфильтровал бы) — не стоп-слово, не filler
    "ab a bb короткие слова длиной меньше трёх символов отфильтровать",
    "The Evidence Confirmed Discovery of primary Source",  # регистронезависимость фильтров
    "Повторение повторение повторение слов слов",  # стабильная дедупликация
]


def main() -> int:
    try:
        import yandi_rs.claim_identity as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.claim_identity as py

    for i, text in enumerate(CANONICALIZE_TEXTS):
        py_canon = py.canonicalize_claim_text(text)
        rs_canon = rs.canonicalize_claim_text(text)
        check(f"C{i} canonicalize_claim_text({text!r}) совпадает", py_canon == rs_canon,
              f"python={py_canon!r} rust={rs_canon!r}")

        py_hash = py.compute_claim_content_hash(text)
        rs_hash = rs.compute_claim_content_hash(text)
        check(f"H{i} compute_claim_content_hash({text!r}) совпадает", py_hash == rs_hash,
              f"python={py_hash!r} rust={rs_hash!r}")

    for i, text in enumerate(SUBJECT_ANCHOR_TEXTS):
        py_anchors = py.extract_subject_anchors(text)
        rs_anchors = rs.extract_subject_anchors(text)
        check(f"S{i} extract_subject_anchors({text!r}) совпадает (включая порядок)",
              py_anchors == rs_anchors, f"python={py_anchors!r} rust={rs_anchors!r}")

    for i, text in enumerate(CONTENT_ANCHOR_TEXTS):
        py_anchors = py.extract_content_anchors(text)
        rs_anchors = rs.extract_content_anchors(text)
        check(f"A{i} extract_content_anchors({text!r}) совпадает (включая порядок)",
              py_anchors == rs_anchors, f"python={py_anchors!r} rust={rs_anchors!r}")

    # ── G: сам переключатель в agent/claim_identity.py ──
    import os
    saved_env = os.environ.get("YANDI_CLAIM_IDENTITY_ENGINE")
    try:
        os.environ.pop("YANDI_CLAIM_IDENTITY_ENGINE", None)
        py._rust_ci = None
        check("G0 по умолчанию (без переменной) активен Python-движок", py._get_rust_ci() is None)

        os.environ["YANDI_CLAIM_IDENTITY_ENGINE"] = "rust"
        py._rust_ci = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", py._get_rust_ci() is rs)

        for i, text in enumerate(CANONICALIZE_TEXTS):
            check(f"G2.{i} переключённый canonicalize_claim_text совпадает с прямым вызовом Rust",
                  py.canonicalize_claim_text(text) == rs.canonicalize_claim_text(text))
            check(f"G3.{i} переключённый compute_claim_content_hash совпадает с прямым вызовом Rust",
                  py.compute_claim_content_hash(text) == rs.compute_claim_content_hash(text))
        for i, text in enumerate(SUBJECT_ANCHOR_TEXTS):
            check(f"G4.{i} переключённый extract_subject_anchors совпадает с прямым вызовом Rust",
                  py.extract_subject_anchors(text) == rs.extract_subject_anchors(text))
        for i, text in enumerate(CONTENT_ANCHOR_TEXTS):
            check(f"G5.{i} переключённый extract_content_anchors совпадает с прямым вызовом Rust",
                  py.extract_content_anchors(text) == rs.extract_content_anchors(text))

        os.environ.pop("YANDI_CLAIM_IDENTITY_ENGINE", None)
        py._rust_ci = None
        check("G6 выключение переменной возвращает Python-движок (кэш не залипает)", py._get_rust_ci() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_CLAIM_IDENTITY_ENGINE", None)
        else:
            os.environ["YANDI_CLAIM_IDENTITY_ENGINE"] = saved_env
        py._rust_ci = None

    src = (ROOT / "agent" / "claim_identity.py").read_text(encoding="utf-8")
    check("G7 сбой импорта yandi_rs пойман (ImportError) и не падает наружу",
          "except ImportError as e:" in src)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
