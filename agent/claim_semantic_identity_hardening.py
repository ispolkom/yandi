"""
agent/claim_semantic_identity_hardening.py — Epistemic Core v1 Phase 9B:
deterministic hardening guard for claim semantic identity.

Root cause of Phase 9's one false positive (causal vs correlational),
diagnosed by re-reading belief_manager.py::_llm_judge_relation()'s exact
prompt (belief_manager.py:291-321): the pair correctly cleared the
embedding prefilter (>=0.70, so NOT an embedding/threshold problem) and
the LLM's raw JSON response parsed cleanly as "equivalent" (NOT a
parsing problem) — the prompt itself only tells the judge that
"different wording or insignificant details" don't break equivalence,
and never flags epistemic-strength language (causal vs correlational,
certainty vs hedged, universal vs existential scope, etc.) as a
SIGNIFICANT detail. So the judge, correctly following an
under-specified prompt, treated "causes" and "is statistically
associated with" as the same thought in different words. Root cause:
JUDGE SEMANTICS (prompt under-specification), not threshold, not
parsing, not the embedding prefilter.

Fix strategy, per the plan's own preference ("если безопасно достичь
без ломки общего judge не удаётся... предложить архитектуру"): a
deterministic, regex-based marker-mismatch GUARD layered on top of the
existing pipeline in agent/claim_semantic_identity_prototype.py — NOT a
change to belief_manager.py's shared, production _llm_judge_relation()
prompt. This is deliberately the lower-risk path: editing the shared
prompt would need its own full regression baseline against every
existing belief_manager.py consumer (per the Phase 9B brief's explicit
requirement for any such change), which is real engineering risk for a
prompt used by live belief deduplication today. A local, additive guard
achieves the same practical safety without touching production code at
all.

This is NOT a new embedding/NLI engine — every check below is a cheap,
deterministic regex marker comparison, run only as a POST-FILTER on an
"equivalent" verdict the real embedding+LLM pipeline already produced.
It can only ever DOWNGRADE equivalent -> different (fail toward safety,
per the plan: "Recall может быть ниже. Лучше UNKNOWN/NOT_EQUIVALENT и
дубликат, чем ложное объединение"). It never upgrades anything to
equivalent, and never touches "contradicts" or "different" verdicts.
"""

from __future__ import annotations

import re
from typing import Optional

# Each dimension pair: (name, pattern_x, pattern_y). If text A matches
# pattern_x but not pattern_y, and text B matches pattern_y but not
# pattern_x (or vice versa), that's an asymmetric marker mismatch — a
# strong signal the two statements differ on a dimension the "equivalent"
# verdict must not paper over.

_CAUSAL = re.compile(r"(?i)\b(вызывает|вызвал[а-я]*|приводит\s+к|привёл[а-я]*\s+к|является\s+причиной|causes?|leads?\s+to)\b")
_CORRELATIONAL = re.compile(r"(?i)\b(связан[а-я]*\s+с|ассоциирован[а-я]*\s+с|коррелирует|статистически\s+связан[а-я]*|associated\s+with|correlated\s+with|linked\s+to)\b")

_NECESSARY = re.compile(r"(?i)\b(необходим[а-я]*|required|necessary)\b")
_SUFFICIENT = re.compile(r"(?i)\b(достаточн[а-я]*|sufficient)\b")

_CERTAINTY = re.compile(r"(?i)\b(точно|определённо|доказан[а-я]*|установлен[а-я]*|certainly|definitely|proven)\b")
_POSSIBILITY = re.compile(r"(?i)\b(возможно|может\s+быть|вероятно|есть\s+вероятность|probably|might|could\b|may\b)\b")

_CURRENT = re.compile(r"(?i)\b(сейчас|в\s+настоящее\s+время|currently|now\b)\b")
_HISTORICAL = re.compile(r"(?i)\b(ранее|прежде|в\s+прошлом|previously|used\s+to|earlier)\b")

_ABSOLUTE = re.compile(r"(?i)\b(всегда|никогда|полностью|always|never)\b")
_QUALIFIED = re.compile(r"(?i)\b(обычно|как\s+правило|часто|редко|usually|generally|often)\b")

_SCOPE_ALL = re.compile(r"(?i)\b(все|всё|каждый|каждая|каждое|all\b|every\b)\b")
_SCOPE_SOME = re.compile(r"(?i)\b(некоторые|несколько|большинство|часть\s+из|some\b|several\b|most\b)\b")

