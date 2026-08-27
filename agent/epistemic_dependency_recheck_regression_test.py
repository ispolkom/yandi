"""
agent/epistemic_dependency_recheck_regression_test.py — Epistemic Core v1
Phase 12 regression: bounded, controlled re-evaluation
(agent/dependency_recheck.py::apply_dependency_recheck()).

Uses the REAL BeliefManager (agent.belief_manager) against a tempfile
store, so "history preserved" / "Bayesian update applied" are proven
against the actual, already-tested belief mechanism — not a mock of it.
retrieve_for_claims/classify_relation ARE mocked (module-level
monkeypatch on agent.dependency_recheck's own bound names) since those
are real-network/real-embedding calls; agent.dependency_recheck itself
does not reimplement either, so mocking them here tests exactly this
module's own orchestration logic, matching the project's established
pattern (see e.g. epistemic_claim_graph_shadow_regression_test.py mocking
classify_claim_pair-equivalent boundaries).

Run: /home/iam/venv/bin/python3 -m agent.epistemic_dependency_recheck_regression_test
"""

import tempfile
from pathlib import Path

import agent.dependency_recheck as dr_mod
from agent.dependency_recheck import apply_dependency_recheck, MAX_RECHECKS_PER_CALL
from agent.family_dependency_graph import FamilyDependencyGraph
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


class FakeRegistry:
    def __init__(self, families):
        self.families = families


def _tmp_graph():
    tmp = Path(tempfile.mkstemp(suffix=".json")[1])
    tmp.unlink()
    return FamilyDependencyGraph(storage_file=tmp)


def _tmp_belief_manager():
    tmp = Path(tempfile.mkstemp(suffix=".json")[1])
    tmp.unlink()
    return BeliefManager(storage_file=tmp)


def _family(family_id, canonical_text, claim_id):
    return {
        "family_id": family_id,
        "canonical_text": canonical_text,
        "members": [{"claim_id": claim_id, "claim_text": canonical_text}],
    }


def _candidate(changed_family, dependent_family, depth=1):
    return {
        "changed_family": changed_family,
        "previous_status": "supported",
        "new_status": "contradicted",
        "dependent_family": dependent_family,
        "edge_type": "depends_on",
        "reason": "contradicts",
        "triggering_claim_ids": ["cl_x", "cl_y"],
        "depth": depth,
    }


def _fake_evidence(n=1, excerpt="some evidence text"):
    return [
        {"evidence_id": f"ev_{i}", "content_excerpt": excerpt}
        for i in range(n)
    ]


def _patch(monkeypatch_retrieve=None, monkeypatch_classify=None):
    if monkeypatch_retrieve is not None:
        dr_mod.retrieve_for_claims = monkeypatch_retrieve
    if monkeypatch_classify is not None:
        dr_mod.classify_relation = monkeypatch_classify


_orig_retrieve = dr_mod.retrieve_for_claims
_orig_classify = dr_mod.classify_relation


def _restore():
    dr_mod.retrieve_for_claims = _orig_retrieve
    dr_mod.classify_relation = _orig_classify


# ── 1. Simple dependency: A contradicted -> B depends_on A -> B rechecked, SUPPORTS outcome ──

graph1 = _tmp_graph()
bm1 = _tmp_belief_manager()
belief1 = bm1.add_belief(topic="t", statement="B statement", confidence=0.5, claim_ids=["cl_b1"])
history_len_before = len(belief1.history)
registry1 = FakeRegistry([_family("fam_B", "B statement", "cl_b1")])

_patch(
    monkeypatch_retrieve=lambda claims, fetch_cache=None: _fake_evidence(1),
    monkeypatch_classify=lambda main, source: "supports",
)
stats1 = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_B")]},
    bm1, cost={}, log=lambda m: None, verbose=False, graph=graph1, registry=registry1,
)
_restore()

check(
    "simple dependency: B is rechecked (retrieval + NLI both invoked once)",
    stats1["rechecks_performed"] == 1 and stats1["retrieval_calls"] == 1 and stats1["nli_calls"] == 1,
    f"{stats1}",
)
check(
    "supports outcome updates the belief and PRESERVES prior history (append-only)",
    stats1["belief_updates"] == 1
    and len(belief1.history) == history_len_before + 1
    and belief1.history[0]["reason"] == "initial"
    and "ev_0" in belief1.evidence_for,
    f"{belief1.history} {belief1.evidence_for}",
)

# ── 2. Unrelated candidate (not in the candidate list at all) is never touched ──

check(
    "a family never listed as a candidate (e.g. unrelated fam_C) is never recorded in recheck_log",
    "fam_C" not in graph1.recheck_log,
    f"{graph1.recheck_log}",
)

