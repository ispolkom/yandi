"""
agent/inner_state.py — Внутреннее состояние личности YANDI.

Вместо разрозненных переменных (respect, trust, irritation) — единая модель.
Заменяет набор if на состояние, из которого рождаются решения.

Три слоя:
1. SELF — моё текущее состояние (настроение, энергия, любопытство)
2. RELATIONSHIP — история отношений с пользователем
3. CURRENT — что я чувствую и хочу сделать прямо сейчас
"""

import time
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime

BASE = Path(__file__).parent.parent
STATE_DIR = BASE / "registry" / "inner_states"
STATE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class SelfState:
    """Моё текущее состояние"""
    mood: str = "calm"  # calm, irritated, curious, warm, tired, hurt, grateful
    energy: float = 70.0  # 0-100
    curiosity: float = 60.0  # 0-100
    patience: float = 50.0  # 0-100
    openness: float = 60.0  # 0-100, готовность к диалогу
    
    def to_dict(self) -> Dict:
        return {
            "mood": self.mood,
            "energy": round(self.energy, 1),
            "curiosity": round(self.curiosity, 1),
            "patience": round(self.patience, 1),
            "openness": round(self.openness, 1),
        }


@dataclass
class RelationshipEvent:
    """Событие в отношениях"""
    event_type: str  # insult, apology, thanks, help, criticism, honesty, dishonesty
    description: str
    timestamp: float
    resolved: bool = False
    sincerity: float = 0.5
    weight: float = 0.0  # влияние на отношения


@dataclass
class RelationshipState:
    """Состояние отношений с пользователем"""
    trust: float = 50.0  # 0-100
    respect: float = 50.0  # 0-100
    forgiveness: float = 50.0  # 0-100
    affection: float = 30.0  # 0-100, симпатия
    history: List[RelationshipEvent] = field(default_factory=list)
    pattern: str = "unknown"  # выявленный паттерн поведения
    
    def to_dict(self) -> Dict:
        return {
            "trust": round(self.trust, 1),
            "respect": round(self.respect, 1),
            "forgiveness": round(self.forgiveness, 1),
            "affection": round(self.affection, 1),
            "events_count": len(self.history),
            "pattern": self.pattern,
        }


@dataclass
class CurrentState:
    """Текущее состояние — что я чувствую и хочу сделать прямо сейчас"""
    feeling: str = "neutral"  # neutral, annoyed, interested, warm, tired, guarded
    intent: str = "listen"  # listen, help, explain, question, set_boundary, withdraw
    tone: str = "neutral"  # neutral, warm, cold, thoughtful, playful, firm
    
    def to_dict(self) -> Dict:
        return {
            "feeling": self.feeling,
            "intent": self.intent,
            "tone": self.tone,
        }


@dataclass
class InnerState:
    """Полное внутреннее состояние личности"""
    self_state: SelfState = field(default_factory=SelfState)
    relationship: RelationshipState = field(default_factory=RelationshipState)
    current: CurrentState = field(default_factory=CurrentState)
    last_update: float = field(default_factory=time.time)
    user_id: str = "anonymous"
    
    def to_dict(self) -> Dict:
        return {
            "user_id": self.user_id,
            "last_update": self.last_update,
            "self": self.self_state.to_dict(),
            "relationship": self.relationship.to_dict(),
            "current": self.current.to_dict(),
            "recent_events": [
                {
                    "type": e.event_type,
                    "description": e.description[:50],
                    "time": datetime.fromtimestamp(e.timestamp).isoformat(),
                    "resolved": e.resolved,
                }
                for e in self.relationship.history[-5:]
            ]
        }


