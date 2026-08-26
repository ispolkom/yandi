"""
agent/claim_graph.py — WORLD CLAIMS + ГРАФ ДЛЯ YANDI V7.

Извлекает утверждения о мире.
Каждый claim = атомарное утверждение + список evidence (поддерживающих и опровергающих).
Строит граф связей между claims.

Пример:
✅ Хорошо: "Разум — это способность к субъективному восприятию"
   evidence_for: ['ev_001', 'ev_002']
   evidence_against: ['ev_003']
   supports: ['cl_002']
   contradicts: ['cl_003']
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import re
import uuid
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field


@dataclass
class Claim:
    """Атомарное утверждение о мире с графовыми связями."""
    claim_id: str
    text: str
    evidence_for: List[str] = field(default_factory=list)   # поддерживающие evidence
    evidence_against: List[str] = field(default_factory=list)  # опровергающие evidence
    claim_type: str = "factual"  # factual | interpretation | hypothesis | definition | uncertainty
    confidence: float = 0.5
    source_reliability: float = 0.5
    verification_status: str = "unverified"
    is_world_claim: bool = True
    
    # Графовые связи
    supports: List[str] = field(default_factory=list)       # claim_id → поддерживает
    contradicts: List[str] = field(default_factory=list)    # claim_id → противоречит
    depends_on: List[str] = field(default_factory=list)     # claim_id → зависит от
    
    # История
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class ClaimGraph:
    """
    Извлекает WORLD CLAIMS и строит граф связей.
    """
    
    def __init__(self):
        self.claims: List[Claim] = []
        self.evidence_map: Dict[str, Dict] = {}
        self.claim_map: Dict[str, Claim] = {}
        self.text_to_claim: Dict[str, str] = {}  # текст → claim_id
    
    def extract_claims(self, evidence_data: List[Dict[str, Any]]) -> List[Claim]:
        """
        Извлечь WORLD CLAIMS из evidence и построить граф.
        """
        if not evidence_data:
            return []
        
        self.evidence_map = {e.get("evidence_id", f"ev_{i}"): e for i, e in enumerate(evidence_data)}
        self.claims = []
        self.claim_map = {}
        self.text_to_claim = {}
        
        # 1. Извлекаем claims из каждого evidence
        for ev_id, ev in self.evidence_map.items():
            content = ev.get("content_excerpt", "")
            if not content or len(content) < 50:
                continue
            
            sentences = self._split_into_sentences(content)
            for sent in sentences[:4]:
                clean = self._clean_sentence(sent)
                if self._is_world_claim(clean):
                    claim = Claim(
                        claim_id=f"cl_{uuid.uuid4().hex[:8]}",
                        text=clean,
                        evidence_for=[ev_id],
                        claim_type=self._determine_claim_type(clean),
                        confidence=self._calculate_confidence(clean, ev),
                        source_reliability=self._get_source_reliability(ev),
                        verification_status="weak",
                        is_world_claim=True,
                    )
                    self.claims.append(claim)
                    self.claim_map[claim.claim_id] = claim
                    self.text_to_claim[clean[:50]] = claim.claim_id
        
        # 2. Дедупликация и объединение evidence
        self._deduplicate_and_merge()
        
        # 3. Построение графа связей
        self._build_graph()
        
        return self.claims
    
    def _split_into_sentences(self, text: str) -> List[str]:
        sentences = re.split(r'[.!?]\s+', text)
        return [s.strip() for s in sentences if len(s.strip()) > 20]
    
    def _clean_sentence(self, sent: str) -> str:
        sent = ' '.join(sent.split())
        sent = re.sub(r'\[[^\]]+\]', '', sent)
        sent = re.sub(r'\*\*([^*]+)\*\*', r'\1', sent)
        sent = re.sub(r'\*([^*]+)\*', r'\1', sent)
        sent = re.sub(r'^\d+\.\s+', '', sent)
        return sent.strip()
    
    def _is_world_claim(self, text: str) -> bool:
        if len(text) < 20:
            return False
        if len(text) > 350:
            return False
        
        meta_patterns = [
            r'(?i)вопрос\s+пользователя',
            r'(?i)запрос\s+пользователя',
            r'(?i)пользователь\s+спрашивает',
            r'(?i)требует\s+философских',
            r'(?i)отвечая\s+на\s+вопрос',
            r'(?i)данный\s+документ',
            r'(?i)эта\s+статья',
            r'(?i)источник\s+содержит',
            r'(?i)содержание\s+источников',
            r'(?i)проанализировав',
            r'(?i)результат\s+анализа',
            r'(?i)сырые\s+данные',
            r'(?i)извлечённые\s+факты',
            r'(?i)матрица\s+эпистемического',
        ]
        for pattern in meta_patterns:
            if re.search(pattern, text):
                return False
        
        world_patterns = [
            r'(?i)является',
            r'(?i)определяется',
            r'(?i)называется',
            r'(?i)представляет собой',
            r'(?i)состоит из',
            r'(?i)включает',
            r'(?i)содержит',
            r'(?i)возник',
            r'(?i)произошёл',
            r'(?i)создан',
            r'(?i)относится к',
            r'(?i)связан с',
            r'(?i)может быть',
            r'(?i)является результатом',
            r'(?i)существует',
            r'(?i)известно',
            r'(?i)установлено',
        ]
        has_world_pattern = any(re.search(p, text) for p in world_patterns)
        if has_world_pattern:
            return True
        if re.search(r'\d+', text):
            return True
        return False
    
    def _determine_claim_type(self, text: str) -> str:
        if re.search(r'(?i)вероятно|возможно|предположительно|может быть|гипотеза', text):
            return "hypothesis"
        if re.search(r'(?i)неизвестно|не\s+установлено|остаётся\s+неясным|предмет\s+дискуссии', text):
            return "uncertainty"
        if re.search(r'(?i)определяется|называется|это|означает|представляет собой', text):
            return "definition"
        if re.search(r'(?i)согласно|по\s+данным|исследование|показывает|установлено|факт|доказано', text):
            return "factual"
        return "interpretation"
    
    def _calculate_confidence(self, text: str, ev: Dict) -> float:
        base = 0.5
        
        if re.search(r'(?i)согласно|по данным|установлено|факт|доказано', text):
            base += 0.25
        elif re.search(r'(?i)вероятно|возможно|предположительно|гипотеза', text):
            base -= 0.1
        elif re.search(r'(?i)неизвестно|не установлено|остаётся неясным', text):
            base += 0.1
        
        if len(text) > 100:
            base += 0.05
        
        relevance = ev.get("relevance_to_query", 0.5)
        base += relevance * 0.15
        
        return min(1.0, max(0.1, base))
    
    def _get_source_reliability(self, ev: Dict) -> float:
        uri = ev.get("source_uri", "")
        if "wikipedia" in uri:
            return 0.9
        if "science" in uri or "nature" in uri:
            return 0.85
        if "news" in uri:
            return 0.6
        if "blog" in uri:
            return 0.4
        if "local" in ev.get("source_type", ""):
            return 0.7
        return 0.5
    
    def _deduplicate_and_merge(self):
        """
        Дедупликация: объединяем одинаковые claims, собираем все evidence.
        """
        seen = {}
        unique_claims = []
        
        for claim in self.claims:
            key = claim.text[:50]
            if key in seen:
                # Объединяем evidence
                existing = seen[key]
                existing.evidence_for.extend(claim.evidence_for)
                # Обновляем уверенность (среднее)
                existing.confidence = (existing.confidence + claim.confidence) / 2
                existing.updated_at = time.time()
            else:
                seen[key] = claim
                unique_claims.append(claim)
        
        self.claims = unique_claims
        self.claim_map = {c.claim_id: c for c in self.claims}
        self.text_to_claim = {c.text[:50]: c.claim_id for c in self.claims}
    
    def _build_graph(self):
        """
        Построить граф связей между claims.
        """
        if len(self.claims) < 2:
            return
        
        # Ищем противоречия и поддержки на основе текста
        for i, claim1 in enumerate(self.claims):
            for claim2 in self.claims[i+1:]:
                # Проверяем на противоречия
                if self._is_contradiction(claim1.text, claim2.text):
                    claim1.contradicts.append(claim2.claim_id)
                    claim2.contradicts.append(claim1.claim_id)
                # Проверяем на поддержку
                elif self._is_support(claim1.text, claim2.text):
                    claim1.supports.append(claim2.claim_id)
                    claim2.depends_on.append(claim1.claim_id)
    
    def _is_contradiction(self, text1: str, text2: str) -> bool:
        """Проверить, противоречат ли два утверждения."""
        # Простейшая эвристика: наличие противоположных маркеров
        neg1 = any(w in text1.lower() for w in ["не", "нет", "нельзя", "невозможно"])
        neg2 = any(w in text2.lower() for w in ["не", "нет", "нельзя", "невозможно"])
        
        # Если одно утверждение содержит отрицание, а другое нет — возможное противоречие
        if neg1 != neg2:
            return True
        
        # Проверка на противоположные утверждения
        if "является" in text1 and "не является" in text2:
            return True
        if "не является" in text1 and "является" in text2:
            return True
        
        return False
    
    def _is_support(self, text1: str, text2: str) -> bool:
        """Проверить, поддерживает ли одно утверждение другое."""
        # Если утверждения говорят об одном и том же
        common_words = set(text1.split()) & set(text2.split())
        if len(common_words) > 3:
            return True
        return False
    
    def to_dict(self) -> List[Dict[str, Any]]:
        return [
            {
                "claim_id": c.claim_id,
                "claim_text": c.text,
                "evidence_for": c.evidence_for,
                "evidence_against": c.evidence_against,
                "claim_type": c.claim_type,
                "claim_confidence": c.confidence,
                "verification_status": c.verification_status,
                "is_world_claim": c.is_world_claim,
                "supports": c.supports,
                "contradicts": c.contradicts,
                "depends_on": c.depends_on,
            }
            for c in self.claims
        ]
    
    def get_graph(self) -> Dict[str, Any]:
        """Получить полный граф."""
        return {
            "nodes": [
                {
                    "id": c.claim_id,
                    "text": c.text[:100],
                    "type": c.claim_type,
                    "confidence": c.confidence,
                }
                for c in self.claims
            ],
            "edges": [
                {"from": c.claim_id, "to": target, "type": "supports"}
                for c in self.claims
                for target in c.supports
            ] + [
                {"from": c.claim_id, "to": target, "type": "contradicts"}
                for c in self.claims
                for target in c.contradicts
            ] + [
                {"from": c.claim_id, "to": target, "type": "depends_on"}
                for c in self.claims
                for target in c.depends_on
            ],
        }
    
    def get_claims_with_evidence(self) -> List[Dict[str, Any]]:
        result = []
        for claim in self.claims:
            evidence_texts = []
            for ev_id in claim.evidence_for:
                ev = self.evidence_map.get(ev_id, {})
                evidence_texts.append(ev.get("content_excerpt", "")[:200])
            result.append({
                "claim": claim.text,
                "evidence": evidence_texts,
                "confidence": claim.confidence,
                "claim_type": claim.claim_type,
                "source_reliability": claim.source_reliability,
                "supports": claim.supports,
                "contradicts": claim.contradicts,
            })
        return result
    
    def summary(self) -> Dict[str, Any]:
        if not self.claims:
            return {"total": 0, "types": {}, "avg_confidence": 0, "graph_edges": 0}
        
        types = {}
        total_conf = 0
        for c in self.claims:
            types[c.claim_type] = types.get(c.claim_type, 0) + 1
            total_conf += c.confidence
        
        edges = sum(len(c.supports) + len(c.contradicts) + len(c.depends_on) for c in self.claims)
        
        return {
            "total": len(self.claims),
            "types": types,
            "avg_confidence": round(total_conf / len(self.claims), 2),
            "graph_edges": edges,
        }


# Глобальный экземпляр
_claim_graph: Optional[ClaimGraph] = None

def get_claim_graph() -> ClaimGraph:
    global _claim_graph
    if _claim_graph is None:
        _claim_graph = ClaimGraph()
    return _claim_graph


if __name__ == "__main__":
    cg = get_claim_graph()
    
    test_evidence = [
        {
            "evidence_id": "ev_001",
            "source_type": "web",
            "source_uri": "https://wikipedia.org/consciousness",
            "content_excerpt": "Сознание определяется как способность к субъективному восприятию и осознанию окружающего мира.",
            "relevance_to_query": 0.8,
        },
        {
            "evidence_id": "ev_002",
            "source_type": "web",
            "source_uri": "https://science.org/consciousness",
            "content_excerpt": "Исследования показывают, что сознание является эмерджентным свойством сложных нейронных сетей.",
            "relevance_to_query": 0.7,
        },
        {
            "evidence_id": "ev_003",
            "source_type": "web",
            "source_uri": "https://philosophy.org/consciousness",
            "content_excerpt": "Сознание не является эмерджентным свойством, оно первично по отношению к материи.",
            "relevance_to_query": 0.6,
        },
    ]
    
    claims = cg.extract_claims(test_evidence)
    
    print("=== WORLD CLAIMS + ГРАФ ===")
    print(f"Извлечено claims: {len(claims)}")
    print("\nСтруктура:")
    for c in claims:
        print(f"  [{c.claim_type}] {c.text[:80]}... (conf: {c.confidence:.2f})")
        print(f"    evidence_for: {c.evidence_for}")
        print(f"    evidence_against: {c.evidence_against}")
        print(f"    supports: {c.supports}")
        print(f"    contradicts: {c.contradicts}")
    
    print("\n=== ГРАФ ===")
    print(cg.get_graph())
    
    print("\n=== СТАТИСТИКА ===")
    print(cg.summary())
