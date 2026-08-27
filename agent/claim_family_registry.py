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

from agent.claim_semantic_identity_prototype import classify_claim_pair

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

    def find_or_link_claim(
        self,
        claim_text: str,
        claim_id: str,
        domain: str,
        log=None,
        verbose: bool = False,
    ) -> Optional[str]:
        """
        Returns the family_id this claim occurrence was linked into (an
        existing family if a match was found, a brand-new one
        otherwise), or None if claim_text/claim_id is empty (no
        fabricated family for a degenerate claim).
        """
        claim_text = (claim_text or "").strip()
        if not claim_text or not claim_id:
            return None

        domain = domain or "unknown"
        candidates = [f for f in self.families if f.get("domain") == domain]

        for family in candidates:
            canonical = family.get("canonical_text", "")
            if not canonical:
                continue

            outcome = classify_claim_pair(canonical, claim_text)
            if outcome in ("exact", "equivalent"):
                # Idempotency: the same claim_id linked twice (e.g. a
                # retry) must not duplicate itself in the members list.
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

        if verbose and log:
            log(f"[Claim Family] new family={new_family['family_id']} claim={claim_id}")

        return new_family["family_id"]


_inst: Optional[ClaimFamilyRegistry] = None


def get_claim_family_registry() -> ClaimFamilyRegistry:
    global _inst
    if _inst is None:
        _inst = ClaimFamilyRegistry()
    return _inst
