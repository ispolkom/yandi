"""
agent/dependency_recheck.py — Epistemic Core v1 Phase 12: bounded,
controlled re-evaluation of claims flagged by Phase 11's dependency graph
as RECHECK_CANDIDATE.

CORE PRINCIPLE (verbatim from the plan): new contradicting evidence does
NOT mean a dependent claim is FALSE. It means the dependent claim
REQUIRES RECHECK. This module performs that recheck — real retrieval,
real NLI, a real (Bayesian, history-preserving) belief update — but never
substitutes "contradicted" for "unverified" or invents a verdict when the
recheck itself is inconclusive or fails.

WHAT THIS MODULE REUSES (no new truth engine, no new NLI, no new
retrieval client):
    - agent.claim_evidence_retriever.retrieve_for_claims — the SAME
      claim-specific retrieval primitive already used in the live request
      path (agent/orchestrator/claims/retrieval.py's second pass), with
      its own existing MAX_CLAIMS/timeout bounds.
    - agent.claim_relation.classify_relation — the SAME single-pair NLI
      classifier used elsewhere (agent/claim_graph.py's docstring
      examples, embedding-gated fallback in claims/status.py's threshold
      calibration note) — not a batch call, since exactly one claim (the
      dependent family's canonical text) is being re-verified per
      candidate.
    - agent.belief_manager.BeliefManager.add_belief — calling it with the
      SAME (topic, statement) as an existing belief routes to its
      existing _update_existing() path (exact-match lookup in
      _find_similar()), which already does Bayesian confidence update +
      append-only history. No belief mutation logic is reimplemented
      here.
    - agent.family_dependency_graph.FamilyDependencyGraph — the Phase 11
      store gains a `recheck_log` (added there, not duplicated here) for
      cooldown/retry-bound bookkeeping.

WHAT THIS MODULE DOES NOT TOUCH: claim verification_status, Trust (any of
its three computation paths), final coverage, or the answer of the
request that happened to trigger the recheck. The belief being updated
belongs to a DIFFERENT semantic family than the one the triggering
request was about — this closes a cross-request epistemic loop, it does
not create an intra-request side effect on the user's own answer.

BOUNDS (12.3/12.4 — a network storm is the one failure mode this module
exists to prevent, not just tolerate):
    MAX_RECHECKS_PER_CALL   — global cap on real retrieval calls per
                              invocation, regardless of how many changed
                              families or candidates Phase 11 found this
                              request. Not per-family: worst case is
                              bounded no matter how many families changed
                              at once.
    CASCADE DEPTH = 1        — only depth==1 candidates (immediate
                              dependents of the family that changed) are
                              ever rechecked synchronously. A depth-2+
                              candidate is not chased in the same
                              request; if the depth-1 recheck itself
                              changes that family's status, THAT becomes
                              a new trigger for a future request's own
                              traversal — multi-hop propagation happens
                              one hop per request cycle, never all at
                              once.
    RECHECK_COOLDOWN_SECONDS — a family already rechecked (any outcome)
                              within the cooldown window is skipped —
                              retry bound / self-trigger protection, so a
                              family sitting at the intersection of
                              several simultaneously-changed dependencies
                              is not re-fetched repeatedly in a burst.

FAILURE SEMANTICS (12.3): retrieval exception -> outcome "error", belief
untouched. Evidence found but every relation is unrelated/uncertain ->
outcome "inconclusive", belief untouched (deliberately: calling
add_belief() with two empty evidence lists would only append a
no-op history entry with no real signal — see the module's own
regression test for why this is a considered choice, not the same as
"claim is false"). Only when at least one supports/contradicts relation
is found does this module call add_belief(), and only for the DIRECTION
of the evidence actually observed — it never fabricates the opposite
side.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from agent.claim_evidence_retriever import retrieve_for_claims
from agent.claim_family_registry import get_claim_family_registry
from agent.claim_relation import ClaimRelation, classify_relation
from agent.family_dependency_graph import FamilyDependencyGraph, get_family_dependency_graph

MAX_RECHECKS_PER_CALL = 3
RECHECK_COOLDOWN_SECONDS = 3600
DEFAULT_RECHECK_EVIDENCE_STRENGTH = 0.5  # same established default as a freshly extracted claim's claim_confidence


def _family_by_id(registry, family_id: str) -> Optional[Dict[str, Any]]:
    for fam in registry.families:
        if fam.get("family_id") == family_id:
            return fam
    return None


def _belief_for_family(belief_manager, family: Dict[str, Any]):
    """
    Read-only bridge between a semantic claim family (Phase 10) and a
    belief (pre-existing, independent identity system) — no schema change
    to either. A belief is associated with this family if ANY of the
    family's member claim_ids appear in that belief's own claim_ids list
    (Belief.claim_ids already exists for this exact purpose — "which
    claims produced this belief" — it was simply never cross-referenced
    against a family before). If several beliefs match (possible: a
    family can span claims from requests that each independently created
    their own belief before ever being linked into one family), the most
    recently updated one is used — a deterministic, defensible tie-break,
    not a claim that the others are wrong.
    """
    member_claim_ids = {m.get("claim_id") for m in family.get("members", []) if m.get("claim_id")}
    if not member_claim_ids:
        return None

    matches = [
        b for b in belief_manager.beliefs
        if member_claim_ids.intersection(set(b.claim_ids or []))
    ]
    if not matches:
        return None
    matches.sort(key=lambda b: b.updated_at, reverse=True)
    return matches[0]


def apply_dependency_recheck(
    family_dependency_stats: Optional[Dict[str, Any]],
    belief_manager,
    cost: Dict[str, Any],
    log,
    verbose: bool,
    graph: Optional[FamilyDependencyGraph] = None,
    registry=None,
    fetch_cache=None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Consumes agent.family_dependency_graph.apply_family_dependency_shadow's
    stats dict (specifically `recheck_candidate_details`) and performs a
    bounded number of real rechecks. Returns a stats dict for logging; the
    production call site treats it the same way (bare statement) since
    nothing about THIS request's own answer/Trust/coverage may depend on
    it — see this module's docstring for why that is still true even
    though, unlike Phase 11, this module DOES cause a real, persisted,
    cross-request belief mutation as its entire purpose.

    run_id (Этап 5, SQL persistence migration): the CURRENT request's
    run — the one whose Phase 11 dependency graph traversal happened to
    trigger this recheck of a DIFFERENT family. Purely an append-only
    provenance tag on the resulting recheck_event row (mandate §16); it
    is optional and defaults to None (this module has exactly one
    production call site, unlike belief_manager.py's — see that
    module's own shadow-write commit for why threading run_id wasn't
    done there).
    """
    t0 = time.time()
    graph = graph or get_family_dependency_graph()
    registry = registry or get_claim_family_registry()

    stats = {
        "candidates_seen": 0,
        "rechecks_performed": 0,
        "retrieval_calls": 0,
        "nli_calls": 0,
        "belief_updates": 0,
        "skipped_cooldown": 0,
        "skipped_depth": 0,
        "skipped_cap": 0,
        "skipped_no_family_record": 0,
        "skipped_no_belief": 0,
        "errors": 0,
        "inconclusive": 0,
        "elapsed_ms": 0.0,
    }

    if not family_dependency_stats:
        return stats

    candidates = family_dependency_stats.get("recheck_candidate_details") or []
    # Only immediate dependents (depth 1) are chased synchronously — see
    # module docstring's CASCADE DEPTH = 1 rationale.
    depth1_candidates = [c for c in candidates if c.get("depth") == 1]
    stats["skipped_depth"] = len(candidates) - len(depth1_candidates)

    seen_families = set()
    rechecks_done = 0

    for cand in depth1_candidates:
        family_id = cand.get("dependent_family")
        if not family_id or family_id in seen_families:
            continue
        seen_families.add(family_id)
        stats["candidates_seen"] += 1

        if rechecks_done >= MAX_RECHECKS_PER_CALL:
            stats["skipped_cap"] += 1
            continue

        if not graph.can_recheck(family_id, RECHECK_COOLDOWN_SECONDS):
            stats["skipped_cooldown"] += 1
            continue

        family = _family_by_id(registry, family_id)
        if not family:
            stats["skipped_no_family_record"] += 1
            continue

        canonical_text = (family.get("canonical_text") or "").strip()
        if not canonical_text:
            stats["skipped_no_family_record"] += 1
            continue

        cand_started_at = time.time()
        cand_trigger_reason = cand.get("changed_family")
        cand_domain = family.get("domain")

        belief = _belief_for_family(belief_manager, family) if belief_manager else None
        if not belief:
            stats["skipped_no_belief"] += 1
            # No belief to update, but this still counts as an attempted
            # recheck slot spent (retrieval below still runs) — logged,
            # not silently free, so it can't be used to bypass the cap by
            # only targeting family-less candidates.
            graph.record_recheck(
                family_id, "no_belief", run_id=run_id, trigger_reason=cand_trigger_reason,
                started_at=cand_started_at, domain=cand_domain, canonical_text=canonical_text,
            )
            rechecks_done += 1
            if verbose:
                log(
                    f"[Dependency Recheck] family={family_id} has no associated "
                    f"belief (claim_ids never matched) — skipping evidence gathering"
                )
            continue

        rechecks_done += 1
        try:
            synthetic_claim = {
                "claim_id": f"recheck_{family_id}",
                "claim_text": canonical_text,
                "claim_type": "factual",
                # No original query survives at family granularity (a
                # family can outlive the request that created it), so the
                # canonical claim text is the best available stand-in for
                # retrieval's own query_context field — same fallback
                # already used elsewhere when no better context exists
                # (agent/orchestrator/claims/lifecycle.py defaults an
                # unset query_context similarly, just from the request's
                # query_to_use instead).
                "query_context": canonical_text,
            }
            retrieval_t0 = time.time()
            recheck_evidence = retrieve_for_claims([synthetic_claim], fetch_cache=fetch_cache)
            stats["retrieval_calls"] += 1

            evidence_for = []
            evidence_against = []
            for ev in recheck_evidence:
                excerpt = (ev.get("content_excerpt") or "").strip()
                if not excerpt:
                    continue
                relation = classify_relation(canonical_text, excerpt)
                stats["nli_calls"] += 1
                ev_id = ev.get("evidence_id")
                if relation == ClaimRelation.SUPPORTS and ev_id:
                    evidence_for.append(ev_id)
                elif relation == ClaimRelation.CONTRADICTS and ev_id:
                    evidence_against.append(ev_id)
                # unrelated / uncertain -> neither list, same convention
                # as every other NLI consumer in this codebase.

            retrieval_elapsed_ms = (time.time() - retrieval_t0) * 1000

            if not recheck_evidence:
                outcome = "no_evidence"
                stats["inconclusive"] += 1
            elif not evidence_for and not evidence_against:
                outcome = "inconclusive"
                stats["inconclusive"] += 1
            else:
                if evidence_for and evidence_against:
                    outcome = "disputed"
                elif evidence_for:
                    outcome = "supported"
                else:
                    outcome = "contradicted"

                belief_manager.add_belief(
                    topic=belief.topic,
                    statement=belief.statement,
                    confidence=DEFAULT_RECHECK_EVIDENCE_STRENGTH,
                    evidence_for=evidence_for,
                    evidence_against=evidence_against,
                    claim_ids=belief.claim_ids,
                )
                stats["belief_updates"] += 1

            graph.record_recheck(
                family_id, outcome, run_id=run_id, trigger_reason=cand_trigger_reason,
                started_at=cand_started_at, domain=cand_domain, canonical_text=canonical_text,
            )
            stats["rechecks_performed"] += 1

            if verbose:
                log(
                    f"[Dependency Recheck] family={family_id} "
                    f"triggered_by={cand.get('changed_family')} "
                    f"belief={belief.id} outcome={outcome} "
                    f"evidence_for={len(evidence_for)} evidence_against={len(evidence_against)} "
                    f"retrieval_ms={retrieval_elapsed_ms:.1f}"
                )

        except Exception as e:
            stats["errors"] += 1
            graph.record_recheck(
                family_id, "error", run_id=run_id, trigger_reason=cand_trigger_reason,
                started_at=cand_started_at, domain=cand_domain, canonical_text=canonical_text,
                reason=str(e)[:120],
            )
            if verbose:
                log(f"[Dependency Recheck] family={family_id} error={e}")
            # Failure never touches the belief — old state (and its
            # history) is left exactly as it was.

    graph._save()
    stats["elapsed_ms"] = (time.time() - t0) * 1000
    cost["dependency_recheck_ms"] = stats["elapsed_ms"]

    if verbose:
        log(
            "[Dependency Recheck Summary] "
            f"candidates_seen={stats['candidates_seen']} "
            f"performed={stats['rechecks_performed']} "
            f"belief_updates={stats['belief_updates']} "
            f"retrieval_calls={stats['retrieval_calls']} "
            f"nli_calls={stats['nli_calls']} "
            f"inconclusive={stats['inconclusive']} "
            f"errors={stats['errors']} "
            f"skipped_cooldown={stats['skipped_cooldown']} "
            f"skipped_depth={stats['skipped_depth']} "
            f"skipped_no_belief={stats['skipped_no_belief']} "
            f"elapsed_ms={stats['elapsed_ms']:.2f}"
        )

    return stats
