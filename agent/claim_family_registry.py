"""
agent/claim_family_registry.py — Epistemic Core v1 Phase 10: cross-
request claim linking.

Introduces the minimal "semantic claim family" concept the plan asks
for: group claim OCCURRENCES (across separate requests, separate
processes) that Phase 9B's hardened classify_claim_pair() judges
equivalent, into one family, WITHOUT:

    - destroying the occurrence claim_id (each occurrence keeps its own
      random claim_id from Phase 2's "CLAIM IDENTITY" block; a family
      groups occurrences, it does not replace them);
    - losing wording (each member's original claim_text is stored, not
      normalized away);
    - losing evidence provenance (this registry stores no evidence at
      all — a claim's evidence_relations stay exactly where Phase 1 put
      them, on the trace's ClaimRecord; linking a claim into a family is
      purely additive metadata alongside that, never a replacement for
      it);
    - collapsing temporal variants ("the company is restructuring" vs
      "the company restructured before" must NOT become one family —
      already protected transitively: Phase 9B's hardening_guard()
      downgrades exactly this current_vs_historical pattern to
      "different", so classify_claim_pair() (reused here unmodified)
      will not judge such a pair equivalent in the first place).

Persistence is deliberately minimal: one JSON file
(registry/claim_families.json), one record per family:
    {
        "family_id": "fam_<uuid8>",
        "domain": "...",                    # scoping key, see below
        "canonical_text": "<first member's claim_text>",
        "members": [
            {"claim_id": "...", "claim_text": "...", "linked_at": <ts>},
            ...
        ],
        "created_at": <ts>,
        "updated_at": <ts>,
    }
members is APPEND-ONLY — find_or_link_claim() never removes or
overwrites an existing member, matching this project's established
"history is never destroyed" discipline (belief_manager.py's history
list, Phase 1's evidence_relations, etc.).

Comparison scope: domain-scoped (mirrors belief_manager.py's own
topic-scoped candidate filtering — an established, not invented,
pattern) — find_or_link_claim() only compares a new claim against
existing families in the SAME domain, bounding cost somewhat. This is
NOT benchmarked at scale (how many families a busy domain accumulates
over weeks of traffic, and what that does to per-claim linking latency,
is an open question — flagged honestly in the Phase 10 report, not
addressed here, matching Phase 4's benchmark-first honesty rather than
either ignoring the question or over-engineering an unrequested fix).
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.claim_semantic_identity_prototype import classify_claim_pair, EMBEDDING_PREFILTER_THRESHOLD
from agent.claim_identity import canonicalize_claim_text
from agent.belief_manager import BeliefManager

BASE = Path(__file__).parent.parent
DEFAULT_REGISTRY_PATH = BASE / "registry" / "claim_families.json"


class ClaimFamilyRegistry:
    def __init__(self, storage_file: Optional[Path] = None):
        self.storage_file = storage_file or DEFAULT_REGISTRY_PATH
        self.families: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.storage_file.exists():
            try:
                with open(self.storage_file, "r", encoding="utf-8") as f:
                    self.families = json.load(f)
            except Exception:
                # Fail-safe: a corrupt/unreadable registry must never
                # crash the pipeline — start empty rather than lose the
                # request. The on-disk file is left untouched until the
                # next successful _save() (never blindly overwritten by
                # an empty in-memory state before a real write happens).
                self.families = []

    def _save(self) -> None:
        try:
            self.storage_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.storage_file, "w", encoding="utf-8") as f:
                json.dump(self.families, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _link_member(self, family, claim_id, claim_text, log, verbose) -> str:
        """Shared tail of both the exact-fast-path and the main decision
        loop below — append-only member linking, unchanged from before
        (P9 §7's old-vs-new equivalence requirement: this is the exact
        same code the old single loop used to run inline)."""
        already_member = any(
            m.get("claim_id") == claim_id for m in family.get("members", [])
        )
        if not already_member:
            family.setdefault("members", []).append({
                "claim_id": claim_id,
                "claim_text": claim_text,
                "linked_at": time.time(),
            })
            family["updated_at"] = time.time()
            self._save()

        if verbose and log:
            log(
                f"[Claim Family] linked claim={claim_id} -> "
                f"family={family['family_id']} "
                f"(members={len(family.get('members', []))})"
            )
        return family["family_id"]

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

        P9 (Этап 4D-2): decision POLICY is unchanged from the original
        per-candidate loop (same domain scoping, same threshold, same
        LLM judge, same hardening_guard, same "first candidate in
        registry order that clears all three wins") — only the
        EMBEDDING step is batched: one /api/embed call for [claim_text]
        + every candidate's canonical_text, instead of one call per
        candidate (was O(candidates) HTTP round-trips per claim; is
        O(1)). See classify_claim_pair()'s precomputed_similarity
        param, which this reuses rather than duplicating the judge/
        hardening logic.

        stats: optional dict this mutates in place with running totals
        (claims/exact_hits/embed_batches/embedded_texts/
        prefilter_candidates/llm_judges/linked/created) for the
        [FamilyIdentity] observability line — purely additive, None
        (default) disables collection with zero extra overhead for any
        other caller.
        """
        claim_text = (claim_text or "").strip()
        if not claim_text or not claim_id:
            return None

        if stats is not None:
            stats["claims"] = stats.get("claims", 0) + 1

        domain = domain or "unknown"
        candidates = [f for f in self.families if f.get("domain") == domain]

        # ---- Exact fast path FIRST, across ALL candidates (P9 §2) ----
        # Pure text normalization, no network at all — must run before
        # any embedding attempt, same as the old per-candidate loop did
        # implicitly (classify_claim_pair_detailed's own exact-check was
        # always the first thing it did, per pair).
        norm_claim = canonicalize_claim_text(claim_text)
        if norm_claim:
            for family in candidates:
                canonical = family.get("canonical_text", "")
                if canonical and canonicalize_claim_text(canonical) == norm_claim:
                    if stats is not None:
                        stats["exact_hits"] = stats.get("exact_hits", 0) + 1
                        stats["linked"] = stats.get("linked", 0) + 1
                    return self._link_member(family, claim_id, claim_text, log, verbose)

        # ---- Batched embedding prefilter (P9 §3) ----
        # ONE /api/embed call for the new claim + every candidate's
        # canonical_text (skipping candidates with no canonical_text,
        # same as the old loop's own "if not canonical: continue").
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
            # vectors is None (embedding endpoint failure): similarities
            # stays empty -> classify_claim_pair() below gets
            # precomputed_similarity=None for every candidate and falls
            # through to its OWN per-pair embedding attempt, which will
            # also fail the same way — identical fail-safe behavior to
            # before (P9 §9), not a new fallback.

        # ---- Decision loop in the SAME OLD registry order (P9 §4) ----
        # First candidate that clears threshold + LLM judge + hardening
        # wins — exactly the old per-pair classify_claim_pair() loop,
        # just fed a precomputed similarity instead of recomputing one.
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
                return self._link_member(family, claim_id, claim_text, log, verbose)

        # No match in this domain — create a new family.
        new_family = {
            "family_id": f"fam_{uuid.uuid4().hex[:8]}",
            "domain": domain,
            "canonical_text": claim_text,
            "members": [{"claim_id": claim_id, "claim_text": claim_text, "linked_at": time.time()}],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self.families.append(new_family)
        self._save()

        if stats is not None:
            stats["created"] = stats.get("created", 0) + 1

        if verbose and log:
            log(f"[Claim Family] new family={new_family['family_id']} claim={claim_id}")

        return new_family["family_id"]


_inst: Optional[ClaimFamilyRegistry] = None


def get_claim_family_registry() -> ClaimFamilyRegistry:
    global _inst
    if _inst is None:
        _inst = ClaimFamilyRegistry()
    return _inst
