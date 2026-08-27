"""
agent/epistemic_independent_support_counting_regression_test.py —
Epistemic Core v1 Phase 7 regression: source-independent claim status
counting (agent/orchestrator/claims/status.py::classify_claim_epistemic_status(),
now cluster-aware via evidence_data + agent/source_clustering.py's
source_cluster_id).

Proves the plan's explicit required scenarios:
    5 syndicated SUPPORTS != 5 independent SUPPORTS
    1 independent SUPPORT + 5 syndicated SUPPORTS has correctly-defined
        semantics (2, not 6 and not 1)
    unknown/missing cluster does NOT destroy evidence (counts as its own
        singleton, never dropped)
    contradiction independence is handled symmetrically to support
    mixed support/contradiction cases are checked separately
    backward compatibility: no evidence_data passed at all -> numerically
        identical to the pre-Phase-7 formula (raw relation count)

This is an intentional, documented semantic change — unlike every prior
phase in this implementation, claim verification_status itself can now
come out differently than before Phase 7 for the same evidence_relations,
by design.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_independent_support_counting_regression_test
"""

from agent.orchestrator.claims.status import classify_claim_epistemic_status

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


def _rel(ev_id, relation):
    return {"evidence_id": ev_id, "relation": relation, "evidence_role": "direct", "evidence_eligible": True}


def _ev(ev_id, cluster_id):
    return {"evidence_id": ev_id, "source_cluster_id": cluster_id}


# ── 1. 5 syndicated SUPPORTS (all one cluster) -> support_count = 1, not 5 ──

claims_syndicated = [{
    "claim_id": "cl_syn",
    "verification_status": "candidate",
    "evidence_relations": [_rel(f"ev_s{i}", "supports") for i in range(5)],
}]
evidence_syndicated = [_ev(f"ev_s{i}", "sc_family_a") for i in range(5)]

classify_claim_epistemic_status(claims_syndicated, log=lambda m: None, verbose=False, evidence_data=evidence_syndicated)
claim = claims_syndicated[0]
check(
    "5 syndicated SUPPORTS (one cluster) -> support_count=1, not 5; status=supported",
    claim["support_count"] == 1 and claim["verification_status"] == "supported",
    f"support_count={claim['support_count']}",
)
check(
    "raw-relations count kept alongside for A/B transparency: raw=5",
    claim["support_count_raw_relations"] == 5,
    f"{claim['support_count_raw_relations']}",
)

# ── 2. 5 INDEPENDENT SUPPORTS (5 distinct clusters) -> support_count = 5 (NOT equivalent to case 1) ──

claims_independent = [{
    "claim_id": "cl_indep",
    "verification_status": "candidate",
    "evidence_relations": [_rel(f"ev_i{i}", "supports") for i in range(5)],
}]
evidence_independent = [_ev(f"ev_i{i}", f"sc_unique_{i}") for i in range(5)]

classify_claim_epistemic_status(claims_independent, log=lambda m: None, verbose=False, evidence_data=evidence_independent)
claim_indep = claims_independent[0]
check(
    "5 independent SUPPORTS (5 distinct clusters) -> support_count=5",
    claim_indep["support_count"] == 5,
    f"support_count={claim_indep['support_count']}",
)
check(
    "5 syndicated SUPPORTS != 5 independent SUPPORTS (the core Phase 7 requirement)",
    claims_syndicated[0]["support_count"] != claim_indep["support_count"],
    f"syndicated={claims_syndicated[0]['support_count']} independent={claim_indep['support_count']}",
)

# ── 3. 1 independent SUPPORT + 5 syndicated SUPPORTS -> support_count = 2, not 6, not 1 ──

claims_mix = [{
    "claim_id": "cl_mix",
    "verification_status": "candidate",
    "evidence_relations": (
        [_rel("ev_solo", "supports")] +
        [_rel(f"ev_m{i}", "supports") for i in range(5)]
    ),
}]
evidence_mix = (
    [_ev("ev_solo", "sc_solo")] +
    [_ev(f"ev_m{i}", "sc_family_b") for i in range(5)]
)

classify_claim_epistemic_status(claims_mix, log=lambda m: None, verbose=False, evidence_data=evidence_mix)
claim_mix = claims_mix[0]
check(
    "1 independent SUPPORT + 5 syndicated SUPPORTS -> support_count=2 (1 solo cluster + 1 family cluster)",
    claim_mix["support_count"] == 2,
    f"support_count={claim_mix['support_count']}",
)

