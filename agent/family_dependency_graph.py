"""
agent/family_dependency_graph.py — Epistemic Core v1 Phase 11: cross-request
semantic-family dependency graph, SHADOW MODE ONLY.

Answers, purely diagnostically, the question the plan poses for this phase:
"if the epistemic state of semantic claim family A changed, which OTHER
families might need re-checking?" Nothing in this module writes
verification_status, Trust, belief status, retrieval behavior, or final
coverage. Its output is persisted (edges + last-seen-status snapshot) and
logged, and returned to the caller as a stats dict that the production call
site must not capture into a decision-bearing variable — same contract as
Phase 8's agent/claim_graph_shadow.py.

WHY THIS IS A NEW MODULE, NOT A NEW "dependency_graph.py" FROM SCRATCH:
The audit's original dependency-graph proposal warned against inventing a
second module when claim_graph.py already had the right *shape* for a
PER-REQUEST, claim_id-keyed graph — and Phase 8 already reactivated that
module in shadow mode (agent/claim_graph_shadow.py). What Phase 8 explicitly
does NOT do (by its own docstring) is accumulate a graph ACROSS requests,
because cross-request identity did not exist until Phase 10's semantic claim
families. This module is the natural continuation once that identity
exists: same reused NLI results, same shadow-mode contract, but keyed on
`semantic_family_id` instead of `claim_id`, and persisted instead of
per-call, because "does this family's state getting revised implicate other
families" is inherently a cross-request question.

DEPENDENCY SEMANTICS — the one substantive design decision this phase makes:

    SUPPORTS    — stored as a diagnostic edge only (mirrors the already-
                  computed claim<->claim NLI "supports" relation, projected
                  onto the two claims' families). Direction follows the
                  NLI pair's own (main_claim, other_claim) order.
                  Does NOT create a depends_on edge.

    CONTRADICTS — stored as a diagnostic edge AND, symmetrically, creates
                  a depends_on edge in BOTH directions (A depends_on B and
                  B depends_on A).

Why not "A supports B => B depends_on A" (the convention the same-request,
throwaway Phase 8 shadow graph uses for its own summary() bookkeeping)? The
plan explicitly warns against this: an NLI "supports" verdict between two
independently-extracted world claims is a lexical/entailment observation,
not proof that B's own justification structurally routes through A — B may
have its own direct evidence and would remain just as justified even if A's
current status were revised. A CONTRADICTION is different in kind: two
claims that were found to contradict each other cannot both remain true:
if new evidence changes one, the other's current status is DIRECTLY put in
question by the same fact pattern that created the edge in the first place.
That is the only relation this phase treats as epistemically license to
flag "requires recheck" — not lexical similarity, not co-occurrence, not a
new NLI pass, not a new truth engine.

TRIGGER (11.2): a family's "state" is the raw `verification_status` string
(no new vocabulary — reuses claims/status.py's existing values verbatim) of
the claim occurrence(s) this request linked into that family. Each call
compares this request's observed status per family against the PREVIOUS
persisted status for that family. First-ever observation of a family
establishes a baseline and is NEVER treated as a change (there is nothing
to have changed FROM). Only a genuine transition (old != new, both known)
triggers a bounded depends_on traversal for RECHECK_CANDIDATEs.

CYCLE SAFETY (11.6): depends_on is symmetric by construction (see above),
so even a single contradicting pair forms a 2-cycle. Traversal is bounded
by MAX_TRAVERSAL_DEPTH, a visited set seeded with the origin (changed)
family so the traversal can never re-flag the family that changed as its
own dependent, and MAX_RECHECK_CANDIDATES as a hard cap. "cycles" counts
edges that lead back to the origin family (a closed loop to the start);
"duplicates_suppressed" counts edges that lead to some OTHER family
already visited via a different path. Both are diagnostic counts only.

SCOPE LIMITATION (documented, not fixed here): claim<->family linking
(agent/orchestrator/claims/lifecycle.py) is capped at claims_data[:3] per
request (an existing Phase 10 cost bound, not something this phase
touches), while claim<->claim NLI pairs (disagreement.py) are NOT capped
the same way. So many NLI pairs this module sees will have no
semantic_family_id on one or both sides — those pairs are skipped (counted
in `skipped_no_family`), not treated as absent relations. This means the
persisted graph only ever grows from the subset of claims that also got a
family assigned, exactly mirroring the existing [:3] bound elsewhere.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.db.sql.shadow_write import shadow_record_recheck_event

BASE = Path(__file__).parent.parent
DEFAULT_STORE_PATH = BASE / "registry" / "claim_family_graph.json"

MAX_TRAVERSAL_DEPTH = 5
MAX_RECHECK_CANDIDATES = 50


class FamilyDependencyGraph:
    def __init__(self, storage_file: Optional[Path] = None):
        self.storage_file = storage_file or DEFAULT_STORE_PATH
        self.edges: List[Dict[str, Any]] = []
        self.family_state: Dict[str, Dict[str, Any]] = {}
        self.recheck_log: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.storage_file.exists():
            try:
                with open(self.storage_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.edges = data.get("edges", []) or []
                self.family_state = data.get("family_state", {}) or {}
                self.recheck_log = data.get("recheck_log", {}) or {}
            except Exception:
                # Fail-safe: a corrupt/unreadable graph must never crash the
                # pipeline. Start empty in memory; the on-disk file is left
                # untouched until the next successful _save() (never
                # blindly overwritten before a real write happens).
                self.edges = []
                self.family_state = {}
                self.recheck_log = {}

    def _save(self) -> None:
        try:
            self.storage_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.storage_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "edges": self.edges,
                        "family_state": self.family_state,
                        "recheck_log": self.recheck_log,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass

    def can_recheck(self, family_id: str, cooldown_seconds: float) -> bool:
        """
        Epistemic Core v1 Phase 12: retry-bound / self-trigger protection.
        A family that was actually rechecked (a real retrieval attempt was
        made, regardless of outcome) within the last `cooldown_seconds` is
        not eligible again yet — prevents a family sitting at the
        intersection of several changed dependencies from being
        re-fetched over and over in a short span.
        """
        entry = self.recheck_log.get(family_id)
        if not entry:
            return True
        return (time.time() - entry.get("last_rechecked_at", 0)) >= cooldown_seconds

    def record_recheck(
        self, family_id: str, outcome: str, run_id: Optional[str] = None,
        trigger_reason: Optional[str] = None, started_at: Optional[float] = None,
        reason: Optional[str] = None, domain: Optional[str] = None,
        canonical_text: Optional[str] = None,
    ) -> None:
        entry = self.recheck_log.get(family_id, {"recheck_count": 0})
        entry["last_rechecked_at"] = time.time()
        entry["last_outcome"] = outcome
        entry["recheck_count"] = entry.get("recheck_count", 0) + 1
        self.recheck_log[family_id] = entry
        # Этап 5 (SQL persistence migration, mandate §16): the JSON
        # recheck_log above is CURRENT-STATE-ONLY (overwrites on every
        # call, a confirmed real bug — see schema.py's recheck_event
        # comment). This is the append-only side: one row per actual
        # recheck attempt, history never lost.
        shadow_record_recheck_event(
            family_id=family_id, outcome=outcome, run_id=run_id,
            trigger_reason=trigger_reason, started_at=started_at, reason=reason,
            domain=domain, canonical_text=canonical_text,
        )

    def _find_edge(self, from_family: str, to_family: str, edge_type: str) -> Optional[Dict[str, Any]]:
        for e in self.edges:
            if (
                e.get("from_family") == from_family
                and e.get("to_family") == to_family
                and e.get("edge_type") == edge_type
            ):
                return e
        return None

    def record_edge(
        self,
        from_family: str,
        to_family: str,
        edge_type: str,
        reason: str,
        triggering_claim_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Additive, idempotent edge upsert. A self-loop (from_family ==
        to_family) is never recorded — it is not a valid dependency and
        would only ever arise from a degenerate case (e.g. two claim
        occurrences in the same request both already linked into the same
        family being NLI-compared against each other), never a real
        cross-family relation.
        """
        if not from_family or not to_family or from_family == to_family:
            return None

        triggering_claim_ids = triggering_claim_ids or []
        now = time.time()
        existing = self._find_edge(from_family, to_family, edge_type)
        if existing:
            existing["last_seen_at"] = now
            existing["observation_count"] = existing.get("observation_count", 1) + 1
            known = set(existing.get("triggering_claim_ids", []))
            for cid in triggering_claim_ids:
                if cid and cid not in known:
                    existing.setdefault("triggering_claim_ids", []).append(cid)
                    known.add(cid)
            return existing

        edge = {
            "edge_id": f"edg_{uuid.uuid4().hex[:8]}",
            "from_family": from_family,
            "to_family": to_family,
            "edge_type": edge_type,
            "reason": reason,
            "triggering_claim_ids": [cid for cid in triggering_claim_ids if cid],
            "created_at": now,
            "last_seen_at": now,
            "observation_count": 1,
        }
        self.edges.append(edge)
        return edge

    def dependents_of(self, family_id: str) -> List[Dict[str, Any]]:
        """
        Families Y such that Y depends_on family_id — i.e. edges recorded
        as (from_family=Y, to_family=family_id, edge_type="depends_on").
        """
        return [
            e
            for e in self.edges
            if e.get("to_family") == family_id and e.get("edge_type") == "depends_on"
        ]

    def observe_family_status(self, family_id: str, status: str) -> Dict[str, Any]:
        """
        Compares `status` against the previously persisted status for
        family_id (if any), updates the persisted snapshot, and returns
        {"changed": bool, "previous_status": str|None, "first_observation": bool}.

        A first-ever observation is NEVER "changed" — there is no prior
        state for the new state to have diverged from.
        """
        prev = self.family_state.get(family_id)
        prev_status = prev.get("last_status") if prev else None
        first_observation = prev is None

        changed = (not first_observation) and (prev_status != status)

        self.family_state[family_id] = {
            "last_status": status,
            "updated_at": time.time(),
        }

        return {
            "changed": changed,
            "previous_status": prev_status,
            "first_observation": first_observation,
        }

    def find_recheck_candidates(
        self,
        changed_family_id: str,
        max_depth: int = MAX_TRAVERSAL_DEPTH,
        max_candidates: int = MAX_RECHECK_CANDIDATES,
    ) -> Dict[str, Any]:
        """
        Bounded BFS over depends_on edges starting from the direct
        dependents of changed_family_id. Returns:

            candidates: [
                {"dependent_family", "edge_type", "reason",
                 "triggering_claim_ids", "depth"}, ...
            ]
            cycles: int              # edges leading back to the origin
            duplicates_suppressed: int  # edges leading to an already-
                                         # visited NON-origin family
            max_depth_reached: int
        """
        visited = {changed_family_id}
        candidates: List[Dict[str, Any]] = []
        cycles = 0
        duplicates_suppressed = 0
        max_depth_reached = 0

        queue: List[tuple] = [(changed_family_id, 0)]

        while queue:
            current_family, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            if len(candidates) >= max_candidates:
                break

            for edge in self.dependents_of(current_family):
                dependent = edge.get("from_family")
                if not dependent:
                    continue

                if dependent == changed_family_id:
                    cycles += 1
                    continue

                if dependent in visited:
                    duplicates_suppressed += 1
                    continue

                visited.add(dependent)
                next_depth = depth + 1
                max_depth_reached = max(max_depth_reached, next_depth)

                candidates.append({
                    "dependent_family": dependent,
                    "edge_type": edge.get("edge_type"),
                    "reason": edge.get("reason"),
                    "triggering_claim_ids": edge.get("triggering_claim_ids", []),
                    "depth": next_depth,
                })

                if len(candidates) >= max_candidates:
                    break

                queue.append((dependent, next_depth))

        return {
            "candidates": candidates,
            "cycles": cycles,
            "duplicates_suppressed": duplicates_suppressed,
            "max_depth_reached": max_depth_reached,
        }


