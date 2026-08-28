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
    # PET_AGENT_BOUNDARY_AUDIT.md Phase 4C: without this, callers outside
    # agent/ (pet/) had no way to reference the trace that produced a
    # response - any later delayed validation/evidence they collected
    # could not be linked back to it except by unreliable timestamp/text
    # matching. Populated in writeback.py from the same trace_id already
    # threaded through the whole request.
    trace_id: str = ""


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
    # P4 (web budget 3+3): which side of a budgeted claim-specific
    # retrieval this snippet came from - "direct"/"counter", or "" for
    # any caller that doesn't distinguish (e.g. the whole-question
    # stage-6 scrape() calls, unchanged). Additive, default-empty, no
    # existing reader/writer of WebSnippet is affected.
    origin: str = ""


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
    # Epistemic Core v1 Phase 6: source-independence cluster metadata
    # (see agent/source_clustering.py). None when not computed (old
    # records, or a code path that doesn't set it) — never a fabricated
    # shared cluster between two unrelated items.
    source_cluster_id: Optional[str] = None

    # P5 (verification memory) — additive, all default to a value that
    # preserves old-record meaning (empty string / current cycle / not
    # from memory). See agent/verification_memory.py.
    #
    # retrieval_claim_id: which CURRENT claim this evidence is owned by
    # (mirrors the runtime evidence dict's own field of the same name —
    # copied through, not recomputed). "" for shared/global evidence not
    # owned by one specific claim.
    retrieval_claim_id: str = ""
    # route: which of the 5 epistemic channels this evidence reached
    # THIS verification cycle through. "internet" for anything actually
    # fetched this cycle (matches all existing/old data's true meaning);
    # "local_memory" when reconstructed from a prior trace via
    # verification_memory.lookup_historical_evidence(); "network_node"/
    # "ai_chat" reserved for future channels (P4 §13 of the Этап 3
    # brief) — nothing sets them yet.
    route: str = "internet"
    observed_at: Optional[float] = None
    # from_memory: this EvidenceRecord was reconstructed from a prior
    # trace, not fetched this cycle. The single authoritative flag
    # downstream code (P1-A gate, Trust) must check before treating a
    # relation involving this evidence as a fresh verification result —
    # see agent/orchestrator/claims/retrieval.py::_claim_has_effective_evidence.
    from_memory: bool = False
    # origin_*: ONLY meaningful when from_memory=True — preserves the
    # provenance chain back to where this evidence was ORIGINALLY
    # observed (P4 §12: "memory reuse is a new ROUTE, not a new SOURCE"
    # — source_uri never changes, only route does; these fields record
    # what route/trace/time it first came from, so reuse never gets
    # mistaken for a second independent root).
    origin_route: Optional[str] = None
    origin_trace_id: Optional[str] = None
    origin_observed_at: Optional[float] = None
    origin_source_cluster_id: Optional[str] = None

    # Schema preparation only (P4 §13) — NOT activated by this patch.
    # Nodes/AI-chat orchestration is untouched; these stay None until a
    # future stage actually populates them.
    node_id: Optional[str] = None
    validator_id: Optional[str] = None
    model_id: Optional[str] = None

    # P6 (Этап 4 §9, Finding 2 fix): which SIDE of a budgeted retrieval
    # this evidence was originally fetched for — "direct"/"counter"
    # (PASS2, from WebSnippet.origin set in orch_web_scraper.py::
    # scrape_budgeted) or "main"/"counter" (stage 6, from
    # scrape_budgeted_side). Deliberately a SEPARATE field from
    # retrieval_origin (which stays "claim_specific"/"initial_web"/
    # "refutation" — the STAGE/mechanism, unchanged meaning) — one
    # field must not carry two different meanings. "" for anything not
    # produced by a budgeted retrieval (old data, non-web evidence).
    route_side: str = ""


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
    # Epistemic Core v1 Phase 3: search-outcome disambiguation, companion
    # to verification_status (which is NOT touched/renamed — see
    # claims/retrieval.py). evidence_search_attempted: None = PASS2 not
    # applicable (claim already resolved by PASS1), True = PASS2 retrieval
    # was actually attempted, False = PASS2 was needed but never ran
    # (skip_rag/enable_web=False/subjective gate). evidence_search_error:
    # set only when the attempt itself failed (network/timeout/etc) —
    # distinct from "attempted, found nothing" (error stays None).
    evidence_search_attempted: Optional[bool] = None
    evidence_search_error: Optional[str] = None
    # Epistemic Core v1 Phase 10: cross-request semantic claim family
    # (see agent/claim_family_registry.py). Groups this OCCURRENCE with
    # other occurrences judged semantically equivalent — does not
    # replace claim_id, which remains this occurrence's own identity.
    # None when not computed (old traces, or the claim wasn't in the
    # capped linking batch).
    semantic_family_id: Optional[str] = None


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
