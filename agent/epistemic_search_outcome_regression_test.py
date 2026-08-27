"""
agent/epistemic_search_outcome_regression_test.py — Epistemic Core v1
Phase 3 regression: search-outcome disambiguation
(evidence_search_attempted / evidence_search_error), companion fields to
verification_status added in
agent/orchestrator/claims/retrieval.py::apply_claim_resolution_and_second_retrieval().

Proves the audit's §9/§3 finding is addressed without touching the
existing verification_status vocabulary or its assignment logic
(claims/status.py is untouched by this phase): a claim that ends up
"unverified" can now be told apart into "PASS2 not applicable (already
resolved)", "PASS2 needed but never attempted (gate blocked)", "PASS2
attempted, succeeded (found nothing new, or something new)", and "PASS2
attempted, errored" — all while verification_status itself keeps meaning
exactly what it meant before.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_search_outcome_regression_test
"""

import json
import time
from unittest.mock import patch

from agent.orchestrator.claims import retrieval as retrieval_mod
from agent.orch_tracer import Trace

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


def _log(msg):
    pass


def _needs_retrieval_claim(cid):
    return {
        "claim_id": cid,
        "claim_text": "some claim needing more evidence",
        "verification_status": "candidate",
        "evidence_relations": [],
        "derived_from_evidence_ids": [],
    }


def _already_resolved_claim(cid):
    return {
        "claim_id": cid,
        "claim_text": "some already-resolved claim",
        "verification_status": "supported",
        "evidence_relations": [
            {"evidence_id": "ev1", "relation": "supports",
             "evidence_role": "direct", "evidence_eligible": True},
        ],
        "derived_from_evidence_ids": ["ev1"],
    }


# ── A. Gate blocked entirely (enable_web=False): needs-retrieval claim -> attempted=False ──

claims_a = [_needs_retrieval_claim("cl_a1"), _already_resolved_claim("cl_a2")]
retrieval_mod.apply_claim_resolution_and_second_retrieval(
    claims_a, evidence_data=[], enable_web=False, is_subjective_answer=False,
    skip_rag=False, request_fetch_cache=None, cost={}, log=_log, verbose=False,
)
claim_a1 = next(c for c in claims_a if c["claim_id"] == "cl_a1")
claim_a2 = next(c for c in claims_a if c["claim_id"] == "cl_a2")
check(
    "gate blocked (enable_web=False): needs-retrieval claim -> attempted=False, error=None",
    claim_a1["evidence_search_attempted"] is False and claim_a1["evidence_search_error"] is None,
    f"{claim_a1}",
)
check(
    "gate blocked: already-resolved claim -> attempted=None (PASS2 not applicable, not 'not attempted')",
    claim_a2["evidence_search_attempted"] is None and claim_a2["evidence_search_error"] is None,
    f"{claim_a2}",
)

# ── B. Gate open, retrieval succeeds (finds nothing new) -> attempted=True, error=None ──

claims_b = [_needs_retrieval_claim("cl_b1")]
with patch.object(retrieval_mod, "retrieve_for_claims", return_value=[]), \
     patch.object(retrieval_mod, "merge_evidence", side_effect=lambda old, new: old):
    retrieval_mod.apply_claim_resolution_and_second_retrieval(
        claims_b, evidence_data=[], enable_web=True, is_subjective_answer=False,
        skip_rag=False, request_fetch_cache=None, cost={}, log=_log, verbose=False,
    )
claim_b1 = claims_b[0]
check(
    "gate open, search attempted and succeeded (nothing new found) -> attempted=True, error=None "
    "(NOT FOUND must be distinguishable from NOT SEARCHED, and is not itself an error)",
    claim_b1["evidence_search_attempted"] is True and claim_b1["evidence_search_error"] is None,
    f"{claim_b1}",
)
check(
    "search attempted+found-nothing must NOT change verification_status by itself "
    "(this phase adds a companion field, it does not touch claims/status.py's logic)",
    claim_b1["verification_status"] == "candidate",
    f"{claim_b1['verification_status']}",
)

# ── C. Gate open, retrieval call itself raises -> attempted=True, error=<message> ──

claims_c = [_needs_retrieval_claim("cl_c1")]
with patch.object(retrieval_mod, "retrieve_for_claims", side_effect=ConnectionError("simulated network outage")):
    retrieval_mod.apply_claim_resolution_and_second_retrieval(
        claims_c, evidence_data=[], enable_web=True, is_subjective_answer=False,
        skip_rag=False, request_fetch_cache=None, cost={}, log=_log, verbose=False,
    )
claim_c1 = claims_c[0]
check(
    "gate open, retrieval raises -> attempted=True AND error set "
    "(ERROR != NOT FOUND: both are distinguishable from each other and from 'not attempted')",
    claim_c1["evidence_search_attempted"] is True
    and claim_c1["evidence_search_error"] == "simulated network outage",
    f"{claim_c1}",
)

# ── D. is_subjective_answer=True gate also blocks (same as enable_web=False) ──

claims_d = [_needs_retrieval_claim("cl_d1")]
retrieval_mod.apply_claim_resolution_and_second_retrieval(
    claims_d, evidence_data=[], enable_web=True, is_subjective_answer=True,
    skip_rag=False, request_fetch_cache=None, cost={}, log=_log, verbose=False,
)
check(
    "subjective-answer gate also blocks retrieval -> attempted=False",
    claims_d[0]["evidence_search_attempted"] is False,
    f"{claims_d[0]}",
)

# ── E. Round trip through Trace for all three attempted states + error ──

trace = Trace(trace_id="t_test", timestamp=time.time(), query="test")
for i, c in enumerate([claim_a1, claim_a2, claim_c1]):
    c2 = dict(c)
    c2["claim_id"] = f"cl_rt{i}"
    c2["claim_text"] = f"Некоторое утверждение номер {i}, достаточно длинное для прохождения фильтра чистоты."
    trace.add_claim_raw(c2)

rt = json.loads(json.dumps(trace.to_dict(), ensure_ascii=False))
by_id = {c["claim_id"]: c for c in rt["claims"]}

check(
    "round trip: attempted=False + error=None survives",
    by_id["cl_rt0"]["evidence_search_attempted"] is False
    and by_id["cl_rt0"]["evidence_search_error"] is None,
    f"{by_id.get('cl_rt0')}",
)
check(
    "round trip: attempted=None (not applicable) survives as null, not False",
    by_id["cl_rt1"]["evidence_search_attempted"] is None,
    f"{by_id.get('cl_rt1')}",
)
check(
    "round trip: attempted=True + real error message survives",
    by_id["cl_rt2"]["evidence_search_attempted"] is True
    and by_id["cl_rt2"]["evidence_search_error"] == "simulated network outage",
    f"{by_id.get('cl_rt2')}",
)

# ── F. Backward compatibility: claim dict with neither key at all ──

trace2 = Trace(trace_id="t_test2", timestamp=time.time(), query="test2")
old_claim = {
    "claim_id": "cl_old",
    "claim_text": "a claim from code that predates Phase 3 entirely, long enough to pass filter",
    "verification_status": "unverified",
}
try:
    trace2.add_claim_raw(old_claim)
    rt2 = json.loads(json.dumps(trace2.to_dict(), ensure_ascii=False))
    check(
        "backward compat: missing both keys -> both None, no crash",
        rt2["claims"][0]["evidence_search_attempted"] is None
        and rt2["claims"][0]["evidence_search_error"] is None,
        f"{rt2['claims'][0]}",
    )
except Exception as e:
    check("backward compat: missing both keys -> both None, no crash", False, repr(e))

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