_ATTRIBUTION = re.compile(r"(?i)\b(по\s+словам|согласно\s+заявлению|считает,?\s+что|заявил[а-я]*|according\s+to|\bsaid\b|\bsays\b)\b")

_PREDICTION = re.compile(r"(?i)\b(ожидается|прогнозируется|ожидают|will\s+\w+|expected\s+to)\b")
_OBSERVATION = re.compile(r"(?i)\b(вырос[а-я]*|снизил[а-я]*|зафиксирован[а-я]*|наблюдал[а-я]*|подтвержд[её]н[а-я]*)\b")

_ABSENCE_OF_EVIDENCE = re.compile(r"(?i)\b(не\s+найден[а-я]*|не\s+обнаружен[а-я]*|не\s+зафиксирован[а-я]*|not\s+found|no\s+evidence\s+of)\b")
_EVIDENCE_OF_ABSENCE = re.compile(r"(?i)(доказан[а-я]*\s+отсутствие|доказано,?\s+что\b.*\bне\b|установлено,?\s+что\b.*\bневозмож)")

_NEGATION = re.compile(r"(?i)\b(не\s+явля|неявля|не\s+был[а-я]*|не\s+являет|неэффектив[а-я]*|не\s+обнаруж|не\s+найден|нельзя|невозможно)\b")

_NUMBER = re.compile(r"\d[\d.,]*")

_DIMENSION_PAIRS = [
    ("causal_vs_correlational", _CAUSAL, _CORRELATIONAL),
    ("necessary_vs_sufficient", _NECESSARY, _SUFFICIENT),
    ("possibility_vs_certainty", _POSSIBILITY, _CERTAINTY),
    ("current_vs_historical", _CURRENT, _HISTORICAL),
    ("absolute_vs_qualified", _ABSOLUTE, _QUALIFIED),
    ("scope_all_vs_some", _SCOPE_ALL, _SCOPE_SOME),
    ("prediction_vs_observation", _PREDICTION, _OBSERVATION),
    ("absence_of_evidence_vs_evidence_of_absence", _ABSENCE_OF_EVIDENCE, _EVIDENCE_OF_ABSENCE),
]


def _asymmetric(text: str, pattern_x: "re.Pattern", pattern_y: "re.Pattern") -> Optional[str]:
    """Returns 'x', 'y', or None depending on which side (if only one) this text matches."""
    hit_x = bool(pattern_x.search(text))
    hit_y = bool(pattern_y.search(text))
    if hit_x and not hit_y:
        return "x"
    if hit_y and not hit_x:
        return "y"
    return None


def hardening_guard(claim_a: str, claim_b: str) -> Optional[str]:
    """
    Returns a human-readable reason string if a dangerous marker mismatch
    is found between claim_a and claim_b (meaning: do NOT treat these as
    equivalent, regardless of what the embedding+LLM judge said), or None
    if no guard fires.
    """
    for name, pattern_x, pattern_y in _DIMENSION_PAIRS:
        side_a = _asymmetric(claim_a, pattern_x, pattern_y)
        side_b = _asymmetric(claim_b, pattern_x, pattern_y)
        if side_a and side_b and side_a != side_b:
            return f"{name}_marker_mismatch"

    # Attribution is a one-sided check (bare assertion has no "opposite"
    # marker to require) — if exactly one text has an attribution marker,
    # that's a mismatch in epistemic sourcing.
    attr_a, attr_b = bool(_ATTRIBUTION.search(claim_a)), bool(_ATTRIBUTION.search(claim_b))
    if attr_a != attr_b:
        return "attribution_marker_mismatch"

    # Negation is one-sided the same way.
    neg_a, neg_b = bool(_NEGATION.search(claim_a)), bool(_NEGATION.search(claim_b))
    if neg_a != neg_b:
        return "negation_marker_mismatch"

    # Numbers: if both texts contain numbers and the sets differ, that's
    # a quantity/date mismatch (catches the plan's named "95 vs 96" case
    # generically, not via a special-cased if for that one example).
    nums_a = set(_NUMBER.findall(claim_a))
    nums_b = set(_NUMBER.findall(claim_b))
    if nums_a and nums_b and nums_a != nums_b:
        return "numeric_mismatch"

    return None
