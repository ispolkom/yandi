"""
agent/epistemic_contradiction_shadow.py — Этап 4G-3 (P10): EPISTEMIC
CONTRADICTION SHADOW.

Pure, read-only diagnostic layered on top of the EXISTING persistent
family dependency graph (agent/family_dependency_graph.py, Phase 11)
and family history read path (agent/verification_memory.py, Этап
4G-2). For each persisted `contradicts` edge between two semantic
families F1<->F2, answers a narrower question than Phase 11/12 ever
asked: is this a contradiction backed by INDEPENDENT, EVIDENCE-
GROUNDED support on both sides — or purely an intra-request,
evidence-free claim<->claim NLI artifact (Этап 4E's central finding,
concretely demonstrated by the real persisted Apple-founding-year
edge: two claim TEXTS disagree, but neither claim has any independent
supporting evidence root behind it)?

ONE NORMALIZED OBSERVATION SHAPE, TWO LAYERS (per the user's explicit
4G-3 brief):
    HISTORICAL — verification_memory.get_family_historical_evidence()
    CURRENT    — this request's own claims_data/evidence_data,
                 normalized here (_normalize_current_observations) into
                 the EXACT SAME dict shape as the historical function
                 already returns (same keys, same compute_stable_root
                 call) — not a second, divergent format.
Both layers are merged and DEDUPED BY stable_root before counting, so
a `local_memory` replay (current layer) of a URL already observed
historically (historical layer) collapses into ONE root, never two.

ELIGIBILITY: reuses agent.orchestrator.claims.status._counts_toward_
status() UNCHANGED — the exact same authority/directness predicate
that already decides whether a relation counts toward a claim's own
verification_status. No second truth table is invented here.

STRUCTURALLY INERT: every function in this module only ever READS
claims_data/evidence_data/the dependency graph. Nothing here is called
from production yet (see the module-level docstring note at the
bottom) — this stage delivers the classifier + its regression proof,
matching the same "read path first, wiring is a separate later
decision" shape as Этап 4G-2.

V1 deliberately has NO threshold — see run_epistemic_contradiction_
shadow()'s docstring for the exact minimal candidate rule, which the
user explicitly asked to keep to "at least one eligible support root
on each side AND at least two distinct roots across both sides",
without picking 2/3 or any other number.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.orchestrator.claims.status import _counts_toward_status
from agent.verification_memory import (
    compute_stable_root,
    get_family_historical_evidence,
)


def _normalize_current_observations(
    family_id: str,
    claims_data: List[Dict[str, Any]],
    evidence_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    CURRENT-request layer, normalized into the SAME shape
    get_family_historical_evidence() returns (Этап 4G-2) — same keys,
    same compute_stable_root() call — so the caller can merge both
    layers without caring which one an observation came from.
    """
    observations: List[Dict[str, Any]] = []

    for claim in claims_data or []:
        if claim.get("semantic_family_id") != family_id:
            continue

        claim_id = claim.get("claim_id")
        for rel in claim.get("evidence_relations", []) or []:
            ev = evidence_by_id.get(rel.get("evidence_id"))
            if not ev:
                continue

            observation = {
                "semantic_family_id": family_id,
                "claim_id": claim_id,
                "trace_id": "__current__",
                "evidence_id": ev.get("evidence_id"),
                "relation": rel.get("relation"),
                "source_uri": ev.get("source_uri"),
                "route": ev.get("route", "internet") or "internet",
                "origin_route": ev.get("origin_route"),
                "observed_at": ev.get("observed_at"),
                "origin_observed_at": ev.get("origin_observed_at"),
                "source_cluster_id": ev.get("source_cluster_id"),
                "origin_source_cluster_id": ev.get("origin_source_cluster_id"),
                "directness": rel.get("directness"),
                "evidence_eligible": rel.get("evidence_eligible"),
                "evidence_role": rel.get("evidence_role"),
                "source_class": rel.get("source_class"),
                "retrieval_origin": rel.get("retrieval_origin"),
                "origin_trace_id": ev.get("origin_trace_id"),
            }
            observation["stable_root"] = compute_stable_root(observation)
            observations.append(observation)

    return observations


