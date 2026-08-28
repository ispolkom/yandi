"""
agent/family_history_read_path_regression_test.py — Этап 4G-2 (P10)
regression: FAMILY HISTORY READ PATH.

Covers the user's own 6-item spec for this sub-stage:
    A. two occurrences of one family, from different traces, are found
       (union across traces, same shape as get_historical_web_urls()).
    B. the same URL observed in two different requests resolves to ONE
       stable root (canonicalization, not raw string equality).
    C. two genuinely different URLs resolve to two DIFFERENT roots.
    D. a route="local_memory" replay of an internet URL is NOT counted
       as a new root — it resolves to the SAME root as the original.
    E. source_cluster_id differs between runs (P4F Finding Y: it's a
       random per-fetch id, never stable) but the stable root stays
       the same regardless.
    F. a DIFFERENT semantic_family_id is never pulled into the query —
       family-scoping is exact, not a fuzzy/union-everything scan.

Everything here is READ-ONLY against data shapes that already exist
(claim_verification_index rows + persisted Trace/EvidenceRecord JSONL).
No production caller is touched by this stage — see verification_memory.
py's Этап 4G-2 section docstring.

Run: /home/iam/venv/bin/python3 -m agent.family_history_read_path_regression_test
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.orch_tracer as ot
import agent.verification_memory as vm
from agent.orch_schemas import EvidenceRecord
from agent.verification_memory import (
    compute_stable_root,
    get_family_historical_evidence,
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


def _make_env():
    traces_dir = Path(tempfile.mkdtemp(prefix="p10_fh_traces_"))
    index_db = Path(tempfile.mkdtemp(prefix="p10_fh_index_")) / "index.db"
    return traces_dir, index_db


def _persist_claim_with_evidence(
    *,
    trace_id: str,
    claim_id: str,
    content_hash: str,
    semantic_family_id: str,
    evidence_id: str,
    source_uri: str,
    route: str = "internet",
    origin_route=None,
    origin_trace_id=None,
    origin_observed_at=None,
    origin_source_cluster_id=None,
    source_cluster_id=None,
):
    """Builds and saves one minimal real Trace with one claim + one
    linked evidence item, through the EXISTING Trace/DecisionTracer
    save path (same pattern as family_identity_ordering_regression_
    test.py's item 5) — not a hand-rolled JSONL shape."""
    trace = ot.Trace(trace_id=trace_id, timestamp=0.0, query="q")
    trace.add_claim_raw({
        "claim_id": claim_id,
        "claim_text": f"claim text for {claim_id}",
        "claim_confidence": 0.5,
        "content_hash": content_hash,
        "semantic_family_id": semantic_family_id,
        "derived_from_evidence_ids": [evidence_id],
        "evidence_relations": [
            {
                "evidence_id": evidence_id,
                "relation": "supports",
                "evidence_role": "direct",
                "evidence_eligible": True,
                "source_class": "reference",
                "directness": "direct",
            },
        ],
    })
    trace.add_evidence(EvidenceRecord(
        evidence_id=evidence_id,
        source_type="web",
        source_uri=source_uri,
        content_excerpt="some excerpt text",
        source_class="reference",
        evidence_eligible=True,
        route=route,
        origin_route=origin_route,
        origin_trace_id=origin_trace_id,
        origin_observed_at=origin_observed_at,
        origin_source_cluster_id=origin_source_cluster_id,
        source_cluster_id=source_cluster_id,
    ))
    ot.DecisionTracer().save_trace(trace)


# ============================================================
# A. Two occurrences of ONE family, from DIFFERENT traces, both found.
# ============================================================

traces_a, index_a = _make_env()
with patch.object(ot, "TRACES_DIR", traces_a), \
     patch.object(vm, "TRACES_DIR", traces_a), \
     patch.object(vm, "INDEX_DB", index_a):

    _persist_claim_with_evidence(
        trace_id="t_a1", claim_id="cl_a1", content_hash="h_a1",
        semantic_family_id="fam_A", evidence_id="ev_a1",
        source_uri="https://a.example/one",
    )
    _persist_claim_with_evidence(
        trace_id="t_a2", claim_id="cl_a2", content_hash="h_a2",
        semantic_family_id="fam_A", evidence_id="ev_a2",
        source_uri="https://a.example/two",
    )

    obs_a = get_family_historical_evidence("fam_A")

check(
    "A: both occurrences of fam_A found, from two different traces (union across traces)",
    {o["trace_id"] for o in obs_a} == {"t_a1", "t_a2"},
    f"{[o['trace_id'] for o in obs_a]}",
)
check(
    "A: both evidence items reconstructed (not just the first)",
    {o["evidence_id"] for o in obs_a} == {"ev_a1", "ev_a2"},
    f"{[o['evidence_id'] for o in obs_a]}",
)

# ============================================================
# F. A DIFFERENT semantic_family_id is never pulled into the query.
# (reuses the same env/data as A — fam_B was never written, and fam_A's
# query must not leak into a fam_B query or vice versa.)
# ============================================================

with patch.object(ot, "TRACES_DIR", traces_a), \
     patch.object(vm, "TRACES_DIR", traces_a), \
     patch.object(vm, "INDEX_DB", index_a):

    _persist_claim_with_evidence(
        trace_id="t_f1", claim_id="cl_f1", content_hash="h_f1",
        semantic_family_id="fam_B_other", evidence_id="ev_f1",
        source_uri="https://b.example/unrelated",
    )

    obs_a_after_b_written = get_family_historical_evidence("fam_A")
    obs_b = get_family_historical_evidence("fam_B_other")

check(
    "F: querying fam_A still returns exactly its own 2 observations, "
    "unaffected by fam_B_other now existing in the same index",
    {o["trace_id"] for o in obs_a_after_b_written} == {"t_a1", "t_a2"},
    f"{[o['trace_id'] for o in obs_a_after_b_written]}",
)
check(
    "F: querying fam_B_other returns ONLY its own observation, not fam_A's",
    [o["trace_id"] for o in obs_b] == ["t_f1"],
    f"{[o['trace_id'] for o in obs_b]}",
)

# ============================================================
# B. Same URL in two different requests -> ONE stable root
# (case/fragment differ, canonicalization makes them equal).
# ============================================================

obs_b1 = {
    "route": "internet", "origin_route": None,
    "source_uri": "HTTPS://Example.COM/page#section1",
}
obs_b2 = {
    "route": "internet", "origin_route": None,
    "source_uri": "https://example.com/page",
}
root_b1 = compute_stable_root(obs_b1)
root_b2 = compute_stable_root(obs_b2)
check(
    "B: same URL (differing only by case/fragment) across two requests -> ONE stable root",
    root_b1 is not None and root_b1 == root_b2,
    f"root_b1={root_b1} root_b2={root_b2}",
)

# ============================================================
# C. Two genuinely different URLs -> two DIFFERENT roots.
# ============================================================

obs_c1 = {"route": "internet", "origin_route": None, "source_uri": "https://x.example/alpha"}
obs_c2 = {"route": "internet", "origin_route": None, "source_uri": "https://x.example/beta"}
root_c1 = compute_stable_root(obs_c1)
root_c2 = compute_stable_root(obs_c2)
check(
    "C: two genuinely different URLs -> two different stable roots",
    root_c1 is not None and root_c2 is not None and root_c1 != root_c2,
    f"root_c1={root_c1} root_c2={root_c2}",
)

# ============================================================
# D. route="local_memory" replay of an internet URL is NOT a new root
# — resolves to the SAME root as the original internet observation.
# ============================================================

obs_d_original = {
    "route": "internet", "origin_route": None,
    "source_uri": "https://original.example/story",
}
obs_d_replay = {
    "route": "local_memory", "origin_route": "internet",
    "source_uri": "https://original.example/story",
}
root_d_original = compute_stable_root(obs_d_original)
root_d_replay = compute_stable_root(obs_d_replay)
check(
    "D: local_memory replay of an internet URL resolves to the SAME root as the original "
    "(not a new independent root)",
    root_d_original is not None and root_d_original == root_d_replay,
    f"original={root_d_original} replay={root_d_replay}",
)

# ============================================================
# E. source_cluster_id differs between runs (Finding Y: random per-
# fetch id) but the stable root stays the same regardless.
# ============================================================

obs_e_run1 = {
    "route": "internet", "origin_route": None,
    "source_uri": "https://stable.example/article",
    "source_cluster_id": "sc_run1_randomuuid",
}
obs_e_run2 = {
    "route": "internet", "origin_route": None,
    "source_uri": "https://stable.example/article",
    "source_cluster_id": "sc_run2_totally_different_uuid",
}
root_e_run1 = compute_stable_root(obs_e_run1)
root_e_run2 = compute_stable_root(obs_e_run2)
check(
    "E: source_cluster_id differs between runs, stable root stays identical "
    "(root ignores the unstable per-fetch cluster id, uses canonicalized source_uri)",
    root_e_run1 is not None and root_e_run1 == root_e_run2,
    f"run1={root_e_run1} run2={root_e_run2}",
)

# ============================================================
# Extra: non-internet channel is NOT countable as a root in V1
# (network_node/ai_chat deliberately excluded — no invented identity).
# ============================================================

obs_non_internet = {"route": "network_node", "origin_route": None, "source_uri": "n/a"}
check(
    "extra: non-internet channel (e.g. network_node) yields stable_root=None in V1, "
    "not a fabricated identity",
    compute_stable_root(obs_non_internet) is None,
    f"{compute_stable_root(obs_non_internet)}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
