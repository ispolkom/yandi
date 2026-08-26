"""
agent/trace_evaluator.py — Детальная оценка каждого этапа когнитивного пайплайна.

Оценивает:
- Intent: правильность определения намерения
- Epistemic: правильность классификации (domain, testability, stability)
- Planner: оптимальность плана (шаги, web, валидация)
- Retrieval: качество поиска (источники, релевантность)
- Evidence: качество доказательств
- Claims: качество атомарных утверждений
- Beliefs: консистентность убеждений
- Synthesis: качество синтеза ответа
- Reflection: качество рефлексии

Использование:
    from agent.trace_evaluator import TraceEvaluator, StageScore
    
    evaluator = TraceEvaluator()
    result = evaluator.evaluate(trace)
    print(result.stage_scores)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))


@dataclass
class StageScore:
    """Оценка одного этапа."""
    stage: str
    score: float  # 0.0 - 1.0
    passed: bool
    details: Dict[str, Any] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)


@dataclass
class EvaluationResult:
    """Результат полной оценки."""
    total_score: float
    passed: bool
    stage_scores: List[StageScore]
    summary: str
    recommendations: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_score": self.total_score,
            "passed": self.passed,
            "summary": self.summary,
            "recommendations": self.recommendations,
            "stages": [
                {
                    "stage": s.stage,
                    "score": s.score,
                    "passed": s.passed,
                    "issues": s.issues,
                    "suggestions": s.suggestions
                }
                for s in self.stage_scores
            ]
        }


class TraceEvaluator:
    """
    Детальная оценка каждого этапа когнитивного пайплайна.
    """
    
    def __init__(self, threshold: float = 0.7):
        self.threshold = threshold
        self.stage_weights = {
            "intent": 0.10,
            "epistemic": 0.15,
            "planner": 0.10,
            "retrieval": 0.10,
            "evidence": 0.15,
            "claims": 0.15,
            "beliefs": 0.10,
            "synthesis": 0.10,
            "reflection": 0.05,
        }
    
    def evaluate(self, trace: Dict[str, Any]) -> EvaluationResult:
        """Оценить все этапы трейса."""
        stages = []
        recommendations = []
        
        # 1. Intent
        intent_result = self._evaluate_intent(trace)
        stages.append(intent_result)
        
        # 2. Epistemic
        epistemic_result = self._evaluate_epistemic(trace)
        stages.append(epistemic_result)
        
        # 3. Planner
        planner_result = self._evaluate_planner(trace)
        stages.append(planner_result)
        
        # 4. Retrieval
        retrieval_result = self._evaluate_retrieval(trace)
        stages.append(retrieval_result)
        
        # 5. Evidence
        evidence_result = self._evaluate_evidence(trace)
        stages.append(evidence_result)
        
        # 6. Claims
        claims_result = self._evaluate_claims(trace)
        stages.append(claims_result)
        
        # 7. Beliefs
        beliefs_result = self._evaluate_beliefs(trace)
        stages.append(beliefs_result)
        
        # 8. Synthesis
        synthesis_result = self._evaluate_synthesis(trace)
        stages.append(synthesis_result)
        
        # 9. Reflection
        reflection_result = self._evaluate_reflection(trace)
        stages.append(reflection_result)
        
        # Общий балл
        total = sum(s.score * self.stage_weights.get(s.stage, 0.1) for s in stages)
        
        # Рекомендации
        for stage in stages:
            if not stage.passed:
                recommendations.extend(stage.suggestions)
        
        # Уникальные рекомендации
        recommendations = list(dict.fromkeys(recommendations))
        
        return EvaluationResult(
            total_score=total,
            passed=total >= self.threshold,
            stage_scores=stages,
            summary=self._generate_summary(stages, total),
            recommendations=recommendations[:5]
        )
    
    def _evaluate_intent(self, trace: Dict[str, Any]) -> StageScore:
        """Оценить Intent."""
        issues = []
        suggestions = []
        details = {}
        
        query_trace = trace.get('query_trace', {})
        intent = query_trace.get('intent', '')
        query_type = query_trace.get('query_type', '')
        
        if not intent:
            issues.append("Intent не определён")
            suggestions.append("Добавить определение intent через классификатор")
            score = 0.0
        else:
            score = 0.8
            details['intent'] = intent
            details['query_type'] = query_type
            
            if intent in ['general', 'unknown']:
                issues.append("Intent слишком общий")
                suggestions.append("Уточнить классификацию intent")
                score -= 0.2
            
            if not query_type:
                issues.append("Query type не определён")
                suggestions.append("Добавить определение query_type")
                score -= 0.1
        
        return StageScore(
            stage="intent",
            score=max(0.0, min(1.0, score)),
            passed=score >= self.threshold,
            details=details,
            issues=issues,
            suggestions=suggestions
        )
    
    def _evaluate_epistemic(self, trace: Dict[str, Any]) -> StageScore:
        """Оценить Epistemic классификацию."""
        issues = []
        suggestions = []
        details = {}
        
        epistemic = trace.get('epistemic', {})
        domain = epistemic.get('domain', '')
        testability = epistemic.get('testability', '')
        answer_mode = epistemic.get('answer_mode', '')
        knowledge_stability = epistemic.get('knowledge_stability', '')
        
        if not domain:
            issues.append("Домен не определён")
            suggestions.append("Добавить определение domain в epistemic_router")
            score = 0.0
        else:
            score = 0.7
            details['domain'] = domain
            details['testability'] = testability
            details['answer_mode'] = answer_mode
            details['knowledge_stability'] = knowledge_stability
            
            if domain in ['unknown', 'general']:
                issues.append("Домен слишком общий")
                suggestions.append("Уточнить определение domain")
                score -= 0.2
            
            if not testability or testability in ['unknown']:
                issues.append("Testability не определена")
                suggestions.append("Добавить определение testability")
                score -= 0.2
            
            if not answer_mode or answer_mode in ['unknown']:
                issues.append("Answer mode не определён")
                suggestions.append("Добавить определение answer_mode")
                score -= 0.1
            
            if not knowledge_stability or knowledge_stability in ['unknown']:
                issues.append("Knowledge stability не определена")
                suggestions.append("Добавить определение knowledge_stability")
                score -= 0.1
        
        return StageScore(
            stage="epistemic",
            score=max(0.0, min(1.0, score)),
            passed=score >= self.threshold,
            details=details,
            issues=issues,
            suggestions=suggestions
        )
    
    def _evaluate_planner(self, trace: Dict[str, Any]) -> StageScore:
        """Оценить Planner."""
        issues = []
        suggestions = []
        details = {}
        
        execution = trace.get('execution', [])
        steps = [e.get('step_type', '') for e in execution]
        
        if not steps:
            issues.append("Шаги не найдены")
            suggestions.append("Добавить execution в трейс")
            score = 0.0
        else:
            score = 0.8
            details['step_count'] = len(steps)
            details['steps'] = steps[:10]
            
            # Проверяем наличие web для интерпретативных вопросов
            epistemic = trace.get('epistemic', {})
            domain = epistemic.get('domain', '')
            
            if domain in ['philosophical', 'interpretive', 'media_interpretation']:
                if 'web_query' in steps or 'web_scrape' in steps:
                    issues.append("Интерпретативный вопрос использует web-поиск")
                    suggestions.append("Для интерпретативных вопросов отключать web-поиск")
                    score -= 0.3
            
            # Проверяем наличие валидации для высокого доверия
            trust = trace.get('trust', '')
            if trust in ['STRONGLY_SUPPORTED', 'VERIFIED']:
                if 'validate' not in steps:
                    issues.append("Высокий trust без шага валидации")
                    suggestions.append("Добавлять validate для высокого trust")
                    score -= 0.2
            
            # Проверяем наличие synthesize
            if 'synthesize' not in steps:
                issues.append("Нет шага synthesize")
                suggestions.append("Всегда добавлять synthesize")
                score -= 0.5
        
        return StageScore(
            stage="planner",
            score=max(0.0, min(1.0, score)),
            passed=score >= self.threshold,
            details=details,
            issues=issues,
            suggestions=suggestions
        )
    
    def _evaluate_retrieval(self, trace: Dict[str, Any]) -> StageScore:
        """Оценить Retrieval (поиск)."""
        issues = []
        suggestions = []
        details = {}
        
        evidence = trace.get('evidence', [])
        
        if not evidence:
            issues.append("Нет evidence (поиск не дал результатов)")
            suggestions.append("Улучшить поиск или расширить источники")
            score = 0.2
        else:
            score = 0.7
            details['evidence_count'] = len(evidence)
            
            # Проверяем наличие rejected
            rejected = sum(1 for ev in evidence if ev.get('rejection_reason'))
            details['rejected'] = rejected
            
            # Проверяем source_uri
            has_sources = sum(1 for ev in evidence if ev.get('source_uri'))
            if has_sources == 0:
                issues.append("Нет source_uri в evidence")
                suggestions.append("Добавлять source_uri для каждого evidence")
                score -= 0.3
            
            # Проверяем релевантность
            high_rel = sum(1 for ev in evidence if ev.get('relevance_to_query', 0) > 0.6)
            if high_rel == 0 and len(evidence) > 0:
                issues.append("Нет evidence с высокой релевантностью")
                suggestions.append("Улучшить фильтрацию evidence по релевантности")
                score -= 0.2
        
        return StageScore(
            stage="retrieval",
            score=max(0.0, min(1.0, score)),
            passed=score >= self.threshold,
            details=details,
            issues=issues,
            suggestions=suggestions
        )
    
    def _evaluate_evidence(self, trace: Dict[str, Any]) -> StageScore:
        """Оценить Evidence."""
        issues = []
        suggestions = []
        details = {}
        
        evidence = trace.get('evidence', [])
        
        if not evidence:
            issues.append("Нет evidence")
            suggestions.append("Улучшить поиск evidence")
            score = 0.0
        else:
            score = 0.7
            details['evidence_count'] = len(evidence)
            
            # Проверяем content_excerpt
            has_content = sum(1 for ev in evidence if ev.get('content_excerpt') and len(ev.get('content_excerpt', '')) > 100)
            details['has_content'] = has_content
            
            if has_content == 0:
                issues.append("Нет content_excerpt в evidence")
                suggestions.append("Добавлять content_excerpt для каждого evidence")
                score = 0.1
            else:
                # Оценка качества контента
                quality_ratio = has_content / len(evidence)
                score = 0.4 + 0.5 * quality_ratio
                details['quality_ratio'] = quality_ratio
        
        return StageScore(
            stage="evidence",
            score=max(0.0, min(1.0, score)),
            passed=score >= self.threshold,
            details=details,
            issues=issues,
            suggestions=suggestions
        )
    
    def _evaluate_claims(self, trace: Dict[str, Any]) -> StageScore:
        """Оценить Claims."""
        issues = []
        suggestions = []
        details = {}
        
        claims = trace.get('claims', [])
        rejected = trace.get('rejected_claims', [])
        
        if not claims:
            issues.append("Нет claims")
            suggestions.append("Добавить извлечение claims из evidence")
            score = 0.0
        else:
            score = 0.7
            details['claims_count'] = len(claims)
            details['rejected_count'] = len(rejected)
            
            # Проверяем claim_text
            world_claims = 0
            for claim in claims:
                text = claim.get('claim_text', '')
                if 'Вопрос пользователя' not in text and 'Матрица фильтрации' not in text:
                    world_claims += 1
            
            details['world_claims'] = world_claims
            
            if world_claims == 0:
                issues.append("Нет WORLD CLAIMS (только мета-текст)")
                suggestions.append("Улучшить извлечение WORLD CLAIMS из evidence")
                score = 0.1
            else:
                quality_ratio = world_claims / len(claims)
                score = 0.4 + 0.5 * quality_ratio
                details['quality_ratio'] = quality_ratio
            
            # Проверяем claim_confidence
            has_confidence = sum(1 for claim in claims if claim.get('claim_confidence', 0) > 0)
            if has_confidence == 0:
                issues.append("Нет claim_confidence")
                suggestions.append("Добавить confidence для каждого claim")
                score -= 0.2
        
        return StageScore(
            stage="claims",
            score=max(0.0, min(1.0, score)),
            passed=score >= self.threshold,
            details=details,
            issues=issues,
            suggestions=suggestions
        )
    
    def _evaluate_beliefs(self, trace: Dict[str, Any]) -> StageScore:
        """Оценить Beliefs."""
        issues = []
        suggestions = []
        details = {}
        
        beliefs = trace.get('beliefs', trace.get('belief_update', {}))
        
        if not beliefs:
            issues.append("Нет данных об убеждениях")
            suggestions.append("Добавить сохранение beliefs в трейс")
            score = 0.5
        else:
            score = 0.8
            if isinstance(beliefs, dict):
                details['beliefs_count'] = len(beliefs)
                for topic, data in beliefs.items():
                    if isinstance(data, dict):
                        details[f'{topic}_conf'] = data.get('confidence', 0)
            elif isinstance(beliefs, list):
                details['beliefs_count'] = len(beliefs)
        
        return StageScore(
            stage="beliefs",
            score=max(0.0, min(1.0, score)),
            passed=score >= self.threshold,
            details=details,
            issues=issues,
            suggestions=suggestions
        )
    
    def _evaluate_synthesis(self, trace: Dict[str, Any]) -> StageScore:
        """Оценить Synthesis."""
        issues = []
        suggestions = []
        details = {}
        
        outcome = trace.get('outcome', {})
        answer = outcome.get('final_answer', trace.get('final_answer', ''))
        
        if not answer or len(answer) < 20:
            issues.append("Ответ слишком короткий или отсутствует")
            suggestions.append("Улучшить синтез ответа из claims")
            score = 0.0
        else:
            score = 0.8
            details['answer_length'] = len(answer)
            
            # Проверяем наличие ссылок
            if '[' in answer and ']' in answer:
                details['has_citations'] = True
            else:
                issues.append("Нет ссылок на источники в ответе")
                suggestions.append("Добавлять ссылки на источники в ответ")
                score -= 0.1
            
            # Проверяем trust
            trust = outcome.get('trust_label', trace.get('trust', ''))
            if trust in ['STRONGLY_SUPPORTED', 'VERIFIED']:
                details['trust_label'] = trust
                score = min(1.0, score + 0.1)
            elif trust in ['UNVERIFIED', 'HYPOTHESIS']:
                issues.append(f"Низкий trust: {trust}")
                suggestions.append("Повысить доверие к ответу через валидацию")
                score -= 0.2
        
        return StageScore(
            stage="synthesis",
            score=max(0.0, min(1.0, score)),
            passed=score >= self.threshold,
            details=details,
            issues=issues,
            suggestions=suggestions
        )
    
    def _evaluate_reflection(self, trace: Dict[str, Any]) -> StageScore:
        """Оценить Reflection."""
        issues = []
        suggestions = []
        details = {}
        
        reflection = trace.get('reflection', {})
        learning = trace.get('learning', [])
        
        if not reflection and not learning:
            issues.append("Нет данных рефлексии")
            suggestions.append("Добавить рефлексию в пайплайн")
            score = 0.4
        else:
            score = 0.8
            if reflection:
                details['has_reflection'] = True
                if isinstance(reflection, dict):
                    if reflection.get('was_correct') is not None:
                        details['was_correct'] = reflection.get('was_correct')
            
            if learning:
                details['learning_count'] = len(learning)
                score = min(1.0, score + 0.1 * min(len(learning), 3))
        
        return StageScore(
            stage="reflection",
            score=max(0.0, min(1.0, score)),
            passed=score >= self.threshold,
            details=details,
            issues=issues,
            suggestions=suggestions
        )
    
    def _generate_summary(self, stages: List[StageScore], total: float) -> str:
        """Сгенерировать краткое резюме."""
        failed = [s for s in stages if not s.passed]
        if failed:
            failed_names = ', '.join([f"{s.stage} ({s.score:.2f})" for s in failed])
            return f"❌ НЕ ПРОЙДЕН: {failed_names}"
        return f"✅ ПРОЙДЕН (общий балл: {total:.2f})"


if __name__ == "__main__":
    # Тестирование
    print("=" * 60)
    print("TEST: TraceEvaluator")
    print("=" * 60)
    
    test_trace = {
        "trace_id": "test_001",
        "query": "Что такое сознание?",
        "query_trace": {"intent": "definition", "query_type": "definitional"},
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
                "source_uri": "https://example.com",
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
    
    evaluator = TraceEvaluator()
    result = evaluator.evaluate(test_trace)
    
    print(f"\nОбщий балл: {result.total_score:.2f}")
    print(f"Пройден: {result.passed}")
    print(f"\nРезюме: {result.summary}")
    print("\nДетально:")
    for stage in result.stage_scores:
        status = "✅" if stage.passed else "❌"
        print(f"  {status} {stage.stage}: {stage.score:.2f}")
        if stage.issues:
            for issue in stage.issues:
                print(f"      ⚠️ {issue}")
        if stage.suggestions:
            for suggestion in stage.suggestions[:2]:
                print(f"      💡 {suggestion}")
    
    if result.recommendations:
        print("\nРекомендации:")
        for rec in result.recommendations:
            print(f"  • {rec}")
