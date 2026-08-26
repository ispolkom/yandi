"""
assistant/orch_planner.py — Planning Engine (Qwen3:14b) с интеграцией рефлексии.
Строит план выполнения запроса ДО запуска цепочки.
Пока использует 14B (в будущем заменить на 7B для скорости).

Рефлексия влияет на планирование через:
- Активные политики из reflection_loop.py
- Уроки, извлечённые из предыдущих запросов
- Корректировку шагов на основе накопленного опыта
"""
from __future__ import annotations

import sys
import json
import re
import time
from pathlib import Path
from typing import List, Optional, Dict, Any

import requests as _requests

# Добавляем путь для импорта
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from agent.orch_schemas import PlanResult, PlanStep, RiskResult, StepName

OLLAMA  = "http://127.0.0.1:11434"
MODEL   = "qwen3:14b"
TIMEOUT = 45

_session = _requests.Session()
_session.trust_env = False

# Пытаемся импортировать рефлексию
try:
    from agent.reflection_loop import get_reflection
    REFLECTION_AVAILABLE = True
except ImportError:
    REFLECTION_AVAILABLE = False
    def get_reflection():
        return None

SYSTEM_PROMPT = """Ты планировщик запросов. Определи оптимальный план обработки.

Верни ТОЛЬКО валидный JSON:
{
  "steps": ["step1", "step2", ...],
  "skip_internet": true/false,
  "mandatory_arbitrage": true/false,
  "reason": "одна строка — почему такой план"
}

Доступные шаги (использовать в нужном порядке):
- "cache_check"   — проверить кэш (всегда первым)
- "risk_assess"   — оценить риск (всегда вторым)
- "intent"        — анализ намерения
- "clarify"       — уточнения у пользователя (только если нужны параметры)
- "enrich"        — расширить запрос
- "local_search"  — поиск в локальной базе
- "web_query"     — формировать поисковый запрос для интернета
- "web_scrape"    — парсинг интернета
- "synthesize"    — сформировать ответ
- "optimistic_respond" — выдать предварительный ответ
- "validate"      — валидация через ноды (для рисковых запросов)
- "arbitrate"     — арбитраж (для критических)

Правила:
- cache_check и risk_assess — всегда первые два
- synthesize и optimistic_respond — всегда последние
- skip_internet=true если запрос про конкретную локальную систему / код / личные данные
- mandatory_arbitrage=true только для медицины, юриспруденции, финансов"""

# Дефолтные планы по уровню риска (без LLM)
_DEFAULT_PLANS: dict[str, list[str]] = {
    "low":      ["cache_check", "risk_assess", "intent", "enrich", "local_search", "synthesize", "optimistic_respond"],
    "medium":   ["cache_check", "risk_assess", "intent", "enrich", "local_search", "web_query", "web_scrape", "synthesize", "optimistic_respond", "validate"],
    "high":     ["cache_check", "risk_assess", "intent", "clarify", "enrich", "local_search", "web_query", "web_scrape", "synthesize", "optimistic_respond", "validate"],
    "critical": ["cache_check", "risk_assess", "intent", "clarify", "enrich", "local_search", "web_query", "web_scrape", "synthesize", "optimistic_respond", "validate", "arbitrate"],
}

# Политики рефлексии, которые влияют на планирование
_REFLECTION_POLICY_MAP = {
    "Запретить web-поиск для interpretive/non_falsifiable вопросов": {
        "action": "remove_web_steps",
        "description": "Убираем web_query и web_scrape для интерпретативных вопросов"
    },
    "Понижать trust для ответов с confidence < 0.4": {
        "action": "add_validation",
        "description": "Добавляем validate для низкой уверенности"
    },
    "Для интерпретативных вопросов использовать pluralistic_contextural": {
        "action": "add_frame_split",
        "description": "Добавляем frame_split для интерпретативных вопросов"
    },
}


def _call_ollama(prompt: str) -> str:
    resp = _session.post(
        f"{OLLAMA}/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": 0.1, "num_predict": 300}},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {}


def _create_plan_step(step_name: str, priority: int = 1) -> PlanStep:
    """Создать PlanStep из строки."""
    return PlanStep(
        name=step_name,
        description=f"Step: {step_name}",
        priority=priority,
        required=True,
        depends_on=[]
    )


