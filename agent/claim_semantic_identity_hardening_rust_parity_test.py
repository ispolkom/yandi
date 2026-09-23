"""
agent/claim_semantic_identity_hardening_rust_parity_test.py — доказательство, что
rustlib/yandi_rs/src/claim_semantic_identity_hardening.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и
agent/claim_semantic_identity_hardening.py::hardening_guard(), на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Четвёртый шаг переноса Python -> Rust (2026-09-23). Методология — rustlib/README.md.

Сценарии собраны из agent/epistemic_claim_semantic_identity_hardening_regression_test.py и
agent/polarity_hardening_regression_test.py — включая случай ("Частица не найдена." /
"Доказано отсутствие частицы."), который при первом переносе поймал реальную ошибку
транскрипции (пропущенный флаг (?i) на одном из паттернов) ещё до этого файла — на уровне
Rust-юнит-тестов; здесь он остаётся как постоянная регрессия на двух реализациях сразу.

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.claim_semantic_identity_hardening_rust_parity_test
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


PAIRS: list[tuple[str, str]] = [
    # ── каждое измерение по отдельности ──
    ("Курение вызывает рак лёгких.", "Курение статистически связано с раком лёгких."),
    ("Кислород необходим для горения.", "Кислорода достаточно для горения."),
    ("Возможно, это верно.", "Это точно верно."),
    ("Компания сейчас проводит реформу.", "Компания ранее проводила реформу."),
    ("Метод всегда работает.", "Метод обычно работает."),
    ("Все птицы летают.", "Некоторые птицы летают."),
    ("Ожидается рост показателей.", "Зафиксирован рост показателей."),
    # именно эта пара при первом переносе поймала пропущенный (?i)
    ("Частица не найдена.", "Доказано отсутствие частицы."),
    # ── attribution / negation (одностороннее) ──
    ("По словам эксперта, рынок растёт.", "Рынок растёт."),
    ("Препарат эффективен.", "Препарат неэффективен."),
    # ── числа ──
    ("У Юпитера 95 спутников.", "У Юпитера 96 спутников."),
    ("В отчёте указано 250 случаев.", "В отчёте указано 340 случаев."),
    ("В 1976 году компания была основана.", "Компания основана в 1976 году в гараже."),
    # РОВНО тот случай, где "оба должны быть непустые" и "хотя бы одно непустое" расходятся:
    # число есть только с одной стороны — правильно НЕ должно сработать как numeric_mismatch
    ("Спутник был выведен на орбиту в 2020 году.", "Спутник был выведен на орбиту."),
    # ── настоящие парафразы — guard НЕ должен сработать ──
    ("Аспартам является одобренной безопасной пищевой добавкой согласно FDA.",
     "По данным FDA, аспартам признан допустимым и безопасным подсластителем."),
    ("Юпитер является крупнейшей планетой Солнечной системы.",
     "Крупнейшей планетой Солнечной системы является Юпитер."),
    ("Исследование показало снижение уровня холестерина у участников.",
     "У участников исследования зафиксировано снижение уровня холестерина."),
    # ── "не явля-" семья (исторический баг с trailing \b) ──
    ("Кофе является канцерогеном.", "Кофе не является канцерогеном."),
    ("Это утверждение эквивалентно.", "Эти утверждения не являются эквивалентными."),
    # ── новые предикатные основы (§2 Этап 4D-1) ──
    ("Кофе вызывает рак.", "Кофе не вызывает рак."),
    ("X влияет на Y.", "X не влияет на Y."),
    ("Исследование подтверждает связь.", "Исследование не подтверждает связь."),
    ("Препарат снижает риск.", "Препарат не снижает риск."),
    ("X приводит к Y.", "X не приводит к Y."),
    # ── идиомы "не", которые НЕ должны ложно сработать ──
    ("Это не только экономический союз, но и политический.", "Это экономический и политический союз."),
    ("В ЕС входит не менее 27 государств.", "В ЕС входит 27 государств."),
    ("В ЕС входит не более 27 государств.", "В ЕС входит 27 государств."),
    ("Это не обязательно означает рост.", "Это означает рост."),
    ("Это не просто экономический союз.", "Это экономический союз."),
    # ── английское отрицание ──
    ("Coffee causes cancer.", "Coffee does not cause cancer."),
    ("This is proven.", "This is not proven."),
    ("It can be true.", "It cannot be true."),
    ("It was proven.", "It was not proven."),
    ("It works.", "It doesn't work."),
    # ── симметрия ──
    ("Кофе не вызывает рак.", "Кофе вызывает рак."),
    # ── парафраз с той же полярностью (не должен сработать даже с новыми основами) ──
    ("Кофе вызывает рак у некоторых людей.", "У некоторых людей кофе вызывает рак."),
    # ── субъекты/entity mismatch ──
    ("На Юпитере обнаружены кольца.", "На Сатурне обнаружены кольца."),
    ("ЕС ввёл новые санкции.", "НАТО провело учения."),
    ("Просто предложение без явного субъекта.", "Другое простое предложение тоже без субъекта."),
    # ── пустые/вырожденные ──
    ("", ""),
    ("", "Курение вызывает рак."),
    ("Курение вызывает рак.", ""),
]


def main() -> int:
    try:
        import yandi_rs.claim_semantic_identity_hardening as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.claim_semantic_identity_hardening as py

    for i, (a, b) in enumerate(PAIRS):
        py_result = py.hardening_guard(a, b)
        rs_result = rs.hardening_guard(a, b)
        check(f"P{i} hardening_guard({a!r}, {b!r}) совпадает", py_result == rs_result,
              f"python={py_result!r} rust={rs_result!r}")
        # симметрия — заодно ещё одна проверка на каждую пару, бесплатно
        py_rev = py.hardening_guard(b, a)
        rs_rev = rs.hardening_guard(b, a)
        check(f"P{i}r обратный порядок тоже совпадает", py_rev == rs_rev, f"python={py_rev!r} rust={rs_rev!r}")

    # ── G: сам переключатель ──
    saved_env = os.environ.get("YANDI_HARDENING_ENGINE")
    try:
        os.environ.pop("YANDI_HARDENING_ENGINE", None)
        py._rust_hardening = None
        check("G0 по умолчанию (без переменной) активен Python-движок", py._get_rust_hardening() is None)

        os.environ["YANDI_HARDENING_ENGINE"] = "rust"
        py._rust_hardening = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", py._get_rust_hardening() is rs)

        for i, (a, b) in enumerate(PAIRS):
            check(f"G2.{i} переключённый hardening_guard совпадает с прямым вызовом Rust",
                  py.hardening_guard(a, b) == rs.hardening_guard(a, b))

        os.environ.pop("YANDI_HARDENING_ENGINE", None)
        py._rust_hardening = None
        check("G3 выключение переменной возвращает Python-движок (кэш не залипает)", py._get_rust_hardening() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_HARDENING_ENGINE", None)
        else:
            os.environ["YANDI_HARDENING_ENGINE"] = saved_env
        py._rust_hardening = None

    src = (ROOT / "agent" / "claim_semantic_identity_hardening.py").read_text(encoding="utf-8")
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