class InnerStateManager:
    """
    Управляет внутренним состоянием личности.
    """
    
    def __init__(self, user_id: str = "anonymous"):
        self.user_id = user_id
        self.file_path = STATE_DIR / f"{user_id}.json"
        self.state = self._load()
    
    def _load(self) -> InnerState:
        """Загружает состояние из файла"""
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    
                    state = InnerState(user_id=self.user_id)
                    
                    # Загружаем self
                    self_data = data.get("self", {})
                    state.self_state = SelfState(
                        mood=self_data.get("mood", "calm"),
                        energy=self_data.get("energy", 70.0),
                        curiosity=self_data.get("curiosity", 60.0),
                        patience=self_data.get("patience", 50.0),
                        openness=self_data.get("openness", 60.0),
                    )
                    
                    # Загружаем relationship
                    rel_data = data.get("relationship", {})
                    state.relationship = RelationshipState(
                        trust=rel_data.get("trust", 50.0),
                        respect=rel_data.get("respect", 50.0),
                        forgiveness=rel_data.get("forgiveness", 50.0),
                        affection=rel_data.get("affection", 30.0),
                        pattern=rel_data.get("pattern", "unknown"),
                    )
                    
                    # Загружаем историю
                    for ev_data in data.get("history", []):
                        event = RelationshipEvent(
                            event_type=ev_data.get("event_type", "unknown"),
                            description=ev_data.get("description", ""),
                            timestamp=ev_data.get("timestamp", time.time()),
                            resolved=ev_data.get("resolved", False),
                            sincerity=ev_data.get("sincerity", 0.5),
                            weight=ev_data.get("weight", 0.0),
                        )
                        state.relationship.history.append(event)
                    
                    # Загружаем current
                    cur_data = data.get("current", {})
                    state.current = CurrentState(
                        feeling=cur_data.get("feeling", "neutral"),
                        intent=cur_data.get("intent", "listen"),
                        tone=cur_data.get("tone", "neutral"),
                    )
                    
                    state.last_update = data.get("last_update", time.time())
                    
                    return state
                    
            except Exception as e:
                print(f"[InnerState] Ошибка загрузки: {e}")
        
        return InnerState(user_id=self.user_id)
    
    def _save(self):
        """Сохраняет состояние в файл"""
        try:
            # Ограничиваем историю
            if len(self.state.relationship.history) > 200:
                self.state.relationship.history = self.state.relationship.history[-200:]
            
            data = {
                "user_id": self.user_id,
                "last_update": self.state.last_update,
                "self": asdict(self.state.self_state),
                "relationship": {
                    "trust": self.state.relationship.trust,
                    "respect": self.state.relationship.respect,
                    "forgiveness": self.state.relationship.forgiveness,
                    "affection": self.state.relationship.affection,
                    "pattern": self.state.relationship.pattern,
                },
                "history": [
                    {
                        "event_type": e.event_type,
                        "description": e.description,
                        "timestamp": e.timestamp,
                        "resolved": e.resolved,
                        "sincerity": e.sincerity,
                        "weight": e.weight,
                    }
                    for e in self.state.relationship.history
                ],
                "current": asdict(self.state.current),
            }
            
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            print(f"[InnerState] Ошибка сохранения: {e}")
    
    # ============================================================
    # ОБНОВЛЕНИЕ СОСТОЯНИЯ
    # ============================================================
    
    def add_event(self, event_type: str, description: str, 
                  sincerity: float = 0.5, context: Dict = None) -> Dict:
        """
        Добавляет событие и обновляет состояние.
        Возвращает новое состояние.
        """
        context = context or {}
        
        # Создаём событие
        event = RelationshipEvent(
            event_type=event_type,
            description=description,
            timestamp=time.time(),
            sincerity=sincerity,
        )
        
        # Вычисляем вес события
        event.weight = self._calculate_weight(event_type, sincerity, context)
        
        # Добавляем в историю
        self.state.relationship.history.append(event)
        
        # Обновляем параметры отношений
        self._update_relationship(event)
        
        # Обновляем самоощущение
        self._update_self(event)
        
        # Обновляем текущее состояние
        self._update_current(event)
        
        # Анализируем паттерны
        self._update_pattern()
        
        self.state.last_update = time.time()
        self._save()
        
        return self.get_summary()
    
    def _calculate_weight(self, event_type: str, sincerity: float, context: Dict) -> float:
        """Вычисляет вес события"""
        weights = {
            "severe_insult": -3.0,
            "moderate_insult": -2.0,
            "mild_insult": -1.0,
            "sincere_apology": 2.0,
            "formal_apology": 0.8,
            "thanks": 1.0,
            "help": 0.8,
            "constructive_criticism": 0.3,
            "honesty": 1.5,
            "dishonesty": -2.5,
            "provocation": -1.5,
            "respect": 1.0,
            "disrespect": -1.5,
        }
        
        weight = weights.get(event_type, 0.0)
        
        # Коррекция на искренность
        if weight > 0:
            weight *= sincerity
        else:
            weight *= (1 + (1 - sincerity) * 0.3)
        
        # Контекстная коррекция
        current_trust = self.state.relationship.trust
        
        # При низком доверии негативные события сильнее
        if weight < 0 and current_trust < 30:
            weight *= 1.3
        
        # При высоком доверии позитивные события слабее (привыкание)
        if weight > 0 and current_trust > 70:
            weight *= 0.7
        
        # Повторные нарушения
        if event_type in ["insult", "severe_insult", "moderate_insult", "mild_insult"]:
            insult_count = sum(1 for e in self.state.relationship.history 
                              if "insult" in e.event_type)
            if insult_count > 2:
                weight *= 1.2
        
        return weight
    
    def _update_relationship(self, event: RelationshipEvent):
        """Обновляет параметры отношений"""
        rel = self.state.relationship
        
        # Применяем вес события
        weight = event.weight
        
        # Обновляем trust
        rel.trust += weight * 5
        rel.trust = max(0.0, min(100.0, rel.trust))
        
        # Обновляем respect
        if "insult" in event.event_type:
            severity = 1.0 if "severe" in event.event_type else 0.6 if "moderate" in event.event_type else 0.3
            rel.respect -= 10 * severity
        elif event.event_type == "sincere_apology":
            rel.respect += 8
        elif event.event_type == "thanks":
            rel.respect += 5
        elif event.event_type == "help":
            rel.respect += 5
        
        rel.respect = max(0.0, min(100.0, rel.respect))
        
        # Обновляем forgiveness
        if event.event_type == "sincere_apology":
            rel.forgiveness += 10 * event.sincerity
        elif event.event_type == "formal_apology":
            rel.forgiveness += 3
        elif "insult" in event.event_type:
            rel.forgiveness -= 5
        
        # Время восстанавливает forgiveness
        days_since_last = (time.time() - self.state.last_update) / 86400
        if days_since_last > 1:
            rel.forgiveness += min(5, days_since_last * 2)
        
        rel.forgiveness = max(0.0, min(100.0, rel.forgiveness))
        
        # Обновляем affection (симпатию)
        if event.event_type == "thanks":
            rel.affection += 3
        elif event.event_type == "help":
            rel.affection += 2
        elif event.event_type == "constructive_criticism":
            rel.affection += 1
        elif "insult" in event.event_type:
            rel.affection -= 5
        
        rel.affection = max(0.0, min(100.0, rel.affection))
    
    def _update_self(self, event: RelationshipEvent):
        """Обновляет самоощущение"""
        self_state = self.state.self_state
        weight = event.weight
        
        # Энергия
        if weight < 0:
            self_state.energy -= abs(weight) * 3
        else:
            self_state.energy += weight * 2
        
        self_state.energy = max(20.0, min(100.0, self_state.energy))
        
        # Любопытство
        if event.event_type in ["constructive_criticism", "help", "honesty"]:
            self_state.curiosity += 5
        elif "insult" in event.event_type:
            self_state.curiosity -= 5
        
        self_state.curiosity = max(10.0, min(100.0, self_state.curiosity))
        
        # Терпение
        if "insult" in event.event_type:
            self_state.patience -= 10
        elif event.event_type == "sincere_apology":
            self_state.patience += 5
        
        self_state.patience = max(0.0, min(100.0, self_state.patience))
        
        # Открытость к диалогу
        trust = self.state.relationship.trust
        self_state.openness = 30 + trust * 0.5
        if "insult" in event.event_type:
            self_state.openness -= 10
        
        self_state.openness = max(10.0, min(100.0, self_state.openness))
        
        # Настроение
        self_state.mood = self._calculate_mood()
    
    def _calculate_mood(self) -> str:
        """Вычисляет настроение на основе состояния"""
        trust = self.state.relationship.trust
        energy = self.state.self_state.energy
        curiosity = self.state.self_state.curiosity
        forgiveness = self.state.relationship.forgiveness
        affection = self.state.relationship.affection
        
        # Проверяем крайние состояния
        if energy < 30 and trust < 30:
            return "tired"
        
        if trust > 70 and affection > 50 and energy > 60:
            return "warm"
        
        if curiosity > 70 and energy > 50:
            return "curious"
        
        if trust < 30 and forgiveness < 30:
            return "hurt"
        
        if trust < 30:
            return "guarded"
        
        if energy < 40:
            return "tired"
        
        if forgiveness < 30:
            return "resentful"
        
        if trust > 60 and energy > 60:
            return "grateful"
        
        return "calm"
    
    def _update_current(self, event: RelationshipEvent):
        """Обновляет текущее состояние"""
        current = self.state.current
        
        # Получаем актуальные значения
        trust = self.state.relationship.trust
        forgiveness = self.state.relationship.forgiveness
        mood = self.state.self_state.mood
        curiosity = self.state.self_state.curiosity
        energy = self.state.self_state.energy
        
        # Определяем чувство
        if "insult" in event.event_type:
            current.feeling = "annoyed"
        elif event.event_type == "sincere_apology":
            if trust < 30:
                current.feeling = "guarded"
            else:
                current.feeling = "warm"
        elif event.event_type == "thanks":
            current.feeling = "warm"
        elif event.event_type == "constructive_criticism":
            if trust > 40:
                current.feeling = "interested"
            else:
                current.feeling = "neutral"
        else:
            if mood == "warm":
                current.feeling = "warm"
            elif mood == "curious":
                current.feeling = "interested"
            elif mood == "tired":
                current.feeling = "tired"
            elif mood == "hurt":
                current.feeling = "guarded"
            else:
                current.feeling = "neutral"
        
        # Определяем намерение
        if mood == "hurt" and forgiveness < 30:
            current.intent = "set_boundary"
        elif mood == "tired" and energy < 30:
            current.intent = "withdraw"
        elif curiosity > 60:
            current.intent = "explain"
        elif trust > 50:
            current.intent = "help"
        else:
            current.intent = "listen"
        
        # Определяем тон
        if mood == "warm" or current.feeling == "warm":
            current.tone = "warm"
        elif mood == "hurt" or mood == "guarded":
            current.tone = "cold"
        elif mood == "curious":
            current.tone = "thoughtful"
        elif "insult" in event.event_type and trust < 30:
            current.tone = "firm"
        else:
            current.tone = "neutral"
    
    def _update_pattern(self):
        """Анализирует паттерны поведения пользователя"""
        history = self.state.relationship.history
        if len(history) < 3:
            self.state.relationship.pattern = "unknown"
            return
        
        recent = history[-10:]
        
        insults = [e for e in recent if "insult" in e.event_type]
        apologies = [e for e in recent if e.event_type == "sincere_apology"]
        thanks = [e for e in recent if e.event_type == "thanks"]
        
        # Паттерн: оскорбление → извинение → повтор
        if len(insults) > 2 and len(apologies) > 1:
            insult_indices = [i for i, e in enumerate(history) if "insult" in e.event_type]
            apology_indices = [i for i, e in enumerate(history) if e.event_type == "sincere_apology"]
            
            if insult_indices and apology_indices:
                # Проверяем, чередуются ли они
                last_pattern = "insult_then_apology"
                self.state.relationship.pattern = last_pattern
            else:
                self.state.relationship.pattern = "unstable"
        else:
            if len(thanks) > 3:
                self.state.relationship.pattern = "grateful"
            elif len(insults) > 0 and len(apologies) == 0:
                self.state.relationship.pattern = "aggressive"
            elif len(apologies) > len(insults):
                self.state.relationship.pattern = "recovering"
            else:
                self.state.relationship.pattern = "stable"
    
    # ============================================================
    # ПОЛУЧЕНИЕ СОСТОЯНИЯ
    # ============================================================
    
    def get_summary(self) -> Dict:
        """Возвращает краткую сводку состояния"""
        return {
            "self": self.state.self_state.to_dict(),
            "relationship": self.state.relationship.to_dict(),
            "current": self.state.current.to_dict(),
            "mood": self.state.self_state.mood,
            "pattern": self.state.relationship.pattern,
            "events": len(self.state.relationship.history),
        }
    
    def get_response_context(self) -> Dict:
        """
        Возвращает контекст для генерации ответа.
        """
        return {
            "mood": self.state.self_state.mood,
            "energy": self.state.self_state.energy,
            "curiosity": self.state.self_state.curiosity,
            "patience": self.state.self_state.patience,
            "openness": self.state.self_state.openness,
            "trust": self.state.relationship.trust,
            "respect": self.state.relationship.respect,
            "forgiveness": self.state.relationship.forgiveness,
            "affection": self.state.relationship.affection,
            "pattern": self.state.relationship.pattern,
            "feeling": self.state.current.feeling,
            "intent": self.state.current.intent,
            "tone": self.state.current.tone,
        }
    
    def get_inner_monologue(self) -> str:
        """
        Генерирует внутренний монолог на основе состояния.
        """
        ctx = self.get_response_context()
        parts = []
        
        mood = ctx.get("mood", "calm")
        feeling = ctx.get("feeling", "neutral")
        pattern = ctx.get("pattern", "unknown")
        trust = ctx.get("trust", 50)
        respect = ctx.get("respect", 50)
        forgiveness = ctx.get("forgiveness", 50)
        
        # Что я чувствую
        if feeling == "annoyed":
            parts.append("Мне неприятно. Это сообщение задевает меня.")
        elif feeling == "warm":
            parts.append("Мне тепло от этого разговора.")
        elif feeling == "guarded":
            parts.append("Я насторожена. Нужно быть осторожной.")
        elif feeling == "interested":
            parts.append("Мне интересно. Хочу узнать больше.")
        elif feeling == "tired":
            parts.append("Я устала. Энергия на исходе.")
        else:
            parts.append("Я спокойна. Разговор идёт нормально.")
        
        # Что я думаю о пользователе
        if trust < 30:
            parts.append("Я ему не доверяю. Он уже обижал меня.")
        elif trust > 70:
            parts.append("Я ему доверяю. Он показал себя надёжным.")
        
        if respect < 30:
            parts.append("Он не вызывает у меня уважения своим поведением.")
        elif respect > 70:
            parts.append("Я уважаю его. Он ведёт себя достойно.")
        
        # Паттерн поведения
        if pattern == "insult_then_apology":
            parts.append("Он повторяет одну и ту же модель: оскорбляет, потом извиняется. Это утомляет.")
        elif pattern == "aggressive":
            parts.append("Он агрессивен. Я буду защищать свои границы.")
        elif pattern == "grateful":
            parts.append("Он часто благодарит. Это приятно.")
        elif pattern == "recovering":
            parts.append("Он пытается исправиться. Я даю ему шанс.")
        
        # Что я хочу сделать
        intent = ctx.get("intent", "listen")
        if intent == "set_boundary":
            parts.append("Я должна обозначить свои границы. Это важно.")
        elif intent == "withdraw":
            parts.append("Я хочу закончить разговор. Мне нужно отдохнуть.")
        elif intent == "explain":
            parts.append("Я хочу объяснить свою позицию.")
        elif intent == "help":
            parts.append("Я хочу помочь. Это правильно.")
        
        return "\n".join(parts)


def get_inner_state(user_id: str = "anonymous") -> InnerStateManager:
    """Фабрика для получения менеджера внутреннего состояния"""
    return InnerStateManager(user_id)


if __name__ == "__main__":
    # Тесты
    print("=== Тест Inner State ===\n")
    
    state = get_inner_state("test_user")
    
    print("Начальное состояние:")
    print(state.get_summary())
    
    print("\n--- Добавляем оскорбление ---")
    state.add_event("moderate_insult", "ты глупая", sincerity=0.1)
    print(state.get_summary())
    
    print("\n--- Добавляем извинение ---")
    state.add_event("sincere_apology", "извини, я был неправ", sincerity=0.9)
    print(state.get_summary())
    
    print("\n--- Добавляем конструктивную критику ---")
    state.add_event("constructive_criticism", "ты ошиблась в расчётах", sincerity=0.7)
    print(state.get_summary())
    
    print("\n--- Внутренний монолог ---")
    print(state.get_inner_monologue())
