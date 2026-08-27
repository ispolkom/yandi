"""
agent/claim_graph_shadow.py — Epistemic Core v1 Phase 8: reactivate
claim_graph.py in SHADOW MODE.

SHADOW ONLY, per the plan: this module's output influences NOTHING —
not the answer, not Trust, not belief status, not retrieval, not final
coverage. It observes the SAME claims and the SAME claim<->claim NLI
results already computed elsewhere this request and builds a
claim_graph.ClaimGraph purely for measurement/comparison logging.
Nothing else in this codebase reads this module's return value for a
decision; the caller in orchestrator_v2.py only logs it.

Two things this module deliberately does NOT do:

1. Does NOT reactivate claim_graph.py's own extract_claims() (sentence-
   splitting from raw evidence text via _is_world_claim/_determine_claim_type/
   etc). Calling that would stand up a SECOND, independent claim-
   extraction engine alongside orch_synthesizer.py's real one — exactly
   what the plan forbids ("НЕ создавать второй... truth engine"). Instead,
   the REAL, already-extracted live claims (claims_data) are mapped
   directly onto claim_graph.Claim instances — reusing the existing
   dataclass as a data model, bypassing its extraction logic entirely.

2. Does NOT run a second pairwise claim<->claim NLI pass, and does NOT
   reimplement claim_graph.py's own regex/word-overlap
   _is_contradiction()/_is_support() heuristics either. Claim<->claim
   relations come from the SAME infer_claim_relations_batch() results
   agent/orchestrator/claims/disagreement.py::apply_claim_claim_disagreement()
   already computed this request (that function now returns them — an
   additive change, see its docstring). Zero additional NLI calls are
   made by this module; nli_calls_reused in the returned stats counts
   how many of those already-paid-for results got turned into an edge.

A fresh ClaimGraph() is built per call, not the module-level singleton
in claim_graph.py (get_claim_graph()) — accumulating claims across
requests into one shared graph is a cross-request-state design question
for a later, separate phase (the audit's dependency-graph proposal), not
something this shadow-mode reactivation should do implicitly as a side
effect.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from agent.claim_graph import Claim, ClaimGraph


def run_claim_graph_shadow(
    claims_data: List[Dict[str, Any]],
    disagreement_result: Optional[Dict[str, Any]],
    claim_status_counts: Optional[Dict[str, int]],
    log,
    verbose: bool,
) -> Dict[str, Any]:
    """
    Builds a claim_graph.ClaimGraph from the real live claims and the
    already-computed claim<->claim NLI results, purely for observation.

    Returns a stats dict: nodes, edges, nli_calls_reused, overhead_ms,
    strange_edges (see below). The caller MUST NOT feed this into any
    decision — nothing in this codebase does, and that is the contract
    this module exists under while it stays in shadow mode.
    """
    t0 = time.time()

    graph = ClaimGraph()
    node_by_claim_id: Dict[str, Claim] = {}

    for claim in claims_data:
        claim_id = claim.get("claim_id")
        text = (claim.get("claim_text") or "").strip()
        if not claim_id or not text:
            continue

        node = Claim(
            claim_id=claim_id,
            text=text,
            claim_type=claim.get("claim_type", "factual"),
            confidence=float(claim.get("claim_confidence", 0.5) or 0.5),
            verification_status=claim.get("verification_status", "unverified"),
            is_world_claim=True,
        )
        graph.claims.append(node)
        graph.claim_map[claim_id] = node
        node_by_claim_id[claim_id] = node

    nli_calls_reused = 0

    if disagreement_result:
        batch_results = disagreement_result.get("batch_results") or []
        pair_claims = disagreement_result.get("pair_claims") or {}

        for result in batch_results:
            # Only a real LLM NLI verdict may create an edge — a fallback
            # (network/parse failure) must never fabricate a supports/
            # contradicts edge, same fail-open principle as Phase 5/6.
            if result.get("method") != "llm_nli_batch":
                continue

            pair_id = str(result.get("pair_id", ""))
            pair = pair_claims.get(pair_id)
            if not pair:
                continue

            c1, c2 = pair
            id1, id2 = c1.get("claim_id"), c2.get("claim_id")
            node1, node2 = node_by_claim_id.get(id1), node_by_claim_id.get(id2)
            if not node1 or not node2 or id1 == id2:
                continue

            nli_calls_reused += 1
            relation = result.get("relation")

            if relation == "contradicts":
                if id2 not in node1.contradicts:
                    node1.contradicts.append(id2)
                if id1 not in node2.contradicts:
                    node2.contradicts.append(id1)
            elif relation == "supports":
                # Same directional convention claim_graph.py's own
                # (now-bypassed) _build_graph() used: if claim1 supports
                # claim2, claim2 depends_on claim1.
                if id2 not in node1.supports:
                    node1.supports.append(id2)
                if id1 not in node2.depends_on:
                    node2.depends_on.append(id1)
            # unrelated / uncertain -> no edge either way. Mirrors the
            # invariant "SUPPORTS и CONTRADICTS никогда не смешиваются":
            # an inconclusive NLI verdict is not treated as either.

    # Diagnostic: a claim should never end up both supporting AND
    # contradicting the same other claim — that would be a genuinely
    # "strange edge" worth flagging (would indicate an NLI
    # inconsistency between the two directions of the same pair, or a
    # data bug in this wiring), not silently ignored.
    strange_edges = 0
    for node in graph.claims:
        overlap = set(node.supports) & set(node.contradicts)
        if overlap:
            strange_edges += len(overlap)
            if verbose:
                log(
                    f"[Claim Graph Shadow] STRANGE EDGE: "
                    f"claim={node.claim_id} both supports and "
                    f"contradicts {sorted(overlap)}"
                )

    elapsed_ms = (time.time() - t0) * 1000
    summary = graph.summary()

    stats = {
        "nodes": len(graph.claims),
        "edges": summary.get("graph_edges", 0),
        "nli_calls_reused": nli_calls_reused,
        "overhead_ms": elapsed_ms,
        "strange_edges": strange_edges,
    }

    if verbose:
        log(
            f"[Claim Graph Shadow] "
            f"nodes={stats['nodes']} "
            f"edges={stats['edges']} "
            f"nli_calls_reused={stats['nli_calls_reused']} "
            f"overhead_ms={stats['overhead_ms']:.2f} "
            f"strange_edges={stats['strange_edges']} "
            f"runtime_claim_status={claim_status_counts if claim_status_counts else 'n/a'}"
        )

    return stats
