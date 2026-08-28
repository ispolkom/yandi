"""
agent/family_embedding_batching_regression_test.py — Этап 4D-2 (P9)
regression: FAMILY EMBEDDING BATCHING.

Performance-only patch: agent.claim_family_registry.ClaimFamilyRegistry
.find_or_link_claim() used to call BeliefManager._embed_batch() ONCE
PER CANDIDATE FAMILY (up to ~121 HTTP /api/embed round-trips per claim,
measured live in Этап 4C — 157s/10 claims). Now it embeds the new
claim + every candidate's canonical_text in ONE batched call, computes
similarities locally, and runs the EXACT SAME decision loop (same
order, same 0.70 threshold, same LLM judge, same hardening_guard) —
see agent.claim_semantic_identity_prototype.classify_claim_pair_detailed
's new precomputed_similarity parameter, which this reuses rather than
duplicating the judge/hardening logic.

This suite's main job is proving DECISION EQUIVALENCE: for every
scenario shape (exact hit, no match, match at an early/middle/late
registry position, multiple >=0.70 candidates where registry ORDER
must still decide the winner — not highest cosine —, and each
hardening_guard veto dimension), the OLD (unbatched, reference
implementation preserved here verbatim) and NEW (batched, real
production code) algorithms make the IDENTICAL decision. Embedding and
LLM-judge calls are deterministically mocked so both algorithms see
the exact same inputs.

Run: /home/iam/venv/bin/python3 -m agent.family_embedding_batching_regression_test
"""
from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import numpy as np

from agent.claim_family_registry import ClaimFamilyRegistry
from agent.belief_manager import BeliefManager
import agent.claim_semantic_identity_prototype as prototype_mod
from agent.claim_semantic_identity_prototype import classify_claim_pair, EMBEDDING_PREFILTER_THRESHOLD

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


def _noop_log(*a, **k):
    pass


# ============================================================
# Deterministic fake embedding + LLM judge — a small explicit
# vocabulary, so both the OLD and NEW algorithms see byte-identical
# similarity/judge inputs regardless of HOW MANY texts are embedded in
# one call.
# ============================================================

# Each vector is a point on a 5-dim unit-ish space. Same-topic texts
# share a base direction (dot product high, clears 0.70); different-
# topic texts are orthogonal (dot product ~0, well under 0.70).
_VOCAB = {
    "Кофе вызывает рак.": [1.0, 0.0, 0.0, 0.0, 0.0],
    "Кофе является причиной рака.": [0.95, 0.05, 0.0, 0.0, 0.0],  # near F1 only
    "Кофе не вызывает рак.": [0.95, 0.0, 0.0, 0.05, 0.0],  # near F1 embedding-wise, polarity-opposite

    "У Юпитера известно 95 спутников.": [0.0, 1.0, 0.0, 0.0, 0.0],
    "У Сатурна известно 95 спутников.": [0.02, 0.95, 0.0, 0.0, 0.0],  # near F2 embedding-wise, different entity

    "В ЕС входит 27 государств.": [0.0, 0.0, 1.0, 0.0, 0.0],
    "Европейский союз включает 27 государств.": [0.0, 0.02, 0.95, 0.0, 0.0],  # near F3 only
    "В ЕС входит 28 государств.": [0.0, 0.0, 0.95, 0.02, 0.0],  # near F3 embedding-wise, different number

    "Растение X ядовито для человека.": [0.0, 0.0, 0.0, 0.0, 1.0],
    "Растение X токсично для человека.": [0.0, 0.0, 0.0, 0.02, 0.98],  # near F4 only

    "Вода кипит при 100 градусах.": [0.0, 1.0, 0.0, 1.0, 0.0],
    "Вода закипает при 100°C.": [0.02, 0.98, 0.0, 0.98, 0.0],  # near F5 only

    "Совершенно новая незнакомая тема о геологии.": [-1.0, -1.0, -1.0, -1.0, -1.0],  # far from everything

    # F/multiple-candidates scenario: claim similar-by-embedding to BOTH
    # F2 (Jupiter, high sim, judged NOT equivalent -> continues) and F4
    # (plant, lower sim but still >=0.70, judged equivalent -> wins).
    # Registry order is F1,F2,F3,F4,F5 - F2 is checked before F4.
    # Exactly equal cosine similarity (0.7071) to BOTH F2 (Jupiter) and
    # F4 (plant) — deliberately a TIE, so the only thing that can decide
    # which one gets checked/matched first is registry ITERATION ORDER,
    # not a similarity ranking (proves batching didn't introduce an
    # implicit "sort by cosine" — ties are structurally undecidable by
    # similarity alone).
    "У Юпитера есть спутники, а растение X ядовито (составное).": [0.0, 1.0, 0.0, 0.0, 1.0],
}