def _get_reflection_policies(query: str = "", domain: str = "") -> List[Dict[str, Any]]:
    """Получить активные политики из рефлексии и уроки из опыта."""
    policies = []

    # P1 (autonomous fix pass, plan=76.19s live-run investigation):
    # sub-profile the two real internal phases so a future live run can
    # attribute build_plan's wall-clock instead of leaving it as one
    # opaque "plan" bucket. Names match the actual code paths below,
    # not invented generic labels.
    _t0_reflection_policies = time.time()

    # 1. Политики из рефлексии
    if REFLECTION_AVAILABLE:
        try:
            reflection = get_reflection()
            if reflection is not None:
                policies = reflection.get_policies()
        except Exception:
            pass

    _reflection_policies_ms = (time.time() - _t0_reflection_policies) * 1000
    _t0_experience_memory = time.time()

    # 2. Уроки из опыта
    # Ограничиваем количество политик из уроков
    lesson_policy_count = 0
    max_lesson_policies = 2
    try:
        import sys
        sys.stderr.write("[Planner] Загрузка уроков из памяти...\n")
        from agent.experience_memory import get_experience_memory
        memory = get_experience_memory()
        if memory:
            lessons = memory.get_relevant_lessons(query, limit=2)
            sys.stderr.write(f"[Planner] Получено {len(lessons)} уроков\n")
            if lessons:
                # Преобразуем уроки в формат политик
                for lesson in lessons:
                    if lesson.get('lessons'):
                        if lesson_policy_count >= max_lesson_policies:
                            break
                        policies.append({
                            "type": "lesson",
                            "source": "experience_memory",
                            "query": lesson.get("query", ""),
                            "domain": lesson.get("domain", "unknown"),
                            "lessons": lesson.get("lessons", []),
                            "confidence": lesson.get("confidence", 0.0),
                            "timestamp": lesson.get("timestamp", ""),
                        })
                        lesson_policy_count += 1
                        sys.stderr.write(f"[Planner] Добавлен урок: {lesson.get('lessons', [])[:1]}\n")
                        # Преобразуем урок в политику
                        lesson_confidence = lesson.get("confidence", 0.0)
                        if lesson_confidence < 0.6:
                            continue
                        # P1 bug fix (autonomous fix pass): lesson_text was
                        # referenced below but never defined anywhere in
                        # this function — any lesson with confidence>=0.6
                        # raised NameError here, silently swallowed by the
                        # broad except below, abandoning the rest of
                        # lesson processing for this call. Never observed
                        # in prior live logs only because confidence
                        # happened to stay <0.6 (the `continue` above fired
                        # first) — not because the path was safe.
                        lesson_text = " ".join(lesson.get("lessons", []) or [])
                        if "прошли валидацию" in lesson_text:
                            policies.append({
                                "type": "policy",
                                "rule": "Добавить валидацию для подтверждённой стратегии",
                                "confidence": lesson.get("confidence", 0.7),
                                "source": "experience_memory"
                            })
                        elif "требует доработки" in lesson_text:
                            policies.append({
                                "type": "policy",
                                "rule": "Понижать trust для ответов с confidence < 0.4",
                                "confidence": lesson.get("confidence", 0.6),
                                "source": "experience_memory"
                            })
                        elif "не прошли валидацию" in lesson_text:
                            policies.append({
                                "type": "policy",
                                "rule": "Запретить web-поиск для interpretive/non_falsifiable вопросов",
                                "confidence": lesson.get("confidence", 0.5),
                                "source": "experience_memory"
                            })
    except Exception as e:
        import sys
        sys.stderr.write(f"[Planner] Ошибка загрузки уроков: {e}\n")

    _experience_memory_ms = (time.time() - _t0_experience_memory) * 1000

    sys.stderr.write(
        "[Plan SubProfile] "
        f"reflection_policies={_reflection_policies_ms:.1f}ms "
        f"experience_memory={_experience_memory_ms:.1f}ms\n"
    )

    return policies


def _apply_reflection_policies(steps: List[str], policies: List[Dict[str, Any]]) -> tuple[List[str], List[str]]:
    """
    Применить политики рефлексии к списку шагов.
    Возвращает: (обновленный список шагов, список применённых политик)
    """
    if not policies:
        return steps, []
    
    result = steps.copy()
    applied = []
    
    for policy in policies:
        rule = policy.get("rule", "")
        confidence = policy.get("confidence", 0.5)
        
        # Применяем только политики с достаточной уверенностью
        if confidence < 0.5:
            continue
        
        # Проверяем, есть ли правило в карте
        for pattern, action_info in _REFLECTION_POLICY_MAP.items():
            if pattern in rule:
                action = action_info.get("action")
                applied.append(pattern)
                
                if action == "remove_web_steps":
                    # Убираем web-шаги
                    result = [s for s in result if s not in ["web_query", "web_scrape"]]
                
                elif action == "add_validation":
                    # Добавляем валидацию, если её нет
                    if "validate" not in result:
                        # Вставляем перед синтезом
                        if "synthesize" in result:
                            idx = result.index("synthesize")
                            result.insert(idx, "validate")
                        else:
                            result.append("validate")
                
                elif action == "add_frame_split":
                    # Добавляем frame_split, если его нет
                    if "frame_split" not in result:
                        if "synthesize" in result:
                            idx = result.index("synthesize")
                            result.insert(idx, "frame_split")
                        else:
                            result.append("frame_split")
                
                break  # применяем только первое подходящее правило
    
    return result, applied


def _get_reflection_lessons() -> List[str]:
    """Получить уроки из self_model через рефлексию."""
    if not REFLECTION_AVAILABLE:
        return []
    
    try:
        reflection = get_reflection()
        if reflection is None:
            return []
        
        # Берём последние уроки из последних рефлексий
        lessons = []
        for r in reflection.reflections[-5:]:
            lessons.extend(r.lessons)
        return lessons[:5]
    except Exception:
        return []


