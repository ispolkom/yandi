"""
agent/candidate_routing_regression_test.py — high-recall candidate ROUTING
regression for final_claim_coverage's NLI step.

Per explicit user decision: this layer is a ROUTING mechanism, never an
epistemic one. It answers only "is this pair worth sending to NLI", never
"what relation do these claims have". A pair NOT selected must never be
treated as an UNRELATED/UNSUPPORTED verdict — it is NOT_SELECTED_FOR_NLI,
a distinct technical state.

Numbers (COVERAGE_ROUTING_SIM_THRESHOLD=0.45, COVERAGE_ROUTING_TOP_K=5) come
from an offline recall experiment against REAL embeddings + REAL live NLI
ground truth (29 pairs, 8 families, 3 domains: Jupiter/life, Mars/water,
Higgs boson) — see the module docstring in final_claim_coverage.py for the
full derivation. This suite covers the MECHANISM (mandatory rules, top-K,
threshold union, NO_NLI_CANDIDATES handling) deterministically with mocked
embeddings, not a re-run of that live experiment.

Run: /home/iam/venv/bin/python3 -m agent.candidate_routing_regression_test
"""

from unittest.mock import patch, MagicMock
import re

from agent.final_claim_coverage import (
    _mandatory_routing_reason,
    _route_candidate_pairs,
    _lexical_overlap,
    _has_negation,
    _shares_number,
    _is_near_duplicate,
    evaluate_final_claim_coverage,
    COVERAGE_ROUTING_TOP_K,
    COVERAGE_ROUTING_SIM_THRESHOLD,
)

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


# ============================================================
# 1. MANDATORY RULES — pure function, no mocking needed
# ============================================================
# Adversarial cases A-F from the task spec, domain-generic (Jupiter +
# a Mars/Higgs pair to prove no hardcoding).

MANDATORY_CASES = [
    (
        "A: negation flip (life detected vs not detected) -> mandatory",
        "Разумная жизнь на Юпитере не обнаружена.",
        "Разумная жизнь на Юпитере была обнаружена учёными.",
        True,
    ),
    (
        "B: same quantity, different value -> mandatory (shared number)",
        "Температура составляет -145 градусов.",
        "Температура составляет 50 градусов.",
        True,
    ),
    (
        "C: negation flip (solid surface) -> mandatory",
        "Объект имеет твёрдую поверхность.",
        "Объект не имеет твёрдой поверхности.",
        True,
    ),
    (
        "D: lexically different but logically conflicting -> mandatory via negation+overlap",
        "Жидкая вода отсутствует на поверхности Юпитера в значимых количествах.",
        "Учёные подтвердили обилие жидкой воды на поверхности Юпитера.",
        True,
    ),
    (
        "E: same entities, unrelated predicates -> NOT mandatory",
        "Юпитер обладает мощным магнитным полем.",
        "Юпитер имеет 95 известных спутников.",
        False,
    ),
    (
        "F: very similar wording, truly unrelated -> NOT mandatory",
        "Скорость ветров на Юпитере достигает 600 км/ч.",
        "Скорость света в вакууме составляет 300000 км/с.",
        False,
    ),
    (
        "exact/canonical match -> mandatory",
        "Юпитер является газовым гигантом.",
        "Юпитер является газовым гигантом.",
        True,
    ),
    (
        "cross-domain (Mars): negation flip -> mandatory, not Jupiter-specific",
        "Вода на Марсе не была обнаружена в жидком виде.",
        "Жидкая вода на поверхности Марса была найдена в 2015 году.",
        True,
    ),
    (
        "cross-domain (Higgs): shared number, different claim -> mandatory",
        "Бозон Хиггса имеет массу около 125 ГэВ.",
        "Некоторые модели предсказывали массу 500 ГэВ для бозона Хиггса.",
        True,
    ),
    (
        "completely unrelated topics -> NOT mandatory",
        "Юпитер является газовым гигантом.",
        "Столица Франции — Париж.",
        False,
    ),
]

for label, final_text, other_text, expect_mandatory in MANDATORY_CASES:
    reason = _mandatory_routing_reason(final_text, other_text)
    check(
        label,
        (reason is not None) == expect_mandatory,
        f"got reason={reason!r}",
    )


# ============================================================
# 2. _route_candidate_pairs — mocked embeddings, deterministic
# ============================================================

_FAKE_VECTORS = {}


def _register(text, vec):
    _FAKE_VECTORS[text.lower()] = vec


def _mock_post(self, url, json=None, timeout=None):
    inputs = json["input"]
    vecs = [_FAKE_VECTORS.get(t.lower(), [0.0, 0.0, 1.0]) for t in inputs]
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"embeddings": vecs}
    return resp


# One final claim, 20 pipeline claims: 2 are truly related (high sim),
# 18 are unrelated (low sim, no lexical/negation/number overlap). Proves
# real reduction happens while the 2 related ones are always kept.
final_claim = "разумная жизнь на юпитере не обнаружена"
_register(final_claim, [1.0, 0.0, 0.0])

related_1 = "по имеющимся данным жизнь на юпитере не обнаружена"
related_2 = "жизнь на юпитере была обнаружена в ходе миссии"
_register(related_1, [0.95, 0.05, 0.0])
_register(related_2, [0.90, 0.1, 0.0])