def _unit(v):
    arr = np.array(v, dtype=np.float32)
    n = np.linalg.norm(arr)
    return arr / n if n else arr


def _fake_embed_batch(texts, call_log=None):
    if call_log is not None:
        call_log.append(list(texts))
    vecs = []
    for t in texts:
        raw = _VOCAB.get(t)
        if raw is None:
            # Unknown text: far from everything (orthogonal-ish), so it
            # never accidentally clears the 0.70 prefilter.
            raw = list(np.random.RandomState(abs(hash(t)) % (2**31)).uniform(-0.1, 0.1, 5))
        vecs.append(_unit(raw))
    return np.array(vecs, dtype=np.float32)


# LLM judge: "equivalent" only for genuinely-same-proposition pairs;
# "different" for embedding-near-but-semantically-distinct pairs (the
# numeric/polarity/entity guard test cases rely on the JUDGE itself
# saying "equivalent" so hardening_guard is what does the real work of
# downgrading it — matching how a real LLM might miss these distinctions).
_EQUIVALENT_PAIRS = {
    frozenset(["Кофе вызывает рак.", "Кофе является причиной рака."]),
    frozenset(["Европейский союз включает 27 государств.", "В ЕС входит 27 государств."]),
    frozenset(["Растение X токсично для человека.", "Растение X ядовито для человека."]),
    frozenset(["Вода закипает при 100°C.", "Вода кипит при 100 градусах."]),
    # Hardening-guard test cases: LLM (deliberately, per the scenario)
    # says equivalent; the DETERMINISTIC guard is what must catch it.
    frozenset(["Кофе не вызывает рак.", "Кофе вызывает рак."]),  # polarity
    frozenset(["У Сатурна известно 95 спутников.", "У Юпитера известно 95 спутников."]),  # entity
    frozenset(["В ЕС входит 28 государств.", "В ЕС входит 27 государств."]),  # numeric
    # Multiple-candidates scenario: NOT equivalent to F2 (continues past
    # it), IS equivalent to F4 (wins there).
    frozenset(["У Юпитера есть спутники, а растение X ядовито (составное).", "Растение X ядовито для человека."]),
}


def _fake_llm_judge(self, a, b, call_log=None):
    if call_log is not None:
        call_log.append((a, b))
    if frozenset([a, b]) in _EQUIVALENT_PAIRS:
        return "equivalent"
    return "different"


def _make_fixture_families(domain="factual"):
    """5 families, in this exact registry order — early/middle/late
    positions for the equivalence tests below."""
    now = time.time()
    texts = [
        "Кофе вызывает рак.",                    # F1 (early)
        "У Юпитера известно 95 спутников.",       # F2 (early-middle)
        "В ЕС входит 27 государств.",             # F3 (middle)
        "Растение X ядовито для человека.",        # F4 (middle-late)
        "Вода кипит при 100 градусах.",           # F5 (late)
    ]
    return [
        {
            "family_id": f"fam_{uuid.uuid4().hex[:8]}",
            "domain": domain,
            "canonical_text": t,
            "members": [{"claim_id": f"cl_seed_{i}", "claim_text": t, "linked_at": now}],
            "created_at": now,
            "updated_at": now,
        }
        for i, t in enumerate(texts)
    ]