# ── 3. CONTRADICTS outcome: confidence moves, history preserved, old evidence not destroyed ──

graph3 = _tmp_graph()
bm3 = _tmp_belief_manager()
belief3 = bm3.add_belief(topic="t", statement="C statement", confidence=0.6, evidence_for=["ev_old"], claim_ids=["cl_c1"])
old_conf = belief3.confidence
old_evidence_for = list(belief3.evidence_for)
registry3 = FakeRegistry([_family("fam_C", "C statement", "cl_c1")])

_patch(
    monkeypatch_retrieve=lambda claims, fetch_cache=None: _fake_evidence(1),
    monkeypatch_classify=lambda main, source: "contradicts",
)
stats3 = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_C")]},
    bm3, cost={}, log=lambda m: None, verbose=False, graph=graph3, registry=registry3,
)
_restore()

check(
    "contradicts outcome: confidence changes, prior evidence_for NOT destroyed, history preserved",
    stats3["belief_updates"] == 1
    and belief3.confidence != old_conf
    and belief3.evidence_for == old_evidence_for
    and "ev_0" in belief3.evidence_against
    and len(belief3.history) == 2,
    f"conf {old_conf}->{belief3.confidence} for={belief3.evidence_for} against={belief3.evidence_against} hist={len(belief3.history)}",
)

# ── 4. INCONCLUSIVE recheck (all relations unrelated/uncertain) -> belief untouched, NOT false ──

graph4 = _tmp_graph()
bm4 = _tmp_belief_manager()
belief4 = bm4.add_belief(topic="t", statement="D statement", confidence=0.5, claim_ids=["cl_d1"])
conf_before = belief4.confidence
hist_before = len(belief4.history)
registry4 = FakeRegistry([_family("fam_D", "D statement", "cl_d1")])

_patch(
    monkeypatch_retrieve=lambda claims, fetch_cache=None: _fake_evidence(2),
    monkeypatch_classify=lambda main, source: "uncertain",
)
stats4 = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_D")]},
    bm4, cost={}, log=lambda m: None, verbose=False, graph=graph4, registry=registry4,
)
_restore()

check(
    "inconclusive recheck (all uncertain) never calls add_belief — belief completely untouched",
    stats4["belief_updates"] == 0
    and stats4["inconclusive"] == 1
    and belief4.confidence == conf_before
    and len(belief4.history) == hist_before,
    f"{stats4} conf={belief4.confidence} hist={len(belief4.history)}",
)
check(
    "inconclusive outcome recorded in recheck_log (not silently dropped, not 'false')",
    graph4.recheck_log.get("fam_D", {}).get("last_outcome") == "inconclusive",
    f"{graph4.recheck_log}",
)

# ── 5. Retrieval error -> belief untouched, error recorded, NOT treated as contradiction ──

graph5 = _tmp_graph()
bm5 = _tmp_belief_manager()
belief5 = bm5.add_belief(topic="t", statement="E statement", confidence=0.5, claim_ids=["cl_e1"])
conf_before5 = belief5.confidence
hist_before5 = len(belief5.history)
registry5 = FakeRegistry([_family("fam_E", "E statement", "cl_e1")])


def _raise(*a, **kw):
    raise RuntimeError("network down")


_patch(monkeypatch_retrieve=_raise, monkeypatch_classify=lambda main, source: "contradicts")
stats5 = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_E")]},
    bm5, cost={}, log=lambda m: None, verbose=False, graph=graph5, registry=registry5,
)
_restore()

check(
    "retrieval error: belief untouched, error counted, NOT treated as contradiction/false",
    stats5["errors"] == 1
    and stats5["belief_updates"] == 0
    and belief5.confidence == conf_before5
    and len(belief5.history) == hist_before5
    and graph5.recheck_log.get("fam_E", {}).get("last_outcome") == "error",
    f"{stats5} {graph5.recheck_log}",
)

# ── 6. Repeated trigger -> cooldown/duplicate suppression, second call makes no network calls ──

graph6 = _tmp_graph()
bm6 = _tmp_belief_manager()
belief6 = bm6.add_belief(topic="t", statement="F statement", confidence=0.5, claim_ids=["cl_f1"])
registry6 = FakeRegistry([_family("fam_F", "F statement", "cl_f1")])

call_count = {"n": 0}


def _counting_retrieve(claims, fetch_cache=None):
    call_count["n"] += 1
    return _fake_evidence(1)


