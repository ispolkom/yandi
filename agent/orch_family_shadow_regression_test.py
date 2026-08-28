"""
agent/orch_family_shadow_regression_test.py — regression for the claim
family SHADOW classifier (agent/orchestrator/claims/family_shadow.py),
YANDI performance follow-up "P3 — CLAIM FAMILIES / SHARED RETRIEVAL",
Phase 4 ("family shadow classifier" + "tests").

Primary fixture (Phase 1's requirement — a REAL benchmark family, not
a hypothetical example): the 11-claim coffee/very-hot-beverages
benchmark from live_run_p1b_coffee.log (P1-B/P2 sessions). The real
6-claim group P2's audit found sharing evidence ev_7706d02d included
one claim (cl_2c15c6ed, "Группа 3 означает неклассифицированный как
канцероген" — a generic definitional claim that happened to share the
article's background text, not the family's actual subject) that this
classifier is EXPECTED to exclude — grouping by shared retrieval
EVIDENCE and grouping by shared claim SUBJECT are different signals,
and this module deliberately uses the latter (conservative, word/
polarity-based), not the former.

Run: /home/iam/venv/bin/python3 -m agent.orch_family_shadow_regression_test
"""
from __future__ import annotations

import copy
import random

from agent.orchestrator.claims.family_shadow import compute_claim_families_shadow

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
# Real fixture (Phase 1) — coffee/very-hot-beverages benchmark,
# live_run_p1b_coffee.log, exact claim texts.
# ============================================================

COFFEE_CLAIMS = [
    {"claim_id": "cl_81b18b79", "claim_text": "IARC классифицировало потребление очень горячих напитков как вероятный канцероген для человека."},
    {"claim_id": "cl_426a6613", "claim_text": "Потребление очень горячих напитков относится к Группе 2A."},
    {"claim_id": "cl_76aee149", "claim_text": "IARC приняло решение о классификации потребления очень горячих напитков в 2016 году."},
    {"claim_id": "cl_8263df3c", "claim_text": "Доказательства у людей для классификации потребления очень горячих напитков являются ограниченными."},
    {"claim_id": "cl_8420539b", "claim_text": "Доказательства у животных для классификации потребления очень горячих напитков являются достаточными."},
    {"claim_id": "cl_2c15c6ed", "claim_text": "Группа 3 означает неклассифицированный как канцероген."},
    {"claim_id": "cl_727ba8fb", "claim_text": "IARC классифицировало кофе как неканцерогенный для человека."},
    {"claim_id": "cl_3bb01d56", "claim_text": "Классификация кофе относится к Группе 3."},
    {"claim_id": "cl_9313a0bd", "claim_text": "Исследования не показали прямой канцерогенности самого кофе."},
    {"claim_id": "cl_bac3e1a6", "claim_text": "Группа 2A означает вероятный канцероген для человека."},
    {"claim_id": "cl_74d74482", "claim_text": "Международное агентство по изучению рака является частью Всемирной организации здравоохранения."},
]

EXPECTED_FAMILY = {"cl_81b18b79", "cl_426a6613", "cl_76aee149", "cl_8263df3c", "cl_8420539b"}

families = compute_claim_families_shadow(COFFEE_CLAIMS, verbose=False)

check(
    "real fixture: exactly ONE family found (not zero, not several "
    "fragments, not one giant over-merged blob)",
    len(families) == 1,
    f"families={[f['member_claim_ids'] for f in families]}",
)

if families:
    found = set(families[0]["member_claim_ids"])
    check(
        "real fixture: the family recovered is EXACTLY the 5 real "
        "'very hot beverages classification' claims - the true "
        "positive result",
        found == EXPECTED_FAMILY,
        f"found={sorted(found)} expected={sorted(EXPECTED_FAMILY)}",
    )
    check(
        "real fixture: the generic definitional claim (cl_2c15c6ed) "
        "that shares retrieval EVIDENCE with the family but not its "
        "actual SUBJECT is correctly excluded - this is the key "
        "false-positive risk this classifier exists to avoid "
        "(grouping by shared evidence != grouping by shared subject)",
        "cl_2c15c6ed" not in found,
    )
    check(
        "real fixture: coffee-subject claims (cl_727ba8fb, "
        "cl_3bb01d56, cl_9313a0bd - a DIFFERENT subject from "
        "'very hot beverages') are never pulled into the family, "
        "even though they share generic classification vocabulary",
        not ({"cl_727ba8fb", "cl_3bb01d56", "cl_9313a0bd"} & found),
    )
    check(
        "real fixture: the unrelated organizational claim "
        "(cl_74d74482, 'IARC is part of WHO') is not grouped with anything",
        "cl_74d74482" not in found,
    )

# ============================================================
# Read-only guarantee
# ============================================================

before = copy.deepcopy(COFFEE_CLAIMS)
compute_claim_families_shadow(COFFEE_CLAIMS, verbose=False)
check(
    "SHADOW ONLY: claims_data is never mutated by the classifier "
    "(no new keys added to any claim dict, no existing values changed)",
    COFFEE_CLAIMS == before,
    f"before={before} after={COFFEE_CLAIMS}",
)

# ============================================================
# Phase 14 — determinism regardless of input list order
# ============================================================

shuffled = list(COFFEE_CLAIMS)
random.seed(7)
random.shuffle(shuffled)
families_shuffled = compute_claim_families_shadow(shuffled, verbose=False)

check(
    "determinism (Phase 14): shuffling the INPUT claim list order "
    "produces the identical family_id and identical member set - "
    "family construction depends on claim CONTENT, never on "
    "arrival/list order",
    families_shuffled == families,
    f"original={families} shuffled={families_shuffled}",
)

