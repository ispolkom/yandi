"""
assistant/orch_tracer.py — Decision Tracer для Orchestrator v2.

Сохраняет полный трейс по новой модели данных:
- QueryTrace
- ExecutionStep
- EvidenceRecord
- ClaimRecord
- DecisionRecord
- TrustReport
- CoverageReport
- OutcomeRecord

Цель: по одному трейсу другой оркестратор должен суметь воспроизвести
весь маршрут принятия решения без доступа к исходному коду.

v2:
- Очистка claims от мусора (таблицы, заголовки, маркдаун)
- Добавлены эпистемические поля
- Фильтрация служебных claim-ов
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

from agent.orch_schemas import (
    QueryTrace,
    ExecutionStep,
    EvidenceRecord,
    ClaimRecord,
    DecisionRecord,
    TrustReport,
    CoverageReport,
    OutcomeRecord,
)

BASE = Path(__file__).parent.parent
TRACES_DIR = BASE / "registry" / "dataset" / "orch_traces"
TRACES_DIR.mkdir(parents=True, exist_ok=True)


# ===== ФИЛЬТРЫ ДЛЯ ОЧИСТКИ CLAIMS =====

# Паттерны мусора в claims
_CLAIM_MARKDOWN_PATTERNS = [
    r"^#{1,6}\s+.*$",                    # Заголовки
    r"^\|.*\|$",                         # Строки таблиц
    r"^---$",                            # Разделители
    r"^===.*===$",                       # Разделители
    r"^\[.*\]\s+.*$",                    # Ссылки/сноски
    r"^\*\s+.*$",                        # Маркеры списков
    r"^-\s+.*$",                         # Маркеры списков
    r"^\+.*$",                           # Маркеры списков
    r"^>\s+.*$",                         # Цитаты
    r"^```.*$",                          # Блоки кода
    r"^`.*`$",                           # Инлайн код
]

_CLAIM_META_PATTERNS = [
    r"(?i)нет\s+информации",
    r"(?i)отсутствует\s+информация",
    r"(?i)не\s+найдено",
    r"(?i)не\s+удалось\s+найти",
    r"(?i)информация\s+не\s+найдена",
    r"(?i)данные\s+отсутствуют",
    r"(?i)не\s+достаточно\s+данных",
    r"(?i)недостаточно\s+информации",
    r"(?i)релевантность",
    r"(?i)матрица\s+релевантности",
    r"(?i)source\s+quality",
    r"(?i)score\s*[:=]\s*\d+",
    r"(?i)доверие\s*[:=]\s*",
    r"(?i)локальная\s+база",
    r"(?i)источник\s+#?\d+",
    r"(?i)результат\s+поиска",
    r"(?i)сниппет",
    r"(?i)url\s*[:=]",
    r"(?i)http[s]?://",
    r"(?i)результат\s+анализа",
    r"(?i)сырые\s+данные",
    r"(?i)ноль\s+релевантных",
    r"(?i)не\s+содержит\s+релевантных",
    r"(?i)отсутствуют\s+релевантные",
    r"(?i)релевантные\s+факты",
    r"(?i)доступные\s+факты",
    r"(?i)данные\s+показывают",
    r"(?i)анализ\s+показал",
    r"(?i)экстракция\s+фактов",
    r"(?i)извлечённые\s+факты",
    r"(?i)список\s+фактов",
]

_CLAIM_SERVICE_PATTERNS = [
    r"(?i)факт\s+#?\d+",
    r"(?i)утверждение\s+#?\d+",
    r"(?i)claim\s+#?\d+",
    r"(?i)evidence\s+#?\d+",
    r"(?i)источник\s+#?\d+",
    r"(?i)согласно\s+источнику",
    r"(?i)в\s+источнике",
    r"(?i)как\s+указано\s+в",
]


def is_claim_clean(claim_text: str) -> tuple[bool, str]:
    """
    Проверить, является ли claim чистым (не мусорным).
    Возвращает (is_clean, reason).
    """
    text = claim_text.strip()
    
    if len(text) < 15:
        return False, "too_short"
    if len(text) > 500:
        return False, "too_long"
    
    for pattern in _CLAIM_MARKDOWN_PATTERNS:
        if re.match(pattern, text, re.MULTILINE):
            return False, "markdown"
    
    for pattern in _CLAIM_META_PATTERNS:
        if re.search(pattern, text):
            return False, "meta_text"
    
    for pattern in _CLAIM_SERVICE_PATTERNS:
        if re.search(pattern, text):
            return False, "service_text"
    
    digit_ratio = sum(c.isdigit() for c in text) / max(1, len(text))
    if digit_ratio > 0.3:
        return False, "too_many_digits"
    
    special_chars = sum(1 for c in text if c in "|-_=+*/\\<>")
    if special_chars > len(text) * 0.2:
        return False, "too_many_special_chars"
    
    return True, "clean"


def filter_claims(claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered = []
    for claim in claims:
        claim_text = claim.get("claim_text", "")
        is_clean, reason = is_claim_clean(claim_text)
        if is_clean:
            filtered.append(claim)
        else:
            claim["_filtered_reason"] = reason
            claim["_filtered"] = True
    return filtered


def clean_claim_text(claim_text: str) -> str:
    claim_text = re.sub(r"#{1,6}\s+", "", claim_text)
    claim_text = re.sub(r"\*\*([^*]+)\*\*", r"\1", claim_text)
    claim_text = re.sub(r"\*([^*]+)\*", r"\1", claim_text)
    claim_text = re.sub(r"`([^`]+)`", r"\1", claim_text)
    claim_text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", claim_text)
    claim_text = re.sub(r"\s+", " ", claim_text)
    return claim_text.strip()


# ===== ЛОКАЛЬНЫЕ СХЕМЫ ДЛЯ НАКОПЛЕНИЯ ДАННЫХ =====

@dataclass
class ExecutionStepLocal:
    step: str
    status: str
    duration_ms: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReasoningStep:
    step: str
    observed: Dict[str, Any]
    decision: str
    alternatives: List[Dict] = field(default_factory=list)
    expected_gain: float = 0.0


@dataclass
class LearningRule:
    type: str
    rule: str
    confidence: float = 0.5


@dataclass
class ConfidencePoint:
    stage: str
    confidence: float
    reason: str = ""


# ===== TRACE-КОНТЕЙНЕР =====

@dataclass
class Trace:
    trace_id: str
    timestamp: float
    query: str
    goal: str = "получить достоверный ответ на вопрос пользователя"
    final_answer: str = ""

    execution: List[ExecutionStepLocal] = field(default_factory=list)
    reasoning: List[ReasoningStep] = field(default_factory=list)

    query_trace: Optional[QueryTrace] = None
    evidence: List[EvidenceRecord] = field(default_factory=list)
    claims: List[ClaimRecord] = field(default_factory=list)
    decisions: List[DecisionRecord] = field(default_factory=list)
    outcome: Optional[OutcomeRecord] = None

    learning: List[LearningRule] = field(default_factory=list)
    confidence_evolution: List[ConfidencePoint] = field(default_factory=list)

    trust: str = "UNVERIFIED"
    trust_reason: str = ""
    cost: Dict[str, float] = field(default_factory=dict)

    epistemic: Dict[str, Any] = field(default_factory=dict)
    filtered_claims: List[Dict[str, Any]] = field(default_factory=list)
    rejected_claims: List[Dict[str, Any]] = field(default_factory=list)

    _observations: Dict[str, Any] = field(default_factory=dict)

    def add_execution(self, step: str, status: str, duration_ms: float = 0.0, details: Dict = None) -> None:
        self.execution.append(ExecutionStepLocal(
            step=step,
            status=status,
            duration_ms=duration_ms,
            details=details or {}
        ))

    def add_reasoning(self, step: str, observed: Dict, decision: str,
                      alternatives: List[Dict] = None, expected_gain: float = 0.0) -> None:
        self.reasoning.append(ReasoningStep(
            step=step,
            observed=observed,
            decision=decision,
            alternatives=alternatives or [],
            expected_gain=expected_gain
        ))

    def add_observation(self, key: str, value: Any) -> None:
        self._observations[key] = value

    def add_source(self, url: str, domain: str = "", domain_score: float = 0.5,
                   freshness: float = 0.5, authority: float = 0.5,
                   used: bool = False, rejected_reason: str = "") -> None:
        ev = EvidenceRecord(
            evidence_id=f"ev_{uuid.uuid4().hex[:8]}",
            source_type="web",
            source_uri=url,
            source_title=domain,
            relevance_to_query=domain_score,
            is_meta_pipeline_output=False,
            is_subject_matter_evidence=True,
            rejection_reason=rejected_reason if not used else None,
        )
        self.evidence.append(ev)

    def add_evidence(self, evidence_record: EvidenceRecord) -> None:
        self.evidence.append(evidence_record)

    def add_claim(self, claim_record: ClaimRecord) -> None:
        is_clean, reason = is_claim_clean(claim_record.claim_text)
        if is_clean:
            claim_record.claim_text = clean_claim_text(claim_record.claim_text)
            self.claims.append(claim_record)
        else:
            rejected_data = {
                "claim_id": claim_record.claim_id,
                "claim_text": claim_record.claim_text[:200],
                "claim_type": claim_record.claim_type,
                "rejection_reason": reason,
            }
            self.rejected_claims.append(rejected_data)

    def add_claims(self, claims: List[ClaimRecord]) -> None:
        for claim in claims:
            self.add_claim(claim)

    def add_claim_raw(self, claim_data: Dict[str, Any]) -> None:
        claim_text = claim_data.get("claim_text", "")
        is_clean, reason = is_claim_clean(claim_text)
        if is_clean:
            clean_text = clean_claim_text(claim_text)
            evidence_relations = [
                {
                    "evidence_id": rel.get("evidence_id", ""),
                    "relation": rel.get("relation", "unrelated"),
                    "relation_method": rel.get("method", "unknown"),
                    "source_claim": (rel.get("source_claim") or "")[:300],
                    # P5 (verification memory): these were being silently
                    # dropped at persist time even though mapping.py's
                    # run_claim_evidence_batch already computes them per
                    # relation — needed both for AUDIT (why did this
                    # relation count/not count toward status?) and so a
                    # future reload can tell a memory-derived relation
                    # apart from a fresh one (from_memory).
                    "source_class": rel.get("source_class", "unknown"),
                    "evidence_eligible": rel.get("evidence_eligible", False),
                    "evidence_role": rel.get("evidence_role", "context"),
                    "directness": rel.get("directness", 0.0),
                    "retrieval_origin": rel.get("retrieval_origin", ""),
                    "from_memory": bool(rel.get("from_memory", False)),
                }
                for rel in (claim_data.get("evidence_relations") or [])
                if rel.get("evidence_id")
            ]
            claim_record = ClaimRecord(
                claim_id=claim_data.get("claim_id", f"cl_{uuid.uuid4().hex[:8]}"),
                claim_text=clean_text[:300],
                derived_from_evidence_ids=claim_data.get("derived_from_evidence_ids", []),
                claim_type=claim_data.get("claim_type", "factual"),
                claim_confidence=claim_data.get("claim_confidence", 0.7),
                verification_status=claim_data.get("verification_status", "unverified"),
                evidence_relations=evidence_relations,
                content_hash=claim_data.get("content_hash"),
                evidence_search_attempted=claim_data.get("evidence_search_attempted"),
                evidence_search_error=claim_data.get("evidence_search_error"),
                semantic_family_id=claim_data.get("semantic_family_id"),
            )
            self.claims.append(claim_record)
        else:
            rejected_data = {
                "claim_id": claim_data.get("claim_id", "unknown"),
                "claim_text": claim_text[:200],
                "claim_type": claim_data.get("claim_type", "unknown"),
                "rejection_reason": reason,
            }
            self.rejected_claims.append(rejected_data)

    def set_epistemic(self, epistemic: Dict[str, Any]) -> None:
        self.epistemic = epistemic

    def add_decision(self, decision_record: DecisionRecord) -> None:
        self.decisions.append(decision_record)

    def add_learning_rule(self, rule_type: str, rule: str, confidence: float = 0.5) -> None:
        self.learning.append(LearningRule(type=rule_type, rule=rule, confidence=confidence))

    def add_confidence(self, stage: str, confidence: float, reason: str = "") -> None:
        self.confidence_evolution.append(ConfidencePoint(stage=stage, confidence=confidence, reason=reason))

    def set_query_trace(self, query_trace: QueryTrace) -> None:
        self.query_trace = query_trace

    def set_outcome(self, outcome: OutcomeRecord) -> None:
        self.outcome = outcome

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
            "query": self.query[:500],
            "goal": self.goal,
            "final_answer": self.final_answer[:2000] if self.final_answer else "",

            "execution": [
                {"step": e.step, "status": e.status, "duration_ms": e.duration_ms, "details": e.details}
                for e in self.execution
            ],

            "reasoning": [
                {
                    "step": r.step,
                    "observed": r.observed,
                    "decision": r.decision,
                    "alternatives": r.alternatives,
                    "expected_gain": r.expected_gain
                }
                for r in self.reasoning
            ],

            "query_trace": {
                "trace_id": self.query_trace.trace_id if self.query_trace else "",
                "session_id": self.query_trace.session_id if self.query_trace else "",
                "query_text": self.query_trace.query_text if self.query_trace else "",
                "query_normalized": self.query_trace.query_normalized if self.query_trace else "",
                "intent": self.query_trace.intent if self.query_trace else "",
                "query_type": self.query_trace.query_type if self.query_trace else "",
                "start_ts": self.query_trace.start_ts if self.query_trace else 0.0,
                "end_ts": self.query_trace.end_ts if self.query_trace else 0.0,
                "final_status": self.query_trace.final_status if self.query_trace else "",
            },

            "evidence": [
                {
                    "evidence_id": e.evidence_id,
                    "source_type": e.source_type,
                    "source_uri": e.source_uri,
                    "source_title": e.source_title,
                    "content_excerpt": e.content_excerpt[:300] if e.content_excerpt else "",
                    "relevance_to_query": e.relevance_to_query,
                    "quality_score": e.quality_score,
                    "source_class": e.source_class,
                    "evidence_eligible": e.evidence_eligible,
                    # P5: pre-existing gap, unrelated to this patch's own
                    # fields but found while building verification-memory
                    # reconstruction (evidence_role was never serialized
                    # here at all, even though EvidenceRecord always had
                    # it) — a reconstructed memory evidence item needs its
                    # real role to correctly re-enter the SAME direct+
                    # eligible eligibility path everything else uses.
                    "evidence_role": e.evidence_role,
                    "authority": e.authority,
                    "traceability": e.traceability,
                    "primaryness": e.primaryness,
                    "is_meta_pipeline_output": e.is_meta_pipeline_output,
                    "is_subject_matter_evidence": e.is_subject_matter_evidence,
                    "rejection_reason": e.rejection_reason,
                    "source_cluster_id": e.source_cluster_id,
                    # P5 (verification memory) additive fields.
                    "retrieval_claim_id": e.retrieval_claim_id,
                    "route": e.route,
                    "observed_at": e.observed_at,
                    "from_memory": e.from_memory,
                    "origin_route": e.origin_route,
                    "origin_trace_id": e.origin_trace_id,
                    "origin_observed_at": e.origin_observed_at,
                    "origin_source_cluster_id": e.origin_source_cluster_id,
                    "node_id": e.node_id,
                    "validator_id": e.validator_id,
                    "model_id": e.model_id,
                }
                # P5: no more [:10] cap — P4 §2 of the verification-memory
                # brief ("не только первые 3") applies just as much to this
                # hard count cap as it did to add_source() only ever being
                # called for the first 3 refutation snippets. What actually
                # bounds this list now is collect_verification_evidence_ids()
                # upstream (agent/verification_memory.py) — only evidence
                # that got a real relation on some claim ever reaches
                # trace.evidence at all, not the raw discovery/rejected pool.
                for e in self.evidence
            ],

            "claims": [
                {
                    "claim_id": c.claim_id,
                    "claim_text": c.claim_text[:300],
                    "derived_from_evidence_ids": c.derived_from_evidence_ids,
                    "claim_type": c.claim_type,
                    "claim_confidence": c.claim_confidence,
                    "verification_status": c.verification_status,
                    "evidence_relations": c.evidence_relations,
                    "content_hash": c.content_hash,
                    "evidence_search_attempted": c.evidence_search_attempted,
                    "evidence_search_error": c.evidence_search_error,
                    "semantic_family_id": c.semantic_family_id,
                }
                # P5: no more [:15] cap, same rationale as evidence above.
                for c in self.claims
            ],

            "rejected_claims": self.rejected_claims[:10],

            "decisions": [
                {
                    "decision_id": d.decision_id,
                    "decision_type": d.decision_type,
                    "options_considered": d.options_considered[:5],
                    "chosen_option": d.chosen_option,
                    "reason": d.reason,
                    "uncertainty_level": d.uncertainty_level,
                }
                for d in self.decisions
            ],

            "outcome": {
                "final_answer": self.outcome.final_answer[:500] if self.outcome and hasattr(self.outcome, "final_answer") else "",
                "final_answer_type": self.outcome.final_answer_type if self.outcome and hasattr(self.outcome, "final_answer_type") else "",
                "trust_label": self.outcome.trust_label if self.outcome and hasattr(self.outcome, "trust_label") else "",
                "trust_score": self.outcome.trust_score if self.outcome and hasattr(self.outcome, "trust_score") else 0.0,
                "supporting_claim_ids": self.outcome.supporting_claim_ids[:5] if self.outcome and hasattr(self.outcome, "supporting_claim_ids") else [],
                "coverage_ratio": self.outcome.coverage_ratio if self.outcome and hasattr(self.outcome, "coverage_ratio") else 0.0,
                "failure_modes": self.outcome.failure_modes if self.outcome and hasattr(self.outcome, "failure_modes") else [],
                "latency_ms": self.outcome.latency_ms if self.outcome and hasattr(self.outcome, "latency_ms") else 0.0,
                "learning_tags": self.outcome.learning_tags if self.outcome and hasattr(self.outcome, "learning_tags") else [],
            },

            "observations": self._observations,

            "learning": [
                {"type": l.type, "rule": l.rule, "confidence": l.confidence}
                for l in self.learning
            ],

            "confidence_evolution": [
                {"stage": c.stage, "confidence": c.confidence, "reason": c.reason}
                for c in self.confidence_evolution
            ],

            "trust": self.trust,
            "trust_reason": self.trust_reason[:300] if self.trust_reason else "",
            "cost": self.cost,

            "epistemic": self.epistemic,
            "claims_filtered_count": len(self.claims),
            "claims_rejected_count": len(self.rejected_claims),
        }
        return data


class DecisionTracer:
    def __init__(self):
        self._traces: List[Dict[str, Any]] = []

    def save_trace(self, trace: Trace) -> Dict[str, Any]:
        data = trace.to_dict()
        self._traces.append(data)

        locator = None

        try:
            day_file_name = f"{datetime.now().strftime('%Y%m%d')}.jsonl"
            day_file = TRACES_DIR / day_file_name
            line = json.dumps(data, ensure_ascii=False) + "\n"

            with day_file.open("a", encoding="utf-8") as f:
                # P5 (verification memory): 'a' mode is always
                # positioned at EOF on open (POSIX append semantics) —
                # this offset is exactly where THIS trace's line starts,
                # used as registry/index.db's locator (agent.
                # verification_memory.index_trace) so a future LOAD is
                # one seek+readline, not a full-file scan.
                byte_offset = f.tell()
                f.write(line)

            locator = (day_file_name, byte_offset)
        except Exception as e:
            print(f"[tracer] Ошибка сохранения: {e}")

        if locator:
            # Deferred import: agent.verification_memory imports
            # TRACES_DIR from this module, so a module-level import here
            # would be circular. Failure here must never affect the
            # trace itself already being safely on disk — it only means
            # the lookup accelerator doesn't know about this trace yet
            # (the JSONL source of truth is unaffected, still fully
            # scannable as a fallback).
            try:
                from agent.verification_memory import index_trace
                index_trace(trace, locator[0], locator[1])
            except Exception as e:
                print(f"[tracer] Ошибка индексации verification memory: {e}")

        return data

    def get_traces(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._traces[-limit:]


def get_tracer() -> DecisionTracer:
    return DecisionTracer()


class OrchestratorTracer:
    def trace(self, *args, **kwargs):
        pass
