"""
agent/claim_history_note.py — "Живая память" (owner request, 2026-09):
surfaces prior verification history for a claim's semantic family
DIRECTLY IN THE DELIVERED ANSWER, when this request's own fresh
verification found something worth comparing against.

Owner's exact requirement: "она должна помнить... вопросы, ответы, она
может менять ответы на условии, что проверила источники" — she may
change/annotate an answer ONLY on the condition that she actually
checked sources this time, never as a background/idle-time habit.

Structural guarantee for that condition, not just a comment: this
module is called ONLY from agent/orchestrator/response/writeback.py's
run_optimistic_respond() — which agent/orchestrator_v2.py reaches ONLY
via the STANDARD pipeline branch, never the pre_pipeline cache-hit/
short-circuit early-return (that branch calls shadow_complete_run()
directly and returns before writeback.py is ever imported into the
call stack for this request). claims_data reaching this module is
therefore always the output of THIS request's own real, fresh claim
extraction + evidence retrieval + NLI — never a replayed/cached answer.

Nothing here is LLM-generated: the note text is a fixed, deterministic
Russian template (see format_history_note_block()) built ONLY from
fields verification_memory.get_family_historical_claims() actually
returns and this request's own already-computed claims_data — the same
"deterministic code, LLM only where real intelligence is needed"
principle already established by agent/orch_tool_agent.py. This can
never hallucinate a comparison that didn't happen, because it never
asks a model to write the comparison at all.
"""
from __future__ import annotations

from typing import Any, Dict, List

from agent.verification_memory import get_family_historical_claims


def build_claim_history_notes(claims_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    For each DISTINCT semantic family among this request's own claims,
    looks up whether that family already has history from a DIFFERENT
    (earlier) request — and if so, returns one compact note comparing
    this request's fresh conclusion against the most recent prior one.

    One note per family (not per claim) even when several of this
    request's claims share a family — get_family_historical_claims()
    already returns newest-first, so the first non-current-request
    entry found is the most recent genuinely prior occurrence.

    Returns [] whenever there is nothing to say (no family history, or
    every historical entry for a family turns out to BE one of this
    request's own claims — e.g. a family created fresh this request)."""
    notes: List[Dict[str, Any]] = []
    current_claim_ids = {c.get("claim_id") for c in (claims_data or []) if c.get("claim_id")}
    seen_families: set = set()

    for claim in claims_data or []:
        family_id = claim.get("semantic_family_id")
        if not family_id or family_id in seen_families:
            continue
        seen_families.add(family_id)

        history = get_family_historical_claims(family_id)
        prior = next((h for h in history if h.get("claim_id") not in current_claim_ids), None)
        if prior is None:
            continue

        current_status = claim.get("verification_status")
        prior_status = prior.get("verification_status")
        notes.append({
            "family_id": family_id,
            "claim_text": claim.get("claim_text"),
            "prior_query": prior.get("query"),
            "prior_claim_text": prior.get("claim_text"),
            "prior_status": prior_status,
            "current_status": current_status,
            "changed": prior_status != current_status,
        })

    return notes


def format_history_note_block(notes: List[Dict[str, Any]]) -> str:
    """Deterministic text block, never LLM-authored — see module
    docstring for why. Returns "" (append nothing) for an empty list,
    so a caller can always safely do `optimistic.text += format_...(...)`."""
    if not notes:
        return ""

    lines = ["", "---", "🕰️ **Память о прошлых проверках:**"]
    for n in notes:
        prior_query = (n.get("prior_query") or "").strip() or "связанный вопрос"
        if n["changed"]:
            lines.append(
                f"- Ранее на «{prior_query}» я отвечала иначе "
                f"(статус тогда: {n.get('prior_status')}). Сейчас, после новой "
                f"проверки источников: {n.get('current_status')}."
            )
        else:
            lines.append(
                f"- Это подтверждает то, что я уже проверяла раньше "
                f"(связанный вопрос: «{prior_query}»; статус не изменился: "
                f"{n.get('current_status')})."
            )
    return "\n".join(lines)
