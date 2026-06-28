"""
assistant/orch_schemas.py — Pydantic-контракты всех модулей оркестратора.
Единый источник истины для входов/выходов каждого шага.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


# ── Общие типы ────────────────────────────────────────────────────────────────

RiskLevel   = Literal["low", "medium", "high", "critical"]
TrustLevel  = Literal["VERIFIED", "PARTIALLY_VERIFIED", "HYPOTHESIS", "PERSONAL", "UNVERIFIED"]
VerdictType = Literal["VERIFIED", "PARTIALLY_VERIFIED", "CONFLICT_DETECTED", "REJECTED"]
StepName    = Literal[
    "cache_check", "risk_assess", "plan", "intent", "clarify",
    "enrich", "local_search", "web_query", "web_scrape",
    "synthesize", "optimistic_respond", "validate", "arbitrate",
]


# ── Запрос / сессия ───────────────────────────────────────────────────────────

class OrchestratorRequest(BaseModel):
    query:          str
    session_id:     str        = ""
    user_id:        str        = "local"
    context:        list[dict] = Field(default_factory=list)
    search_queries: list[str]  = Field(default_factory=list)  # из QueryFrame, приоритет над formulate_queries
    query_frame:    dict       = Field(default_factory=dict)   # матрица слотов для синтезатора


# ── [0] Cache ─────────────────────────────────────────────────────────────────

class CacheResult(BaseModel):
    hit:         bool
    answer:      Optional[str]  = None
    trust_level: Optional[TrustLevel] = None
    similarity:  float          = 0.0


# ── [1] Risk ──────────────────────────────────────────────────────────────────

class RiskResult(BaseModel):
    risk_level:           RiskLevel
    mandatory_arbitrage:  bool
    validator_model:      Literal["7b", "14b"]
    nodes_required:       int  # 1, 2 или 3


# ── [2] Plan ──────────────────────────────────────────────────────────────────

class PlanResult(BaseModel):
    steps:               list[StepName]
    risk_level:          RiskLevel
    skip_internet:       bool
    mandatory_arbitrage: bool
    raw:                 str = ""  # сырой ответ модели


# ── [3] Intent ────────────────────────────────────────────────────────────────

class IntentResult(BaseModel):
    intent:             str         # cooking, tech, medical, legal, general, ...
    entities:           dict        # {"product": "рыба", "method": None}
    missing:            list[str]   # ["тип рыбы", "способ приготовления"]
    need_clarification: bool
    confidence:         float       # 0.0–1.0
    raw:                str = ""


# ── [4] Clarification ────────────────────────────────────────────────────────

class ClarificationQuestion(BaseModel):
    param:    str   # какой параметр уточняем
    question: str   # текст вопроса пользователю

class ClarificationResult(BaseModel):
    questions:   list[ClarificationQuestion]
    enriched:    dict   # заполненные параметры после ответа пользователя
    complete:    bool   # все required параметры получены
    rounds_used: int


# ── [5] Enricher ─────────────────────────────────────────────────────────────

class EnrichedQuery(BaseModel):
    original:  str
    enriched:  str        # нормализованный расширенный запрос
    params:    dict       # собранные параметры
    tags:      list[str] = []  # иерархические теги для DHT-маршрутизации
    raw:       str = ""


# ── [6] Local Search ─────────────────────────────────────────────────────────

class SearchDoc(BaseModel):
    text:        str
    trust_level: TrustLevel
    score:       float       # cosine similarity
    source:      str         # откуда запись
    topic:       str = ""

class SearchResult(BaseModel):
    docs:       list[SearchDoc]
    confidence: float   # среднее top-3 score
    source:     Literal["local", "web", "cache"]
    top_k:      int = 5


# ── [7a] Web Query ───────────────────────────────────────────────────────────

class WebQueryResult(BaseModel):
    queries: list[str]  # 2-3 варианта поисковых запросов
    raw:     str = ""


# ── [7b] Web Scrape ──────────────────────────────────────────────────────────

class WebSnippet(BaseModel):
    url:   str
    title: str
    text:  str   # обрезанный до 3k токенов
    rank:  float

class WebScrapeResult(BaseModel):
    snippets:    list[WebSnippet]
    total_chars: int
    queries_used: list[str]


# ── [8] Synthesizer ──────────────────────────────────────────────────────────

class SynthesisResult(BaseModel):
    answer:      str
    confidence:  float
    sources:     list[str]
    trust_level: TrustLevel
    raw:         str = ""


# ── [9] Optimistic Response ──────────────────────────────────────────────────

class OptimisticResponse(BaseModel):
    text:           str   # то что показывается пользователю
    preliminary:    bool  = True
    validation_id:  str   = ""  # ID фоновой валидации


# ── [10] Node Selector ───────────────────────────────────────────────────────

class NodeInfo(BaseModel):
    node_id:      str
    model:        str        # "qwen3:14b", "deepseek-r1:14b", etc.
    endpoint:     str        # ollama URL или external
    reputation:   float = 0.7   # 0.0–1.0
    domain_score: float = 0.7   # точность в domain intent
    speed_score:  float = 0.5

class NodeSelectorResult(BaseModel):
    nodes:        list[NodeInfo]
    fallback_used: bool  # true если нод < required


# ── [11] Validator ───────────────────────────────────────────────────────────

class NodeValidation(BaseModel):
    node_id:  str
    verdict:  Literal["agree", "disagree", "partial"]
    reason:   str
    latency:  float  # секунды

class ValidationResult(BaseModel):
    validations:   list[NodeValidation]
    agree_count:   int
    disagree_count: int
    timed_out:     list[str]  # node_id которые не ответили


# ── [12] Consensus Arbiter ───────────────────────────────────────────────────

class ArbiterResult(BaseModel):
    verdict:     VerdictType
    explanation: str
    final_answer: Optional[str]  # скорректированный ответ если нужно
    raw:         str = ""


# ── [13] Arbiter Connector ──────────────────────────────────────────────────

class CouncilResponse(BaseModel):
    model:  str
    answer: str

class ConnectorResult(BaseModel):
    responses:    list[CouncilResponse]
    final_verdict: str
    comparison:   str  # текст сравнения для UI


# ── Финальный ответ ───────────────────────────────────────────────────────────

class OrchestratorResponse(BaseModel):
    answer:         str
    trust_level:    TrustLevel
    preliminary:    bool
    sources:        list[str]    = Field(default_factory=list)
    verdict:        Optional[VerdictType] = None
    steps_taken:    list[StepName] = Field(default_factory=list)
    latency_total:  float = 0.0
    tokens_used:    int   = 0
    session_id:     str   = ""


# ── Ошибка шага ──────────────────────────────────────────────────────────────

class StepError(BaseModel):
    step:    StepName
    error:   str
    timeout: bool = False
