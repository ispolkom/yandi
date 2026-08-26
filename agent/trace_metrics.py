"""
agent/trace_metrics.py — Метрики качества для Trace Evaluation Framework.

Оценивает каждый этап когнитивного пайплайна:
- Intent Accuracy
- Epistemic Accuracy
- Planner Efficiency
- Evidence Quality
- Claim Quality
- Belief Consistency
- Answer Quality
- Reflection Quality

Использование:
    from agent.trace_metrics import TraceMetrics
    
    metrics = TraceMetrics()
    score = metrics.evaluate(trace, testcase)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))


@dataclass
class MetricScore:
    """Оценка одного этапа."""
    name: str
    score: float  # 0.0 - 1.0
    weight: float = 1.0
    details: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class TraceScore:
    """Полная оценка трейса."""
    total_score: float
    metrics: List[MetricScore]
    passed: bool
    summary: str
    
    # Детальные оценки
    intent_score: float = 0.0
    epistemic_score: float = 0.0
    planner_score: float = 0.0
    evidence_score: float = 0.0
    claim_score: float = 0.0
    belief_score: float = 0.0
    answer_score: float = 0.0
    reflection_score: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_score": self.total_score,
            "passed": self.passed,
            "summary": self.summary,
            "metrics": [
                {"name": m.name, "score": m.score, "details": m.details}
                for m in self.metrics
            ]
        }


class TraceMetrics:
    """
    Оценка качества трейса.
    """
    
    def __init__(self):
        self.weights = {
            "intent": 0.10,
            "epistemic": 0.15,
            "planner": 0.10,
            "evidence": 0.15,
            "claim": 0.15,
            "belief": 0.10,
            "answer": 0.15,
            "reflection": 0.10,
        }
        self.threshold = 0.7  # минимальный проходной балл
    
    def evaluate(self, trace: Dict[str, Any], testcase: Optional[Dict[str, Any]] = None) -> TraceScore:
        """
        Оценить трейс.
        
        Args:
            trace: трейс из orchestrator
            testcase: ожидаемый результат (опционально)
        
        Returns:
            TraceScore
        """
        metrics = []
        
        # 1. Intent
        intent_score = self._evaluate_intent(trace, testcase)
        metrics.append(intent_score)
        
        # 2. Epistemic
        epistemic_score = self._evaluate_epistemic(trace, testcase)
        metrics.append(epistemic_score)
        
        # 3. Planner
        planner_score = self._evaluate_planner(trace, testcase)
        metrics.append(planner_score)
        
        # 4. Evidence
        evidence_score = self._evaluate_evidence(trace, testcase)
        metrics.append(evidence_score)
        
        # 5. Claims
        claim_score = self._evaluate_claims(trace, testcase)
        metrics.append(claim_score)
        
        # 6. Beliefs
        belief_score = self._evaluate_beliefs(trace, testcase)
        metrics.append(belief_score)
        
        # 7. Answer
        answer_score = self._evaluate_answer(trace, testcase)
        metrics.append(answer_score)
        
        # 8. Reflection
        reflection_score = self._evaluate_reflection(trace, testcase)
        metrics.append(reflection_score)
        
        # Общий балл
        total = sum(
            m.score * self.weights.get(m.name, 0.1)
            for m in metrics
        ) / sum(self.weights.values())
        
        passed = total >= self.threshold
        
        # Формируем summary
        summary = self._generate_summary(metrics, total, passed)
        
        return TraceScore(
            total_score=total,
            metrics=metrics,
            passed=passed,
            summary=summary,
            intent_score=intent_score.score,
            epistemic_score=epistemic_score.score,
            planner_score=planner_score.score,
            evidence_score=evidence_score.score,
            claim_score=claim_score.score,
            belief_score=belief_score.score,
            answer_score=answer_score.score,
            reflection_score=reflection_score.score,
        )
    
    def _evaluate_intent(self, trace: Dict[str, Any], testcase: Optional[Dict[str, Any]]) -> MetricScore:
        """Оценить Intent."""
        errors = []
        warnings = []
        details = {}
        
        query_trace = trace.get('query_trace', {})
        intent = query_trace.get('intent', '')
        
        if not intent:
            errors.append("Intent не определён")
            score = 0.0
        else:
            score = 0.8
            details['intent'] = intent
            
            if intent in ['general', 'unknown']:
                warnings.append("Intent слишком общий")
                score -= 0.2
            
            if testcase and 'expected_intent' in testcase:
                expected = testcase['expected_intent']
                if intent == expected:
                    score = 1.0
                elif intent in expected or expected in intent:
                    score = 0.7
                    warnings.append(f"Intent частично совпадает: {intent} vs {expected}")
                else:
                    score = 0.3
                    errors.append(f"Intent не совпадает: {intent} vs {expected}")
        
        score = max(0.0, min(1.0, score))
        return MetricScore(
            name="intent",
            score=score,
            details=details,
            errors=errors,
            warnings=warnings
        )
    
    def _evaluate_epistemic(self, trace: Dict[str, Any], testcase: Optional[Dict[str, Any]]) -> MetricScore:
        """Оценить Epistemic классификацию."""
        errors = []
        warnings = []
        details = {}
        
        epistemic = trace.get('epistemic', {})
        domain = epistemic.get('domain', '')
        testability = epistemic.get('testability', '')
        answer_mode = epistemic.get('answer_mode', '')
        knowledge_stability = epistemic.get('knowledge_stability', '')
        
        if not domain:
            errors.append("Домен не определён")
            score = 0.0
        else:
            score = 0.7
            details['domain'] = domain
            details['testability'] = testability
            details['answer_mode'] = answer_mode
            details['knowledge_stability'] = knowledge_stability
            
            if domain in ['unknown', 'general']:
                warnings.append("Домен слишком общий")
                score -= 0.2
            
            if testability in ['', 'unknown']:
                warnings.append("Testability не определена")
                score -= 0.2
            
            if testcase:
                if 'expected_domain' in testcase and testcase['expected_domain'] != domain:
                    if testcase['expected_domain'] in domain or domain in testcase['expected_domain']:
                        score = 0.6
                        warnings.append(f"Домен частично совпадает: {domain} vs {testcase['expected_domain']}")
                    else:
                        score = 0.3
                        errors.append(f"Домен не совпадает: {domain} vs {testcase['expected_domain']}")
                
                if 'expected_testability' in testcase and testcase['expected_testability'] != testability:
                    warnings.append(f"Testability не совпадает: {testability} vs {testcase['expected_testability']}")
        
        score = max(0.0, min(1.0, score))
        return MetricScore(
            name="epistemic",
            score=score,
            details=details,
            errors=errors,
            warnings=warnings
        )
    
    def _evaluate_planner(self, trace: Dict[str, Any], testcase: Optional[Dict[str, Any]]) -> MetricScore:
        """Оценить Planner."""
        errors = []
        warnings = []
        details = {}
        
        execution = trace.get('execution', [])
        steps = [e.get('step', e.get('step_type', '')) for e in execution]
        
        if not steps:
            errors.append("Шаги не найдены")
            score = 0.0
        else:
            score = 0.8
            details['step_count'] = len(steps)
            details['steps'] = steps[:10]
            
            epistemic = trace.get('epistemic', {})
            domain = epistemic.get('domain', '')
            
            if domain in ['philosophical', 'interpretive', 'media_interpretation'] and 'web_query' in steps:
                warnings.append("Интерпретативный вопрос использует web-поиск")
                score -= 0.3
            
            outcome = trace.get('outcome', {})
            trust = outcome.get('trust_label', '')
            if trust in ['STRONGLY_SUPPORTED', 'VERIFIED']:
                if 'validate' not in steps and 'arbitrate' not in steps:
                    warnings.append("Высокий trust без валидации")
                    score -= 0.2
            
            if 'synthesize' not in steps:
                errors.append("Нет шага synthesize")
                score -= 0.5
            
            if len(steps) > 15:
                warnings.append(f"Много шагов: {len(steps)}")
                score -= 0.1
        
        score = max(0.0, min(1.0, score))
        return MetricScore(
            name="planner",
            score=score,
            details=details,
            errors=errors,
            warnings=warnings
        )
    
    def _evaluate_evidence(self, trace: Dict[str, Any], testcase: Optional[Dict[str, Any]]) -> MetricScore:
        """Оценить Evidence."""
        errors = []
        warnings = []
        details = {}
        
        evidence = trace.get('evidence', [])
        
        if not evidence:
            errors.append("Нет evidence")
            score = 0.0
        else:
            score = 0.7
            details['evidence_count'] = len(evidence)
            
            good_evidence = 0
            rejected = 0
            for ev in evidence:
                if ev.get('rejection_reason'):
                    rejected += 1
                elif ev.get('content_excerpt') and len(ev.get('content_excerpt', '')) > 100:
                    good_evidence += 1
            
            details['good_evidence'] = good_evidence
            details['rejected_evidence'] = rejected
            
            if good_evidence == 0:
                errors.append("Нет качественного evidence")
                score = 0.1
            else:
                quality_ratio = good_evidence / len(evidence)
                score = 0.5 + 0.4 * quality_ratio
                details['quality_ratio'] = quality_ratio
            
            # Проверяем relevance
            high_relevance = sum(1 for ev in evidence if ev.get('relevance_to_query', 0) > 0.6)
            if high_relevance > 0:
                details['high_relevance'] = high_relevance
            else:
                warnings.append("Нет evidence с высокой релевантностью")
                score -= 0.1
        
        score = max(0.0, min(1.0, score))
        return MetricScore(
            name="evidence",
            score=score,
            details=details,
            errors=errors,
            warnings=warnings
        )
    
    def _evaluate_claims(self, trace: Dict[str, Any], testcase: Optional[Dict[str, Any]]) -> MetricScore:
        """Оценить Claims."""
        errors = []
        warnings = []
        details = {}
        
        claims = trace.get('claims', [])
        rejected_claims = trace.get('rejected_claims', [])
        
        if not claims:
            errors.append("Нет claims")
            score = 0.0
        else:
            score = 0.7
            details['claims_count'] = len(claims)
            details['rejected_claims_count'] = len(rejected_claims)
            
            # Проверяем качество claims
            world_claims = 0
            for claim in claims:
                text = claim.get('claim_text', '')
                if 'Вопрос пользователя' not in text and 'Матрица фильтрации' not in text:
                    world_claims += 1
            
            details['world_claims'] = world_claims
            
            if world_claims == 0:
                errors.append("Нет WORLD CLAIMS (только мета-текст)")
                score = 0.1
            else:
                quality_ratio = world_claims / len(claims)
                score = 0.4 + 0.5 * quality_ratio
                details['quality_ratio'] = quality_ratio
            
            # Проверяем confidence
            high_conf = sum(1 for claim in claims if claim.get('claim_confidence', 0) > 0.6)
            if high_conf > 0:
                details['high_confidence_claims'] = high_conf
            else:
                warnings.append("Нет claims с высокой уверенностью")
                score -= 0.1
        
        score = max(0.0, min(1.0, score))
        return MetricScore(
            name="claim",
            score=score,
            details=details,
            errors=errors,
            warnings=warnings
        )
    
    def _evaluate_beliefs(self, trace: Dict[str, Any], testcase: Optional[Dict[str, Any]]) -> MetricScore:
        """Оценить Beliefs."""
        errors = []
        warnings = []
        details = {}
        
        beliefs = trace.get('beliefs', trace.get('belief_update', {}))
        
        if not beliefs:
            warnings.append("Нет данных об убеждениях")
            score = 0.5  # нейтральная оценка
        else:
            score = 0.7
            if isinstance(beliefs, dict):
                details['beliefs_count'] = len(beliefs)
                for topic, data in beliefs.items():
                    if isinstance(data, dict):
                        confidence = data.get('confidence', 0)
                        if confidence > 0.8:
                            details[f'{topic}_conf'] = confidence
            elif isinstance(beliefs, list):
                details['beliefs_count'] = len(beliefs)
        
        score = max(0.0, min(1.0, score))
        return MetricScore(
            name="belief",
            score=score,
            details=details,
            errors=errors,
            warnings=warnings
        )
    
    def _evaluate_answer(self, trace: Dict[str, Any], testcase: Optional[Dict[str, Any]]) -> MetricScore:
        """Оценить Answer."""
        errors = []
        warnings = []
        details = {}
        
        outcome = trace.get('outcome', {})
        answer = outcome.get('final_answer', trace.get('final_answer', ''))
        
        if not answer or len(answer) < 20:
            errors.append("Ответ слишком короткий или отсутствует")
            score = 0.0
        else:
            score = 0.8
            details['answer_length'] = len(answer)
            
            # Проверяем наличие ссылок на источники
            if '[' in answer and ']' in answer:
                details['has_citations'] = True
            else:
                warnings.append("Нет ссылок на источники")
                score -= 0.1
            
            # Проверяем доверие
            trust = outcome.get('trust_label', trace.get('trust', ''))
            if trust in ['STRONGLY_SUPPORTED', 'VERIFIED']:
                details['trust_label'] = trust
                score = min(1.0, score + 0.1)
            elif trust in ['UNVERIFIED', 'HYPOTHESIS']:
                warnings.append(f"Низкий trust: {trust}")
                score -= 0.2
            
            # Проверяем, ответил ли на вопрос
            query = trace.get('query', '')
            if query and len(query) > 10:
                # Простая проверка: ответ должен быть длиннее запроса
                if len(answer) < len(query) * 1.5:
                    warnings.append("Ответ короче запроса, возможно неполный")
                    score -= 0.1
        
        score = max(0.0, min(1.0, score))
        return MetricScore(
            name="answer",
            score=score,
            details=details,
            errors=errors,
            warnings=warnings
        )
    
    def _evaluate_reflection(self, trace: Dict[str, Any], testcase: Optional[Dict[str, Any]]) -> MetricScore:
        """Оценить Reflection."""
        errors = []
        warnings = []
        details = {}
        
        reflection = trace.get('reflection', {})
        learning = trace.get('learning', [])
        
        if not reflection and not learning:
            warnings.append("Нет данных рефлексии")
            score = 0.5
        else:
            score = 0.7
            
            if reflection:
                details['has_reflection'] = True
                if isinstance(reflection, dict):
                    if reflection.get('was_correct') is not None:
                        details['was_correct'] = reflection.get('was_correct')
            
            if learning:
                details['learning_count'] = len(learning)
                score = min(1.0, score + 0.1 * min(len(learning), 3))
        
        score = max(0.0, min(1.0, score))
        return MetricScore(
            name="reflection",
            score=score,
            details=details,
            errors=errors,
            warnings=warnings
        )
    
    def _generate_summary(self, metrics: List[MetricScore], total: float, passed: bool) -> str:
        """Сгенерировать краткое резюме."""
        if passed:
            status = "✅ ПРОЙДЕН"
        else:
            status = "❌ НЕ ПРОЙДЕН"
        
        # Находим слабые места
        weak = [m for m in metrics if m.score < 0.6]
        strong = [m for m in metrics if m.score > 0.85]
        
        summary = f"{status} (общий балл: {total:.2f})"
        
        if weak:
            weak_names = ', '.join([f"{m.name} ({m.score:.2f})" for m in weak])
            summary += f"\n⚠️ Слабые места: {weak_names}"
        
        if strong:
            strong_names = ', '.join([f"{m.name} ({m.score:.2f})" for m in strong])
            summary += f"\n✅ Сильные стороны: {strong_names}"
        
        return summary


if __name__ == "__main__":
    # Тестирование
    print("=" * 60)
    print("TEST: TraceMetrics")
    print("=" * 60)
    
    # Создаём тестовый трейс
    test_trace = {
        "trace_id": "test_001",
        "query": "Что такое сознание?",
        "query_trace": {"intent": "definition"},
        "epistemic": {
            "domain": "philosophical",
            "testability": "interpretive",
            "answer_mode": "pluralistic_contextual",
            "knowledge_stability": "controversial"
        },
        "execution": [
            {"step_type": "cache_check"},
            {"step_type": "risk_assess"},
            {"step_type": "intent"},
            {"step_type": "enrich"},
            {"step_type": "local_search"},
            {"step_type": "synthesize"},
            {"step_type": "optimistic_respond"}
        ],
        "evidence": [
            {
                "evidence_id": "ev_001",
                "content_excerpt": "Сознание — это сложное философское понятие...",
                "relevance_to_query": 0.8
            }
        ],
        "claims": [
            {
                "claim_id": "cl_001",
                "claim_text": "Сознание — это субъективный опыт",
                "claim_confidence": 0.7
            }
        ],
        "rejected_claims": [],
        "beliefs": {"consciousness": {"confidence": 0.6}},
        "outcome": {
            "final_answer": "Сознание — это сложное философское понятие, которое описывает субъективный опыт...",
            "trust_label": "PARTIALLY_SUPPORTED"
        },
        "reflection": {"was_correct": True},
        "learning": [{"type": "reflection", "rule": "test"}],
        "trust": "PARTIALLY_SUPPORTED"
    }
    
    metrics = TraceMetrics()
    score = metrics.evaluate(test_trace)
    
    print(f"\nОбщий балл: {score.total_score:.2f}")
    print(f"Пройден: {score.passed}")
    print(f"\nДетально:")
    for m in score.metrics:
        status = "✅" if m.score >= 0.7 else "⚠️" if m.score >= 0.5 else "❌"
        print(f"  {status} {m.name}: {m.score:.2f}")
        if m.warnings:
            for w in m.warnings:
                print(f"      ⚠️ {w}")
        if m.errors:
            for e in m.errors:
                print(f"      ❌ {e}")
    
    print(f"\nРезюме:\n{score.summary}")
