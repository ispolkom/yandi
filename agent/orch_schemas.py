"""
assistant/orch_schemas.py — схемы данных для оркестратора.
Включает новую модель данных: QueryTrace, EvidenceRecord, ClaimRecord, AnswerCoverage, OutcomeRecord.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class SearchDoc:
    text: str
    trust_level: str
    score: float
    source: str
    topic: str
    meta: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    docs: List[SearchDoc]
    confidence: float
    source: str
    top_k: int


@dataclass
class IntentResult:
    intent: str
    entities: Dict[str, str]
    missing: List[str]
    need_clarification: bool
    confidence: float
    raw: Optional[Dict[str, Any]] = None
    is_instruction: bool = False


@dataclass
class EnrichedQuery:
    original: str
    enriched: str
    params: Dict[str, str]
    tags: List[str] = field(default_factory=list)


@dataclass
class SynthesisResult:
    answer: str
    confidence: float
    sources: List[str]
    trust_level: str


@dataclass
class OrchestratorRequest:
    query: str
    session_id: str = ""
    context: str = ""
    search_queries: List[str] = field(default_factory=list)
    query_frame: dict = field(default_factory=dict)


@dataclass
class OrchestratorResponse:
    answer: str
    trust_level: str
    preliminary: bool
    sources: List[str] = field(default_factory=list)
    steps_taken: List[str] = field(default_factory=list)
    latency_total: float = 0.0
    session_id: str = ""


@dataclass
class WebQueryResult:
    queries: List[str]
    raw: str = ""


@dataclass
class TrustLevel:
    HYPOTHESIS: str = "HYPOTHESIS"
    VERIFIED: str = "VERIFIED"
    PARTIAL: str = "PARTIAL"
    REJECTED: str = "REJECTED"
    UNVERIFIED: str = "UNVERIFIED"


@dataclass
class RiskResult:
    risk_level: str
    mandatory_arbitrage: bool = False
    validator_model: str = "7b"
    nodes_required: int = 1


@dataclass
class CacheResult:
    """Результат поиска в кэше с полным объектом знания."""
    hit: bool
    answer: str = ""
    trust_level: str = "HYPOTHESIS"
    similarity: float = 0.0
    claims: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    epistemic: Dict[str, Any] = field(default_factory=dict)
    entity_id: Optional[str] = None
    entity_type: Optional[str] = None
    created_at: float = 0.0
    ttl: int = 86400


@dataclass
class WebSnippet:
    url: str
    title: str = ""
    content: str = ""
    text: str = ""
    relevance: float = 0.5


@dataclass
class WebScrapeResult:
    snippets: List[WebSnippet] = field(default_factory=list)
    total_chars: int = 0
    urls: List[str] = field(default_factory=list)


@dataclass
class OptimisticResponse:
    text: str
    trust_level: str = "HYPOTHESIS"
    validation_id: Optional[str] = None
    preliminary: bool = True


@dataclass
class NodeValidation:
    node_id: str
    verdict: str
    confidence: float
    explanation: str
    latency: float


@dataclass
class ValidationResult:
    validations: List[NodeValidation] = field(default_factory=list)
    agree_count: int = 0
    disagree_count: int = 0
    timed_out: List[str] = field(default_factory=list)


@dataclass
class ValidationResultCollection:
    validations: List[NodeValidation] = field(default_factory=list)
    agree_count: int = 0
    disagree_count: int = 0
    uncertain_count: int = 0


@dataclass
class ArbitrationResult:
    verdict: str
    explanation: str
    confidence: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    final_answer: Optional[str] = None


@dataclass
class ArbiterResult:
    verdict: str
    explanation: str
    confidence: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    final_answer: Optional[str] = None


@dataclass
class NodeInfo:
    node_id: str
    reputation: float = 0.5
    capabilities: List[str] = field(default_factory=list)
    latency: float = 0.0
    active: bool = True


@dataclass
class NodeSelectionResult:
    nodes: List[NodeInfo]
    strategy: str = "default"
    total_candidates: int = 0


@dataclass
class NodeSelectorResult:
    nodes: List[NodeInfo]
    selected: bool = True


@dataclass
class PlanStep:
    name: str
    description: str
    priority: int = 1
    required: bool = True
    depends_on: List[str] = field(default_factory=list)


@dataclass
class PlanResult:
    steps: List[PlanStep]
    skip_internet: bool = False
    estimated_time: float = 0.0


@dataclass
class StepName:
    RISK_ASSESS: str = "risk_assess"
    INTENT: str = "intent"
    ENRICH: str = "enrich"
    LOCAL_SEARCH: str = "local_search"
    WEB_SEARCH: str = "web_search"
    SYNTHESIZE: str = "synthesize"
    VALIDATE: str = "validate"
    ARBITRATE: str = "arbitrate"


@dataclass
class StepError:
    step_name: str
    error: str
    timeout: bool = False
    details: Optional[Dict[str, Any]] = None


@dataclass
class ClarificationQuestion:
    question_id: str
    text: str
    options: List[str] = field(default_factory=list)
    required: bool = True


@dataclass
class ClarificationResult:
    original_intent: IntentResult
    clarified_intent: IntentResult
    questions_asked: List[ClarificationQuestion]
    answers: Dict[str, str] = field(default_factory=dict)
    complete: bool = False


@dataclass
class ValidatorResponse:
    verdict: str
    confidence: float
    explanation: str
    latency: float


@dataclass
class KnowledgeEntry:
    question: str
    answer: str
    confidence: float
    trust_level: str
    timestamp: float
    sources: List[Dict[str, Any]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    verified_by: List[str] = field(default_factory=list)


# ============================================================
# НОВАЯ МОДЕЛЬ ДАННЫХ ДЛЯ YANDI CORE (v1.0)
# ============================================================

@dataclass
class QueryTrace:
    """Полный след запроса."""
    trace_id: str
    session_id: str = ""
    query_text: str = ""
    query_normalized: str = ""
    intent: str = ""
    query_type: str = ""
    aspects: List[str] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0
    final_status: str = ""


@dataclass
class ExecutionStep:
    """Шаг выполнения пайплайна."""
    step_id: str
    step_type: str
    goal: str = ""
    selected_tool: str = ""
    selection_reason: str = ""
    status: str = ""
    duration_ms: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class TrustReport:
    """Итоговое доверие."""
    overall_score: float = 0.0
    label: str = "UNVERIFIED"
    source_quality: float = 0.0
    claim_grounding: float = 0.0
    source_independence: float = 0.0
    coverage_score: float = 0.0
    reason: List[str] = field(default_factory=list)


@dataclass
class CoverageReport:
    """Покрытие аспектов вопроса."""
    query_aspects: List[str] = field(default_factory=list)
    covered_aspects: List[str] = field(default_factory=list)
    uncovered_aspects: List[str] = field(default_factory=list)
    coverage_ratio: float = 0.0
    answerability_status: str = ""


@dataclass
class EvidenceRecord:
    """Доказательство."""
    evidence_id: str
    source_type: str
    source_uri: str = ""
    source_title: str = ""
    retrieval_query: str = ""
    retrieval_rank: int = 0
    content_excerpt: str = ""
    subject_entities: List[str] = field(default_factory=list)
    fact_candidates: List[str] = field(default_factory=list)
    relevance_to_query: float = 0.0
    supports_query_aspect: List[str] = field(default_factory=list)

    # Source Quality Gate.
    #
    # Эти поля описывают пригодность ИСТОЧНИКА как evidence,
    # а не истинность содержащегося в нём утверждения.
    quality_score: float = 0.0
    source_class: str = "unknown"
    evidence_eligible: bool = False
    evidence_role: str = "context"
    authority: float = 0.0
    traceability: float = 0.0
    primaryness: float = 0.0

    is_meta_pipeline_output: bool = False
    is_subject_matter_evidence: bool = True
    rejection_reason: Optional[str] = None


@dataclass
class ClaimRecord:
    """Атомарное утверждение."""
    claim_id: str
    claim_text: str
    derived_from_evidence_ids: List[str] = field(default_factory=list)
    claim_subject: str = ""
    claim_type: str = ""
    claim_confidence: float = 0.5
    supports_query_aspect: List[str] = field(default_factory=list)
    conflicts_with_claim_ids: List[str] = field(default_factory=list)
    verification_status: str = ""
    # Epistemic Core v1 Phase 1: per-evidence NLI verdict, so a persisted
    # trace can answer "why" (not just "with which evidence_id") a claim
    # got its verification_status. Each entry:
    # {"evidence_id", "relation" (supports/contradicts/unrelated/uncertain),
    # "relation_method", "source_claim"}. Additive-only field — old traces
    # without it simply have an empty list here (default_factory), nothing
    # to migrate.
    evidence_relations: List[Dict[str, Any]] = field(default_factory=list)
    # Epistemic Core v1 Phase 2: deterministic content identity (see
    # agent/claim_identity.py) — NOT a replacement for claim_id, NOT
    # semantic/paraphrase identity. None when absent/not computable
    # (old traces, or empty claim text), never a fabricated value.
    content_hash: Optional[str] = None


@dataclass
class DecisionRecord:
    """Решение на развилке."""
    decision_id: str
    decision_type: str
    options_considered: List[str] = field(default_factory=list)
    chosen_option: str = ""
    reason: str = ""
    evidence_used: List[str] = field(default_factory=list)
    uncertainty_level: float = 0.0


@dataclass
class OutcomeRecord:
    """Итог выполнения запроса."""
    final_answer: str = ""
    final_answer_type: str = ""
    trust_label: str = "UNVERIFIED"
    trust_score: float = 0.0
    supporting_claim_ids: List[str] = field(default_factory=list)
    coverage_ratio: float = 0.0
    failure_modes: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
    learning_tags: List[str] = field(default_factory=list)
