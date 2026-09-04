"""
agent/claim_family_registry.py — Epistemic Core v1 Phase 10: cross-
request claim linking.

Groups claim OCCURRENCES (across separate requests, separate processes)
that classify_claim_pair() judges equivalent, into one family, WITHOUT:

    - destroying the occurrence claim_id (each occurrence keeps its own
      random claim_id from Phase 2's "CLAIM IDENTITY" block; a family
      groups occurrences, it does not replace them);
    - losing wording (each member's original claim_text is stored on
      claim_occurrence itself, not duplicated here);
    - losing evidence provenance (this registry stores no evidence at
      all — a claim's evidence_relations stay exactly where Phase 1 put
      them);
    - collapsing temporal variants ("the company is restructuring" vs
      "the company restructured before" must NOT become one family —
      already protected transitively: hardening_guard() downgrades
      exactly this current_vs_historical pattern to "different",
      classify_claim_pair() will not judge such a pair equivalent).

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/claim_families.json is
retired. claim_family + family_member (agent/db/sql/schema.py, class A
+ B) are the ONLY source of truth now — these tables already existed
before this rewrite, along with their own write functions (agent.db.sql.
repositories.get_or_create_claim_family()/link_family_member()), but
nothing in production ever called them; this rewrite is what actually
connects them. members is naturally append-only now (family_member's
own PRIMARY KEY (family_id, claim_id) plus INSERT IGNORE makes re-
linking the same claim a safe no-op at the SQL level — no more manual
"already_member" check needed, the old JSON version's own workaround
for not having a real uniqueness constraint to lean on).

Comparison scope: domain-scoped (mirrors belief_manager.py's own
topic-scoped candidate filtering) — find_or_link_claim() only compares
a new claim against existing families in the SAME domain.

FAIL LOUD, not fail-open: same discipline as belief_manager.py's own
"точка ноль" rewrite — SqlUnavailable propagates out of every method
here. There is no JSON fallback left to quietly succeed against.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from agent.claim_semantic_identity_prototype import classify_claim_pair, EMBEDDING_PREFILTER_THRESHOLD
from agent.claim_identity import canonicalize_claim_text
from agent.belief_manager import BeliefManager
from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


class ClaimFamilyRegistry:
    def find_or_link_claim(
        self,
        claim_text: str,
        claim_id: str,
        domain: str,
        log=None,
        verbose: bool = False,
        stats: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Returns the family_id this claim occurrence was linked into (an
        existing family if a match was found, a brand-new one
        otherwise), or None if claim_text/claim_id is empty (no
        fabricated family for a degenerate claim).

        Decision policy is unchanged from the original per-candidate
        loop (same domain scoping, same threshold, same LLM judge, same
        hardening_guard, same "first candidate in registry order that
        clears all three wins") — only the persistence layer changed.

        stats: optional dict this mutates in place with running totals
        (claims/exact_hits/embed_batches/embedded_texts/
        prefilter_candidates/llm_judges/linked/created) for the
        [FamilyIdentity] observability line.
        """
        claim_text = (claim_text or "").strip()
        if not claim_text or not claim_id:
            return None

        if stats is not None:
            stats["claims"] = stats.get("claims", 0) + 1

        domain = domain or "unknown"
        with get_connection() as conn:
            candidates = repo.list_claim_families_by_domain(conn, domain)

        # ---- Exact fast path FIRST, across ALL candidates ----
        norm_claim = canonicalize_claim_text(claim_text)
        if norm_claim:
            for family in candidates:
                canonical = family.get("canonical_text", "")
                if canonical and canonicalize_claim_text(canonical) == norm_claim:
                    if stats is not None:
                        stats["exact_hits"] = stats.get("exact_hits", 0) + 1
                        stats["linked"] = stats.get("linked", 0) + 1
                    return self._link_member(family["family_id"], claim_id, log, verbose)

        # ---- Batched embedding prefilter ----
        canonical_texts = [f.get("canonical_text", "") for f in candidates]
        nonempty_idx = [i for i, c in enumerate(canonical_texts) if c]

        similarities: Dict[int, float] = {}
        if nonempty_idx:
            texts_to_embed = [claim_text] + [canonical_texts[i] for i in nonempty_idx]
            vectors = BeliefManager._embed_batch(texts_to_embed)

            if stats is not None:
                stats["embed_batches"] = stats.get("embed_batches", 0) + 1
                stats["embedded_texts"] = stats.get("embedded_texts", 0) + len(texts_to_embed)
                stats["prefilter_candidates"] = stats.get("prefilter_candidates", 0) + len(nonempty_idx)

            if vectors is not None:
                import numpy as np
                claim_vec = vectors[0]
                for offset, idx in enumerate(nonempty_idx):
                    similarities[idx] = float(np.dot(claim_vec, vectors[offset + 1]))

        # ---- Decision loop in the SAME registry order ----
        for i, family in enumerate(candidates):
            canonical = canonical_texts[i]
            if not canonical:
                continue

            precomputed = similarities.get(i)
            if stats is not None and precomputed is not None and precomputed >= EMBEDDING_PREFILTER_THRESHOLD:
                stats["llm_judges"] = stats.get("llm_judges", 0) + 1

            outcome = classify_claim_pair(canonical, claim_text, precomputed_similarity=precomputed)
            if outcome in ("exact", "equivalent"):
                if stats is not None:
                    stats["linked"] = stats.get("linked", 0) + 1
                return self._link_member(family["family_id"], claim_id, log, verbose)

        # No match in this domain — create a new family.
        family_id = f"fam_{uuid.uuid4().hex[:8]}"
        now = time.time()
        with get_connection() as conn:
            repo.get_or_create_claim_family(conn, family_id, domain, claim_text, created_at=now)
            repo.link_family_member(conn, family_id, claim_id, linked_at=now)
            conn.commit()

        if stats is not None:
            stats["created"] = stats.get("created", 0) + 1

        if verbose and log:
            log(f"[Claim Family] new family={family_id} claim={claim_id}")

        return family_id

    def _link_member(self, family_id: str, claim_id: str, log, verbose: bool) -> str:
        """INSERT IGNORE on (family_id, claim_id) makes re-linking an
        already-linked claim a safe no-op at the SQL level — no manual
        "already a member" check needed, unlike the retired JSON
        version (which had no real uniqueness constraint to lean on)."""
        with get_connection() as conn:
            repo.link_family_member(conn, family_id, claim_id, linked_at=time.time())
            conn.commit()

        if verbose and log:
            log(f"[Claim Family] linked claim={claim_id} -> family={family_id}")
        return family_id

    def get_family(self, family_id: str) -> Optional[Dict[str, Any]]:
        """Public lookup-by-id — replaces directly reaching into a (now
        nonexistent) `.families` in-memory list, which agent/
        dependency_recheck.py's _family_by_id() used to do."""
        with get_connection() as conn:
            return repo.get_claim_family(conn, family_id)


_inst: Optional[ClaimFamilyRegistry] = None


def get_claim_family_registry() -> ClaimFamilyRegistry:
    global _inst
    if _inst is None:
        _inst = ClaimFamilyRegistry()
    return _inst
