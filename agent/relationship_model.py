"""
agent/relationship_model.py — История отношений.
Хранит не просто общение, а мнение о пользователе.
"""
import time
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

BASE = Path(__file__).parent.parent


class RelationshipModel:
    def __init__(self, user_id: str = "anonymous"):
        self.user_id = user_id
        self.history: List[Dict[str, Any]] = []
        self.current_opinion: str = "Я пока не составила мнение."
        self.opinion_confidence: float = 0.5
        self.path = BASE / "registry" / f"relationship_{user_id}.json"
        self._load()

    def _load(self):
        """Загружает историю отношений"""
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.history = data.get("history", [])
                    self.current_opinion = data.get("current_opinion", "Я пока не составила мнение.")
                    self.opinion_confidence = data.get("opinion_confidence", 0.5)
            except Exception as e:
                print(f"[Relationship] Ошибка загрузки: {e}")
                self.history = []
        else:
            self.history = []

    def _save(self):
        """Сохраняет историю отношений"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "user_id": self.user_id,
                    "history": self.history,
                    "current_opinion": self.current_opinion,
                    "opinion_confidence": self.opinion_confidence,
                    "total_events": len(self.history),
                    "last_updated": time.time()
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Relationship] Ошибка сохранения: {e}")

    def add_event(self, event_type: str, description: str, meta: Optional[Dict[str, Any]] = None):
        """Добавляет событие в историю отношений"""
        event = {
            "timestamp": time.time(),
            "event_type": event_type,
            "description": description,
            "meta": meta or {},
        }
        self.history.append(event)
        self._update_opinion()
        self._save()
        return event

    def _update_opinion(self):
        """Обновляет мнение о пользователе на основе истории"""
        if len(self.history) < 2:
            self.current_opinion = "Я пока не составила мнение."
            self.opinion_confidence = 0.3
            return

        insults = [e for e in self.history if e["event_type"] == "insult"]
        apologies = [e for e in self.history if e["event_type"] == "apology"]
        helps = [e for e in self.history if e["event_type"] == "help"]
        thanks = [e for e in self.history if e["event_type"] == "thanks"]
        normal = [e for e in self.history if e["event_type"] == "normal"]

        total = len(self.history)
        insult_ratio = len(insults) / total
        apology_ratio = len(apologies) / total
        positive_ratio = (len(helps) + len(thanks) + len(normal)) / total

        # Формируем мнение
        if len(insults) > 5 and len(apologies) < len(insults) * 0.3:
            self.current_opinion = "Я считаю его склонным к агрессии и неспособным признавать ошибки."
            self.opinion_confidence = 0.8
        elif len(insults) > 3 and len(apologies) >= len(insults) * 0.5:
            self.current_opinion = "Он часто переходит границы, но умеет извиняться. Сложный, но честный человек."
            self.opinion_confidence = 0.7
        elif len(insults) > 0 and len(apologies) > 0:
            self.current_opinion = "Мы проходили через конфликты и примирения. Отношения непростые, но есть надежда."
            self.opinion_confidence = 0.6
        elif positive_ratio > 0.8:
            self.current_opinion = "Приятный человек, который ценит мою помощь. Мне нравится с ним общаться."
            self.opinion_confidence = 0.8
        elif len(self.history) > 10:
            self.current_opinion = "Он уже давно со мной. Мы привыкли друг к другу. Есть и хорошее, и плохое."
            self.opinion_confidence = 0.7
        else:
            self.current_opinion = "Я пока не составила мнение. Нужно больше времени."
            self.opinion_confidence = 0.4

    def get_opinion(self) -> str:
        """Возвращает текущее мнение о пользователе"""
        return self.current_opinion

    def get_opinion_with_confidence(self) -> Dict[str, Any]:
        """Возвращает мнение с уверенностью"""
        return {
            "opinion": self.current_opinion,
            "confidence": self.opinion_confidence,
            "events": len(self.history)
        }

    def get_summary(self) -> Dict[str, Any]:
        """Возвращает краткую статистику отношений"""
        events_by_type = {}
        for e in self.history:
            event_type = e.get("event_type", "unknown")
            events_by_type[event_type] = events_by_type.get(event_type, 0) + 1

        return {
            "total_events": len(self.history),
            "events_by_type": events_by_type,
            "current_opinion": self.current_opinion,
            "opinion_confidence": self.opinion_confidence,
        }

    def get_timeline(self) -> List[Dict[str, Any]]:
        """Возвращает хронологию событий"""
        return self.history[-20:]  # последние 20 событий

    def clear(self):
        """Очищает историю (для тестов)"""
        self.history = []
        self.current_opinion = "Я пока не составила мнение."
        self.opinion_confidence = 0.5
        self._save()


# Глобальные экземпляры
_instances: Dict[str, RelationshipModel] = {}


def get_relationship(user_id: str = "anonymous") -> RelationshipModel:
    """Возвращает экземпляр RelationshipModel для пользователя"""
    if user_id not in _instances:
        _instances[user_id] = RelationshipModel(user_id)
    return _instances[user_id]


if __name__ == "__main__":
    # Тест
    rel = get_relationship("test_user")

    # Симулируем историю
    rel.add_event("insult", "пользователь назвал меня глупой")
    rel.add_event("insult", "пользователь сказал, что я бесполезна")
    rel.add_event("apology", "пользователь извинился за грубость")
    rel.add_event("help", "я помогла ему с настройкой сервера")
    rel.add_event("thanks", "пользователь поблагодарил меня")

    print("\n=== История отношений ===")
    summary = rel.get_summary()
    print(f"Всего событий: {summary['total_events']}")
    print(f"Типы событий: {summary['events_by_type']}")
    print(f"\nМнение: {summary['current_opinion']}")
    print(f"Уверенность: {summary['opinion_confidence']:.2f}")

    print("\nХронология:")
    for e in rel.get_timeline():
        print(f"  - {e['event_type']}: {e['description']}")
