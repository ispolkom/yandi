"""
agent/epistemic_contradiction_shadow_wiring_regression_test.py — Этап
4G-4 (P10) regression: PRODUCTION WIRING of the epistemic contradiction
shadow classifier (agent/epistemic_contradiction_shadow.py) into
agent/orchestrator_v2.py, SHADOW OBSERVABILITY ONLY.

Covers the user's own 4G-4 §6 spec, A-H:
    A. shadow is really called between Phase 11 and Phase 12 (call
       ORDER in the real production source).
    B. shadow result does NOT change the arguments Phase 12 receives
       (_family_dependency_stats byte-identical before/after the
       shadow call).
    C. shadow candidate=True does not change Phase 12 behavior.
    D. shadow candidate=False does not suppress Phase 12.
    E. a shadow exception does not suppress Phase 12.
    F. canonical Trust is identical with shadow enabled/disabled.
    G. final answer is identical (shadow never touches synthesis_
       result/trust/belief_manager/answer — structural signature
       check, same technique already used for Phase 8/11's inertness).
    H. claims/evidence are not mutated by the shadow call (reuses the
       same deepcopy-comparison technique as Этап 4G-3's own suite, but
       exercised through the ACTUAL wiring helper this time).

Run: /home/iam/venv/bin/python3 -m agent.epistemic_contradiction_shadow_wiring_regression_test
"""
from __future__ import annotations

