"""
agent/orchestrator/claims/family_shadow.py — Claim family SHADOW
classifier (YANDI performance follow-up, "P3 — CLAIM FAMILIES /
SHARED RETRIEVAL", Phases 0-4).

SHADOW ONLY. This module is read-only and side-effect-free: it never
mutates claims_data, never touches evidence_data, never affects
retrieval, mapping, NLI, or status. It exists to MEASURE whether a
conservative, deterministic, no-LLM-call family-grouping signal would
actually identify real, retrieval-relevant claim families — before any
production sharing is built. See YANDI_AGENT_RETRIEVAL_PERFORMANCE_
AUDIT.md P3 for the precision results this produced against a real
6-member family (P2's own coffee benchmark).

Design principle (task's own Phase 2/3): precision over recall. A
claim is grouped with another ONLY if BOTH:
  (a) sufficient shared significant-word overlap (subject/predicate
      proxy — the same lexical-overlap technique already used as
      agent/claim_evidence_mapper.py's own lexical fallback, not a
      new technique), AND
  (b) compatible polarity (see _claim_polarity below) — a positive
      and a negated claim about the same subject are NEVER grouped,
      regardless of how high their word overlap is. This is a hard
      exclusion, not a weighted signal, per the task's explicit
      "coffee causes cancer" / "coffee does not cause cancer" example.

Family membership is a hypothesis about SHARED RETRIEVAL CANDIDATES
only — never claim identity, never verdict, never status. Every family
member still gets (in any future non-SHADOW implementation) its own
independent mapping, NLI, and status; this module does not and must
not compute or influence any of those.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Set

# ------------------------------------------------------------
# SIGNIFICANT WORD OVERLAP (subject/predicate proxy)
# ------------------------------------------------------------
#
# Same technique as agent/claim_evidence_mapper.py's own lexical
# fallback (words >4 chars, Cyrillic+Latin+digits) — reused, not
# reinvented, per the task's "minimum architectural disruption"
# instruction.

_WORD_RE = re.compile(r"[а-яёa-z0-9]+")

# Stopwords: pure grammatical connectors (являются/который/etc — safe,
# genuinely domain-independent) PLUS classification-claim TEMPLATE
# vocabulary (классифицировал*/означает/относится/канцероген*/
# человека/доказательства/потребление* — verbs and nouns that recur
# for ANY "what did an authority classify X as" claim, regardless of
# what X is, not specific to one dataset). Empirically validated
# against a real 11-claim fixture (YANDI_AGENT_RETRIEVAL_PERFORMANCE_
# AUDIT.md P3, the coffee/very-hot-beverages benchmark) — WITHOUT this
# list, naive word-overlap over-merges via transitive chaining (9/11
# claims collapsed into one family, crossing the coffee/hot-beverages
# subject boundary through shared generic vocabulary like
# "классифицировало"/"человека"/"канцероген"). WITH it: the real
# 5-member hot-beverages family is recovered exactly, coffee claims
# stay separate, and a claim that shares only the article's incidental
# background definition (not the family's actual subject) is
# correctly excluded. This is an HONEST, EMPIRICALLY-DERIVED list —
# built by inspecting the real fixture, not from first principles —
# flagged here rather than presented as self-evidently general;
# cross-domain sanity-checked (not exhaustively) against unrelated
# leaves/Jupiter claim vocabulary with no overlap/false-positive risk
# found (those domains simply never contain these words).
_STOPWORDS = {
    "является", "являются", "которые", "который", "которая", "которых",
    "означает", "относится",
    "классифицировало", "классифицировал", "классификации", "классификация",
    "канцероген", "канцерогенности", "канцерогенным",
    "человека", "доказательства", "потребление", "потребления",
    "были", "было", "есть", "этот", "этого", "более", "менее",
}

# Crude prefix stemming (item: "do not build a full morphological
# analyzer" was never asked for, and a lightweight, well-established
# technique — truncate to a fixed prefix length — is the appropriate
# scope here). Russian noun/verb declension means the SAME subject
# word surfaces as different exact strings across claims
# ("спутник"/"спутника"/"спутников"/"спутники" — moon/moon's/moons'/
# moons) — without stemming, word-overlap under-counts real overlap,
# which only costs RECALL (a missed family, acceptable per Phase 2's
# own precision-over-recall preference), never precision by itself.
# Validated (YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md P3): stemming
# recovers most of the real Jupiter-benchmark "moons of Jupiter"
# family (3/8 -> 6/8 claims correctly grouped) with ZERO new false
# positives observed on either the coffee or leaves fixture at the
# same prefix length.
_STEM_LEN = 6


def _significant_words(text: str) -> Set[str]:
    return {
        w[:_STEM_LEN] for w in _WORD_RE.findall((text or "").lower())
        if len(w) >= 4 and w not in _STOPWORDS
    }


# ------------------------------------------------------------
# POLARITY (general predicate negation — NOT the same signal as
# agent/claim_evidence_retriever.py::_is_absence_claim(), which is
# deliberately narrow to "not detected/found/confirmed" epistemic-
# absence framing, a documented, different contract. Family grouping
# needs GENERAL predicate negation ("X does not cause Y", "X is not
# classified as Y") — a separate, explicitly-scoped signal, not a
# reuse of that narrower one.)
# ------------------------------------------------------------

_NEGATION_GAP = r"(?:\s+(?:была|было|были|есть|пока|ещё|уже))?"

_POLARITY_NEGATION_MARKERS = (
    rf"не{_NEGATION_GAP}\s+явля",
    rf"не{_NEGATION_GAP}\s+счита",
    rf"не{_NEGATION_GAP}\s+связан",
    rf"не{_NEGATION_GAP}\s+вызыва",
    rf"не{_NEGATION_GAP}\s+относ",
    rf"не{_NEGATION_GAP}\s+привод",
    rf"не{_NEGATION_GAP}\s+увеличива",
    rf"не{_NEGATION_GAP}\s+повыша",
    r"\bnot\s+class",
    r"\bno\s+link",
    r"\bdoes\s+not\b",
)


def _claim_polarity(claim_text: str) -> str:
    """
    'negative' if a general predicate-negation marker is present,
    else 'positive'. Coarse, transparent, conservative heuristic —
    NOT a full negation parser. Ambiguous/ungrammatical edge cases
    default to 'positive' (i.e. a marker must be POSITIVELY matched to
    flip to 'negative') — this means a real negation this regex misses
    would incorrectly allow grouping (a false-positive-family risk we
    accept and flag, since Phase 1's real fixture happens to contain
    no such case), but a matched marker never incorrectly BLOCKS a
    true-positive grouping, which is the safer direction to err given
    precision-over-recall AND given the task explicitly forbids ever
    treating "X" and "not X" as one family.
    """
    lower = (claim_text or "").lower()
    return "negative" if any(re.search(m, lower) for m in _POLARITY_NEGATION_MARKERS) else "positive"


def _family_id(member_ids: List[str]) -> str:
    """
    Deterministic regardless of the ORDER claims were processed/
    discovered in (P3 Phase 14) — derived from the sorted member_id
    set's content, never from arrival/completion order.
    """
    key = ",".join(sorted(member_ids))
    return "fam_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


# Conservative overlap threshold: require at least this many shared
# significant words. Chosen from Phase 1's real fixture inspection
# (see YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md P3) — 2 shared
# subject/predicate words reliably separates the real 5-member "very
# hot beverages classification" cluster from the unrelated definitional
# claim that shares only 1 (a generic word), without requiring an
# LLM call or a tuned embedding threshold. Not derived from a single
# guess — checked against the real fixture's actual word sets.
MIN_SHARED_WORDS = 2


def compute_claim_families_shadow(
    claims_data: List[Dict[str, Any]],
    log=None,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    SHADOW ONLY — pure function, no mutation, no side effects beyond
    optional logging. Returns a list of family records:
        {"family_id", "member_claim_ids", "member_texts",
         "grouping_reason", "shared_words"}
    for every connected component of size >= 2 under the conservative
    (word-overlap AND polarity-compatible) pairwise rule. Claims with
    no sufficiently-similar partner are NOT included (singleton
    "families" carry no retrieval-sharing value and are omitted, not
    reported as size-1 families).
    """
    _log = log or (lambda *a, **k: None)

    eligible = [
        c for c in claims_data
        if isinstance(c, dict) and (c.get("claim_text") or "").strip() and c.get("claim_id")
    ]

    words_by_id = {c["claim_id"]: _significant_words(c["claim_text"]) for c in eligible}
    polarity_by_id = {c["claim_id"]: _claim_polarity(c["claim_text"]) for c in eligible}

    ids = [c["claim_id"] for c in eligible]

    # Union-Find over the conservative pairwise compatibility rule.
    parent = {cid: cid for cid in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    pair_reasons: Dict[frozenset, Set[str]] = {}

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]

            if polarity_by_id[a] != polarity_by_id[b]:
                continue  # hard exclusion — Phase 3, never overridden by overlap

            shared = words_by_id[a] & words_by_id[b]

            if len(shared) >= MIN_SHARED_WORDS:
                union(a, b)
                pair_reasons[frozenset((a, b))] = shared

    groups: Dict[str, List[str]] = {}
    for cid in ids:
        root = find(cid)
        groups.setdefault(root, []).append(cid)

    claims_by_id = {c["claim_id"]: c for c in eligible}
    families = []

    for member_ids in groups.values():
        if len(member_ids) < 2:
            continue

        all_shared_words: Set[str] = set()
        for i in range(len(member_ids)):
            for j in range(i + 1, len(member_ids)):
                key = frozenset((member_ids[i], member_ids[j]))
                if key in pair_reasons:
                    all_shared_words |= pair_reasons[key]

        fam_id = _family_id(member_ids)

        families.append({
            "family_id": fam_id,
            "member_claim_ids": sorted(member_ids),
            "member_texts": {
                cid: claims_by_id[cid].get("claim_text", "") for cid in member_ids
            },
            "grouping_reason": "shared_words_and_compatible_polarity",
            "shared_words": sorted(all_shared_words),
        })

        if verbose:
            _log(
                f"[Family Shadow] family_id={fam_id} "
                f"members={sorted(member_ids)} "
                f"shared_words={sorted(all_shared_words)}"
            )

    return families