_inst: Optional[FamilyDependencyGraph] = None


def get_family_dependency_graph() -> FamilyDependencyGraph:
    global _inst
    if _inst is None:
        _inst = FamilyDependencyGraph()
    return _inst


def apply_family_dependency_shadow(
    claims_data: List[Dict[str, Any]],
    disagreement_result: Optional[Dict[str, Any]],
    log,
    verbose: bool,
    graph: Optional[FamilyDependencyGraph] = None,
) -> Dict[str, Any]:
    """
    Builds/updates the persisted family-level dependency graph from the
    SAME claim<->claim NLI results agent/claim_graph_shadow.py already
    reuses (zero additional NLI calls), projected onto
    `semantic_family_id` (Phase 10) instead of `claim_id`. Detects
    family-level status transitions and logs bounded RECHECK_CANDIDATE
    diagnostics.

    This function itself remains pure shadow with respect to the CURRENT
    request: it only ever reads claims_data (never assigns into a claim
    dict) and takes no synthesis_result/trust/belief_manager/evidence_data
    parameter, so it is structurally incapable of influencing this
    request's own answer, Trust, claim verification_status, retrieval, or
    coverage — see the regression suite's structural checks.

    Epistemic Core v1 Phase 12 (additive): the returned stats dict now
    ALSO includes `recheck_candidate_details` (the flat list of every
    RECHECK_CANDIDATE found this call, across all changed families) so
    agent/dependency_recheck.py can read it and decide what to actually
    re-verify. That is a SEPARATE actor reading this function's output on
    purpose — not a violation of this function's own inertness, which is
    about what THIS function does, not about who may consume its return
    value afterward.
    """
    t0 = time.time()
    graph = graph or get_family_dependency_graph()

    claim_by_id = {
        c.get("claim_id"): c for c in claims_data if c.get("claim_id")
    }

    edges_recorded = 0
    skipped_no_family = 0

    if disagreement_result:
        batch_results = disagreement_result.get("batch_results") or []
        pair_claims = disagreement_result.get("pair_claims") or {}

        for result in batch_results:
            if result.get("method") != "llm_nli_batch":
                continue

            relation = result.get("relation")
            if relation not in ("contradicts", "supports"):
                # unrelated / uncertain -> no edge, same fail-open
                # principle as Phase 8.
                continue

            pair_id = str(result.get("pair_id", ""))
            pair = pair_claims.get(pair_id)
            if not pair:
                continue

            c1, c2 = pair
            id1, id2 = c1.get("claim_id"), c2.get("claim_id")
            if not id1 or not id2 or id1 == id2:
                continue

            fam1 = c1.get("semantic_family_id")
            fam2 = c2.get("semantic_family_id")
            if not fam1 or not fam2:
                skipped_no_family += 1
                continue

            if relation == "contradicts":
                e1 = graph.record_edge(fam1, fam2, "contradicts", "claim_claim_nli:contradicts", [id1, id2])
                e2 = graph.record_edge(fam2, fam1, "contradicts", "claim_claim_nli:contradicts", [id1, id2])
                d1 = graph.record_edge(fam1, fam2, "depends_on", "contradicts", [id1, id2])
                d2 = graph.record_edge(fam2, fam1, "depends_on", "contradicts", [id1, id2])
                edges_recorded += sum(1 for e in (e1, e2, d1, d2) if e)
            else:  # supports
                e = graph.record_edge(fam1, fam2, "supports", "claim_claim_nli:supports", [id1, id2])
                edges_recorded += 1 if e else 0

    # ---- Family status observation + trigger detection ----
    families_seen = set()
    for c in claims_data:
        fam = c.get("semantic_family_id")
        if fam:
            families_seen.add(fam)

    changed_families = []
    for fam in families_seen:
        # If several occurrences of this family appear this request (rare
        # — family linking is capped at claims_data[:3]), the LAST one's
        # status wins; this mirrors no established precedent because none
        # exists yet for this exact case, and is documented as such.
        status = None
        for c in claims_data:
            if c.get("semantic_family_id") == fam:
                status = c.get("verification_status", status)

        outcome = graph.observe_family_status(fam, status)
        if outcome["changed"]:
            changed_families.append((fam, outcome["previous_status"], status))

    recheck_candidate_details = []
    cycles_total = 0
    duplicates_suppressed_total = 0
    max_depth_reached = 0

    for fam, prev_status, new_status in changed_families:
        result = graph.find_recheck_candidates(fam)
        cycles_total += result["cycles"]
        duplicates_suppressed_total += result["duplicates_suppressed"]
        max_depth_reached = max(max_depth_reached, result["max_depth_reached"])

        for cand in result["candidates"]:
            entry = {
                "changed_family": fam,
                "previous_status": prev_status,
                "new_status": new_status,
                **cand,
            }
            recheck_candidate_details.append(entry)
            if verbose:
                log(
                    "[Family Dependency Shadow] RECHECK_CANDIDATE "
                    f"changed_family={fam} {prev_status}->{new_status} "
                    f"dependent_family={cand['dependent_family']} "
                    f"edge_type={cand['edge_type']} reason={cand['reason']} "
                    f"depth={cand['depth']}"
                )

    graph._save()

    elapsed_ms = (time.time() - t0) * 1000

    stats = {
        "nodes": len(families_seen),
        "dependency_edges_recorded": edges_recorded,
        "families_changed": len(changed_families),
        "recheck_candidates": len(recheck_candidate_details),
        "recheck_candidate_details": recheck_candidate_details,
        "cycles": cycles_total,
        "duplicates_suppressed": duplicates_suppressed_total,
        "max_depth_reached": max_depth_reached,
        "skipped_no_family": skipped_no_family,
        "elapsed_ms": elapsed_ms,
    }

    if verbose:
        log(
            "[Family Dependency Shadow] "
            f"nodes={stats['nodes']} "
            f"edges_recorded={stats['dependency_edges_recorded']} "
            f"families_changed={stats['families_changed']} "
            f"recheck_candidates={stats['recheck_candidates']} "
            f"cycles={stats['cycles']} "
            f"duplicates_suppressed={stats['duplicates_suppressed']} "
            f"max_depth_reached={stats['max_depth_reached']} "
            f"skipped_no_family={stats['skipped_no_family']} "
            f"elapsed_ms={stats['elapsed_ms']:.2f}"
        )

    return stats
