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
from typing import List

_TRAILING_PUNCT_RE = re.compile(r"[\s.!?…]+$")
_WHITESPACE_RE = re.compile(r"\s+")

# P7 (Этап 4C §6): single shared implementation of subject-anchor
# extraction — moved here (was a private helper duplicated nowhere else
# yet, but about to be) from agent/claim_evidence_retriever.py's Subject
# Gate so BOTH that gate (web-source relevance) and the new entity guard
# (agent/claim_semantic_identity_hardening.py, claim<->claim identity)
# use ONE implementation, not two copies of the same regex. Pure by
# construction (claim_text only, no web-source context) — the Subject
# Gate wrapper that also checks title/url/passage stays in
# claim_evidence_retriever.py, calling this.
#
# ВАЖНО: не полноценный NER — узкая эвристика "заглавные слова, кроме
# первого слова предложения" + небольшой alias-слой для явных синонимов
# одного объекта. EU/NATO/Eurozone aliases added for Этап 4C's entity
# guard (§5 of that brief: "ЕС/Евросоюз/Европейский союз" must be
# treated as compatible, "ЕС/НАТО/Еврозона" must not) — purely additive
# to the existing astronomical-body aliases, does not change any
# existing alias group's membership.
_SUBJECT_ANCHOR_ALIASES = {
    "юпитере": ["юпитер", "jupiter"],
    "юпитер": ["юпитер", "jupiter"],
    "европе": ["европа", "europa"],
    "европа": ["европа", "europa"],
    "сатурне": ["сатурн", "saturn"],
    "сатурн": ["сатурн", "saturn"],
    "венере": ["венера", "venus"],
    "венера": ["венера", "venus"],
    "марсе": ["марс", "mars"],
    "марс": ["марс", "mars"],
    # EU family — deliberately NOT merged with "европа"/"europa" above
    # (the Galilean moon of Jupiter): "ес"/"евросоюз"/"европейский" are
    # a SEPARATE alias group from the planet aliases, even though both
    # groups mention "европ*" as a substring in Russian.
    "ес": ["ес", "евросоюз", "европейский союз", "eu", "european union"],
    "евросоюз": ["ес", "евросоюз", "европейский союз", "eu", "european union"],
    "европейский": ["ес", "евросоюз", "европейский союз", "eu", "european union"],
    # Deliberately separate groups — must NOT share aliases with EU or
    # with each other (Этап 4C entity guard test cases C/D/E).
    "нато": ["нато", "nato"],
    "еврозона": ["еврозона", "eurozone"],
}

# P7 (Этап 4C): keys that are already a COMPLETE grammatical case-form
# (Russian locative "на Европе"/"на Юпитере"/...), not a stem meant to
# be prefix-extended further — found via a real collision: "европе"
# (locative, the "европа"/"europa" moon/continent group) is a genuine
# left-anchored PREFIX of the unrelated word "европейский" (adjective
# "European", the EU group), so a plain prefix match wrongly pulled EU
# aliases onto any claim containing "Европейский". These specific keys
# require a RIGHT boundary too (whole-word match) in the substring
# fallback pass below; nominative/stem keys (юпитер, европа, еврозона,
# европейский, ...) keep prefix-only matching, which is what lets them
# correctly catch genitive/instrumental/etc. inflections elsewhere in
# a claim (юпитера, юпитером, ...).
_WHOLE_WORD_ONLY_KEYS = {"юпитере", "европе", "сатурне", "венере", "марсе"}


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


def extract_subject_anchors(claim_text: str) -> List[str]:
    """
    Извлечь явные subject anchors из claim.

    Это НЕ полноценный entity resolver.

    Задача очень узкая:
    не позволять semantic embedding/family-matching подменять
    конкретный объект тематически близким объектом.

    Пример:
        claim  -> "На Юпитере разумная жизнь не обнаружена"
        anchor -> "юпитер"

    Иностранные варианты и явные синонимы для нескольких частых
    объектов (астрономические тела, EU/NATO/Eurozone) добавляются как
    lexical aliases (_SUBJECT_ANCHOR_ALIASES), а не как источник истины.

    Moved here (Этап 4C §6) from agent/claim_evidence_retriever.py's
    Subject Gate — that gate's own web-source-specific wrapper
    (_subject_anchor_matches, checking title/url/passage) still lives
    there and calls this function; agent/claim_semantic_identity_
    hardening.py's entity guard calls the SAME function — one
    implementation of subject-anchor extraction, not two.
    """
    text = (claim_text or "").strip()

    if not text:
        return []

    anchors = []

    # Именованные слова с заглавной буквы внутри claim.
    #
    # Первое слово предложения специально не принимаем автоматически:
    # оно может быть заглавным только из-за начала предложения.
    words = re.findall(
        r"[A-Za-zА-Яа-яЁё0-9-]+",
        text,
    )

    for i, word in enumerate(words):
        if i == 0:
            continue

        if re.match(r"^[А-ЯЁA-Z][A-Za-zА-Яа-яЁё0-9-]+$", word):
            anchors.append(word.lower())

    expanded = []

    for anchor in anchors:
        expanded.append(anchor)

        for alias in _SUBJECT_ANCHOR_ALIASES.get(anchor, []):
            expanded.append(alias)

    # Дополнительно ловим известные формы прямо в claim,
    # даже если regex заглавной буквы их не выделил.
    #
    # P7 (Этап 4C): LEFT word-boundary required (не preceded by a
    # letter/digit) — plain substring containment let a short key like
    # "ес" match inside unrelated words (изВЕСтно, интЕРЕС, ...), a real
    # false-positive found while building the entity guard. No RIGHT
    # boundary requirement — Russian case endings are suffixes on a
    # stem (юпитер -> юпитера/юпитере/юпитером), so a left-anchored
    # prefix match is exactly what already made those forms work; only
    # the missing left boundary was the bug.
    claim_lower = text.lower()

    for form, form_aliases in _SUBJECT_ANCHOR_ALIASES.items():
        boundary_pattern = (
            r"(?<![a-zа-яё0-9])" + re.escape(form) + r"(?![a-zа-яё0-9])"
            if form in _WHOLE_WORD_ONLY_KEYS
            else r"(?<![a-zа-яё0-9])" + re.escape(form)
        )
        if re.search(boundary_pattern, claim_lower):
            expanded.extend(form_aliases)

    # stable dedup
    return list(dict.fromkeys(expanded))
