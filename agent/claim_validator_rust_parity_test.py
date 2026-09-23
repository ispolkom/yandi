"""
agent/claim_validator_rust_parity_test.py — доказательство, что
rustlib/yandi_rs/src/claim_validator.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и
agent/claim_validator.py::ClaimValidator.normalize_claim_text/validate, на одних и тех же
примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Шестой шаг переноса Python -> Rust (2026-09-23). Методология — rustlib/README.md.

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.claim_validator_rust_parity_test
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


NORMALIZE_TEXTS = [
    "", "   ",
    "- Юпитер является крупнейшей планетой",
    "* Юпитер является крупнейшей планетой",
    "+ Юпитер является крупнейшей планетой",
    "1. Юпитер является крупнейшей планетой",
    "2) Юпитер является крупнейшей планетой",
    "  10.   Отступы вокруг номера",
    "**Юпитер является планетой**",
    "**не закрыто в конце",
    "Обычный текст без разметки",
    "-Без пробела после маркера",
    "- - Двойной маркер списка",
]

VALIDATE_TEXTS = [
    "", "коротко", "A" * 19, "A" * 20, "A" * 300, "A" * 301,
    "Вот извлеченные атомарные claims:",
    "Анализ текста модели показывает важные закономерности в данных.",
    "Ответ на вопрос о жизни на Марсе содержит противоречивую информацию.",
    "Модель утверждает, что дальнейший анализ не требуется для этого случая.",
    "Из текста были извлечены основные факты о планетах солнечной системы.",
    "Источник содержит подробное описание процесса эволюции звёзд.",
    "Согласно источнику, информация о планете полностью подтверждена данными.",
    "Данный документ описывает основные принципы термоядерного синтеза.",
    "По имеющейся информации ответ содержит неточности в оценке данных.",
    "По имеющимся данным жизнь на Марсе пока не обнаружена никем вообще.",
    "Некоторые источники указывают на возможное наличие воды подо льдом.",
    "Юпитер является крупнейшей планетой Солнечной системы по объёму.",
    "Первый признак жизни датируется 3,5 млрд лет назад согласно находкам.",
    "Александр Македонский основал множество городов на завоёванных землях.",
    "Компания была основана в тысяча девятьсот семьдесят шестом году точно.",
    "| колонка | значение |",
    "---",
    "=== Заголовок раздела ===",
    "Вода состоит из двух атомов водорода и одного атома кислорода всегда.",
]


def main() -> int:
    try:
        import yandi_rs.claim_validator as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    from agent.claim_validator import ClaimValidator

    for i, text in enumerate(NORMALIZE_TEXTS):
        py_norm = ClaimValidator.normalize_claim_text(text)
        rs_norm = rs.normalize_claim_text(text)
        check(f"N{i} normalize_claim_text({text!r}) совпадает", py_norm == rs_norm,
              f"python={py_norm!r} rust={rs_norm!r}")

    validator = ClaimValidator()
    for i, text in enumerate(VALIDATE_TEXTS):
        py_result = validator.validate(text)
        rs_result = tuple(rs.validate(text))
        check(f"V{i} validate({text!r}) совпадает", py_result == rs_result,
              f"python={py_result!r} rust={rs_result!r}")

    # ── G: сам переключатель ──
    saved_env = os.environ.get("YANDI_CLAIM_VALIDATOR_ENGINE")
    try:
        import agent.claim_validator as cv_mod

        os.environ.pop("YANDI_CLAIM_VALIDATOR_ENGINE", None)
        cv_mod._rust_cv = None
        check("G0 по умолчанию (без переменной) активен Python-движок", cv_mod._get_rust_cv() is None)

        os.environ["YANDI_CLAIM_VALIDATOR_ENGINE"] = "rust"
        cv_mod._rust_cv = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", cv_mod._get_rust_cv() is rs)

        for i, text in enumerate(NORMALIZE_TEXTS):
            check(f"G2.{i} переключённый normalize_claim_text совпадает с прямым вызовом Rust",
                  ClaimValidator.normalize_claim_text(text) == rs.normalize_claim_text(text))
        for i, text in enumerate(VALIDATE_TEXTS):
            check(f"G3.{i} переключённый validate совпадает с прямым вызовом Rust",
                  validator.validate(text) == tuple(rs.validate(text)))

        os.environ.pop("YANDI_CLAIM_VALIDATOR_ENGINE", None)
        cv_mod._rust_cv = None
        check("G4 выключение переменной возвращает Python-движок (кэш не залипает)", cv_mod._get_rust_cv() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_CLAIM_VALIDATOR_ENGINE", None)
        else:
            os.environ["YANDI_CLAIM_VALIDATOR_ENGINE"] = saved_env
        cv_mod._rust_cv = None

    src = (ROOT / "agent" / "claim_validator.py").read_text(encoding="utf-8")
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
