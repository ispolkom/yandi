"""
agent/trust_model.py — Управление доверием.

Доверие — это не просто число от 0 до 100.
Это история взаимодействий, которая формирует ожидания.

Принципы:
1. Доверие строится медленно, рушится быстро
2. Каждое событие имеет вес
3. Контекст влияет на изменение
4. Доверие предсказывает поведение
"""

import time
import json
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path

BASE = Path(__file__).parent.parent


@dataclass
class TrustEvent:
    """Событие, влияющее на доверие"""
    event_type: str  # apology, insult, honesty, dishonesty, help, consistency
    description: str
    weight: float  # -1.0 до 1.0
    timestamp: float
    sincerity: float = 0.5  # 0-1, насколько искренне
    context: Dict = field(default_factory=dict)


@dataclass
class TrustState:
    """Текущее состояние доверия"""
    level: float = 50.0  # 0-100
    trend: str = "stable"  # rising | stable | falling
    history: List[TrustEvent] = field(default_factory=list)
    prediction: str = "unknown"  # что я ожидаю от пользователя
    last_update: float = field(default_factory=time.time)


class TrustModel:
    """
    Модель доверия с учётом истории и контекста.
    """
    
    def __init__(self, user_id: str = "anonymous"):
        self.user_id = user_id
        self.state = TrustState()
        self._load()
    
    def _load(self):
        """Загружает состояние из файла"""
        path = BASE / f"registry/trust_{self.user_id}.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.state.level = data.get("level", 50.0)
                    self.state.trend = data.get("trend", "stable")
                    self.state.prediction = data.get("prediction", "unknown")
                    # Загружаем историю
                    for event_data in data.get("history", []):
                        self.state.history.append(TrustEvent(
                            event_type=event_data.get("event_type", "unknown"),
                            description=event_data.get("description", ""),
                            weight=event_data.get("weight", 0.0),
                            timestamp=event_data.get("timestamp", time.time()),
                            sincerity=event_data.get("sincerity", 0.5),
                            context=event_data.get("context", {}),
                        ))
            except Exception as e:
                print(f"[Trust] Ошибка загрузки: {e}")
    
    def _save(self):
        """Сохраняет состояние в файл"""
        path = BASE / f"registry/trust_{self.user_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "level": self.state.level,
                "trend": self.state.trend,
                "prediction": self.state.prediction,
                "last_update": self.state.last_update,
                "history": [
                    {
                        "event_type": e.event_type,
                        "description": e.description,
                        "weight": e.weight,
                        "timestamp": e.timestamp,
                        "sincerity": e.sincerity,
                        "context": e.context,
                    }
                    for e in self.state.history[-100:]  # храним последние 100 событий
                ]
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Trust] Ошибка сохранения: {e}")
    
    def add_event(self, event_type: str, description: str, 
                  sincerity: float = 0.5, context: Dict = None) -> float:
        """
        Добавляет событие и обновляет доверие.
        Возвращает новое значение доверия.
        """
        context = context or {}
        
        # ---- ВЕС СОБЫТИЯ ----
        weight = self._calculate_weight(event_type, sincerity, context)
        
        # ---- СОЗДАЁМ СОБЫТИЕ ----
        event = TrustEvent(
            event_type=event_type,
            description=description,
            weight=weight,
            timestamp=time.time(),
            sincerity=sincerity,
            context=context,
        )
        
        self.state.history.append(event)
        
        # ---- ОБНОВЛЯЕМ УРОВЕНЬ ----
        self.state.level += weight * 5  # масштабируем
        
        # Ограничиваем
        self.state.level = max(0.0, min(100.0, self.state.level))
        
        # ---- ОБНОВЛЯЕМ ТРЕНД ----
        self._update_trend()
        
        # ---- ОБНОВЛЯЕМ ПРОГНОЗ ----
        self._update_prediction()
        
        self.state.last_update = time.time()
        self._save()
        
        return self.state.level
    
    def _calculate_weight(self, event_type: str, sincerity: float, 
                          context: Dict) -> float:
        """
        Вычисляет вес события.
        Положительные события строят доверие (медленно).
        Отрицательные рушат доверие (быстро).
        """
        
        # Базовые веса
        weights = {
            "apology": 0.6,
            "sincere_apology": 1.0,
            "insult": -1.5,
            "repeated_insult": -2.5,
            "honesty": 0.8,
            "dishonesty": -1.8,
            "help": 0.5,
            "consistency": 0.4,
            "inconsistency": -0.6,
            "criticism": 0.1,  # конструктивная критика не вредит доверию
            "insult_with_criticism": -0.8,
            "respect": 0.3,
            "disrespect": -1.0,
            "manipulation": -2.0,
        }
        
        weight = weights.get(event_type, 0.0)
        
        # Коррекция на искренность
        if weight > 0:
            weight *= sincerity
        else:
            weight *= (1 + (1 - sincerity) * 0.5)  # неискренность усиливает негатив
        
        # Коррекция на контекст
        trust_level = context.get("current_trust", self.state.level)
        
        # При низком доверии негативные события сильнее
        if weight < 0 and trust_level < 30:
            weight *= 1.3
        
        # При высоком доверии позитивные события слабее (привыкание)
        if weight > 0 and trust_level > 70:
            weight *= 0.7
        
        # Повторные нарушения
        if event_type == "insult" and self._count_events("insult") > 2:
            weight *= 1.2
        
        return weight
    
    def _count_events(self, event_type: str) -> int:
        """Считает количество событий определённого типа"""
        return sum(1 for e in self.state.history if e.event_type == event_type)
    
    def _update_trend(self):
        """Обновляет тренд на основе последних событий"""
        recent = self.state.history[-10:]  # последние 10 событий
        
        if len(recent) < 3:
            self.state.trend = "stable"
            return
        
        positive = sum(1 for e in recent if e.weight > 0)
        negative = len(recent) - positive
        
        if positive > negative + 2:
            self.state.trend = "rising"
        elif negative > positive + 2:
            self.state.trend = "falling"
        else:
            self.state.trend = "stable"
    
    def _update_prediction(self):
        """Обновляет прогноз поведения пользователя"""
        level = self.state.level
        insults = self._count_events("insult")
        apologies = self._count_events("sincere_apology")
        
        if level > 70:
            self.state.prediction = "positive"
        elif level < 30:
            if insults > apologies:
                self.state.prediction = "negative"
            else:
                self.state.prediction = "cautious"
        else:
            self.state.prediction = "neutral"
    
    def get_level(self) -> float:
        """Возвращает текущий уровень доверия"""
        return self.state.level
    
    def get_trend(self) -> str:
        """Возвращает тренд"""
        return self.state.trend
    
    def get_prediction(self) -> str:
        """Возвращает прогноз"""
        return self.state.prediction
    
    def get_history(self, limit: int = 20) -> List[TrustEvent]:
        """Возвращает историю событий"""
        return self.state.history[-limit:]
    
    def get_summary(self) -> Dict:
        """Возвращает краткую сводку"""
        return {
            "level": round(self.state.level, 1),
            "trend": self.state.trend,
            "prediction": self.state.prediction,
            "events": len(self.state.history),
            "insults": self._count_events("insult"),
            "apologies": self._count_events("sincere_apology"),
            "last_update": self.state.last_update,
        }
    
    def should_trust(self, action: str, confidence: float = 0.5) -> Tuple[bool, str]:
        """
        Определяет, стоит ли доверять действию.
        Возвращает (доверять, причина).
        """
        level = self.state.level
        
        if action == "apology":
            if level > 30:
                return True, "доверие достаточно высокое"
            else:
                return False, "доверие слишком низкое для принятия извинения"
        
        if action == "criticism":
            if level > 20:
                return True, "критика принимается как конструктивная"
            else:
                return False, "критика воспринимается как атака"
        
        if action == "help":
            if level > 10:
                return True, "я готова помочь"
            else:
                return False, "доверие слишком низкое"
        
        # По умолчанию
        if level > 40:
            return True, "доверие достаточное"
        else:
            return False, "доверие недостаточное"