def _should_skip_internet(query: str, policies: List[Dict[str, Any]]) -> bool:
    """Определить, нужно ли пропускать интернет."""
    # Проверяем политики
    for policy in policies:
        rule = policy.get("rule", "")
        if "Запретить web-поиск" in rule:
            return True
    
    # Локальные ключевые слова
    local_keywords = [
        "print", "def ", "class ", "import ", "код", "функция",
        "локальный", "моя система", "мой сервер", "localhost",
        "127.0.0.1", "внутренний", "на моём"
    ]
    if any(k in query.lower() for k in local_keywords):
        return True
    
    return False


def _default_plan(risk: RiskResult, query: str = "", policies: Optional[List[Dict[str, Any]]] = None) -> PlanResult:
    steps_str = _DEFAULT_PLANS.get(risk.risk_level, _DEFAULT_PLANS["low"])
    
    # Применяем политики рефлексии
    # Загружаем политики, если не переданы
    if policies is None:
        policies = _get_reflection_policies(query)
    applied_policies = []
    import sys; sys.stderr.write(f"[DefaultPlan] Загружено {len(policies)} политик\n")
    if policies:
        steps_str, applied_policies = _apply_reflection_policies(steps_str, policies)
    
    steps = [_create_plan_step(s, i) for i, s in enumerate(steps_str)]
    
    skip_internet = _should_skip_internet(query, policies)
    
    return PlanResult(
        steps=steps,
        skip_internet=skip_internet,
        estimated_time=len(steps) * 0.5,
    )


def build_plan(query: str, risk: RiskResult, use_llm: bool = False) -> PlanResult:
    """
    Построить план выполнения запроса с учётом рефлексии.

    Args:
        query:   запрос пользователя
        risk:    результат Risk Engine
        use_llm: использовать LLM для плана (медленнее, но гибче)

    Returns:
        PlanResult
    """
    # Загружаем политики рефлексии
    import sys; sys.stderr.write(f"[BuildPlan] Начало планирования, use_llm={use_llm}\n")
    policies = _get_reflection_policies(query)

    # По умолчанию — дефолтный план по уровню риска (без LLM, мгновенно)
    if not use_llm:
        _t0_planner_core = time.time()
        result = _default_plan(risk, query, policies)
        _planner_core_ms = (time.time() - _t0_planner_core) * 1000
        sys.stderr.write(f"[Plan SubProfile] planner_core={_planner_core_ms:.1f}ms\n")
        return result

    # LLM-план (более гибкий, но медленнее)
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Запрос: {query}\n"
        f"Уровень риска: {risk.risk_level}"
    )
    
    # Добавляем информацию о политиках рефлексии
    if policies:
        policy_rules = [p.get("rule", "") for p in policies if p.get("confidence", 0) > 0.5]
        if policy_rules:
            prompt += f"\nАктивные политики рефлексии: {', '.join(policy_rules)}"
    
    try:
        raw  = _call_ollama(prompt)
        data = _extract_json(raw)
        steps_raw = data.get("steps", [])
        # Валидировать шаги — только известные
        valid = {"cache_check","risk_assess","intent","clarify","enrich","local_search",
                 "web_query","web_scrape","synthesize","optimistic_respond","validate","arbitrate","frame_split"}
        steps_str = [s for s in steps_raw if s in valid]
        if not steps_str:
            return _default_plan(risk, query, policies)
        
        # Применяем политики рефлексии
        if policies:
            steps_str, _ = _apply_reflection_policies(steps_str, policies)
        
        steps = [_create_plan_step(s, i) for i, s in enumerate(steps_str)]
        
        skip_internet = _should_skip_internet(query, policies) or bool(data.get("skip_internet", False))
        
        return PlanResult(
            steps=steps,
            skip_internet=skip_internet,
            estimated_time=len(steps) * 0.5,
        )
    except Exception:
        return _default_plan(risk, query, policies)


if __name__ == "__main__":
    from agent.orch_risk import assess_risk
    
    # Тестируем с рефлексией
    print("=" * 80)
    print("ПЛАНИРОВЩИК С ИНТЕГРАЦИЕЙ РЕФЛЕКСИИ")
    print("=" * 80)
    
    if REFLECTION_AVAILABLE:
        ref = get_reflection()
        if ref:
            policies = ref.get_policies()
            print(f"Активных политик рефлексии: {len(policies)}")
            for p in policies:
                print(f"  - {p.get('rule')} (conf: {p.get('confidence', 0):.2f})")
        else:
            print("Рефлексия недоступна")
    else:
        print("Модуль рефлексии не найден")
    
    print("\n" + "-" * 80)
    
    tests = [
        "Как работает DHT?",
        "Как лечить кашель?",
        "Напиши мне договор аренды",
        "print hello world в Python",
        "Что такое сознание?",
    ]
    
    for q in tests:
        risk = assess_risk(q)
        plan = build_plan(q, risk, use_llm=False)
        
        print(f"\n📝 {q}")
        print(f"  Риск: {risk.risk_level}")
        print(f"  Шаги: {' → '.join([s.name for s in plan.steps])}")
        print(f"  Интернет: {'ДА' if not plan.skip_internet else 'НЕТ'}")
