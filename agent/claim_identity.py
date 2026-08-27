"""
agent/claim_identity.py — Epistemic Core v1 Phase 2: deterministic claim
content identity.

Adds `content_hash` alongside the existing random `claim_id`
(f"cl_{uuid.uuid4().hex[:8]}"). The two serve different purposes and are
NOT interchangeable:

    claim_id        occurrence identity — unique per extraction, even for
                     the exact same text extracted twice.
    content_hash     normalized textual identity — deterministic, same
                     normalized text always hashes the same, across
                     requests/sessions.
    semantic identity  NOT this module. Two paraphrases of the same fact
                     ("Jupiter has 95 moons" / "Jupiter has ninety-five
                     moons") are NOT required or expected to produce the
                     same content_hash. That is a separate, harder
                     problem (Phase 9/10 of the implementation plan),
                     reusing belief_manager.py's embedding+LLM-judge
                     pattern — not this module, and not reimplemented
                     here.

Canonicalization policy (deliberately conservative — exact/near-exact
matching only, not fuzzy matching):

    1. Unicode NFC normalization — so visually/semantically identical
       text that happens to use a different Unicode composition (e.g.
       precomposed "é" vs "e" + combining acute) hashes the same.
    2. casefold() — locale-agnostic case folding (stronger than
       .lower(), correct for non-ASCII scripts too).
    3. Whitespace collapse — any run of whitespace (including newlines)
       collapses to a single space; leading/trailing stripped.
    4. Trailing sentence-ending punctuation (. ! ? … and combinations of
       them, e.g. "?!") is stripped, since claim extraction formatting
       differences (trailing period present or not) are not epistemic
       differences. INTERNAL punctuation is preserved — a comma changing
       the meaning of a sentence is not something this function should
       paper over.
    5. Empty text after normalization -> no content hash (returns None).
       A degenerate/empty claim must never be treated as "the same claim"
       as another degenerate/empty claim purely because both normalize to
       nothing — that would be a fabricated identity, not a real one.

Multilingual text is NOT translated or otherwise unified — a Russian and
an English statement of the same fact hash differently, deliberately (see
"semantic identity" above).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_TRAILING_PUNCT_RE = re.compile(r"[\s.!?…]+$")
_WHITESPACE_RE = re.compile(r"\s+")


def canonicalize_claim_text(claim_text: str) -> str:
    """Pure normalization step, exposed separately so tests (and any
    future caller that wants the canonical string itself, not just its
    hash) don't have to re-derive it from compute_claim_content_hash()."""
    if not claim_text:
        return ""
    text = unicodedata.normalize("NFC", claim_text)
    text = text.casefold()
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = _TRAILING_PUNCT_RE.sub("", text).strip()
    return text


def compute_claim_content_hash(claim_text: str) -> "str | None":
    """Deterministic sha256 hex digest of the canonicalized claim text,
    or None if the claim text is empty/whitespace-only after
    normalization (no fabricated identity for degenerate input)."""
    canonical = canonicalize_claim_text(claim_text)
    if not canonical:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
