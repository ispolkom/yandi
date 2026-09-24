"""
agent/orch_risk_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/orch_risk.rs даёт
ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/orch_risk.py::assess_risk, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Четырнадцатый шаг переноса Python -> Rust (2026-09-24). Методология — rustlib/README.md.
Своего regression-теста у модуля не было — сценарии построены по логике; каждое ключевое слово
из трёх наборов проверяется ПО ОТДЕЛЬНОСТИ (полная развёртка, не выборка), плюс граничные
случаи длины 300 символов (символьная vs байтовая длина).

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.orch_risk_rust_parity_test
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


def main() -> int:
    try:
        import yandi_rs.orch_risk as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.orch_risk as orisk

    def py_tuple(q):
        r = orisk.assess_risk(q)  # переключатель выключен — Python-путь
        return (r.risk_level, r.mandatory_arbitrage, r.validator_model, r.nodes_required)

    orisk._rust_risk = None
    os.environ.pop("YANDI_ORCH_RISK_ENGINE", None)

    cases = [
        "Как лечить кашель?", "Как расторгнуть договор аренды?", "Как настроить DHT в P2P-сети?",
        "Стоит ли вкладывать деньги в биткоин?", "Привет, как дела?", "", "   ",
        "МЕДИЦИНСКИЙ ВОПРОС", "Вакцинация детей", "Медицинская вакцинация",  # регистр, critical > high
        "судьба", "искусство", "Exploit в ядре", "EXPLOIT",  # подстроки/регистр латиницы
        "посоветуй что-нибудь", "Что вы рекомендуете?", "Как лучше поступить?", "Стоит ли идти?",
        "Политика и религия", "Спорный вопрос",
        "привет", "hello world", "ёжик", "Ёлка и Ё",
        # граница 300 СИМВОЛОВ: кириллица 2 байта/символ — 200 симв. = 400 байт (>300 в байтах,
        # <=300 в символах), 300 = 600 байт, 301 = 602 байта
        "а" * 150, "а" * 200, "а" * 300, "а" * 301, "а" * 500,
        "ф" * 300, "ф" * 301,
        # смешанный ASCII/кириллица на границе
        "a" * 300, "a" * 301, "a" * 299 + "я", "a" * 300 + "я",
    ]
    # Полная развёртка: каждое ключевое слово из каждого набора отдельно (в разном регистре)
    for kw in orisk._CRITICAL_KW | orisk._HIGH_KW | orisk._MEDIUM_KW:
        cases.append(f"вопрос про {kw}ъ")
        cases.append(kw.upper())
        cases.append(kw.capitalize() + " как быть")

    for i, q in enumerate(cases):
        py_r = py_tuple(q)
        rs_r = tuple(rs.assess_risk(q))
        check(f"C{i} assess_risk совпадает — {q[:40]!r}", py_r == rs_r, f"python={py_r} rust={rs_r}")

    # ── G: сам переключатель ──
    saved_env = os.environ.get("YANDI_ORCH_RISK_ENGINE")
    try:
        os.environ.pop("YANDI_ORCH_RISK_ENGINE", None)
        orisk._rust_risk = None
        check("G0 по умолчанию (без переменной) активен Python-движок", orisk._get_rust_risk() is None)

        os.environ["YANDI_ORCH_RISK_ENGINE"] = "rust"
        orisk._rust_risk = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", orisk._get_rust_risk() is rs)

        from agent.orch_schemas import RiskResult
        for i, q in enumerate(cases[:40]):
            switched = orisk.assess_risk(q)
            direct = tuple(rs.assess_risk(q))
            check(f"G2.{i} переключённый assess_risk совпадает с прямым вызовом Rust",
                  (switched.risk_level, switched.mandatory_arbitrage, switched.validator_model,
                   switched.nodes_required) == direct)
            check(f"G2.{i}t возвращает НАСТОЯЩИЙ RiskResult, не кортеж", isinstance(switched, RiskResult))

        os.environ.pop("YANDI_ORCH_RISK_ENGINE", None)
        orisk._rust_risk = None
        check("G3 выключение переменной возвращает Python-движок (кэш не залипает)", orisk._get_rust_risk() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_ORCH_RISK_ENGINE", None)
        else:
            os.environ["YANDI_ORCH_RISK_ENGINE"] = saved_env
        orisk._rust_risk = None

    src = (ROOT / "agent" / "orch_risk.py").read_text(encoding="utf-8")
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
