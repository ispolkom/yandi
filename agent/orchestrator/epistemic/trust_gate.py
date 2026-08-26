"""
Trust gate — extracted from agent/orchestrator_v2.py: the top-level trust
helpers (`TRUST_STATES`, `_TRUST_ORDER`, `_calculate_delta_factors`,
`_apply_trust_cap`) plus the [8] "Эпистемическая корректировка trust (v3)"
block (epistemic-classification-based label computation, coverage/grounding
trust gates, belief-confidence gate).

Structural extraction only: no epistemic semantics, thresholds, or ordering
changed.
"""

from typing import Dict

from agent.orch_registry_search import CONF_THRESHOLD

TRUST_STATES = {
    "GENERATED": "GENERATED",
    "VERIFYING": "VERIFYING",
    "VERIFIED": "VERIFIED",
    "REJECTED": "REJECTED",
    "PARTIAL": "PARTIAL",
    "REPUTATION_UPDATED": "REPUTATION_UPDATED",
}

_TRUST_ORDER = {
    "STRONGLY_SUPPORTED": 5,
    "SUPPORTED": 4,
    "VERIFIED": 4,
    "PARTIALLY_SUPPORTED": 3,
    "PARTIAL": 3,
    "EMPIRICALLY_SUPPORTED": 4,
    "EMPIRICALLY_UNTESTABLE": 2,
    "UNVERIFIED": 1,
    "HYPOTHESIS": 1,
    "RELIGIOUS_CLAIM": 2,
    "METAPHYSICAL_UNTESTABLE": 2,
    "VALUE_FRAMEWORK": 2,
    "BOUNDARY_QUESTION": 2,
    "ONTOLOGICAL_INQUIRY": 2,
    "NORMATIVE_POSITION": 2,
    "CONTESTED": 2,
}


def _calculate_delta_factors(
    verification_verdict: str,
    confidence: float,
    has_sources: bool,
    consensus_agreement: int = 0,
    total_nodes: int = 0,
) -> Dict[str, float]:
    verification_weight = {
        "VERIFIED": 0.5,
        "PARTIALLY_VERIFIED": 0.2,
        "CONFLICT": -0.2,
        "REJECTED": -0.5,
        "TIMEOUT": -0.1,
    }.get(verification_verdict, 0.0)

    confidence_factor = min(1.0, max(0.0, confidence))
    source_quality = 1.0 if has_sources else 0.7

    if total_nodes > 0:
        consensus_ratio = consensus_agreement / total_nodes
        consensus_factor = 0.5 + 0.5 * consensus_ratio
    else:
        consensus_factor = 0.7

    total_delta = verification_weight * confidence_factor * source_quality * consensus_factor
    total_delta = round(max(-0.5, min(0.5, total_delta)), 3)

    return {
        "total": total_delta,
        "verification_weight": round(verification_weight, 3),
        "confidence_factor": round(confidence_factor, 3),
        "source_quality": round(source_quality, 3),
        "consensus_factor": round(consensus_factor, 3),
    }


def _apply_trust_cap(current_label: str, cap_label: str) -> str:
    current_order = _TRUST_ORDER.get(current_label, 0)
    cap_order = _TRUST_ORDER.get(cap_label, 0)

    if current_order > cap_order:
        return cap_label
    return current_label


