"""
agent/epistemic_claim_semantic_identity_hardening_regression_test.py —
Epistemic Core v1 Phase 9B regression: deterministic hardening guard
(agent/claim_semantic_identity_hardening.py::hardening_guard()) and its
wiring into classify_claim_pair()/classify_claim_pair_detailed().

The guard itself is pure regex — no network calls, fully deterministic,
tested directly here. The full real-corpus evaluation (50 pairs, real
Ollama calls, precision=1.000 recall=1.000 after hardening vs 0.800
before) is documented in YANDI_EPISTEMIC_CORE_V1_PHASE9B_HARDENING.md,
not re-run in this suite — same reasoning as Phase 9's suite (a
regression sweep must not depend on Ollama being reachable).

Run: /home/iam/venv/bin/python3 -m agent.epistemic_claim_semantic_identity_hardening_regression_test
"""

from unittest.mock import patch
import numpy as np

from agent.claim_semantic_identity_hardening import hardening_guard
from agent.claim_semantic_identity_prototype import classify_claim_pair_detailed
from agent.belief_manager import BeliefManager

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# ── 1. Each dimension the guard covers fires on a real asymmetric pair ──

DIMENSION_CASES = [
    ("causal_vs_correlational", "Курение вызывает рак лёгких.",
     "Курение статистически связано с раком лёгких."),
    ("necessary_vs_sufficient", "Кислород необходим для горения.",
     "Кислорода достаточно для горения."),
    ("possibility_vs_certainty", "Возможно, это верно.",
     "Это точно верно."),
    ("current_vs_historical", "Компания сейчас проводит реформу.",
     "Компания ранее проводила реформу."),
    ("absolute_vs_qualified", "Метод всегда работает.",
     "Метод обычно работает."),
    ("scope_all_vs_some", "Все птицы летают.",
     "Некоторые птицы летают."),
    ("prediction_vs_observation", "Ожидается рост показателей.",
     "Зафиксирован рост показателей."),
    ("absence_of_evidence_vs_evidence_of_absence", "Частица не найдена.",
     "Доказано отсутствие частицы."),
]

for label, a, b in DIMENSION_CASES:
    reason = hardening_guard(a, b)
    check(
        f"hardening_guard fires on a real {label} asymmetric pair",
        reason == f"{label}_marker_mismatch",
        f"got={reason!r}",
    )

# ── 2. Attribution and negation are one-sided checks ──

check(
    "attribution mismatch (one text attributed to a speaker, other bare) fires",
    hardening_guard("По словам эксперта, рынок растёт.", "Рынок растёт.") == "attribution_marker_mismatch",
)
check(
    "negation mismatch (one text negated, other not) fires",
    hardening_guard("Препарат эффективен.", "Препарат неэффективен.") == "negation_marker_mismatch",
)

# ── 3. Numeric mismatch fires generically (not a hardcoded 95-vs-96 special case) ──

check(
    "numeric mismatch fires for a moons-count-style pair",
    hardening_guard("У Юпитера 95 спутников.", "У Юпитера 96 спутников.") == "numeric_mismatch",
)
check(
    "numeric mismatch fires for a wholly different numeric pair (proves it's general, not one testcase's if)",
    hardening_guard("В отчёте указано 250 случаев.", "В отчёте указано 340 случаев.") == "numeric_mismatch",
)
check(
    "same numbers in both texts do NOT trigger a numeric mismatch",
    hardening_guard("В 1976 году компания была основана.", "Компания основана в 1976 году в гараже.") is None,
)

# ── 4. The guard does NOT fire on genuine paraphrases (recall must not be gutted) ──

PARAPHRASE_CASES = [
    ("Аспартам является одобренной безопасной пищевой добавкой согласно FDA.",
     "По данным FDA, аспартам признан допустимым и безопасным подсластителем."),
    ("Юпитер является крупнейшей планетой Солнечной системы.",
     "Крупнейшей планетой Солнечной системы является Юпитер."),
    ("Исследование показало снижение уровня холестерина у участников.",
     "У участников исследования зафиксировано снижение уровня холестерина."),
]
for a, b in PARAPHRASE_CASES:
    reason = hardening_guard(a, b)
    check(
        "guard does NOT fire on a genuine paraphrase (no dangerous marker mismatch present)",
        reason is None,
        f"unexpectedly fired: {reason!r} for {a!r} / {b!r}",
    )

# ── 5. Integration: classify_claim_pair_detailed downgrades 'equivalent' only when the guard fires ──

with patch.object(BeliefManager, "_embed_batch", return_value=np.array([[1.0, 0.0], [0.99, 0.14]], dtype=np.float32)), \
     patch.object(BeliefManager, "_llm_judge_relation", return_value="equivalent"):
    detail_mismatch = classify_claim_pair_detailed(
        "Курение вызывает рак лёгких.",
        "Курение статистически связано с раком лёгких.",
    )
check(
    "integration: LLM says 'equivalent' but a marker mismatch is present -> "
    "outcome downgraded to 'different', raw_verdict still recorded as 'equivalent'",
    detail_mismatch["outcome"] == "different"
    and detail_mismatch["raw_verdict"] == "equivalent"
    and detail_mismatch["guard_reason"] == "causal_vs_correlational_marker_mismatch",
    f"{detail_mismatch}",
)

with patch.object(BeliefManager, "_embed_batch", return_value=np.array([[1.0, 0.0], [0.99, 0.14]], dtype=np.float32)), \
     patch.object(BeliefManager, "_llm_judge_relation", return_value="equivalent"):
    detail_clean = classify_claim_pair_detailed(
        "Юпитер является крупнейшей планетой Солнечной системы.",
        "Крупнейшей планетой Солнечной системы является Юпитер.",
    )
check(
    "integration: LLM says 'equivalent', no marker mismatch -> outcome stays 'equivalent'",
    detail_clean["outcome"] == "equivalent" and detail_clean["guard_reason"] is None,
    f"{detail_clean}",
)

# ── 6. The guard never touches 'contradicts' or 'different' verdicts, only 'equivalent' ──

with patch.object(BeliefManager, "_embed_batch", return_value=np.array([[1.0, 0.0], [0.99, 0.14]], dtype=np.float32)), \
     patch.object(BeliefManager, "_llm_judge_relation", return_value="contradicts"):
    detail_contra = classify_claim_pair_detailed(
        "Курение вызывает рак лёгких.",
        "Курение статистически связано с раком лёгких.",
    )
check(
    "integration: a 'contradicts' verdict is never touched by the guard, even with a marker mismatch present",
    detail_contra["outcome"] == "contradicts" and detail_contra["guard_reason"] is None,
    f"{detail_contra}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
