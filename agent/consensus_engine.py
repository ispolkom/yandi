"""
agent/consensus_engine.py — Consensus Engine для Federation v2
Голосование по Claims с учетом репутации
"""

from typing import List, Dict, Set, Optional
from dataclasses import dataclass, field
from collections import defaultdict
import hashlib
import re
from datetime import datetime

from agent.orch_reputation import get_node_score


@dataclass
class ClaimVote:
    """Голос за конкретный Claim"""
    claim_id: str
    claim_text: str
    node_id: str
    support: bool  # True = подтверждает, False = опровергает
    confidence: float  # 0-1
    evidence_ids: List[str]
    reasoning: str = ""
    domain: str = "general"


@dataclass
class ConsensusResult:
    """Результат консенсуса по одному Claim"""
    claim_id: str
    claim_text: str
    support_count: int
    contradict_count: int
    unknown_count: int
    weighted_support: float  # 0-1
    weighted_confidence: float
    independent_sources: int
    status: str  # accepted | questioned | rejected | uncertain
    supporting_nodes: List[str]
    contradicting_nodes: List[str]
    consensus_level: float  # 0-1
    domain: str = "general"


class ConsensusEngine:
    """Consensus Engine с взвешенным голосованием"""
    
    def __init__(self):
        self.claim_history = defaultdict(list)  # claim_hash -> list of votes
        
    def evaluate_claims(self, votes: List[ClaimVote]) -> List[ConsensusResult]:
        """
        Оценка всех Claims с учетом репутации нод
        
        Args:
            votes: Список голосов от разных нод
            
        Returns:
            Список результатов консенсуса
        """
        # Группируем по нормализованному тексту
        claim_groups = defaultdict(list)
        for vote in votes:
            normalized = self._normalize_claim(vote.claim_text)
            claim_groups[normalized].append(vote)
        
        results = []
        for normalized, group_votes in claim_groups.items():
            result = self._evaluate_claim_group(normalized, group_votes)
            results.append(result)
            
            # Сохраняем историю
            claim_hash = hashlib.sha256(normalized.encode()).hexdigest()[:16]
            self.claim_history[claim_hash].append({
                'timestamp': datetime.utcnow().isoformat(),
                'result': result.__dict__
            })
        
        return sorted(results, key=lambda x: x.weighted_support, reverse=True)
    
    def _evaluate_claim_group(self, claim_text: str, votes: List[ClaimVote]) -> ConsensusResult:
        """Оценка группы голосов за один Claim"""
        support_weight = 0.0
        contradict_weight = 0.0
        total_weight = 0.0
        
        supporting_nodes = []
        contradicting_nodes = []
        domains = set()
        
        for vote in votes:
            # Получаем репутацию узла
            score = get_node_score(vote.node_id, vote.domain)
            trust_score = score.get('composite', 0.7)
            
            # Вес = доверие * уверенность
            weight = trust_score * vote.confidence
            
            total_weight += weight
            domains.add(vote.domain)
            
            if vote.support:
                support_weight += weight
                supporting_nodes.append(vote.node_id)
            else:
                contradict_weight += weight
                contradicting_nodes.append(vote.node_id)
        
        # Вычисляем метрики
        weighted_support = support_weight / max(total_weight, 0.001)
        weighted_confidence = sum(v.confidence for v in votes) / max(len(votes), 1)
        independent_sources = len(set(v.node_id for v in votes))
        
        # Определяем статус
        if weighted_support > 0.7:
            status = "accepted"
        elif weighted_support > 0.4:
            status = "questioned"
        elif weighted_support > 0.2:
            status = "uncertain"
        else:
            status = "rejected"
        
        # Определяем основной домен
        main_domain = max(set(v.domain for v in votes), key=lambda d: sum(1 for v in votes if v.domain == d))
        
        return ConsensusResult(
            claim_id=hashlib.sha256(claim_text.encode()).hexdigest()[:16],
            claim_text=claim_text,
            support_count=len(supporting_nodes),
            contradict_count=len(contradicting_nodes),
            unknown_count=len(votes) - len(supporting_nodes) - len(contradicting_nodes),
            weighted_support=weighted_support,
            weighted_confidence=weighted_confidence,
            independent_sources=independent_sources,
            status=status,
            supporting_nodes=supporting_nodes,
            contradicting_nodes=contradicting_nodes,
            consensus_level=weighted_support,
            domain=main_domain
        )
    
    def _normalize_claim(self, text: str) -> str:
        """Нормализация текста Claim для сравнения"""
        # Приводим к нижнему регистру
        text = text.lower()
        # Удаляем лишние пробелы
        text = ' '.join(text.split())
        # Удаляем пунктуацию
        text = re.sub(r'[^\w\s]', '', text)
        return text
    
    def get_claim_trust(self, claim_text: str) -> Dict:
        """Получить историю доверия к Claim"""
        normalized = self._normalize_claim(claim_text)
        claim_hash = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        
        history = self.claim_history.get(claim_hash, [])
        
        if not history:
            return {
                'claim_id': claim_hash,
                'claim_text': claim_text,
                'verification_count': 0,
                'avg_consensus': 0.0,
                'status': 'never_verified',
                'history': []
            }
        
        avg_consensus = sum(h['result']['consensus_level'] for h in history) / len(history)
        latest = history[-1]['result']
        
        return {
            'claim_id': claim_hash,
            'claim_text': claim_text,
            'verification_count': len(history),
            'avg_consensus': avg_consensus,
            'status': latest['status'],
            'last_verified': history[-1]['timestamp'],
            'history': history[-10:]  # Последние 10 записей
        }
