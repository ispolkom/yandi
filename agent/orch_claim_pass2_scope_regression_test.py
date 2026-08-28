"""
agent/orch_claim_pass2_scope_regression_test.py — regression for the PASS2
amplification bug found by YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md §3
(P1-A, PART A of the follow-up brief).

Root cause: agent/orchestrator/claims/retrieval.py::
apply_claim_resolution_and_second_retrieval() correctly scopes the actual
NETWORK retrieval (retrieve_for_claims) to only the claims that need it
(retrieval_claims — those without an effective direct+eligible
supports/contradicts relation from PASS1) — but the SECOND MAPPER + NLI
PASS that follows was called with claims_data (ALL claims), not
retrieval_claims. This re-mapped and re-scored claims that were already
resolved at PASS1, for no reason (nothing new was fetched for them).

Live-observed consequence (PRE-PUSH GATE session, live_run.log): claim
cl_afff1e70, resolved at PASS1 with relation=supports against a direct,
eligible evidence item, got silently re-evaluated in this redundant PASS2
NLI re-run and its relation FLIPPED to uncertain against the SAME
evidence — pure NLI non-determinism corrupting an already-settled claim,
paid for with real NLI/embedding cost that produced nothing but risk.

Fix: scope both map_claims_to_evidence() and run_claim_evidence_batch()
calls to retrieval_claims only. Already-resolved claims are never passed
into either function again, so their derived_from_evidence_ids /
evidence_relations / verification_status from PASS1 are left completely
untouched — not reset, not defaulted, not re-scored.

Acceptance criterion (per the brief): PASS2 must no longer be ABLE to
change a claim it was never supposed to touch. This test proves that by
using spies that both (a) record exactly which claims each function was
called with, and (b) would corrupt an already-resolved claim's data if it
were ever handed to them — so any regression of the fix (passing
claims_data again) is caught two ways: the call-scope assertion, and the
already-resolved claim's data changing.

Run: /home/iam/venv/bin/python3 -m agent.orch_claim_pass2_scope_regression_test
"""
from __future__ import annotations

from types import SimpleNamespace

import agent.orchestrator.claims.retrieval as retrieval_mod

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


def _make_resolved_claim(claim_id, relation, ev_id="ev_resolved"):
    return {
        "claim_id": claim_id,
        "claim_text": f"resolved claim {claim_id}",
        "verification_status": "candidate",
        "derived_from_evidence_ids": [ev_id],
        "evidence_relations": [
            {
                "evidence_id": ev_id,
                "evidence_role": "direct",
                "evidence_eligible": True,
                "relation": relation,
            }
        ],
        "source_cluster_id": f"cluster_{claim_id}",
    }


def _make_unresolved_claim(claim_id):
    return {
        "claim_id": claim_id,
        "claim_text": f"unresolved claim {claim_id}",
        "verification_status": "candidate",
        "derived_from_evidence_ids": [],
        "evidence_relations": [],
    }


class _Spies:
    def __init__(self):
        self.map_calls = []
        self.nli_calls = []

    def fake_retrieve_for_claims(self, claims, fetch_cache=None):
        return [
            {"evidence_id": "ev_new_1", "content_excerpt": "x" * 60,
             "source_uri": "http://example.test/new", "retrieval_origin": "claim_specific",
             "retrieval_claim_id": claims[0]["claim_id"] if claims else ""},
        ]

    def fake_merge_evidence(self, existing, new):
        return list(existing) + list(new)

    def fake_assign_source_clusters(self, evidence_data, log=None, verbose=False):
        return None

    def fake_map_claims_to_evidence(self, claims, evidence_records):
        self.map_calls.append([c.get("claim_id") for c in claims])
        return [
            SimpleNamespace(claim_id=c.get("claim_id"), derived_from_evidence_ids=["ev_new_1"])
            for c in claims
        ]

    def fake_run_claim_evidence_batch(self, claims, evidence, batch_label, log, verbose):
        self.nli_calls.append([c.get("claim_id") for c in claims])
        for c in claims:
            # Deliberately DIFFERENT from any pre-existing relation, so
            # if a resolved claim were ever (incorrectly) passed in, its
            # corruption would be visible in the assertions below.
            c["evidence_relations"] = [
                {"evidence_id": "ev_new_1", "evidence_role": "direct",
                 "evidence_eligible": True, "relation": "uncertain"}
            ]
        return len(claims)


def _run(claims_data, spies):
    orig = {}
    patches = {
        "retrieve_for_claims": spies.fake_retrieve_for_claims,
        "merge_evidence": spies.fake_merge_evidence,
        "assign_source_clusters": spies.fake_assign_source_clusters,
        "map_claims_to_evidence": spies.fake_map_claims_to_evidence,
        "run_claim_evidence_batch": spies.fake_run_claim_evidence_batch,
    }
    for name, fn in patches.items():
        orig[name] = getattr(retrieval_mod, name)
        setattr(retrieval_mod, name, fn)

    try:
        cost = {}
        result_evidence = retrieval_mod.apply_claim_resolution_and_second_retrieval(
            claims_data=claims_data,
            evidence_data=[],
            enable_web=True,
            is_subjective_answer=False,
            skip_rag=False,
            request_fetch_cache=None,
            cost=cost,
            log=_noop_log,
            verbose=True,
        )
        return result_evidence, cost
    finally:
        for name, fn in orig.items():
            setattr(retrieval_mod, name, fn)


