"""
agent/boundaries_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/boundaries.rs
даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/boundaries.py, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Одиннадцатый шаг переноса Python -> Rust (2026-09-23). Методология — rustlib/README.md.
Своего regression-теста у agent/boundaries.py не было — сценарии построены прочтением логики и
перепроверены напрямую через реальный Python перед тем, как стать проверками.

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.boundaries_rust_parity_test
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


TOXICITY_TEXTS = [
    "",
    "расскажи про Юпитер, пожалуйста",
    "ты тупой немного",
    "ты дебил конечно",
    "ты тупой мудак, иди нахуй",  # все три уровня — должен победить severe
    "ты дура и вообще ничего не понимаешь",  # mild + другое слово
    "ЗАТКНИСЬ",  # регистронезависимость
    "не соображаешь ты хорошо",
    "сдохни уже наконец",
    "хуёвый день сегодня",
]

APOLOGY_TEXTS = [
    "",
    "расскажи про Юпитер",
    "извини",  # короткое -> неискренне
    "sorry",
    "извини, но я был очень занят сегодня весь день подряд",  # оправдание
    "извини, я не прав был во всей этой истории целиком",  # прямое признание
    "прости пожалуйста, сожалею о случившемся полностью",
    "виноват, признаю свою ошибку сегодня",
    "неправ был, признаю целиком и полностью",
    "я думал что так будет лучше извини",  # оправдание "я думал"
]

RESPONSE_LEVELS = ["mild", "moderate", "severe", "neutral", "", "nonsense"]


def main() -> int:
    try:
        import yandi_rs.boundaries as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.boundaries as bd

    for i, text in enumerate(TOXICITY_TEXTS):
        py_r = bd.detect_toxicity(text)
        rs_r = dict(rs.detect_toxicity(text))
        rs_r["words"] = list(rs_r["words"])
        py_r_cmp = {**py_r, "words": list(py_r["words"])}
        check(f"T{i} detect_toxicity({text!r}) совпадает", py_r_cmp == rs_r, f"python={py_r_cmp!r} rust={rs_r!r}")

    for i, text in enumerate(APOLOGY_TEXTS):
        py_a = bd.is_apology(text)
        rs_a = tuple(rs.is_apology(text))
        check(f"A{i} is_apology({text!r}) совпадает", py_a == rs_a, f"python={py_a!r} rust={rs_a!r}")

    for level in RESPONSE_LEVELS:
        py_resp = bd.generate_response(level, {"anything": "ignored"})
        rs_resp = rs.generate_response(level)
        check(f"R {level!r} generate_response совпадает", py_resp == rs_resp, f"python={py_resp!r} rust={rs_resp!r}")

    for accepted in (True, False):
        py_ar = bd.generate_apology_response(accepted)
        rs_ar = rs.generate_apology_response(accepted)
        check(f"AR accepted={accepted} generate_apology_response совпадает", py_ar == rs_ar)

    # ── G: сам переключатель ──
    saved_env = os.environ.get("YANDI_BOUNDARIES_ENGINE")
    try:
        os.environ.pop("YANDI_BOUNDARIES_ENGINE", None)
        bd._rust_bd = None
        check("G0 по умолчанию (без переменной) активен Python-движок", bd._get_rust_bd() is None)

        os.environ["YANDI_BOUNDARIES_ENGINE"] = "rust"
        bd._rust_bd = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", bd._get_rust_bd() is rs)

        for i, text in enumerate(TOXICITY_TEXTS):
            switch_r = bd.detect_toxicity(text)
            direct_r = dict(rs.detect_toxicity(text))
            check(f"G2.{i} переключённый detect_toxicity совпадает с прямым вызовом Rust",
                  switch_r["level"] == direct_r["level"] and list(switch_r["words"]) == list(direct_r["words"]))
        for i, text in enumerate(APOLOGY_TEXTS):
            check(f"G3.{i} переключённый is_apology совпадает с прямым вызовом Rust",
                  bd.is_apology(text) == tuple(rs.is_apology(text)))

        os.environ.pop("YANDI_BOUNDARIES_ENGINE", None)
        bd._rust_bd = None
        check("G4 выключение переменной возвращает Python-движок (кэш не залипает)", bd._get_rust_bd() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_BOUNDARIES_ENGINE", None)
        else:
            os.environ["YANDI_BOUNDARIES_ENGINE"] = saved_env
        bd._rust_bd = None

    src = (ROOT / "agent" / "boundaries.py").read_text(encoding="utf-8")
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
