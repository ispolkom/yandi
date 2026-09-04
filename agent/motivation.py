"""
agent/motivation.py — Мотивационная система для YANDI V3.

Внутренние стремления системы:
- accuracy — стремление быть правильным
- curiosity — стремление уменьшать неизвестность
- coherence — стремление сохранять непротиворечивую модель мира
- usefulness — стремление быть полезным
- safety — ограничение вредных действий

Цель: система должна иметь внутренние цели, а не только реагировать.
"""

from __future__ import annotations

import sys
from pathlib import Path
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import json
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

from agent.self_model import get_self_model


@dataclass
class Motivation:
    """Мотивационная структура системы."""
    accuracy: float = 0.8      # стремление к правильности
    curiosity: float = 0.6     # стремление к познанию нового
    coherence: float = 0.7     # стремление к непротиворечивости
    usefulness: float = 0.9    # стремление быть полезным
    safety: float = 0.8        # стремление к безопасности
    
    # Параметры
    exploration_rate: float = 0.3  # готовность пробовать новое
    caution_rate: float = 0.5      # осторожность
    
    # Состояние
    last_update: float = field(default_factory=time.time)
    history: List[Dict[str, Any]] = field(default_factory=list)


class MotivationSystem:
    """Система мотивации — управление целями и стремлениями."""
    
    def __init__(self):
        self.self_model = get_self_model()
        self.motivation = self._load_or_create()
    
    def _load_or_create(self) -> Motivation:
        """Загрузить или создать мотивацию."""
        metadata = self.self_model.get_metadata()
        if 'motivation' in metadata:
            try:
                return Motivation(**metadata['motivation'])
            except Exception:
                pass
        return Motivation()

    def _save(self):
        """Сохранить мотивацию в self_model."""
        self.motivation.last_update = time.time()
        self.self_model.set_metadata_value('motivation', self.motivation.__dict__)
    
    # ---- ГЕТТЕРЫ ----
    
    def get_accuracy(self) -> float:
        return self.motivation.accuracy
    
    def get_curiosity(self) -> float:
        return self.motivation.curiosity
    
    def get_coherence(self) -> float:
        return self.motivation.coherence
    
    def get_usefulness(self) -> float:
        return self.motivation.usefulness
    
    def get_safety(self) -> float:
        return self.motivation.safety
    
    def get_exploration_rate(self) -> float:
        return self.motivation.exploration_rate
    
    def get_caution_rate(self) -> float:
        return self.motivation.caution_rate
    
    # ---- СЕТТЕРЫ ----
    
    def set_accuracy(self, value: float):
        self.motivation.accuracy = max(0.0, min(1.0, value))
        self._save()
    
    def set_curiosity(self, value: float):
        self.motivation.curiosity = max(0.0, min(1.0, value))
        self._save()
    
    def set_coherence(self, value: float):
        self.motivation.coherence = max(0.0, min(1.0, value))
        self._save()
    
    def set_usefulness(self, value: float):
        self.motivation.usefulness = max(0.0, min(1.0, value))
        self._save()
    
    def set_safety(self, value: float):
        self.motivation.safety = max(0.0, min(1.0, value))
        self._save()
    
    def set_exploration(self, value: float):
        self.motivation.exploration_rate = max(0.0, min(1.0, value))
        self._save()
    
    def set_caution(self, value: float):
        self.motivation.caution_rate = max(0.0, min(1.0, value))
        self._save()
    
    # ---- ПРИНЯТИЕ РЕШЕНИЙ НА ОСНОВЕ МОТИВАЦИИ ----
    
    def should_explore(self, confidence: float = 0.5, uncertainty: float = 0.5) -> bool:
        """
        Решение: исследовать ли новое.
        
        Args:
            confidence: текущая уверенность
            uncertainty: уровень неопределённости
        """
        # Чем выше любопытство и выше неопределённость — тем больше шанс исследовать
        explore_chance = self.motivation.curiosity * (1 - confidence) * (1 + uncertainty)
        return explore_chance > 0.3
    
    def should_verify(self, trust: str, confidence: float) -> bool:
        """
        Решение: проверять ли ответ.
        """
        if trust == "UNVERIFIED" and confidence < 0.5:
            return True
        if self.motivation.accuracy > 0.7 and confidence < 0.6:
            return True
        return False
    
    def should_ask_clarification(self, uncertainty: float, safety: float = 0.5) -> bool:
        """
        Решение: задать уточняющий вопрос.
        """
        # Если высокая неопределённость и безопасность важна
        if uncertainty > 0.7 and self.motivation.safety > 0.6:
            return True
        return False
    
    def should_use_web(self, testability: str, confidence: float) -> bool:
        """
        Решение: использовать ли web-поиск.
        """
        # Для проверяемых вопросов с низкой уверенностью — да
        if testability == "fully_testable" and confidence < 0.6:
            return True
        # Если полезность важна и данных мало
        if self.motivation.usefulness > 0.8 and confidence < 0.5:
            return True
        return False
    
    def get_answer_mode_preference(self, domain: str, testability: str) -> str:
        """
        Выбрать предпочтительный режим ответа на основе мотивации.
        """
        # Если точность важна — выбираем проверяемые режимы
        if self.motivation.accuracy > 0.8:
            if testability == "fully_testable":
                return "factual"
            elif testability == "partially_testable":
                return "qualified_factual"
        
        # Если любопытство высоко — выбираем исследовательские режимы
        if self.motivation.curiosity > 0.7:
            if testability == "interpretive":
                return "pluralistic_contextual"
            return "exploratory"
        
        # Если полезность важна — выбираем практичные режимы
        if self.motivation.usefulness > 0.8:
            if domain == "procedural":
                return "procedural"
            return "factual"
        
        return "contextual"
    
    # ---- ОБНОВЛЕНИЕ МОТИВАЦИИ НА ОСНОВЕ ОПЫТА ----
    
    def update_from_experience(self, result: Dict[str, Any]):
        """
        Обновить мотивацию на основе опыта.
        """
        # Если ответ был полезным — повышаем полезность
        if result.get("was_useful", False):
            self.motivation.usefulness = min(1.0, self.motivation.usefulness + 0.05)
        
        # Если была ошибка из-за недостатка данных — повышаем любопытство
        if result.get("error", ""):
            self.motivation.curiosity = min(1.0, self.motivation.curiosity + 0.05)
        
        # Если был конфликт источников — повышаем осторожность
        if result.get("had_conflict", False):
            self.motivation.caution_rate = min(1.0, self.motivation.caution_rate + 0.1)
            self.motivation.safety = min(1.0, self.motivation.safety + 0.05)
        
        # Если ответ был правильным — подтверждаем текущий курс
        if result.get("was_correct", False):
            self.motivation.accuracy = min(1.0, self.motivation.accuracy + 0.02)
        
        self._save()
    
    # ---- ПРЕДСТАВЛЕНИЕ ----
    
    def get_summary(self) -> Dict[str, Any]:
        """Получить сводку мотивации."""
        return {
            "accuracy": round(self.motivation.accuracy, 2),
            "curiosity": round(self.motivation.curiosity, 2),
            "coherence": round(self.motivation.coherence, 2),
            "usefulness": round(self.motivation.usefulness, 2),
            "safety": round(self.motivation.safety, 2),
            "exploration_rate": round(self.motivation.exploration_rate, 2),
            "caution_rate": round(self.motivation.caution_rate, 2),
        }
    
    def summary_text(self) -> str:
        """Текстовое представление мотивации."""
        stats = self.get_summary()
        return f"""
=== МОТИВАЦИОННАЯ СИСТЕМА ===
Точность (accuracy):    {stats['accuracy']:.2f}
Любопытство (curiosity): {stats['curiosity']:.2f}
Непротиворечивость:     {stats['coherence']:.2f}
Полезность (usefulness): {stats['usefulness']:.2f}
Безопасность (safety):   {stats['safety']:.2f}
Исследование:           {stats['exploration_rate']:.2f}
Осторожность:           {stats['caution_rate']:.2f}
"""


