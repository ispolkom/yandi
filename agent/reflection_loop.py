"""
agent/reflection_loop.py — Рефлексивный цикл для YANDI V7.

Внутренний диалог системы:
- анализ собственных решений
- оценка качества ответов
- выявление ошибок
- генерация уроков
- обновление self_model и памяти
- ПРИМЕНЕНИЕ УРОКОВ К ПЛАНИРОВЩИКУ (Planner Update)

Цель: система учится на собственном опыте и меняет своё поведение.

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/reflection_policies.json
is retired, not migrated — old JSON was disposable test-era cruft.
Policy state now lives exclusively in reflection_policy (class C,
mutable current-state projection) — agent/db/sql/schema.py. Every
get_policies() call now returns a genuinely FRESH list of dicts from a
SQL query — the old JSON version's "returns active_policies BY
REFERENCE" aliasing hazard (agent/orch_planner.py's own comment
documents a real incident: ad-hoc entries silently leaking into the
live JSON file through a shared list reference) is now structurally
impossible, not just guarded against by callers remembering to copy.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here. There is no JSON fallback left to quietly succeed against.
"""

from __future__ import annotations

import sys
from pathlib import Path
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List

from agent.self_model import get_self_model, SelfModel
from agent.memory_episodic import get_memory, EpisodicMemory
from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


@dataclass
class ReflectionResult:
    """Результат рефлексии одного действия."""
    timestamp: float
    action_id: str
    action_type: str  # query | decision | synthesize | validate
    
    # Анализ
    was_correct: bool
    confidence: float
    alternatives: List[str]
    chosen_path: str
    why_chosen: str
    
    # Ошибки
    mistakes: List[str]
    missing_information: List[str]
    
    # Уроки
    lessons: List[str]
    should_change_behavior: bool
    suggested_policy_change: Optional[str] = None
    applied_policy_changes: List[str] = field(default_factory=list)
    
    # Состояние после рефлексии
    self_state_after: Dict[str, Any] = field(default_factory=dict)
    
    # Влияние на будущее
    future_actions: List[str] = field(default_factory=list)


