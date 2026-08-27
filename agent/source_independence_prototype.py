"""
agent/source_independence_prototype.py — Epistemic Core v1 Phase 5:
OFFLINE research prototype for source-independence clustering.

*** NOT WIRED INTO PRODUCTION. ***
Nothing in agent/orchestrator/*, claims/status.py's support_count tally,
evidence_pool.py's dedup, or source_quality.py imports or calls anything
in this file. This module exists purely to evaluate candidate clustering
approaches against a labeled corpus before any production decision is
made — per the night-shift plan's explicit "offline model first, don't
touch production semantics this phase" instruction.

Problem this evaluates: today, N syndicated copies of one wire story
(different URLs, same underlying content) each count as an independent
`supports_count` toward a claim's verification_status
(claims/status.py:146-156, per the architecture audit's §5 finding).
evidence_pool.py::_dedupe() only catches exact-URL or exact-content-prefix
duplicates — different URLs carrying the same story both survive as
"independent" evidence.

Explicit constraints from the plan (do not violate either):
    - same domain != necessarily same origin (a domain can publish many
      genuinely independent stories)
    - different domains != necessarily independent (wire-service syndication
      crosses domains routinely)

This module implements three clustering variants and evaluates them
against a small labeled corpus (agent/source_independence_corpus.py) for
precision/recall, with special attention to FALSE MERGES (independent
sources wrongly clustered together) — a false merge silently destroys
real independent corroboration, which is the worse failure mode of the
two per the plan's own emphasis ("две независимые публикации нельзя
бездумно превратить в одну").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from agent.source_quality import _hostname
from agent.orch_web_scraper import SharedFetchCache
from agent.claim_identity import canonicalize_claim_text


@dataclass
class SourceCandidate:
    """Minimal fields already present on EvidenceRecord (orch_schemas.py)
    — this prototype deliberately does not invent new evidence fields,
    it only asks "given what we already collect per evidence item, can
    a clustering signal be derived offline"."""
    url: str
    title: str = ""
    content_excerpt: str = ""


# ── Signal 1: canonical URL (reuses orch_web_scraper.SharedFetchCache.canonicalize) ──

def canonical_url(url: str) -> str:
    return SharedFetchCache.canonicalize(url)


# ── Signal 2: domain (reuses source_quality._hostname) ──

def domain(url: str) -> str:
    return _hostname(url)


# ── Signal 3: title similarity ──

def title_similarity(title_a: str, title_b: str) -> float:
    a = canonicalize_claim_text(title_a or "")
    b = canonicalize_claim_text(title_b or "")
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ── Signal 4: content fingerprint via word-shingle Jaccard similarity ──
# Deterministic, no network call, no model dependency — appropriate for
# an offline evaluation phase that needs to be reproducible without
# Ollama being reachable. A real embedding-based signal is a reasonable
# future upgrade (noted in the phase report) but is NOT required to
# answer this phase's question: does ANY cross-domain content signal
# beat "same domain" as a clustering heuristic.

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def _shingles(text: str, n: int = 5) -> set:
    words = _WORD_RE.findall(canonicalize_claim_text(text or ""))
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def content_fingerprint_similarity(text_a: str, text_b: str) -> float:
    sa, sb = _shingles(text_a), _shingles(text_b)
    if not sa or not sb:
        return 0.0
    intersection = len(sa & sb)
    union = len(sa | sb)
    return intersection / union if union else 0.0


# ── Clustering variants ──
#
# Each variant is a function (SourceCandidate, SourceCandidate) -> bool
# ("should these two be treated as the same origin / non-independent").

def cluster_url_exact(a: SourceCandidate, b: SourceCandidate) -> bool:
    """Variant A — baseline, mirrors today's production behavior
    (evidence_pool.py::_dedupe()'s exact-URL/content-prefix key)."""
    return canonical_url(a.url) == canonical_url(b.url)


def cluster_domain_only(a: SourceCandidate, b: SourceCandidate) -> bool:
    """Variant B — the naive assumption the plan explicitly warns
    against ("same domain = обязательно same origin"). Included
    specifically to measure how badly it over-merges."""
    da, db = domain(a.url), domain(b.url)
    return bool(da) and da == db


# Thresholds chosen by evaluation against the labeled corpus (see the
# Phase 5 report) — not arbitrary, but also not claimed optimal; a
# documented starting point for any future real integration decision.
TITLE_SIM_THRESHOLD = 0.55
CONTENT_FINGERPRINT_THRESHOLD = 0.25


def cluster_combined(a: SourceCandidate, b: SourceCandidate) -> bool:
    """Variant C — the actual candidate model. Cross-domain aware (does
    NOT require same domain) and same-domain-tolerant (does NOT assume
    same domain implies same origin) — clusters on content signal alone:
    title similarity OR shingle-overlap content fingerprint, whichever is
    stronger evidence for this pair. Same-domain-ness is deliberately
    NOT a required precondition NOR a free pass here."""
    t_sim = title_similarity(a.title, b.title)
    c_sim = content_fingerprint_similarity(a.content_excerpt, b.content_excerpt)
    return t_sim >= TITLE_SIM_THRESHOLD or c_sim >= CONTENT_FINGERPRINT_THRESHOLD


VARIANTS = {
    "url_exact": cluster_url_exact,
    "domain_only": cluster_domain_only,
    "combined": cluster_combined,
}


def evaluate_variant(variant_fn, labeled_pairs) -> dict:
    """labeled_pairs: list of (SourceCandidate, SourceCandidate, should_cluster: bool, category: str).

    Returns precision/recall/false_merges over the corpus. A "false
    merge" is a predicted-cluster on a pair whose ground truth is
    should_cluster=False — the failure mode the plan calls out as the
    more dangerous one (silently destroying real independent
    corroboration)."""
    tp = fp = fn = tn = 0
    false_merges = []
    missed_merges = []

    for a, b, should_cluster, category in labeled_pairs:
        predicted = variant_fn(a, b)
        if predicted and should_cluster:
            tp += 1
        elif predicted and not should_cluster:
            fp += 1
            false_merges.append(category)
        elif not predicted and should_cluster:
            fn += 1
            missed_merges.append(category)
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0

    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall,
        "false_merge_categories": false_merges,
        "missed_merge_categories": missed_merges,
    }
