"""
Trust gate — extracted from agent/orchestrator_v2.py: the top-level trust
helpers (`TRUST_STATES`, `_TRUST_ORDER`, `_calculate_delta_factors`,
`_apply_trust_cap`) plus the [8] "Эпистемическая корректировка trust (v3)"
block (epistemic-classification-based label computation, coverage/grounding
trust gates, belief-confidence gate).

Structural extraction only: no epistemic semantics, thresholds, or ordering
changed.

Rust-перенос (2026-09-24): rustlib/yandi_rs/src/trust_gate.rs — _apply_trust_cap, _calculate_delta_factors и
«решение» apply_epistemic_trust_adjustment (метка + причины); запись в trace и learning rules остаются здесь.
Таблица _TRUST_ORDER здесь — единственный источник (Rust-таблица сгенерирована из неё: rustlib/gen_trust_data.py).
Доказан на совпадение тестом agent/trust_gate_rust_parity_test.py. По умолчанию ВЫКЛЮЧЕН; включается переменной
окружения YANDI_TRUST_GATE_ENGINE=rust ПОСЛЕ сборки rustlib/yandi_rs (`maturin develop`, см. rustlib/README.md).
"""

import logging
import os
from typing import Dict

from agent.orch_registry_search import CONF_THRESHOLD

log = logging.getLogger("yandi.trust_gate")

_rust_tg = None          # None = ещё не пробовали; False = не запрошено/не собрано; модуль = подключён


def _get_rust_tg():
    global _rust_tg
    if _rust_tg is None:
        if os.environ.get("YANDI_TRUST_GATE_ENGINE") == "rust":
            try:
                import yandi_rs.trust_gate as _rs
                _rust_tg = _rs
                log.warning("YANDI_TRUST_GATE_ENGINE=rust: используется Rust-реализация trust_gate (rustlib/yandi_rs)")
            except ImportError as e:
                log.warning("YANDI_TRUST_GATE_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python", e)
                _rust_tg = False
        else:
            _rust_tg = False
    return _rust_tg or None


def _is_num(x) -> bool:
    """int/float (bool — тоже int) в пределах, где приведение к float безопасно и точно."""
    return isinstance(x, (int, float)) and not (isinstance(x, int) and abs(x) > 2 ** 53)

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
    # Bugfix (Epistemic Core v1 Phase 13 audit,
    # YANDI_EPISTEMIC_TRUST_CONSOLIDATION_REPORT.md): this table was
    # missing "WEAKLY_SUPPORTED" — a real, actively-assigned label
    # (agent/orchestrator/claims/status.py, this module's own
    # apply_epistemic_trust_adjustment, response/writeback.py's
    # reflection downgrade). A missing key silently defaulted to
    # _TRUST_ORDER.get(label, 0) == 0 in _apply_trust_cap — BELOW
    # UNVERIFIED's rank of 1 — which inverted the intended ordering and
    # broke _apply_trust_cap's "caps only ever lower, never raise"
    # invariant in both directions: a WEAKLY_SUPPORTED current value
    # could never be capped down by anything (0 can't exceed any real
    # rank), and using WEAKLY_SUPPORTED as a cap against an UNVERIFIED
    # current value would incorrectly UPGRADE it (1 > 0 was read as
    # "current exceeds the cap"). Rank 2 matches where this label
    # already sits, uncontested, in this exact module's own two local
    # `trust_rank` copies (apply_epistemic_trust_adjustment's disputed-
    # claims and verified==0 branches: UNVERIFIED=0 < WEAKLY_SUPPORTED=1
    # < PARTIALLY_SUPPORTED=2 < ...) — this is not a new value invented
    # for this fix, it is the value the codebase already agreed on
    # elsewhere, now made consistent here too. Found via a live Phase 13
    # canonical-trust run, not synthetic testing alone (see the
    # consolidation report's live-results section).
    "WEAKLY_SUPPORTED": 2,
}


def _calculate_delta_factors(
    verification_verdict: str,
    confidence: float,
    has_sources: bool,
    consensus_agreement: int = 0,
    total_nodes: int = 0,
) -> Dict[str, float]:
    rs = _get_rust_tg()
    if (rs is not None and isinstance(verification_verdict, str) and _is_num(confidence)
            and _is_num(consensus_agreement) and _is_num(total_nodes)):
        return dict(rs.calculate_delta_factors(
            verification_verdict, float(confidence), bool(has_sources), float(consensus_agreement), float(total_nodes)))

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
    rs = _get_rust_tg()
    if rs is not None and isinstance(current_label, str) and isinstance(cap_label, str):
        return rs.apply_trust_cap(current_label, cap_label)
    current_order = _TRUST_ORDER.get(current_label, 0)
    cap_order = _TRUST_ORDER.get(cap_label, 0)

    if current_order > cap_order:
        return cap_label
    return current_label


def _compute_label_python(
    is_subjective_answer,
    epistemic_trust_label,
    epistemic_result,
    entity,
    final_claim_coverage_score,
    support_grounding_score,
    belief_manager,
):
    """Исходная (Python) логика метки и причин из apply_epistemic_trust_adjustment — перенесена сюда ДОСЛОВНО."""
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

    return label, trust_reasons


def _belief_average(belief_manager):
    """Средняя уверенность активных убеждений (как в исходном try/except Exception) или None."""
    if not belief_manager:
        return None
    try:
        beliefs = belief_manager.get_all_active()
        if beliefs:
            return sum(b.confidence for b in beliefs) / len(beliefs)
    except Exception:
        pass
    return None


def _compute_label_rust(
    rs,
    is_subjective_answer,
    epistemic_trust_label,
    epistemic_result,
    entity,
    final_claim_coverage_score,
    support_grounding_score,
    belief_manager,
):
    """(label, reasons) через Rust или None, если типы входов не те, что знает Rust (тогда — Python-путь)."""
    if not (isinstance(epistemic_trust_label, str) and _is_num(final_claim_coverage_score) and _is_num(support_grounding_score)):
        return None
    # атрибуты читаем ДО обращения к belief_manager — тот же порядок возможных ошибок, что и в оригинале
    domain, testability = epistemic_result.domain, epistemic_result.testability
    cap = epistemic_result.max_trust_cap
    science = epistemic_result.is_science_as_model
    if not (isinstance(domain, str) and isinstance(testability, str) and isinstance(cap, str)):
        return None
    avg = _belief_average(belief_manager)
    if avg is not None and not _is_num(avg):
        return None
    label, reasons = rs.compute_trust_label(
        bool(is_subjective_answer), epistemic_trust_label, domain, testability, cap, bool(science), bool(entity),
        float(final_claim_coverage_score), float(support_grounding_score), None if avg is None else float(avg),
    )
    return label, list(reasons)


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
    rs = _get_rust_tg()
    computed = None
    if rs is not None:
        computed = _compute_label_rust(
            rs, is_subjective_answer, epistemic_trust_label, epistemic_result, entity,
            final_claim_coverage_score, support_grounding_score, belief_manager)
    if computed is None:
        computed = _compute_label_python(
            is_subjective_answer, epistemic_trust_label, epistemic_result, entity,
            final_claim_coverage_score, support_grounding_score, belief_manager)
    label, trust_reasons = computed

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