# Глобальный экземпляр
_motivation: Optional[MotivationSystem] = None

def get_motivation() -> MotivationSystem:
    global _motivation
    if _motivation is None:
        _motivation = MotivationSystem()
    return _motivation


if __name__ == "__main__":
    # Тестирование
    mot = get_motivation()
    print(mot.summary_text())
    
    # Тест решений
    print("\n=== ТЕСТ РЕШЕНИЙ ===")
    print(f"Исследовать (conf=0.3, unc=0.7): {mot.should_explore(0.3, 0.7)}")
    print(f"Исследовать (conf=0.8, unc=0.2): {mot.should_explore(0.8, 0.2)}")
    print(f"Проверить (trust=UNVERIFIED, conf=0.3): {mot.should_verify('UNVERIFIED', 0.3)}")
    print(f"Проверить (trust=SUPPORTED, conf=0.7): {mot.should_verify('SUPPORTED', 0.7)}")
    print(f"Уточнить (unc=0.8): {mot.should_ask_clarification(0.8)}")
    print(f"Уточнить (unc=0.2): {mot.should_ask_clarification(0.2)}")
    print(f"Использовать web (testability=fully_testable, conf=0.4): {mot.should_use_web('fully_testable', 0.4)}")
    
    # Обновление мотивации
    print("\n=== ОБНОВЛЕНИЕ МОТИВАЦИИ ===")
    mot.update_from_experience({"was_useful": True})
    mot.update_from_experience({"error": "недостаточно данных"})
    print(mot.summary_text())