# ── 4. Unknown/missing cluster does NOT destroy evidence — treated as its own singleton ──

claims_unknown = [{
    "claim_id": "cl_unknown",
    "verification_status": "candidate",
    "evidence_relations": [
        _rel("ev_known", "supports"),
        _rel("ev_no_cluster_field", "supports"),   # present in evidence_data but source_cluster_id is None
        _rel("ev_not_in_pool", "supports"),         # not present in evidence_data at all
    ],
}]
evidence_unknown = [
    _ev("ev_known", "sc_known"),
    {"evidence_id": "ev_no_cluster_field", "source_cluster_id": None},
    # "ev_not_in_pool" deliberately absent from evidence_data
]

classify_claim_epistemic_status(claims_unknown, log=lambda m: None, verbose=False, evidence_data=evidence_unknown)
claim_unknown = claims_unknown[0]
check(
    "unknown/missing cluster info is NOT destroyed — each unclustered item counts as its own "
    "singleton (1 known-cluster + 2 unclustered singletons = 3), not merged, not dropped",
    claim_unknown["support_count"] == 3,
    f"support_count={claim_unknown['support_count']}",
)

# ── 5. Contradiction independence handled symmetrically ──

claims_contra = [{
    "claim_id": "cl_contra",
    "verification_status": "candidate",
    "evidence_relations": [_rel(f"ev_c{i}", "contradicts") for i in range(4)],
}]
evidence_contra = [_ev(f"ev_c{i}", "sc_contra_family") for i in range(4)]

classify_claim_epistemic_status(claims_contra, log=lambda m: None, verbose=False, evidence_data=evidence_contra)
claim_contra = claims_contra[0]
check(
    "4 syndicated CONTRADICTS (one cluster) -> contradiction_count=1, not 4 (symmetric with supports)",
    claim_contra["contradiction_count"] == 1 and claim_contra["verification_status"] == "contradicted",
    f"contradiction_count={claim_contra['contradiction_count']}",
)

# ── 6. Mixed support/contradiction case, checked separately with independent clusters on each side ──

claims_disputed = [{
    "claim_id": "cl_disputed",
    "verification_status": "candidate",
    "evidence_relations": (
        [_rel(f"ev_sup{i}", "supports") for i in range(3)] +
        [_rel(f"ev_con{i}", "contradicts") for i in range(2)]
    ),
}]
evidence_disputed = (
    [_ev(f"ev_sup{i}", "sc_support_family") for i in range(3)] +  # 3 syndicated supports -> 1 cluster
    [_ev(f"ev_con{i}", f"sc_contra_{i}") for i in range(2)]        # 2 independent contradicts -> 2 clusters
)

classify_claim_epistemic_status(claims_disputed, log=lambda m: None, verbose=False, evidence_data=evidence_disputed)
claim_disputed = claims_disputed[0]
check(
    "mixed case: 3 syndicated supports (1 cluster) + 2 independent contradicts (2 clusters) "
    "-> support_count=1, contradiction_count=2, status=disputed",
    claim_disputed["support_count"] == 1
    and claim_disputed["contradiction_count"] == 2
    and claim_disputed["verification_status"] == "disputed",
    f"support={claim_disputed['support_count']} contradiction={claim_disputed['contradiction_count']} "
    f"status={claim_disputed['verification_status']}",
)

# ── 7. Backward compatibility: no evidence_data at all -> numerically identical to pre-Phase-7 formula ──

claims_no_evidence_data = [{
    "claim_id": "cl_no_ed",
    "verification_status": "candidate",
    "evidence_relations": [_rel(f"ev_x{i}", "supports") for i in range(5)],
}]
# evidence_data deliberately omitted entirely (old caller, e.g. an
# existing test or any code not yet updated for Phase 6/7)
classify_claim_epistemic_status(claims_no_evidence_data, log=lambda m: None, verbose=False)
claim_no_ed = claims_no_evidence_data[0]
check(
    "no evidence_data passed at all -> degrades to old raw-relation-count formula "
    "(5 relations, no cluster info resolvable -> support_count=5, same as pre-Phase-7)",
    claim_no_ed["support_count"] == 5 == claim_no_ed["support_count_raw_relations"],
    f"support_count={claim_no_ed['support_count']} raw={claim_no_ed['support_count_raw_relations']}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