def apply_epistemic_trust_adjustment(
    is_subjective_answer,
    epistemic_trust_label,
    epistemic_result,
    entity,
    final_claim_coverage_score,
    support_grounding_score,
    belief_manager,
    trace,
    web_used,
    claims_data,
    search_result,
    epistemic_grounding_score,
    clarification_answered,
    is_media_query,
    supporting_ids,
    coverage_report_data,
    intent_result,
):
    """
    Computes the final trust label from the epistemic classification, the
    trust cap, testability/domain adjustments, final-claim-coverage and
    evidence-support grounding gates, and belief-manager confidence.

    Mutates `trace` in place (trust, trust_reason, add_learning_rule calls,
    _coverage) and returns the computed label.
    """
    label = "UNVERIFIED"
    trust_reasons = []

    if not is_subjective_answer and epistemic_trust_label not in ["PARTIALLY_SUPPORTED", "UNVERIFIED"]:
        label = epistemic_trust_label
        trust_reasons.append(f"эпистемическая классификация: {epistemic_result.domain} ({epistemic_result.testability})")

    if not is_subjective_answer:
        cap_label = epistemic_result.max_trust_cap
        if label != cap_label:
            old_label = label
            label = _apply_trust_cap(label, cap_label)
            if old_label != label:
                trust_reasons.append(f"trust понижен с {old_label} до {label} (cap={cap_label})")

    if not is_subjective_answer and epistemic_result.testability in ["interpretive", "non_falsifiable"]:
        if label in ["VERIFIED", "STRONGLY_SUPPORTED", "EMPIRICALLY_SUPPORTED"]:
            label = "PARTIALLY_SUPPORTED"
            trust_reasons.append("интерпретативный вопрос не может быть STRONGLY_SUPPORTED")
        trust_reasons.append(f"ответ дан в рамках {epistemic_result.testability} перспективы")

    if not is_subjective_answer and epistemic_result.domain in ["axiological", "normative", "philosophical"]:
        if label in ["VERIFIED", "STRONGLY_SUPPORTED", "EMPIRICALLY_SUPPORTED"]:
            label = "VALUE_FRAMEWORK"
            trust_reasons.append("ценностный вопрос не имеет единственного правильного ответа")

    if not is_subjective_answer and epistemic_result.domain == "media_interpretation":
        if not entity:
            trust_reasons.append("фильм не идентифицирован")
            if label in ["STRONGLY_SUPPORTED", "SUPPORTED"]:
                label = "PARTIALLY_SUPPORTED"

    if not is_subjective_answer and epistemic_result.is_science_as_model:
        if label in ["STRONGLY_SUPPORTED", "VERIFIED"]:
            label = "SUPPORTED"
            trust_reasons.append("научное утверждение — это модель, а не истина")

    # ----------------------------------------------------
    # FINAL CLAIM COVERAGE TRUST GATE
    # ----------------------------------------------------
    #
    # Coverage может только ОГРАНИЧИТЬ Trust сверху.
    # Высокий coverage никогда сам по себе Trust не повышает.
    #
    # < 0.50:
    #   большая часть factual answer вообще не прошла lifecycle.
    #
    # < 0.80:
    #   существенная часть ответа всё ещё вне проверки.
    if final_claim_coverage_score < 0.50:
        trust_reasons.append(
            "низкое покрытие фактических утверждений "
            f"финального ответа "
            f"({final_claim_coverage_score:.2f})"
        )

        if label not in [
            "RELIGIOUS_CLAIM",
            "METAPHYSICAL_UNTESTABLE",
            "VALUE_FRAMEWORK",
            "BOUNDARY_QUESTION",
            "ONTOLOGICAL_INQUIRY",
        ]:
            label = "UNVERIFIED"

    elif final_claim_coverage_score < 0.80:
        trust_reasons.append(
            "частичное покрытие фактических утверждений "
            f"финального ответа "
            f"({final_claim_coverage_score:.2f})"
        )

        if label in [
            "VERIFIED",
            "STRONGLY_SUPPORTED",
            "EMPIRICALLY_SUPPORTED",
            "SUPPORTED",
        ]:
            label = "PARTIALLY_SUPPORTED"

    # ----------------------------------------------------
    # EVIDENCE SUPPORT TRUST GATE
    # ----------------------------------------------------
    #
    # semantic_grounding здесь намеренно НЕ используется:
    # тематическая привязка evidence != поддержка claim.
    #
    # epistemic_grounding также НЕ является положительным
    # сигналом Trust: evidence может противоречить claim.
    #
    # Trust ограничивается только реальным DIRECT +
    # ELIGIBLE support coverage.
    if support_grounding_score < 0.3:
        trust_reasons.append(
            "слабое покрытие claims прямыми "
            "поддерживающими evidence"
        )

        if label not in [
            "RELIGIOUS_CLAIM",
            "METAPHYSICAL_UNTESTABLE",
            "VALUE_FRAMEWORK",
            "BOUNDARY_QUESTION",
            "ONTOLOGICAL_INQUIRY",
        ]:
            label = "UNVERIFIED"

    elif support_grounding_score < 0.6:
        trust_reasons.append(
            "частичное покрытие claims прямыми "
            "поддерживающими evidence"
        )

        # Grounding Gate только ограничивает Trust сверху.
        # Он никогда не повышает label.
        if label in [
            "VERIFIED",
            "STRONGLY_SUPPORTED",
            "EMPIRICALLY_SUPPORTED",
        ]:
            label = "PARTIALLY_SUPPORTED"

    if belief_manager:
        try:
            beliefs = belief_manager.get_all_active()
            if beliefs:
                avg_belief_conf = sum(b.confidence for b in beliefs) / len(beliefs)
                if avg_belief_conf < 0.5 and label in ["STRONGLY_SUPPORTED", "SUPPORTED"]:
                    label = "PARTIALLY_SUPPORTED"
                    trust_reasons.append(f"средняя уверенность убеждений {avg_belief_conf:.2f}")
        except Exception:
            pass

    trace.trust = label
    trace.trust_reason = "; ".join(trust_reasons[:4])

    if final_claim_coverage_score < 0.80:
        trace.add_learning_rule(
            "coverage",
            (
                f"final factual claim coverage="
                f"{final_claim_coverage_score:.2f}"
            ),
            final_claim_coverage_score,
        )

    if support_grounding_score >= 0.6:
        trace.add_learning_rule(
            "trust",
            (
                f"direct support coverage="
                f"{support_grounding_score:.2f} → {label}"
            ),
            support_grounding_score,
        )
    if web_used and len(claims_data) >= 2:
        trace.add_learning_rule("retrieval", f"для {intent_result.intent} запросов полезен web-поиск", 0.6)
    if search_result.confidence < CONF_THRESHOLD:
        trace.add_learning_rule("planner", f"registry confidence < {CONF_THRESHOLD} → использовать web", 0.7)
    if epistemic_grounding_score >= 0.6:
        trace.add_learning_rule(
            "evidence",
            (
                f"direct evidence coverage="
                f"{epistemic_grounding_score:.2f}"
            ),
            epistemic_grounding_score,
        )

    if not is_subjective_answer:
        if epistemic_result.trust_score >= 0.7:
            trace.add_learning_rule("epistemic", f"high epistemic trust ({epistemic_result.trust_score:.2f}) → {epistemic_result.domain}", epistemic_result.trust_score)
        if epistemic_result.need_clarification and clarification_answered:
            trace.add_learning_rule("epistemic", f"clarification helped for {epistemic_result.domain}", 0.6)
        if epistemic_result.needs_frame_split:
            trace.add_learning_rule("epistemic", f"needs_frame_split=True for {epistemic_result.domain}", 0.7)
        if is_media_query and entity:
            trace.add_learning_rule("media", f"entity resolution succeeded for {entity.get('title', 'unknown')}", 0.8)

    if belief_manager and claims_data:
        trace.add_learning_rule("belief", f"beliefs updated: {len(claims_data)} new claims", 0.6)
    if supporting_ids:
        trace.add_learning_rule("linker", f"answer linked to {len(supporting_ids)} claims", 0.7)

    if not is_subjective_answer and epistemic_result.is_science_as_model:
        trace.add_learning_rule("epistemic_skepticism", f"science as model for {epistemic_result.domain}", 0.8)

    if coverage_report_data:
        trace._coverage = coverage_report_data

    return label
