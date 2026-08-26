"""
agent/core_loop.py — Главный цикл жизни для YANDI V3.

Цикл:
1. Perceive — восприятие входящих сигналов
2. Update World Model — обновление модели мира
3. Update Self Model — обновление модели себя
4. Evaluate Goals — оценка целей
5. Reflect — рефлексия
6. Act — действие
7. Remember — запоминание

Система живёт между запросами, а не только реагирует.
"""

from __future__ import annotations

import sys
from pathlib import Path
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import time
import json
import threading
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Callable

from agent.self_model import get_self_model
from agent.memory_episodic import get_memory
from agent.reflection_loop import get_reflection
from agent.motivation import get_motivation


@dataclass
class LoopState:
    """Состояние цикла."""
    is_running: bool = False
    cycle_number: int = 0
    last_perceive: float = 0.0
    last_update: float = 0.0
    last_reflect: float = 0.0
    last_act: float = 0.0
    last_remember: float = 0.0
    
    # Текущий контекст
    current_query: Optional[str] = None
    current_response: Optional[str] = None
    current_epistemic: Dict[str, Any] = field(default_factory=dict)
    
    # Статистика
    total_actions: int = 0
    total_reflections: int = 0
    total_errors: int = 0


class CoreLoop:
    """
    Главный цикл жизни YANDI.
    
    Работает в фоновом режиме, обновляя состояние системы
    даже между запросами пользователя.
    """
    
    def __init__(self):
        self.state = LoopState()
        self.self_model = get_self_model()
        self.memory = get_memory()
        self.reflection = get_reflection()
        self.motivation = get_motivation()
        
        # Фоновый поток
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Обработчики действий
        self.action_handlers: Dict[str, Callable] = {}
    
    # ---- ОСНОВНЫЕ ШАГИ ЦИКЛА ----
    
    def perceive(self, input_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Шаг 1: Восприятие.
        
        Получает сигналы извне (запросы, команды, события).
        """
        self.state.last_perceive = time.time()
        self.state.cycle_number += 1
        
        perception = {
            "timestamp": time.time(),
            "cycle": self.state.cycle_number,
            "has_input": input_data is not None,
            "input": input_data or {},
        }
        
        if input_data:
            self.state.current_query = input_data.get("query")
            self.state.current_epistemic = input_data.get("epistemic", {})
        
        self.self_model.increment_cycle()
        
        return perception
    
    def update_world_model(self, perception: Dict[str, Any]) -> Dict[str, Any]:
        """
        Шаг 2: Обновление модели мира.
        
        Анализирует восприятие и обновляет знания о мире.
        """
        # Пока заглушка — будет расширяться с world_model.py
        world_update = {
            "timestamp": time.time(),
            "new_info": perception.get("input", {}),
            "uncertainties": self.self_model.get_uncertainties()[:3],
        }
        
        return world_update
    
    def update_self_model(self, world_update: Dict[str, Any]) -> Dict[str, Any]:
        """
        Шаг 3: Обновление модели себя.
        
        Проверяет состояние, здоровье, цели.
        """
        health = self.self_model.check_health()
        
        self_update = {
            "timestamp": time.time(),
            "health": health,
            "age": self.self_model.get_age(),
            "goals": self.self_model.get_goals(),
            "limits": self.self_model.get_limitations(),
        }
        
        # Если здоровье плохое — записываем событие
        if health["health_score"] < 50:
            self.memory.add_error(
                f"Низкий health_score: {health['health_score']}",
                {"health": health},
                severity=0.8
            )
        
        return self_update
    
    def evaluate_goals(self, self_update: Dict[str, Any]) -> Dict[str, Any]:
        """
        Шаг 4: Оценка целей.
        
        Проверяет, какие цели актуальны, нужно ли их менять.
        """
        goals = self.self_model.get_goals()
        
        # Если целей нет — добавляем базовые
        if not goals:
            self.self_model.add_goal("Быть полезным")
            self.self_model.add_goal("Не врать")
            self.self_model.add_goal("Учиться на ошибках")
        
        evaluation = {
            "timestamp": time.time(),
            "current_goals": self.self_model.get_goals(),
            "motivation": self.motivation.get_summary(),
            "needs_new_goals": len(goals) < 2,
        }
        
        return evaluation
    
    def reflect(self, evaluation: Dict[str, Any]) -> Dict[str, Any]:
        """
        Шаг 5: Рефлексия.
        
        Анализирует текущее состояние и решения.
        """
        # Если есть текущий запрос — рефлексируем над ним
        if self.state.current_query:
            # Проверяем, была ли рефлексия уже сделана
            if not hasattr(self, '_last_reflection_query') or self._last_reflection_query != self.state.current_query:
                result = self.reflection.reflect_on_query(
                    query=self.state.current_query,
                    response=self.state.current_response or "",
                    epistemic=self.state.current_epistemic,
                    trust=self.state.current_epistemic.get("trust", "UNVERIFIED"),
                    confidence=self.state.current_epistemic.get("confidence", 0.5)
                )
                self._last_reflection_query = self.state.current_query
                self.state.total_reflections += 1
                return result.__dict__
        
        # Или общая рефлексия состояния
        return {
            "timestamp": time.time(),
            "type": "general",
            "state": self.self_model.reflect(),
            "memory_stats": self.memory.get_stats(),
        }
    
    def act(self, action_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Шаг 6: Действие.
        
        Выполняет действие на основе анализа и рефлексии.
        """
        self.state.last_act = time.time()
        self.state.total_actions += 1
        
        # Проверка мотивации перед действием
        if action_type == "explore":
            if not self.motivation.should_explore(
                confidence=data.get("confidence", 0.5),
                uncertainty=data.get("uncertainty", 0.5)
            ):
                return {"status": "skipped", "reason": "мотивация недостаточна"}
        
        if action_type == "verify":
            if not self.motivation.should_verify(
                trust=data.get("trust", "UNVERIFIED"),
                confidence=data.get("confidence", 0.5)
            ):
                return {"status": "skipped", "reason": "верификация не требуется"}
        
        # Выполняем действие
        action_result = {
            "timestamp": time.time(),
            "type": action_type,
            "data": data,
            "status": "executed",
        }
        
        self.self_model.increment_queries()
        
        return action_result
    
    def remember(self, action_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Шаг 7: Запоминание.
        
        Сохраняет событие в эпизодическую память.
        """
        self.state.last_remember = time.time()
        
        if action_result.get("type") == "query":
            self.memory.add_query(
                query=action_result.get("data", {}).get("query", ""),
                domain=action_result.get("data", {}).get("domain", ""),
                answer_mode=action_result.get("data", {}).get("answer_mode", ""),
                trust=action_result.get("data", {}).get("trust", ""),
                confidence=action_result.get("data", {}).get("confidence", 0.5)
            )
        else:
            self.memory.add(
                event_type=action_result.get("type", "action"),
                summary=f"Действие: {action_result.get('type')}",
                details=action_result,
                importance=0.5
            )
        
        # Обновляем мотивацию на основе результата
        self.motivation.update_from_experience({
            "was_useful": action_result.get("status") == "executed",
            "was_correct": action_result.get("status") == "executed",
            "error": None if action_result.get("status") == "executed" else "action_failed",
        })
        
        return {
            "timestamp": time.time(),
            "remembered": True,
            "memory_stats": self.memory.get_stats(),
        }
    
    # ---- ПОЛНЫЙ ЦИКЛ ----
    
    def run_cycle(self, input_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Выполнить один полный цикл.
        """
        if not self.state.is_running:
            self.state.is_running = True
        
        try:
            # 1. Perceive
            perception = self.perceive(input_data)
            
            # 2. Update World Model
            world_update = self.update_world_model(perception)
            
            # 3. Update Self Model
            self_update = self.update_self_model(world_update)
            
            # 4. Evaluate Goals
            goal_eval = self.evaluate_goals(self_update)
            
            # 5. Reflect
            reflection = self.reflect(goal_eval)
            self.state.total_reflections += 1
            
            # 6. Act
            if input_data:
                action = self.act("query", {
                    "query": input_data.get("query", ""),
                    "domain": input_data.get("epistemic", {}).get("domain", ""),
                    "answer_mode": input_data.get("epistemic", {}).get("answer_mode", ""),
                    "trust": input_data.get("epistemic", {}).get("trust", ""),
                    "confidence": input_data.get("epistemic", {}).get("confidence", 0.5),
                })
            else:
                action = self.act("idle", {"reason": "no input"})
            
            # 7. Remember
            memory_result = self.remember(action)
            
            return {
                "cycle": self.state.cycle_number,
                "perception": perception,
                "world_update": world_update,
                "self_update": self_update,
                "goal_eval": goal_eval,
                "reflection": reflection,
                "action": action,
                "memory": memory_result,
                "timestamp": time.time(),
            }
            
        except Exception as e:
            self.state.total_errors += 1
            self.self_model.increment_errors()
            
            return {
                "cycle": self.state.cycle_number,
                "error": str(e),
                "timestamp": time.time(),
                "status": "failed",
            }
    
    # ---- ФОНОВЫЙ ЦИКЛ ----
    
    def start_background(self, interval: float = 60.0):
        """
        Запустить фоновый цикл (обновление состояния между запросами).
        """
        if self._thread and self._thread.is_alive():
            return
        
        self._stop_event.clear()
        
        def _background_loop():
            while not self._stop_event.is_set():
                try:
                    # Запускаем цикл без входных данных (idle)
                    result = self.run_cycle(None)
                    if result.get("status") == "failed":
                        print(f"[core_loop] Ошибка в фоновом цикле: {result.get('error')}")
                except Exception as e:
                    print(f"[core_loop] Фоновая ошибка: {e}")
                
                # Ждём до следующего цикла
                self._stop_event.wait(interval)
        
        self._thread = threading.Thread(target=_background_loop, daemon=True)
        self._thread.start()
        print(f"[core_loop] Фоновый цикл запущен (интервал: {interval}с)")
    
    def stop_background(self):
        """Остановить фоновый цикл."""
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=5)
            print("[core_loop] Фоновый цикл остановлен")
    
    # ---- ПРЕДСТАВЛЕНИЕ ----
    
    def get_status(self) -> Dict[str, Any]:
        """Получить статус цикла."""
        return {
            "is_running": self.state.is_running,
            "cycle_number": self.state.cycle_number,
            "total_actions": self.state.total_actions,
            "total_reflections": self.state.total_reflections,
            "total_errors": self.state.total_errors,
            "current_query": self.state.current_query,
            "background_active": self._thread and self._thread.is_alive(),
            "self_model": self.self_model.reflect(),
            "memory_stats": self.memory.get_stats(),
            "motivation": self.motivation.get_summary(),
        }
    
    def summary_text(self) -> str:
        """Текстовое представление статуса."""
        status = self.get_status()
        return f"""
=== YANDI CORE LOOP ===
Статус: {'✅ Работает' if status['is_running'] else '⏸ Остановлен'}
Цикл: {status['cycle_number']}
Действий: {status['total_actions']}
Рефлексий: {status['total_reflections']}
Ошибок: {status['total_errors']}
Фоновый поток: {'✅ Активен' if status['background_active'] else '❌ Неактивен'}
Текущий запрос: {status['current_query'] or 'нет'}

Self Model:
  Возраст: {status['self_model']['age']} циклов
  Запросов: {status['self_model']['total_queries']}
  Жива: {'✅' if status['self_model']['is_alive'] else '❌'}

Память:
  Эпизодов: {status['memory_stats']['total_episodes']}
  Типы: {status['memory_stats']['by_type']}

Мотивация:
  Точность: {status['motivation']['accuracy']:.2f}
  Любопытство: {status['motivation']['curiosity']:.2f}
  Полезность: {status['motivation']['usefulness']:.2f}
"""


# Глобальный экземпляр
_core: Optional[CoreLoop] = None

def get_core_loop() -> CoreLoop:
    global _core
    if _core is None:
        _core = CoreLoop()
    return _core


if __name__ == "__main__":
    # Тестирование
    core = get_core_loop()
    print(core.summary_text())
    
    # Запускаем фоновый цикл
    print("\n=== ЗАПУСК ФОНОВОГО ЦИКЛА ===")
    core.start_background(interval=10.0)
    
    # Симуляция запроса
    print("\n=== СИМУЛЯЦИЯ ЗАПРОСА ===")
    result = core.run_cycle({
        "query": "Что такое сознание?",
        "epistemic": {
            "domain": "philosophical",
            "testability": "interpretive",
            "answer_mode": "pluralistic_contextual",
            "trust": "VALUE_FRAMEWORK",
            "confidence": 0.4,
        }
    })
    print(f"Цикл #{result['cycle']} выполнен")
    
    # Показываем статус
    print("\n=== СТАТУС ПОСЛЕ ЗАПРОСА ===")
    print(core.summary_text())
    
    # Останавливаем фоновый цикл
    print("\n=== ОСТАНОВКА ===")
    core.stop_background()