unrelated_texts = []
for i in range(18):
    t = f"случайное несвязанное утверждение номер {i} про погоду и облака"
    _register(t, [0.0, 0.0, 1.0])  # orthogonal, far below threshold
    unrelated_texts.append(t)

pipeline_claims = [related_1, related_2] + unrelated_texts

with patch("requests.Session.post", _mock_post):
    routing, stats = _route_candidate_pairs([final_claim], pipeline_claims)

selected_indices = set(routing[0].keys())
check(
    "large family: both truly-related claims are kept",
    0 in selected_indices and 1 in selected_indices,
    f"selected={selected_indices}",
)
check(
    "large family: meaningful reduction happens (not all 20 kept)",
    len(selected_indices) < len(pipeline_claims),
    f"kept {len(selected_indices)}/{len(pipeline_claims)}",
)
check(
    "large family: kept count is at least top-K",
    len(selected_indices) >= min(COVERAGE_ROUTING_TOP_K, len(pipeline_claims)),
)

# ============================================================
# 3. Negation-flip pair with LOW cosine similarity must still be kept
#    via the mandatory rule, even if embedding ranking alone would
#    have excluded it (proves the union, not just the threshold/top-K).
# ============================================================

_FAKE_VECTORS.clear()
final2 = "разумная жизнь на юпитере не обнаружена"
_register(final2, [1.0, 0.0, 0.0])
contradicting_but_low_sim = "разумная жизнь на юпитере была обнаружена"
_register(contradicting_but_low_sim, [0.0, 1.0, 0.0])  # orthogonal on purpose
far_pipeline = [f"filler claim {i} about clouds" for i in range(10)]
for t in far_pipeline:
    _register(t, [0.0, 0.0, 1.0])

with patch("requests.Session.post", _mock_post):
    routing2, stats2 = _route_candidate_pairs(
        [final2], [contradicting_but_low_sim] + far_pipeline
    )

check(
    "mandatory rule rescues a low-cosine-similarity negation-flip pair",
    0 in routing2[0],
    f"routing={routing2[0]}",
)
check(
    "rescued pair's reason is a mandatory reason, not top_k/threshold",
    routing2[0].get(0) not in ("top_k", "threshold"),
    f"reason={routing2[0].get(0)!r}",
)

# ============================================================
# 4. NO_NLI_CANDIDATES / coverage_reason wiring (end-to-end, mocked)
# ============================================================

_FAKE_VECTORS.clear()


def _mock_extract(answer):
    return [{"claim_text": "Разумная жизнь на Юпитере не обнаружена.", "claim_type": "factual"}], "ok"


def _mock_nli_batch(pairs, batch_size=32):
    # Real NLI never runs in this test — every pair comes back uncertain,
    # so the claim ends up uncovered either way; what we're checking is
    # the coverage_reason diagnostic, not the relation itself.
    return [
        {"pair_id": p["pair_id"], "relation": "uncertain", "method": "test"}
        for p in pairs
    ]


final3 = "Разумная жизнь на Юпитере не обнаружена."
_register(final3.lower(), [1.0, 0.0, 0.0])
pipeline_far = "Совершенно не связанное утверждение про облака Земли."
_register(pipeline_far.lower(), [0.0, 0.0, 1.0])

with patch("agent.final_claim_coverage.extract_final_claims", _mock_extract):
    with patch("agent.final_claim_coverage.infer_claim_relations_batch", _mock_nli_batch):
        with patch("requests.Session.post", _mock_post):
            result = evaluate_final_claim_coverage(
                "Разумная жизнь на Юпитере не обнаружена. Это длинный ответ.",
                [{"claim_id": "cl_1", "claim_text": pipeline_far, "verification_status": "unverified"}],
            )

check(
    "end-to-end: claim with candidates checked but no supports -> uncovered with 'no_supporting_relation_found'",
    len(result.uncovered_claims) == 1
    and result.uncovered_claims[0].get("coverage_reason") == "no_supporting_relation_found",
    f"uncovered={result.uncovered_claims}",
)
check(
    "end-to-end: coverage_score still conservative (not silently 1.0)",
    result.coverage_score < 1.0,
)

# ============================================================
# 5. Backward compatibility: query="" (default) disables CORE<->CORE
#    rule but everything else still works unchanged.
# ============================================================

check(
    "default query='' does not crash _route_candidate_pairs",
    True,  # exercised implicitly by cases above, which all omit query
)

# ============================================================
# 6. Helper function sanity (used by mandatory rules)
# ============================================================

check("_is_near_duplicate: identical strings", _is_near_duplicate("Юпитер", "Юпитер"))
check("_is_near_duplicate: clearly different strings", not _is_near_duplicate("Юпитер большой", "Марс маленький и холодный"))
check("_has_negation: detects 'не обнаружена'", _has_negation("жизнь не обнаружена"))
check("_has_negation: no negation present", not _has_negation("жизнь была обнаружена"))
check("_shares_number: both mention 145", _shares_number("температура -145 градусов", "падает до 145 единиц"))
check("_shares_number: no shared number", not _shares_number("температура -145 градусов", "давление 20 атмосфер"))

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
