"""
agent/claim_types_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/claim_types.rs
даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/claim_types.py, на одних и тех же примерах — включая то,
что переключённая версия по-прежнему возвращает НАСТОЯЩИЕ Python Enum (ClaimType/ResponseMode),
а не строки или что-то ещё.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Восьмой шаг переноса Python -> Rust (2026-09-23). Методология — rustlib/README.md.

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.claim_types_rust_parity_test
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


GUESS_TEXTS = [
    "", "   ",
    "Существует ли Бог?",
    "Ты должен это сделать",
    "Как сделать бутерброд?",
    "Есть такая теория относительности",
    "В ходе эксперимента получены данные",
    "В чём смысл жизни и в чём ценность этого",
    "Это доказанный научный факт",
    "Юпитер — газовый гигант",
    "Доказано существование Бога наукой",  # оба маркера — должен победить первый по порядку
    "Обычное нейтральное предложение без маркеров",
]

TESTABILITY_VALUES = ["fully_testable", "partially_testable", "interpretive", "non_falsifiable", "unknown_value", ""]


def main() -> int:
    try:
        import yandi_rs.claim_types as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.claim_types as ct

    for claim_type in ct.ClaimType:
        py_mode = ct.get_response_mode(claim_type)
        rs_mode_str = rs.get_response_mode(claim_type.value)
        check(f"RM {claim_type} -> get_response_mode совпадает", py_mode.value == rs_mode_str,
              f"python={py_mode!r} rust={rs_mode_str!r}")

        py_web = ct.should_use_web_for_type(claim_type)
        rs_web = bool(rs.should_use_web_for_type(claim_type.value))
        check(f"WEB {claim_type} -> should_use_web_for_type совпадает", py_web == rs_web,
              f"python={py_web!r} rust={rs_web!r}")

    for mode in ct.ResponseMode:
        py_desc = ct.get_response_mode_description(mode)
        rs_desc = rs.get_response_mode_description(mode.value)
        check(f"DESC {mode} -> get_response_mode_description совпадает", py_desc == rs_desc,
              f"python={py_desc!r} rust={rs_desc!r}")

    for i, text in enumerate(GUESS_TEXTS):
        py_type = ct.guess_claim_type_by_text(text)
        rs_type_str = rs.guess_claim_type_by_text(text)
        check(f"G{i} guess_claim_type_by_text({text!r}) совпадает", py_type.value == rs_type_str,
              f"python={py_type!r} rust={rs_type_str!r}")

    for testability in TESTABILITY_VALUES:
        py_cap = ct.get_trust_cap_for_testability(testability)
        rs_cap = rs.get_trust_cap_for_testability(testability)
        check(f"T get_trust_cap_for_testability({testability!r}) совпадает", py_cap == rs_cap,
              f"python={py_cap!r} rust={rs_cap!r}")

    # ── переключённая версия возвращает НАСТОЯЩИЕ Python Enum, не строки ──
    saved_env = os.environ.get("YANDI_CLAIM_TYPES_ENGINE")
    try:
        os.environ.pop("YANDI_CLAIM_TYPES_ENGINE", None)
        ct._rust_ct = None
        check("G0 по умолчанию (без переменной) активен Python-движок", ct._get_rust_ct() is None)

        os.environ["YANDI_CLAIM_TYPES_ENGINE"] = "rust"
        ct._rust_ct = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", ct._get_rust_ct() is rs)

        switched_type = ct.guess_claim_type_by_text("Существует ли Бог?")
        check("G2 переключённый guess_claim_type_by_text возвращает НАСТОЯЩИЙ ClaimType",
              isinstance(switched_type, ct.ClaimType) and switched_type == ct.ClaimType.METAPHYSICAL_CLAIM,
              f"got {switched_type!r} ({type(switched_type)})")

        switched_mode = ct.get_response_mode(ct.ClaimType.FACTUAL)
        check("G3 переключённый get_response_mode возвращает НАСТОЯЩИЙ ResponseMode",
              isinstance(switched_mode, ct.ResponseMode) and switched_mode == ct.ResponseMode.FACTUAL,
              f"got {switched_mode!r} ({type(switched_mode)})")

        for claim_type in ct.ClaimType:
            check(f"G4.{claim_type} переключённый get_response_mode совпадает с прямым Rust+восстановлением",
                  ct.get_response_mode(claim_type).value == rs.get_response_mode(claim_type.value))

        os.environ.pop("YANDI_CLAIM_TYPES_ENGINE", None)
        ct._rust_ct = None
        check("G5 выключение переменной возвращает Python-движок (кэш не залипает)", ct._get_rust_ct() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_CLAIM_TYPES_ENGINE", None)
        else:
            os.environ["YANDI_CLAIM_TYPES_ENGINE"] = saved_env
        ct._rust_ct = None

    src = (ROOT / "agent" / "claim_types.py").read_text(encoding="utf-8")
    check("G6 сбой импорта yandi_rs пойман (ImportError) и не падает наружу",
          "except ImportError as e:" in src)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