class ReflectionLoop:
    """Цикл рефлексии — анализ решений, обучение и применение изменений."""
    
    def __init__(self):
        self.self_model = get_self_model()
        self.memory = get_memory()
        self.reflection_count = 0

        # История рефлексий
        self.reflections: List[ReflectionResult] = []

    # Foundation Repair P0-1 safety gate: a brand-new policy must recur this
    # many times (independent triggers, i.e. separate reflect_on_query calls)
    # before it is allowed to affect production Planner behavior. This is
    # NOT a PolicyHypothesis/Shadow/Experiment lifecycle (explicitly out of
    # scope for Foundation Repair) — just the minimal gate needed to stop a
    # single mistake occurrence from immediately mutating production plans.
    _MIN_OBSERVATIONS_TO_ACTIVATE = 3

    def _apply_policy_to_planner(self, policy: Dict[str, Any]) -> bool:
        """
        Применить политику к планировщику.
        Возвращает True, если политика ПРИМЕНЕНА (status == "active").

        Foundation Repair P0-1: repetition of the same rule no longer
        inflates `confidence` unconditionally (was: +0.1 per repeat, capped
        at 1.0, with no check that the rule ever actually helped — a
        self-reinforcing loop with no delayed-outcome signal, in violation
        of ROADMAP_v7 invariants 1.12/1.13). Repetition now only grows
        `observed_count` (observation/накопление is preserved, per the
        Foundation Repair brief). A policy only starts affecting the
        Planner once it has recurred `_MIN_OBSERVATIONS_TO_ACTIVATE` times
        (status flips "observed" -> "active"); `confidence` is fixed at
        creation time and never self-inflates afterward.
        """
        policy_type = policy.get("type")
        rule = policy.get("rule")

        with get_connection() as conn:
            # Проверяем, не существует ли уже такая политика
            existing = repo.find_reflection_policy_by_rule(conn, rule)
            if existing:
                new_count = existing["observed_count"] + 1
                activate = existing["status"] != "active" and new_count >= self._MIN_OBSERVATIONS_TO_ACTIVATE
                repo.bump_reflection_policy_observed(conn, existing["policy_id"], activate=activate)
                conn.commit()
                return activate or existing["status"] == "active"

            # Новая политика — фиксируем confidence на момент создания (больше
            # не растёт от одного лишь повторения) и НЕ применяем к planner,
            # пока не накопится _MIN_OBSERVATIONS_TO_ACTIVATE независимых
            # повторений одного и того же rule.
            policy_id = f"pol_{uuid.uuid4().hex[:8]}"
            repo.create_reflection_policy(conn, policy_id, policy_type, rule, policy.get("confidence", 0.7))
            conn.commit()
        return False
    
    def reflect_on_query(self, query: str, response: str, 
                         epistemic: Dict[str, Any], 
                         trust: str, confidence: float,
                         errors: Optional[List[str]] = None,
                         validation_result: Optional[Dict[str, Any]] = None,
                         context: Optional[Dict[str, Any]] = None) -> ReflectionResult:
        """
        Рефлексия над запросом и ответом.
        """
        self.self_model.increment_reflections()
        self.reflection_count += 1
        
        action_id = f"ref_{uuid.uuid4().hex[:12]}"
        
        # Анализ правильности
        was_correct = self._evaluate_response(epistemic, trust, confidence, errors)
        
        # Альтернативы
        alternatives = self._generate_alternatives(query, epistemic)
        
        # Причины выбора
        why_chosen = self._explain_choice(epistemic)
        
        # Ошибки
        mistakes = self._identify_mistakes(epistemic, trust, confidence, errors)
        
        # Пропущенная информация
        missing = self._identify_missing(epistemic, query)
        
        # Уроки
        lessons = self._extract_lessons(mistakes, was_correct, context, validation_result)
        
        # Предложение изменений
        policy_change = self._suggest_policy_change(mistakes, epistemic) if mistakes else None
        
        # Применяем политику
        applied_changes = []
        if policy_change:
            policy = {
                "type": "behavioral",
                "rule": policy_change,
                "confidence": 0.7 if len(mistakes) > 1 else 0.5,
            }
            if self._apply_policy_to_planner(policy):
                applied_changes.append(policy_change)
        
        # Планируем будущие действия
        future_actions = self._plan_future_actions(mistakes, lessons, epistemic)
        
        result = ReflectionResult(
            timestamp=time.time(),
            action_id=action_id,
            action_type="query",
            was_correct=was_correct,
            confidence=confidence,
            alternatives=alternatives,
            chosen_path=epistemic.get("answer_mode", "unknown"),
            why_chosen=why_chosen,
            mistakes=mistakes,
            missing_information=missing,
            lessons=lessons,
            should_change_behavior=len(mistakes) > 0 or confidence < 0.5,
            suggested_policy_change=policy_change,
            applied_policy_changes=applied_changes,
            self_state_after=self.self_model.reflect(),
            future_actions=future_actions,
        )
        
        self.reflections.append(result)
        
        # Сохраняем в эпизодическую память
        for lesson in lessons:
            self.memory.add_learning(lesson, {
                "query": query[:60],
                "epistemic": epistemic,
                "trust": trust,
                "confidence": confidence,
                "applied_changes": applied_changes,
            })
        
        if mistakes:
            for mistake in mistakes:
                self.memory.add_error(mistake, {
                    "query": query[:60],
                    "context": context or {},
                    "applied_changes": applied_changes,
                })
        
        # Обновляем self_model - используем правильные методы
        if result.should_change_behavior:
            for lesson in lessons[:2]:
                # Используем add_learning вместо add_lesson
                self.self_model.add_learning(lesson, f"reflection_{action_id}", importance=0.7)
            if applied_changes:
                self.self_model.add_learning(
                    f"Применены изменения: {', '.join(applied_changes)}",
                    f"reflection_{action_id}",
                    importance=0.8
                )
        
        # Добавляем саму рефлексию в self_model
        self.self_model.add_reflection({
            "action_id": action_id,
            "query": query[:60],
            "was_correct": was_correct,
            "confidence": confidence,
            "mistakes": mistakes,
            "lessons": lessons,
            "applied_changes": applied_changes,
        })
        
        return result
    
    def _plan_future_actions(self, mistakes: List[str], lessons: List[str], epistemic: Dict[str, Any]) -> List[str]:
        """Спланировать будущие действия на основе рефлексии."""
        actions = []
        
        if not mistakes:
            actions.append("повторить текущую стратегию")
        else:
            actions.append("пересмотреть стратегию поиска")
        
        if any("web" in m.lower() for m in mistakes):
            actions.append("проверить необходимость web-поиска перед использованием")
        
        if epistemic.get("domain") in ["philosophical", "interpretive"]:
            actions.append("для интерпретативных вопросов использовать pluralistic_contextual")
        
        if epistemic.get("testability") == "fully_testable" and epistemic.get("confidence", 0) < 0.6:
            actions.append("увеличить количество источников для проверяемых вопросов")
        
        return actions[:3]
    
    def _evaluate_response(self, epistemic: Dict[str, Any], 
                           trust: str, confidence: float,
                           errors: Optional[List[str]]) -> bool:
        """Оценить, был ли ответ правильным."""
        if errors:
            return False
        
        if trust == "UNVERIFIED" and confidence < 0.3:
            return False
        
        if epistemic.get("testability") == "fully_testable" and confidence < 0.5:
            return False
        
        return True
    
    def _generate_alternatives(self, query: str, epistemic: Dict[str, Any]) -> List[str]:
        """Сгенерировать альтернативные варианты ответа."""
        alternatives = []
        
        domain = epistemic.get("domain", "unknown")
        testability = epistemic.get("testability", "unknown")
        current_mode = epistemic.get("answer_mode", "unknown")
        
        if domain == "philosophical":
            alternatives.append("factual — если бы были подтверждённые данные")
            alternatives.append("pluralistic_contextual — уже выбрано")
        elif domain == "factual":
            alternatives.append("pluralistic_contextual — если бы вопрос был интерпретативным")
            alternatives.append("qualified_factual — если бы была неопределённость")
        
        if testability == "interpretive":
            alternatives.append("exploratory — если бы данных было ещё меньше")
            alternatives.append("dialogic — если бы нужно было уточнить")
        elif testability == "fully_testable":
            alternatives.append("qualified_factual — если бы источники были спорными")
        
        return alternatives[:4]
    
    def _explain_choice(self, epistemic: Dict[str, Any]) -> str:
        """Объяснить, почему выбран этот путь."""
        domain = epistemic.get("domain", "unknown")
        testability = epistemic.get("testability", "unknown")
        mode = epistemic.get("answer_mode", "unknown")
        reason = epistemic.get("reason", "")
        
        if reason:
            return f"{mode} выбран потому что: {reason}"
        
        if testability == "interpretive":
            return f"Выбран {mode}, потому что вопрос интерпретативный (domain={domain})"
        elif testability == "fully_testable":
            return f"Выбран {mode}, потому что вопрос проверяемый (domain={domain})"
        else:
            return f"Выбран {mode} на основе эпистемической классификации"
    
    def _identify_mistakes(self, epistemic: Dict[str, Any], 
                           trust: str, confidence: float,
                           errors: Optional[List[str]]) -> List[str]:
        """Выявить ошибки в решении."""
        mistakes = []
        
        if errors:
            mistakes.extend(errors)
        
        if trust == "UNVERIFIED" and confidence < 0.3:
            mistakes.append("Низкая уверенность при отсутствии проверки")
        
        if epistemic.get("testability") == "fully_testable" and confidence < 0.5:
            mistakes.append("Проверяемый вопрос получил низкую уверенность")
        
        if epistemic.get("domain") == "philosophical" and epistemic.get("should_use_web"):
            mistakes.append("Философский вопрос использует web-поиск (избыточно)")
        
        if epistemic.get("domain") == "factual" and epistemic.get("should_use_web") is False:
            mistakes.append("Фактологический вопрос без web-поиска (может быть недостаточно данных)")
        
        return mistakes
    
    def _identify_missing(self, epistemic: Dict[str, Any], query: str) -> List[str]:
        """Определить, какой информации не хватало."""
        missing = []
        
        if epistemic.get("domain") == "factual" and epistemic.get("should_use_web") is False:
            missing.append("Возможно, нужны были дополнительные источники")
        
        if epistemic.get("need_clarification"):
            missing.append("Требовалось уточнение запроса")
        
        if epistemic.get("testability") == "interpretive" and epistemic.get("evidence_count", 0) < 2:
            missing.append("Мало источников для интерпретативного ответа")
        
        return missing
    
    def _extract_lessons(self, mistakes: List[str], 
                         was_correct: bool,
                         context: Optional[Dict[str, Any]],
                         validation_result: Optional[Dict[str, Any]] = None) -> List[str]:
        """Извлечь уроки из опыта."""
        lessons = []
        
        # Уроки о валидации извлекаем ТОЛЬКО если проверка реально выполнялась.
        # Наличие словаря validation_result само по себе не означает проверку.
        if validation_result and validation_result.get("performed", False):
            accepted = validation_result.get("accepted", 0)
            rejected = validation_result.get("rejected", 0)
            total = validation_result.get("total", 0)

            if total > 0:
                if accepted == total and rejected == 0:
                    lessons.append(
                        f"Все {accepted} claims прошли валидацию. "
                        "Стратегия эффективна для этого типа запросов."
                    )
                elif rejected > 0:
                    lessons.append(
                        f"{rejected} из {total} claims не прошли валидацию. "
                        "Стратегия требует доработки."
                    )
                elif accepted == 0 and rejected == 0:
                    lessons.append(
                        "Валидация не дала результатов. "
                        "Необходимо проверить качество источников."
                    )
            else:
                lessons.append(
                    "Внешняя валидация была запущена, но подтверждённых "
                    "результатов проверки claims нет."
                )
        else:
            if was_correct:
                lessons.append("Решение было правильным — повторять стратегию")
            else:
                lessons.append("Решение было ошибочным — пересмотреть стратегию")
        
        for mistake in mistakes:
            if "web" in mistake.lower():
                lessons.append("Проверять необходимость web-поиска перед использованием")
            if "уверенность" in mistake.lower():
                lessons.append("Не давать ответы с низкой уверенностью без проверки")
            if "интерпретативный" in mistake.lower():
                lessons.append("Интерпретативные вопросы не должны ходить в web")
        
        if context and context.get("entity_not_found"):
            lessons.append("Перед ответом нужно проверять существование сущности")
        
        return lessons[:3]
    
    def _suggest_policy_change(self, mistakes: List[str], 
                               epistemic: Dict[str, Any]) -> Optional[str]:
        """Предложить изменение политики."""
        if not mistakes:
            return None
        
        if "web" in str(mistakes).lower():
            if epistemic.get("domain") in ["philosophical", "interpretive"]:
                return "Запретить web-поиск для interpretive/non_falsifiable вопросов"
        
        if "уверенность" in str(mistakes).lower():
            return "Понижать trust для ответов с confidence < 0.4"
        
        if "интерпретативный" in str(mistakes).lower():
            return "Для интерпретативных вопросов использовать pluralistic_contextural"
        
        return None
    
    def get_policies(self) -> List[Dict[str, Any]]:
        """Получить все политики (оба статуса: observed и active) — имя
        метода отражает исторический Python-атрибут, не SQL-фильтр по
        статусу."""
        with get_connection() as conn:
            return repo.list_all_reflection_policies(conn)
    
    def get_summary(self) -> Dict[str, Any]:
        """Получить сводку рефлексий."""
        active_policy_count = len(self.get_policies())
        if not self.reflections:
            return {
                "total_reflections": 0,
                "recent_reflections": 0,
                "avg_confidence": 0,
                "mistakes_rate": 0,
                "lessons_count": 0,
                "policy_changes_suggested": 0,
                "applied_policies": 0,
                "active_policies": active_policy_count,
            }

        recent = self.reflections[-20:] if len(self.reflections) >= 20 else self.reflections
        return {
            "total_reflections": self.reflection_count,
            "recent_reflections": len(recent),
            "avg_confidence": sum(r.confidence for r in recent) / len(recent) if recent else 0,
            "mistakes_rate": sum(1 for r in recent if r.mistakes) / len(recent) if recent else 0,
            "lessons_count": sum(len(r.lessons) for r in recent),
            "policy_changes_suggested": sum(1 for r in self.reflections if r.suggested_policy_change),
            "applied_policies": sum(1 for r in self.reflections if r.applied_policy_changes),
            "active_policies": active_policy_count,
        }

    def summary_text(self) -> str:
        """Текстовое представление сводки."""
        stats = self.get_summary()
        recent = self.reflections[-3:] if self.reflections else []

        lessons_text = ""
        for r in recent:
            for l in r.lessons[:2]:
                lessons_text += f"  - {l}\n"

        if not lessons_text:
            lessons_text = "  нет\n"

        policies_text = ""
        for p in self.get_policies()[:3]:
            policies_text += f"  - {p.get('rule')} (conf: {p.get('confidence', 0):.2f})\n"

        if not policies_text:
            policies_text = "  нет\n"
        
        return f"""
=== РЕФЛЕКСИВНЫЙ ЦИКЛ V7 ===
Всего рефлексий: {stats['total_reflections']}
Средняя уверенность: {stats['avg_confidence']:.2f}
Доля ошибок: {stats['mistakes_rate']:.2f}
Уроков: {stats['lessons_count']}
Предложений по политике: {stats['policy_changes_suggested']}
Применённых политик: {stats['applied_policies']}
Активных политик: {stats['active_policies']}

Последние уроки:
{lessons_text}

Активные политики:
{policies_text}"""


