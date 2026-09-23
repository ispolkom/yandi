"""
agent/claim_answer_linker_rust_parity_test.py — доказательство, что
rustlib/yandi_rs/src/claim_answer_linker.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и
agent/claim_answer_linker.py::ClaimAnswerLinker.link_answer_to_claims, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Двенадцатый шаг переноса Python -> Rust (2026-09-23). Методология — rustlib/README.md.
Своего regression-теста у этого модуля не было — сценарии построены прочтением логики.

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.claim_answer_linker_rust_parity_test
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


ANSWER_1 = (
    "Сознание определяется как способность к субъективному восприятию. "
    "Оно возникает из активности нейронных сетей. "
    "Современная наука изучает его через функциональную роль в принятии решений."
)
CLAIMS_1 = [
    {"claim_id": "cl_001", "claim_text": "Сознание определяется как способность к субъективному восприятию"},
    {"claim_id": "cl_002", "claim_text": "Нейробиология связывает сознание с активностью коры головного мозга"},
    {"claim_id": "cl_003", "claim_text": "В современной психологии сознание определяется по его функциональной роли"},
]

CASES = [
    (ANSWER_1, CLAIMS_1),
    ("", CLAIMS_1),  # пустой ответ
    (ANSWER_1, []),  # нет claims
    ("Совсем короткий ответ.", CLAIMS_1),  # ответ не даёт ключевых фраз (< 20 символов)
    # РОВНО тот случай, где символьная и байтовая длина расходятся: 15 кириллических символов =
    # 30 байт в UTF-8 — символьная длина (15) НЕ больше 20 (правильно НЕ должна пройти фильтр),
    # байтовая (30) больше 20 (неверно прошла бы, если считать байты вместо символов).
    ("Оченькороткое. " + "Пятнадцатьсимв.", CLAIMS_1),
    (ANSWER_1, [{"claim_text": "Сознание определяется как способность к субъективному восприятию"}]),  # нет claim_id
    (ANSWER_1, [{"claim_id": 42, "claim_text": "Сознание определяется как способность к субъективному восприятию"}]),  # claim_id — число, не строка
    (ANSWER_1, [{"claim_id": "cl_x", "claim_text": ""}]),  # пустой claim_text
    (ANSWER_1, [{"claim_id": "cl_y"}]),  # нет claim_text вообще
    ("Юпитер является крупнейшей планетой Солнечной системы по объёму и массе.", [
        {"claim_id": "a", "claim_text": "Юпитер — крупнейшая планета Солнечной системы"},
        {"claim_id": "b", "claim_text": "Марс — четвёртая планета от Солнца"},
    ]),
]


def main() -> int:
    try:
        import yandi_rs.claim_answer_linker as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    from agent.claim_answer_linker import ClaimAnswerLinker

    linker = ClaimAnswerLinker()

    for i, (answer, claims) in enumerate(CASES):
        py_result = linker.link_answer_to_claims(answer, claims)
        rs_result = tuple(rs.link_answer_to_claims(answer, claims))
        check(f"C{i} link_answer_to_claims совпадает", py_result == rs_result,
              f"python={py_result!r} rust={rs_result!r}")

    # ── extract_key_phrases / is_claim_supporting напрямую ──
    for i, (answer, _claims) in enumerate(CASES):
        py_phrases = linker._extract_key_phrases(answer)
        rs_phrases = list(rs.extract_key_phrases(answer))
        check(f"P{i} _extract_key_phrases совпадает", py_phrases == rs_phrases,
              f"python={py_phrases!r} rust={rs_phrases!r}")

    for i, (answer, claims) in enumerate(CASES):
        phrases = linker._extract_key_phrases(answer)
        for j, claim in enumerate(claims):
            text = claim.get("claim_text", "")
            py_s = linker._is_claim_supporting(text, phrases)
            rs_s = rs.is_claim_supporting(text, phrases)
            check(f"S{i}.{j} _is_claim_supporting совпадает", py_s == rs_s)

    # ── G: сам переключатель ──
    saved_env = os.environ.get("YANDI_CLAIM_ANSWER_LINKER_ENGINE")
    try:
        import agent.claim_answer_linker as cal_mod

        os.environ.pop("YANDI_CLAIM_ANSWER_LINKER_ENGINE", None)
        cal_mod._rust_cal = None
        check("G0 по умолчанию (без переменной) активен Python-движок", cal_mod._get_rust_cal() is None)

        os.environ["YANDI_CLAIM_ANSWER_LINKER_ENGINE"] = "rust"
        cal_mod._rust_cal = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", cal_mod._get_rust_cal() is rs)

        for i, (answer, claims) in enumerate(CASES):
            switch_result = linker.link_answer_to_claims(answer, claims)
            direct_result = tuple(rs.link_answer_to_claims(answer, claims))
            check(f"G2.{i} переключённый link_answer_to_claims совпадает с прямым вызовом Rust",
                  switch_result == direct_result)

        os.environ.pop("YANDI_CLAIM_ANSWER_LINKER_ENGINE", None)
        cal_mod._rust_cal = None
        check("G3 выключение переменной возвращает Python-движок (кэш не залипает)", cal_mod._get_rust_cal() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_CLAIM_ANSWER_LINKER_ENGINE", None)
        else:
            os.environ["YANDI_CLAIM_ANSWER_LINKER_ENGINE"] = saved_env
        cal_mod._rust_cal = None

    src = (ROOT / "agent" / "claim_answer_linker.py").read_text(encoding="utf-8")
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