def _eligible_support_roots(
    family_id: str,
    claims_data: List[Dict[str, Any]],
    evidence_by_id: Dict[str, Dict[str, Any]],
) -> Set[Tuple[str, str]]:
    """
    Distinct stable_root values (Этап 4G-2) among observations that
    genuinely SUPPORT family_id's proposition — relation == "supports"
    AND _counts_toward_status(rel) is True (agent.orchestrator.claims.
    status's own, unmodified eligibility predicate — not a new one).

    "does this evidence really support the proposition" is deliberately
    NOT "does this family merely have some evidence attached to it" —
    unrelated/uncertain/rejected/ineligible relations never contribute
    a root, per the user's explicit RELATION POLICY.

    Merges CURRENT + HISTORICAL layers BEFORE dedup, so a stable_root
    observed in both layers (e.g. a local_memory replay this request of
    a URL first seen historically) counts once, not twice.
    """
    historical = get_family_historical_evidence(family_id)
    current = _normalize_current_observations(family_id, claims_data, evidence_by_id)

    roots: Set[Tuple[str, str]] = set()
    for obs in historical + current:
        if obs.get("relation") != "supports":
            continue

        counted, _via = _counts_toward_status(obs)
        if not counted:
            continue

        root = obs.get("stable_root")
        if root is not None:
            roots.add(root)

    return roots