# Глобальный экземпляр
_reflection: Optional[ReflectionLoop] = None

def get_reflection() -> ReflectionLoop:
    global _reflection
    if _reflection is None:
        _reflection = ReflectionLoop()
    return _reflection


if __name__ == "__main__":
    # Тестирование
    ref = get_reflection()
    print(ref.summary_text())
    
    # Симуляция рефлексии на запросе
    result = ref.reflect_on_query(
        query="Что такое сознание?",
        response="На этот вопрос нет единственного объективно проверяемого ответа...",
        epistemic={
            "domain": "philosophical",
            "testability": "interpretive",
            "answer_mode": "pluralistic_contextual",
            "should_use_web": False,
            "reason": "интерпретативный вопрос",
            "evidence_count": 0
        },
        trust="VALUE_FRAMEWORK",
        confidence=0.4
    )
    
    print("\n=== РЕЗУЛЬТАТ РЕФЛЕКСИИ ===")
    print(f"Правильно: {result.was_correct}")
    print(f"Ошибки: {result.mistakes}")
    print(f"Уроки: {result.lessons}")
    print(f"Изменение поведения: {result.should_change_behavior}")
    if result.suggested_policy_change:
        print(f"Предложение политики: {result.suggested_policy_change}")
    if result.applied_policy_changes:
        print(f"Применено изменений: {result.applied_policy_changes}")
    if result.future_actions:
        print(f"Будущие действия: {result.future_actions}")
    
    print("\n=== СВОДКА ПОСЛЕ РЕФЛЕКСИИ ===")
    print(ref.summary_text())
    
    print("\n=== АКТИВНЫЕ ПОЛИТИКИ ===")
    for p in ref.get_policies():
        print(f"  {p.get('rule')} (conf: {p.get('confidence', 0):.2f})")