def _old_find_or_link_reference(families, claim_text, claim_id, domain, embed_call_log=None):
    """Verbatim snapshot of the PRE-Этап-4D-2 algorithm (one
    classify_claim_pair() call per candidate, own embedding each time)
    — kept here permanently as the ground-truth reference for decision
    equivalence, since the production code no longer contains this
    shape (P9 §7: 'если старый implementation неудобно вызывать —
    зафиксировать reference behavior в fixture')."""
    claim_text = (claim_text or "").strip()
    if not claim_text or not claim_id:
        return None, families

    candidates = [f for f in families if f.get("domain") == domain]

    for family in candidates:
        canonical = family.get("canonical_text", "")
        if not canonical:
            continue
        outcome = classify_claim_pair(canonical, claim_text)  # own embedding, no precomputed_similarity
        if outcome in ("exact", "equivalent"):
            already_member = any(m.get("claim_id") == claim_id for m in family.get("members", []))
            if not already_member:
                family.setdefault("members", []).append(
                    {"claim_id": claim_id, "claim_text": claim_text, "linked_at": time.time()}
                )
            return family["canonical_text"], families

    new_family = {
        "family_id": f"fam_{uuid.uuid4().hex[:8]}",
        "domain": domain,
        "canonical_text": claim_text,
        "members": [{"claim_id": claim_id, "claim_text": claim_text, "linked_at": time.time()}],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    families.append(new_family)
    return None, families  # None == "created new family" (no existing canonical matched)


def _run_new(families, claim_text, claim_id, domain, embed_call_log=None):
    """Runs the REAL, current production ClaimFamilyRegistry.find_or_link_claim()."""
    tmp = Path(tempfile.mkdtemp(prefix="p9_scenario_")) / "families.json"
    registry = ClaimFamilyRegistry(storage_file=tmp)
    registry.families = families
    pre_existing_ids = {f["family_id"] for f in families}
    stats = {}
    family_id = registry.find_or_link_claim(claim_text, claim_id, domain, log=_noop_log, verbose=False, stats=stats)
    if family_id is None or family_id not in pre_existing_ids:
        # None -> degenerate input; not-pre-existing -> a NEW family was
        # created for this claim (canonical_text would just be the
        # claim's own text, which is not a meaningful "matched an
        # existing family" signal — normalize both to None, same as
        # the OLD reference's own "no match" return value).
        return None, registry.families, stats
    matched = next((f for f in registry.families if f["family_id"] == family_id), None)
    return (matched["canonical_text"] if matched else None), registry.families, stats


# ============================================================
# Old-vs-new decision equivalence (§7) — one scenario per required case.
# ============================================================

_SCENARIOS = [
    ("A: exact hit", "Кофе вызывает рак.", "Кофе вызывает рак."),
    ("B: no match -> new family", "Совершенно новая незнакомая тема о геологии.", None),
    ("C: match EARLY family (F1)", "Кофе является причиной рака.", "Кофе вызывает рак."),
    ("D: match MIDDLE family (F3)", "Европейский союз включает 27 государств.", "В ЕС входит 27 государств."),
    ("E: match LATE family (F5)", "Вода закипает при 100°C.", "Вода кипит при 100 градусах."),
    ("F: multiple >=0.70 candidates, tied similarity, REGISTRY ORDER decides (no implicit cosine sort)",
     "У Юпитера есть спутники, а растение X ядовито (составное).", "Растение X ядовито для человека."),
    ("G: candidate rejected by NUMERIC guard -> no match, new family",
     "В ЕС входит 28 государств.", None),
    ("H: candidate rejected by POLARITY guard -> no match, new family",
     "Кофе не вызывает рак.", None),
    ("I: candidate rejected by ENTITY guard -> no match, new family",
     "У Сатурна известно 95 спутников.", None),
]

for label, claim_text, expected_canonical in _SCENARIOS:
    with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: _fake_embed_batch(texts))), \
         patch.object(BeliefManager, "_llm_judge_relation", _fake_llm_judge):

        old_result, _ = _old_find_or_link_reference(_make_fixture_families(), claim_text, "cl_old", "factual")
        new_result, _, _ = _run_new(_make_fixture_families(), claim_text, "cl_new", "factual")

    check(
        f"{label}: OLD decision == NEW decision",
        old_result == new_result == expected_canonical,
        f"old={old_result!r} new={new_result!r} expected={expected_canonical!r}",
    )

# ============================================================
# 8. HTTP embed call count: O(claims x families) -> O(claims).
# ============================================================

embed_calls_old = []
embed_calls_new = []

with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: _fake_embed_batch(texts, embed_calls_old))), \
     patch.object(BeliefManager, "_llm_judge_relation", _fake_llm_judge):
    _old_find_or_link_reference(_make_fixture_families(), "Кофе является причиной рака.", "cl_x", "factual")

with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: _fake_embed_batch(texts, embed_calls_new))), \
     patch.object(BeliefManager, "_llm_judge_relation", _fake_llm_judge):
    _run_new(_make_fixture_families(), "Кофе является причиной рака.", "cl_x", "factual")

check(
    "8: OLD makes one embed HTTP call PER CANDIDATE it inspects before matching (>=1, scales with position)",
    len(embed_calls_old) >= 1,
    f"{len(embed_calls_old)} calls",
)
check(
    "8: NEW makes EXACTLY ONE batched embed HTTP call for the whole claim, "
    "regardless of how many candidates (5 families -> still 1 call, embedding all 6 texts at once)",
    len(embed_calls_new) == 1 and len(embed_calls_new[0]) == 6,  # claim + 5 canonicals
    f"calls={len(embed_calls_new)} sizes={[len(c) for c in embed_calls_new]}",
)

# For the "no match" (new family) case, OLD must scan ALL 5 candidates
# (5 embed calls); NEW must still make exactly 1 batched call.
embed_calls_old_nomatch = []
embed_calls_new_nomatch = []