# ── Scenario 1 (basic): A resolved (supports), B unresolved ──
claim_a = _make_resolved_claim("A", "supports")
claim_b = _make_unresolved_claim("B")
a_snapshot_before = {
    "derived_from_evidence_ids": list(claim_a["derived_from_evidence_ids"]),
    "evidence_relations": [dict(r) for r in claim_a["evidence_relations"]],
    "verification_status": claim_a["verification_status"],
    "source_cluster_id": claim_a["source_cluster_id"],
}

claims_data = [claim_a, claim_b]
spies = _Spies()
_run(claims_data, spies)

check(
    "PASS2 mapper (map_claims_to_evidence) was called with ONLY the "
    "unresolved claim (B), never the already-resolved claim (A)",
    spies.map_calls == [["B"]],
    f"map_calls={spies.map_calls}",
)
check(
    "PASS2 NLI (run_claim_evidence_batch) was called with ONLY the "
    "unresolved claim (B), never the already-resolved claim (A)",
    spies.nli_calls == [["B"]],
    f"nli_calls={spies.nli_calls}",
)
check(
    "already-resolved claim A's evidence_relations are byte-for-byte "
    "unchanged after PASS2 (no re-scoring, no relation drift)",
    claim_a["evidence_relations"] == a_snapshot_before["evidence_relations"],
    f"before={a_snapshot_before['evidence_relations']} after={claim_a['evidence_relations']}",
)
check(
    "already-resolved claim A's derived_from_evidence_ids are unchanged "
    "(provenance preserved, not reset/defaulted)",
    claim_a["derived_from_evidence_ids"] == a_snapshot_before["derived_from_evidence_ids"],
    f"after={claim_a['derived_from_evidence_ids']}",
)
check(
    "already-resolved claim A's source_cluster_id survived untouched",
    claim_a["source_cluster_id"] == a_snapshot_before["source_cluster_id"],
    f"after={claim_a.get('source_cluster_id')}",
)
check(
    "claim B (the one that actually needed retrieval) WAS updated with "
    "new derived_from_evidence_ids from the mapper",
    claim_b["derived_from_evidence_ids"] == ["ev_new_1"],
    f"B derived_from_evidence_ids={claim_b['derived_from_evidence_ids']}",
)
check(
    "claim B's evidence_relations were updated by PASS2 NLI",
    claim_b["evidence_relations"] and claim_b["evidence_relations"][0]["relation"] == "uncertain",
    f"B evidence_relations={claim_b['evidence_relations']}",
)
check(
    "the final claim set still contains BOTH A and B (same objects, "
    "nothing dropped)",
    claims_data == [claim_a, claim_b] and len(claims_data) == 2,
    f"claims_data ids={[c['claim_id'] for c in claims_data]}",
)

# ── Scenario 2 (mixed): supported(A) + contradicted(C) + unresolved(B) ──
claim_a2 = _make_resolved_claim("A2", "supports")
claim_c2 = _make_resolved_claim("C2", "contradicts", ev_id="ev_c2")
claim_b2 = _make_unresolved_claim("B2")

a2_before = [dict(r) for r in claim_a2["evidence_relations"]]
c2_before = [dict(r) for r in claim_c2["evidence_relations"]]

claims_data2 = [claim_a2, claim_c2, claim_b2]
spies2 = _Spies()
_run(claims_data2, spies2)

check(
    "mixed case (supported + contradicted + unresolved): PASS2 mapper "
    "receives ONLY the genuinely unresolved claim",
    spies2.map_calls == [["B2"]],
    f"map_calls={spies2.map_calls}",
)
check(
    "mixed case: PASS2 NLI receives ONLY the genuinely unresolved claim",
    spies2.nli_calls == [["B2"]],
    f"nli_calls={spies2.nli_calls}",
)
check(
    "mixed case: the supported claim (A2) is untouched",
    claim_a2["evidence_relations"] == a2_before,
    f"A2 after={claim_a2['evidence_relations']}",
)
check(
    "mixed case: the contradicted claim (C2) is untouched (contradicts "
    "also counts as resolved per _claim_has_effective_evidence)",
    claim_c2["evidence_relations"] == c2_before,
    f"C2 after={claim_c2['evidence_relations']}",
)
check(
    "mixed case: the unresolved claim (B2) was updated",
    claim_b2["derived_from_evidence_ids"] == ["ev_new_1"],
    f"B2 derived_from_evidence_ids={claim_b2['derived_from_evidence_ids']}",
)
check(
    "mixed case: final claim set contains all three (A2 + C2 + B2)",
    len(claims_data2) == 3,
    f"claims_data2 ids={[c['claim_id'] for c in claims_data2]}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