def evaluate_contradiction_event(
    from_family: str,
    to_family: str,
    claims_data: List[Dict[str, Any]],
    evidence_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Evidence-grounded classification for ONE contradicts edge
    (from_family <-> to_family — order does not matter, the edge is
    treated as symmetric here regardless of which direction the
    persisted graph happened to store it in).

    V1 minimal candidate rule (no threshold, per the user's explicit
    instruction — just factual metrics + this one conservative rule):

        candidate = True  iff
            len(roots_a) >= 1        # F1 has at least one independent,
                                      # eligible, SUPPORTING root
            AND len(roots_b) >= 1    # same for F2
            AND len(roots_a | roots_b) >= 2
                # at least two DISTINCT roots across BOTH sides —
                # rules out the collision case where the SAME document
                # is being read as "supporting" both mutually
                # exclusive claims (roots_a == roots_b == {R} would
                # give a union of size 1, never counted as
                # independent).

    Returns roots as plain counts (not the raw sets) in the primary
    dict, plus the raw sets under "_roots_a_raw"/"_roots_b_raw" for a
    caller (e.g. the comparison-matrix diagnostic) that needs them —
    never logged as-is (see run_epistemic_contradiction_shadow's
    compact log line).
    """
    roots_a = _eligible_support_roots(from_family, claims_data, evidence_by_id)
    roots_b = _eligible_support_roots(to_family, claims_data, evidence_by_id)
    overlap = roots_a & roots_b
    distinct_union = roots_a | roots_b

    if not roots_a and not roots_b:
        reason = "no_support_root_either_side"
    elif not roots_a:
        reason = "no_support_root_family_a"
    elif not roots_b:
        reason = "no_support_root_family_b"
    elif len(distinct_union) < 2:
        reason = "roots_collide_not_independent"
    else:
        reason = "independent_support_both_sides"

    candidate = bool(roots_a) and bool(roots_b) and len(distinct_union) >= 2

    return {
        "edge": f"{from_family}<->{to_family}",
        "family_a": from_family,
        "family_b": to_family,
        "roots_a": len(roots_a),
        "roots_b": len(roots_b),
        "overlap": len(overlap),
        "distinct": len(distinct_union),
        "candidate": candidate,
        "reason": reason,
        "_roots_a_raw": roots_a,
        "_roots_b_raw": roots_b,
    }


def _distinct_contradicts_pairs(graph) -> List[Tuple[str, str]]:
    """
    record_edge() always writes a `contradicts` edge in BOTH directions
    (family_dependency_graph.py's apply_family_dependency_shadow, lines
    407-408) — this collapses that back down to one unordered pair per
    contradiction, so evaluate_contradiction_event() is never run twice
    for the same edge.
    """
    seen: Set[frozenset] = set()
    pairs: List[Tuple[str, str]] = []
    for edge in getattr(graph, "edges", []):
        if edge.get("edge_type") != "contradicts":
            continue
        fam_a, fam_b = edge.get("from_family"), edge.get("to_family")
        if not fam_a or not fam_b or fam_a == fam_b:
            continue
        key = frozenset((fam_a, fam_b))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((fam_a, fam_b))
    return pairs


def run_epistemic_contradiction_shadow(
    claims_data: List[Dict[str, Any]],
    evidence_data: Optional[List[Dict[str, Any]]],
    graph=None,
    log=None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Top-level entry point: evaluates every persisted `contradicts` edge
    (Phase 11's graph, not just ones touched this request) and returns
    aggregate stats + the per-edge event list.

    FAIL-OPEN (per the user's explicit requirement): any exception
    anywhere in this function is caught here, logged if verbose, and an
    empty/safe stats dict is returned — this function must never be
    able to raise into its caller's request path. Nothing it does can
    gate Phase 12, mutate family_state, belief, claims, Trust, or the
    answer — it does not accept a belief_manager/synthesis_result/trust
    parameter, structurally the same inertness contract as
    apply_family_dependency_shadow() (Phase 11).
    """
    t0 = time.time()
    try:
        if graph is None:
            from agent.family_dependency_graph import get_family_dependency_graph
            graph = get_family_dependency_graph()

        evidence_by_id = {
            ev.get("evidence_id"): ev
            for ev in (evidence_data or [])
            if ev.get("evidence_id")
        }

        events = []
        for fam_a, fam_b in _distinct_contradicts_pairs(graph):
            event = evaluate_contradiction_event(fam_a, fam_b, claims_data, evidence_by_id)
            events.append(event)

            if verbose and log:
                log(
                    "[EpistemicEventShadow] "
                    f"edge={event['edge']} "
                    f"roots_a={event['roots_a']} "
                    f"roots_b={event['roots_b']} "
                    f"overlap={event['overlap']} "
                    f"distinct={event['distinct']} "
                    f"candidate={event['candidate']} "
                    f"reason={event['reason']}"
                )

        stats = {
            "contradicts_edges_evaluated": len(events),
            "candidates_true": sum(1 for e in events if e["candidate"]),
            "candidates_false": sum(1 for e in events if not e["candidate"]),
            "events": events,
            "elapsed_ms": (time.time() - t0) * 1000,
            "error": None,
        }

        if verbose and log:
            log(
                "[EpistemicEventShadow] "
                f"edges_evaluated={stats['contradicts_edges_evaluated']} "
                f"candidates_true={stats['candidates_true']} "
                f"candidates_false={stats['candidates_false']} "
                f"elapsed_ms={stats['elapsed_ms']:.2f}"
            )

        return stats

    except Exception as e:
        if verbose and log:
            try:
                log(f"[EpistemicEventShadow] ERROR fail-open: {e}")
            except Exception:
                pass
        return {
            "contradicts_edges_evaluated": 0,
            "candidates_true": 0,
            "candidates_false": 0,
            "events": [],
            "elapsed_ms": (time.time() - t0) * 1000,
            "error": str(e),
        }


def build_shadow_request_summary(
    claims_data: List[Dict[str, Any]],
    contradiction_stats: Dict[str, Any],
    family_dependency_stats: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Этап 4G-4: JSON-safe (plain ints/strings/None only — never the raw
    `_roots_a_raw`/`_roots_b_raw` sets inside contradiction_stats
    "events", which are NOT serializable) per-request diagnostic
    summary, meant for trace.add_observation() — never the canonical
    claim/evidence/Trust/verdict schema.

    Deliberately keeps TWO separate notions apart (Этап 4G-4 §5): the
    global registry scan (contradiction_stats — ALL persisted
    contradicts edges, most of which have nothing to do with THIS
    request) vs a request-SCOPED subset (edges where at least one side
    is a semantic_family_id that actually appears among claims_data
    this cycle). A live run must never present a stale whole-registry
    number as if it says something about the current question — the
    caller gets both, clearly labeled, and decides.

    current_recheck_candidates is Phase 12's OWN, already-computed
    candidate count (family_dependency_stats["recheck_candidate_
    details"], read-only) — not recomputed here, so this can never
    drift from what Phase 12 itself actually saw this request.
    """
    families_this_request = {
        c.get("semantic_family_id")
        for c in (claims_data or [])
        if c.get("semantic_family_id")
    }

    events = contradiction_stats.get("events") or []
    touched_events = [
        e for e in events
        if e.get("family_a") in families_this_request
        or e.get("family_b") in families_this_request
    ]

    return {
        "edges_checked": contradiction_stats.get("contradicts_edges_evaluated", 0),
        "candidates_true": contradiction_stats.get("candidates_true", 0),
        "candidates_false": contradiction_stats.get("candidates_false", 0),
        "shadow_error": contradiction_stats.get("error"),
        "current_recheck_candidates": len(
            (family_dependency_stats or {}).get("recheck_candidate_details") or []
        ),
        "touched_this_request": len(touched_events),
        "touched_current_yes_shadow_yes": sum(1 for e in touched_events if e.get("candidate")),
        "touched_current_yes_shadow_no": sum(1 for e in touched_events if not e.get("candidate")),
    }


# ============================================================
# PRODUCTION WIRING (Этап 4G-4): SHADOW OBSERVABILITY ONLY
# ============================================================
#
# agent/orchestrator_v2.py calls run_epistemic_contradiction_shadow()
# right after apply_family_dependency_shadow() and BEFORE
# apply_dependency_recheck() — the SAME claims_data/evidence_data/log/
# verbose already in scope there, no pipeline restructuring. Its
# return value is summarized via build_shadow_request_summary() and
# recorded on the trace via trace.add_observation() for later
# inspection; apply_dependency_recheck() right after it is called
# EXACTLY as before, reading only _family_dependency_stats — it never
# receives this call's return value, so it is structurally unable to
# be gated, suppressed, or otherwise altered by this shadow classifier
# (proven by agent/epistemic_contradiction_shadow_wiring_regression_
# test.py's call-order + non-mutation checks).