with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: _fake_embed_batch(texts, embed_calls_old_nomatch))), \
     patch.object(BeliefManager, "_llm_judge_relation", _fake_llm_judge):
    _old_find_or_link_reference(_make_fixture_families(), "Совершенно новая незнакомая тема о геологии.", "cl_y", "factual")

with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: _fake_embed_batch(texts, embed_calls_new_nomatch))), \
     patch.object(BeliefManager, "_llm_judge_relation", _fake_llm_judge):
    _run_new(_make_fixture_families(), "Совершенно новая незнакомая тема о геологии.", "cl_y", "factual")

check(
    "8b: worst case (no match, must scan ALL 5 candidates) — OLD makes 5 separate embed calls",
    len(embed_calls_old_nomatch) == 5,
    f"{len(embed_calls_old_nomatch)}",
)
check(
    "8b: NEW still makes exactly 1 batched call for the same worst case "
    "(O(claims x families) -> O(claims) proven on the expensive case, not just the easy one)",
    len(embed_calls_new_nomatch) == 1 and len(embed_calls_new_nomatch[0]) == 6,
    f"calls={len(embed_calls_new_nomatch)}",
)

# ============================================================
# 9. Empty / small registry cases.
# ============================================================

ec = []
with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: _fake_embed_batch(texts, ec))), \
     patch.object(BeliefManager, "_llm_judge_relation", _fake_llm_judge):
    tmp0 = Path(tempfile.mkdtemp(prefix="p9_empty_")) / "families.json"
    registry0 = ClaimFamilyRegistry(storage_file=tmp0)
    fam_id0 = registry0.find_or_link_claim("Совсем новое утверждение про физику частиц.", "cl_empty", "factual", log=_noop_log)

check(
    "9a: 0 existing families -> creates family WITHOUT any embedding call at all",
    fam_id0 is not None and len(ec) == 0,
    f"family_id={fam_id0} embed_calls={len(ec)}",
)

with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: _fake_embed_batch(texts))), \
     patch.object(BeliefManager, "_llm_judge_relation", _fake_llm_judge):
    tmp1 = Path(tempfile.mkdtemp(prefix="p9_one_")) / "families.json"
    registry1 = ClaimFamilyRegistry(storage_file=tmp1)
    registry1.families = _make_fixture_families()[:1]  # only F1
    fam_id1 = registry1.find_or_link_claim("Кофе является причиной рака.", "cl_one", "factual", log=_noop_log)
    matched1 = next((f for f in registry1.families if f["family_id"] == fam_id1), None)

check(
    "9b: 1 existing family, claim matches it correctly",
    matched1 is not None and matched1["canonical_text"] == "Кофе вызывает рак.",
    f"{matched1}",
)

# Embedding endpoint failure -> same fail-safe as before (no fabricated
# equivalence: each candidate's precomputed_similarity stays None, so
# classify_claim_pair() falls through to its OWN embedding attempt,
# which is the SAME mocked failing function -> also fails -> "different"
# for every candidate -> a NEW family is created for this claim, never
# a spurious link into one of the 5 PRE-EXISTING fixture families).
_fixture2 = _make_fixture_families()
_pre_existing_ids2 = {f["family_id"] for f in _fixture2}
with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: None)):
    tmp2 = Path(tempfile.mkdtemp(prefix="p9_fail_")) / "families.json"
    registry2 = ClaimFamilyRegistry(storage_file=tmp2)
    registry2.families = _fixture2
    fam_id2 = registry2.find_or_link_claim("Кофе является причиной рака.", "cl_fail", "factual", log=_noop_log)

check(
    "9c: embedding endpoint failure -> fail-safe unchanged, no fabricated match into "
    "any pre-existing fixture family (creates a genuinely NEW family instead of guessing)",
    fam_id2 is not None and fam_id2 not in _pre_existing_ids2,
    f"family_id={fam_id2} pre_existing={_pre_existing_ids2}",
)

# ============================================================
# Observability: stats dict populated correctly.
# ============================================================

with patch.object(BeliefManager, "_embed_batch", staticmethod(lambda texts: _fake_embed_batch(texts))), \
     patch.object(BeliefManager, "_llm_judge_relation", _fake_llm_judge):
    tmp3 = Path(tempfile.mkdtemp(prefix="p9_stats_")) / "families.json"
    registry3 = ClaimFamilyRegistry(storage_file=tmp3)
    registry3.families = _make_fixture_families()
    stats3 = {}
    registry3.find_or_link_claim("Европейский союз включает 27 государств.", "cl_stats", "factual", stats=stats3)

check(
    "stats: embed_batches==1, prefilter_candidates==5, linked==1 for a mid-registry match",
    stats3.get("embed_batches") == 1 and stats3.get("prefilter_candidates") == 5 and stats3.get("linked") == 1,
    f"{stats3}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