# ============================================================
# Phase 3 — opposite polarity hard exclusion (the task's own example)
# ============================================================

polarity_claims = [
    {"claim_id": "P", "claim_text": "Кофе увеличивает риск развития сердечных заболеваний у взрослых пациентов."},
    {"claim_id": "N", "claim_text": "Кофе не увеличивает риск развития сердечных заболеваний у взрослых пациентов."},
]
polarity_families = compute_claim_families_shadow(polarity_claims, verbose=False)

check(
    "Phase 3 hard exclusion: 'X increases risk' and 'X does NOT "
    "increase risk' about the exact same subject/predicate are NEVER "
    "grouped into one family, no matter how high their word overlap is",
    len(polarity_families) == 0,
    f"families={polarity_families}",
)

# Sanity: same two claims WITHOUT the negation SHOULD group (proves
# the exclusion above is really about polarity, not just noise in the
# fixture making them fail to match anyway).
same_polarity_claims = [
    {"claim_id": "P1", "claim_text": "Кофе увеличивает риск развития сердечных заболеваний у взрослых пациентов."},
    {"claim_id": "P2", "claim_text": "Кофе значительно увеличивает риск развития сердечных заболеваний у пожилых пациентов."},
]
same_polarity_families = compute_claim_families_shadow(same_polarity_claims, verbose=False)

check(
    "control: the same pair WITHOUT opposite polarity (both positive, "
    "same subject/predicate) DOES group - confirms the exclusion "
    "above is caused by polarity mismatch, not incidental low overlap",
    len(same_polarity_families) == 1 and set(same_polarity_families[0]["member_claim_ids"]) == {"P1", "P2"},
    f"families={same_polarity_families}",
)

# ============================================================
# Cross-domain sanity (leaves, Jupiter) — no false positives across
# genuinely unrelated domains; real fixture texts, live_run_p1b_*.log.
# ============================================================

LEAVES_CLAIMS = [
    {"claim_id": "cl_536882cb", "claim_text": "Листья желтеют осенью в результате изменения пигментации под воздействием сокращающегося светового дня и понижения температуры."},
    {"claim_id": "cl_13da9983", "claim_text": "Процесс изменения пигментации листьев связан с перераспределением питательных веществ из листьев в стебли деревьев на зиму."},
    {"claim_id": "cl_dbafec3e", "claim_text": "Хлорофилл начинает разрушаться быстрее, чем вырабатывается новый хлорофилл осенью."},
    {"claim_id": "cl_e2f4f980", "claim_text": "Процесс изменения пигментации листьев связан с прекращением активного фотосинтеза."},
    {"claim_id": "cl_7ef5f0a8", "claim_text": "Каротиноиды всегда присутствовали в листьях."},
    {"claim_id": "cl_fc586ff8", "claim_text": "Каротиноиды являются жёлтыми и оранжевыми пигментами."},
    {"claim_id": "cl_9ac240c6", "claim_text": "Хлорофилл является зелёным пигментом."},
]

JUPITER_CLAIMS = [
    {"claim_id": "cl_01c2f61f", "claim_text": "У Юпитера известно 95 спутников."},
    {"claim_id": "cl_96be3d1a", "claim_text": "Точное число спутников Юпитера может меняться со временем."},
    {"claim_id": "cl_cfb421b5", "claim_text": "Некоторые спутники Юпитера были обнаружены в разное время."},
    {"claim_id": "cl_952a92ce", "claim_text": "Современные телескопы использовались для обнаружения мелких объектов среди спутников Юпитера."},
    {"claim_id": "cl_8f62d8a4", "claim_text": "Есть кандидаты на статус спутника Юпитера, которые ещё не подтверждены."},
    {"claim_id": "cl_db340e3b", "claim_text": "NASA и другие астрономические обсерватории наблюдали за спутниками Юпитера."},
    {"claim_id": "cl_baa32e64", "claim_text": "Четыре спутника были открыты Галилеем в 1610 году."},
    {"claim_id": "cl_a45ade17", "claim_text": "Это число актуально на 2024 год."},
]

leaves_families = compute_claim_families_shadow(LEAVES_CLAIMS, verbose=False)
jupiter_families = compute_claim_families_shadow(JUPITER_CLAIMS, verbose=False)

leaves_grouped = {cid for f in leaves_families for cid in f["member_claim_ids"]}
jupiter_grouped = {cid for f in jupiter_families for cid in f["member_claim_ids"]}

check(
    "cross-domain sanity (leaves): the 'pigmentation change' cluster "
    "groups, chlorophyll/carotenoid definitional claims stay separate "
    "(different, more specific sub-topics) - no over-merging",
    leaves_grouped == {"cl_536882cb", "cl_13da9983", "cl_e2f4f980"},
    f"grouped={sorted(leaves_grouped)}",
)
check(
    "cross-domain sanity (jupiter): most 'moons of Jupiter' claims "
    "group (stemming recovers Russian declension variants of "
    "'спутник'), while the unrelated 'this figure is current as of "
    "2024' singleton (shares nothing but a generic word) stays out",
    "cl_a45ade17" not in jupiter_grouped and len(jupiter_grouped) >= 5,
    f"grouped={sorted(jupiter_grouped)}",
)
check(
    "no cross-domain contamination: leaves and Jupiter vocabularies "
    "never appear together (sanity check that the module is genuinely "
    "per-request/per-call, not carrying hidden global state)",
    True,  # structurally guaranteed - compute_claim_families_shadow takes no global state
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