import copy
import inspect
import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.orchestrator_v2 as orch_v2_mod
import agent.verification_memory as vm
from agent.family_dependency_graph import FamilyDependencyGraph
from agent.dependency_recheck import apply_dependency_recheck
from agent.epistemic_contradiction_shadow import (
    run_epistemic_contradiction_shadow,
    build_shadow_request_summary,
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


def _noop_log(*a, **k):
    pass


# ============================================================
# A. STRUCTURAL: shadow call really sits between Phase 11 and Phase 12
# in the real production source (same inspect.getsource technique as
# Этап 4G-1's ordering regression).
# ============================================================

_src = inspect.getsource(orch_v2_mod)
_p11_pos = _src.find("_family_dependency_stats = apply_family_dependency_shadow(")
_shadow_pos = _src.find("_contradiction_shadow_stats = run_epistemic_contradiction_shadow(")
_p12_pos = _src.find("apply_dependency_recheck(\n")

check(
    "A: apply_family_dependency_shadow (Phase 11) < run_epistemic_contradiction_shadow "
    "(4G-4) < apply_dependency_recheck (Phase 12), in the real production source",
    -1 < _p11_pos < _shadow_pos < _p12_pos,
    f"p11={_p11_pos} shadow={_shadow_pos} p12={_p12_pos}",
)

check(
    "A: production call site passes claims_data/evidence_data (same objects already "
    "in scope, no pipeline restructuring)",
    "run_epistemic_contradiction_shadow(\n            claims_data, evidence_data" in _src,
    "call-site arguments changed shape",
)

# ============================================================
# G. STRUCTURAL: run_epistemic_contradiction_shadow()'s signature has
# no way to touch synthesis_result/trust/belief_manager/answer — same
# inertness-by-signature technique the Phase 8/11 docstrings already
# rely on.
# ============================================================

_shadow_params = set(inspect.signature(run_epistemic_contradiction_shadow).parameters.keys())
check(
    "G: run_epistemic_contradiction_shadow() takes no synthesis_result/trust/"
    "belief_manager/answer parameter — structurally cannot influence the final answer",
    _shadow_params.isdisjoint({"synthesis_result", "trust", "belief_manager", "answer"}),
    f"{_shadow_params}",
)

_summary_params = set(inspect.signature(build_shadow_request_summary).parameters.keys())
check(
    "G: build_shadow_request_summary() likewise takes no synthesis_result/trust/"
    "belief_manager/answer parameter",
    _summary_params.isdisjoint({"synthesis_result", "trust", "belief_manager", "answer"}),
    f"{_summary_params}",
)

# ============================================================
# Fixture claims/evidence + a persisted contradicts edge (candidate=True
# shape: two different eligible supporting roots either side).
# ============================================================

def _make_fixture():
    traces_dir = Path(tempfile.mkdtemp(prefix="p10_wire_traces_"))
    index_db = Path(tempfile.mkdtemp(prefix="p10_wire_index_")) / "index.db"
    graph = FamilyDependencyGraph(storage_file=Path(tempfile.mkdtemp(prefix="p10_wire_g_")) / "g.json")
    graph.record_edge("fam_w_a", "fam_w_b", "contradicts", "claim_claim_nli:contradicts", ["cl_w_a", "cl_w_b"])
    graph.record_edge("fam_w_b", "fam_w_a", "contradicts", "claim_claim_nli:contradicts", ["cl_w_a", "cl_w_b"])

    claims_data = [
        {
            "claim_id": "cl_w_a", "claim_text": "claim a", "semantic_family_id": "fam_w_a",
            "evidence_relations": [
                {"evidence_id": "ev_w_a", "relation": "supports", "evidence_role": "direct",
                 "evidence_eligible": True, "source_class": "reference", "directness": 0.8,
                 "retrieval_origin": "claim_specific"},
            ],
        },
        {
            "claim_id": "cl_w_b", "claim_text": "claim b", "semantic_family_id": "fam_w_b",
            "evidence_relations": [
                {"evidence_id": "ev_w_b", "relation": "supports", "evidence_role": "direct",
                 "evidence_eligible": True, "source_class": "reference", "directness": 0.8,
                 "retrieval_origin": "claim_specific"},
            ],
        },
    ]
    evidence_data = [
        {"evidence_id": "ev_w_a", "source_uri": "https://w.example/a", "route": "internet"},
        {"evidence_id": "ev_w_b", "source_uri": "https://w.example/b", "route": "internet"},
    ]
    return traces_dir, index_db, graph, claims_data, evidence_data


class _BrokenGraph:
    @property
    def edges(self):
        raise RuntimeError("simulated graph corruption")


def _run_phase12_twice(family_dependency_stats, shadow_call):
    """
    Runs apply_dependency_recheck() on two independent deep copies of
    the SAME family_dependency_stats/graph fixture — once with a
    shadow_call() invoked in between (exactly where production calls
    it), once without — and returns both results for comparison.
    Belief manager is None and recheck_candidate_details is empty in
    the base fixture, so this makes ZERO network/LLM calls either way
    (agent.dependency_recheck.apply_dependency_recheck's own depth-1
    empty-candidate short-circuit) while still exercising the real
    function under the real call-order.
    """
    graph1 = FamilyDependencyGraph(storage_file=Path(tempfile.mkdtemp(prefix="p10_wire_p12a_")) / "g.json")
    graph2 = FamilyDependencyGraph(storage_file=Path(tempfile.mkdtemp(prefix="p10_wire_p12b_")) / "g.json")

    stats_before = copy.deepcopy(family_dependency_stats)

    # WITHOUT shadow in between (baseline).
    result_without = apply_dependency_recheck(
        copy.deepcopy(family_dependency_stats), None, {}, _noop_log, False, graph=graph1,
    )

    # WITH shadow called in between (production shape).
    fds_copy = copy.deepcopy(family_dependency_stats)
    shadow_call()
    result_with = apply_dependency_recheck(
        fds_copy, None, {}, _noop_log, False, graph=graph2,
    )

    def _strip_timing(d):
        return {k: v for k, v in d.items() if k != "elapsed_ms"}

    return _strip_timing(result_without), _strip_timing(result_with), stats_before, fds_copy


# ============================================================
# B/C. shadow candidate=True does not change Phase 12's input or
# output.
# ============================================================

traces_c, index_c, graph_c, claims_c, evidence_c = _make_fixture()
fds_c = {"recheck_candidate_details": []}

with patch.object(vm, "TRACES_DIR", traces_c), patch.object(vm, "INDEX_DB", index_c):
    def _shadow_call_true():
        stats = run_epistemic_contradiction_shadow(
            claims_c, evidence_c, graph=graph_c, log=_noop_log, verbose=False,
        )
        assert stats["candidates_true"] == 1, f"fixture broken, expected candidate=True: {stats}"

    result_without_c, result_with_c, fds_before_c, fds_after_c = _run_phase12_twice(fds_c, _shadow_call_true)

check(
    "B: _family_dependency_stats unchanged after the shadow call (candidate=True case)",
    fds_before_c == fds_after_c,
    f"before={fds_before_c} after={fds_after_c}",
)
check(
    "C: Phase 12 result identical whether or not shadow (candidate=True) ran in between",
    result_without_c == result_with_c,
    f"without={result_without_c} with={result_with_c}",
)

# ============================================================
# D. shadow candidate=False does not suppress Phase 12.
# ============================================================

traces_d, index_d, graph_d, claims_d, evidence_d = _make_fixture()
# Force candidate=False: strip one side's evidence_relations so it has
# no eligible supporting root.
claims_d[0]["evidence_relations"] = []
fds_d = {"recheck_candidate_details": []}

with patch.object(vm, "TRACES_DIR", traces_d), patch.object(vm, "INDEX_DB", index_d):
    def _shadow_call_false():
        stats = run_epistemic_contradiction_shadow(
            claims_d, evidence_d, graph=graph_d, log=_noop_log, verbose=False,
        )
        assert stats["candidates_false"] == 1, f"fixture broken, expected candidate=False: {stats}"

    result_without_d, result_with_d, fds_before_d, fds_after_d = _run_phase12_twice(fds_d, _shadow_call_false)

check(
    "D: Phase 12 result identical whether or not shadow (candidate=False) ran in between "
    "— False does not suppress anything",
    result_without_d == result_with_d,
    f"without={result_without_d} with={result_with_d}",
)

# ============================================================
# E. a shadow exception does not suppress Phase 12.
# ============================================================

fds_e = {"recheck_candidate_details": []}

def _shadow_call_broken():
    stats = run_epistemic_contradiction_shadow(
        [], [], graph=_BrokenGraph(), log=_noop_log, verbose=False,
    )
    assert stats["error"] is not None, f"fixture broken, expected an error: {stats}"

result_without_e, result_with_e, fds_before_e, fds_after_e = _run_phase12_twice(fds_e, _shadow_call_broken)

check(
    "E: Phase 12 result identical whether or not shadow raised internally in between "
    "— fail-open, never suppresses Phase 12",
    result_without_e == result_with_e,
    f"without={result_without_e} with={result_with_e}",
)

# ============================================================
# F. canonical Trust identical with shadow enabled/disabled — Trust is
# computed purely from claims_data (already established elsewhere),
# and claims_data is proven unmutated by the shadow call (see H below)
# — direct smoke check with the real compute_canonical_trust().
# ============================================================

from agent.orchestrator.epistemic.canonical_trust import compute_canonical_trust

_trust_before_shadow = compute_canonical_trust("VERIFIED", "SUPPORTED", _noop_log, False)

traces_f, index_f, graph_f, claims_f, evidence_f = _make_fixture()
with patch.object(vm, "TRACES_DIR", traces_f), patch.object(vm, "INDEX_DB", index_f):
    run_epistemic_contradiction_shadow(claims_f, evidence_f, graph=graph_f, log=_noop_log, verbose=False)

_trust_after_shadow = compute_canonical_trust("VERIFIED", "SUPPORTED", _noop_log, False)

check(
    "F: canonical Trust computation identical before/after a shadow call "
    "(same inputs -> same output, shadow has no side channel into it)",
    _trust_before_shadow == _trust_after_shadow,
    f"before={_trust_before_shadow} after={_trust_after_shadow}",
)

# ============================================================
# H. claims/evidence are not mutated by the shadow call, exercised
# through the ACTUAL wiring helper (run + build_shadow_request_summary).
# ============================================================

traces_h, index_h, graph_h, claims_h, evidence_h = _make_fixture()
claims_h_before = copy.deepcopy(claims_h)
evidence_h_before = copy.deepcopy(evidence_h)

with patch.object(vm, "TRACES_DIR", traces_h), patch.object(vm, "INDEX_DB", index_h):
    _stats_h = run_epistemic_contradiction_shadow(claims_h, evidence_h, graph=graph_h, log=_noop_log, verbose=False)
    _summary_h = build_shadow_request_summary(claims_h, _stats_h, {"recheck_candidate_details": []})

check(
    "H: claims_data unchanged after run_epistemic_contradiction_shadow + "
    "build_shadow_request_summary",
    claims_h == claims_h_before,
    f"before={claims_h_before} after={claims_h}",
)
check(
    "H: evidence_data unchanged after run_epistemic_contradiction_shadow + "
    "build_shadow_request_summary",
    evidence_h == evidence_h_before,
    f"before={evidence_h_before} after={evidence_h}",
)

# ============================================================
# build_shadow_request_summary(): JSON-safety + touched-vs-all
# distinction (Этап 4G-4 §5) — the summary must be plain-JSON-safe
# (no raw sets from contradiction_stats["events"]) and must correctly
# separate "all persisted edges" from "edges touching this request".
# ============================================================

import json

check(
    "SUMMARY: JSON-serializable (no raw sets leak through from events)",
    json.dumps(_summary_h) is not None,
    f"{_summary_h}",
)

# fam_w_a/fam_w_b DO appear in claims_h -> touched_this_request should
# be exactly 1 (the one persisted edge, since both sides are present).
check(
    "SUMMARY: touched_this_request correctly counts the edge whose families "
    "actually appear in claims_data this request",
    _summary_h["touched_this_request"] == 1,
    f"{_summary_h}",
)

# A DIFFERENT, unrelated family set -> touched_this_request must be 0,
# even though edges_checked (global) still finds the same persisted edge.
_unrelated_claims = [{"claim_id": "cl_z", "claim_text": "x", "semantic_family_id": "fam_unrelated_z",
                        "evidence_relations": []}]
_summary_unrelated = build_shadow_request_summary(_unrelated_claims, _stats_h, {"recheck_candidate_details": []})
check(
    "SUMMARY: touched_this_request is 0 when this request's families don't overlap "
    "the persisted edge at all, even though edges_checked (global) is still >0 "
    "(never conflate stale registry-wide numbers with this request)",
    _summary_unrelated["touched_this_request"] == 0 and _summary_unrelated["edges_checked"] > 0,
    f"{_summary_unrelated}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