def get_trust_model(user_id: str = "anonymous") -> TrustModel:
    """Фабрика для получения модели доверия"""
    return TrustModel(user_id)


if __name__ == "__main__":
    # Тесты
    print("=== Тест Trust Model ===\n")
    
    trust = get_trust_model("test_user")
    print(f"Начальное доверие: {trust.get_level():.1f}")
    
    # ---- СИМУЛЯЦИЯ ----
    events = [
        ("insult", "ты глупая", 0.2),
        ("insult", "ты ничего не понимаешь", 0.1),
        ("criticism", "ты ошиблась в расчётах", 0.5),
        ("insult", "как у тебя вообще язык поворачивается", 0.1),
        ("sincere_apology", "извини, я был груб", 0.9),
        ("help", "помоги разобраться", 0.7),
        ("honesty", "я признаю, что был неправ", 0.8),
    ]
    
    for event_type, description, sincerity in events:
        new_level = trust.add_event(event_type, description, sincerity)
        summary = trust.get_summary()
        print(f"\nСобытие: {event_type} ({sincerity:.1f})")
        print(f"  Описание: {description}")
        print(f"  Уровень доверия: {new_level:.1f}")
        print(f"  Тренд: {summary['trend']}")
        print(f"  Прогноз: {summary['prediction']}")
        print(f"  Всего событий: {summary['events']}")
    
    print("\n=== Проверка решений ===")
    print(f"Доверять извинению: {trust.should_trust('apology')}")
    print(f"Доверять критике: {trust.should_trust('criticism')}")