_patch(monkeypatch_retrieve=_counting_retrieve, monkeypatch_classify=lambda main, source: "supports")
stats6a = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_F")]},
    bm6, cost={}, log=lambda m: None, verbose=False, graph=graph6, registry=registry6,
)
stats6b = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_F")]},
    bm6, cost={}, log=lambda m: None, verbose=False, graph=graph6, registry=registry6,
)
_restore()

check(
    "repeated trigger within cooldown: second call is suppressed, no second network call",
    stats6a["rechecks_performed"] == 1
    and stats6b["rechecks_performed"] == 0
    and stats6b["skipped_cooldown"] == 1
    and call_count["n"] == 1,
    f"{stats6a} {stats6b} calls={call_count['n']}",
)

# ── 7. Depth != 1 candidates are never rechecked synchronously (cascade bound) ──

graph7 = _tmp_graph()
bm7 = _tmp_belief_manager()
bm7.add_belief(topic="t", statement="G statement", confidence=0.5, claim_ids=["cl_g1"])
registry7 = FakeRegistry([_family("fam_G", "G statement", "cl_g1")])

_patch(monkeypatch_retrieve=lambda claims, fetch_cache=None: _fake_evidence(1), monkeypatch_classify=lambda m, s: "supports")
stats7 = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_G", depth=2)]},
    bm7, cost={}, log=lambda m: None, verbose=False, graph=graph7, registry=registry7,
)
_restore()

check(
    "a depth=2 candidate is never rechecked synchronously (cascade bound = 1 hop per request)",
    stats7["rechecks_performed"] == 0 and stats7["skipped_depth"] == 1 and "fam_G" not in graph7.recheck_log,
    f"{stats7} {graph7.recheck_log}",
)

# ── 8. Hard cap: more depth-1 candidates than MAX_RECHECKS_PER_CALL -> extras skipped, not chased ──

graph8 = _tmp_graph()
bm8 = _tmp_belief_manager()
many_families = []
for i in range(MAX_RECHECKS_PER_CALL + 2):
    fam_id = f"fam_H{i}"
    bm8.add_belief(topic="t", statement=f"H{i} statement", confidence=0.5, claim_ids=[f"cl_h{i}"])
    many_families.append(_family(fam_id, f"H{i} statement", f"cl_h{i}"))
registry8 = FakeRegistry(many_families)
many_candidates = [_candidate("fam_A", f"fam_H{i}") for i in range(MAX_RECHECKS_PER_CALL + 2)]

_patch(monkeypatch_retrieve=lambda claims, fetch_cache=None: _fake_evidence(1), monkeypatch_classify=lambda m, s: "supports")
stats8 = apply_dependency_recheck(
    {"recheck_candidate_details": many_candidates},
    bm8, cost={}, log=lambda m: None, verbose=False, graph=graph8, registry=registry8,
)
_restore()

check(
    "more candidates than MAX_RECHECKS_PER_CALL: hard cap respected, no network storm",
    stats8["rechecks_performed"] == MAX_RECHECKS_PER_CALL
    and stats8["retrieval_calls"] == MAX_RECHECKS_PER_CALL
    and stats8["skipped_cap"] == 2,
    f"{stats8}",
)

# ── 9. No belief associated with the family -> gathering skipped, no crash, no fabricated belief ──

graph9 = _tmp_graph()
bm9 = _tmp_belief_manager()  # no beliefs added at all
registry9 = FakeRegistry([_family("fam_I", "I statement", "cl_i1")])

_patch(monkeypatch_retrieve=lambda claims, fetch_cache=None: _fake_evidence(1), monkeypatch_classify=lambda m, s: "supports")
stats9 = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_I")]},
    bm9, cost={}, log=lambda m: None, verbose=False, graph=graph9, registry=registry9,
)
_restore()

check(
    "no belief found for the family: no crash, no belief fabricated, no evidence gathered",
    stats9["skipped_no_belief"] == 1 and stats9["belief_updates"] == 0 and len(bm9.beliefs) == 0,
    f"{stats9} beliefs={len(bm9.beliefs)}",
)

# ── 10. Empty/None family_dependency_stats -> no crash, all zeros ──

graph10 = _tmp_graph()
bm10 = _tmp_belief_manager()
stats10a = apply_dependency_recheck(None, bm10, cost={}, log=lambda m: None, verbose=False, graph=graph10)
stats10b = apply_dependency_recheck({}, bm10, cost={}, log=lambda m: None, verbose=False, graph=graph10)
check(
    "None/empty family_dependency_stats never crashes, performs zero rechecks",
    stats10a["rechecks_performed"] == 0 and stats10b["rechecks_performed"] == 0,
    f"{stats10a} {stats10b}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
