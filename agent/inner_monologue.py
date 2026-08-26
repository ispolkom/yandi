"""
agent/inner_monologue.py — Внутренний монолог Янди.
Записывает внутренние состояния, чувства, решения и ожидания.
Это автобиография личности.
"""
import time
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

BASE = Path(__file__).parent.parent


class InnerMonologue:
    def __init__(self, user_id: str = "anonymous"):
        self.user_id = user_id
        self.events: List[Dict[str, Any]] = []
        self.path = BASE / "registry" / f"inner_monologue_{user_id}.json"
        self._load()

    def _load(self):
        """Загружает внутренний монолог из файла"""
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.events = data.get("events", [])
            except Exception as e:
                print(f"[InnerMonologue] Ошибка загрузки: {e}")
                self.events = []
        else:
            self.events = []

    def _save(self):
        """Сохраняет внутренний монолог"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "user_id": self.user_id,
                    "events": self.events,
                    "total_events": len(self.events),
                    "last_updated": time.time()
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[InnerMonologue] Ошибка сохранения: {e}")

    def add_event(
        self,
        event_type: str,
        feeling: str,
        reason: str,
        decision: str,
        expectation: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ):
        """Добавляет событие во внутренний монолог"""
        event = {
            "timestamp": time.time(),
            "event_type": event_type,
            "feeling": feeling,
            "reason": reason,
            "decision": decision,
            "expectation": expectation or "неизвестно",
            "meta": meta or {},
        }
        self.events.append(event)
        self._save()
        return event

    def add_from_state(self, state: Dict[str, Any], query: str, event_type: str = "interaction"):
        """Генерирует внутренний монолог из состояния"""
        irritation = state.get("irritation", 10)
        respect = state.get("respect", 50)
        trust = state.get("trust", 50)

        # Определяем чувство
        if irritation > 80:
            feeling = "сильное раздражение"
        elif irritation > 60:
            feeling = "раздражена"
        elif irritation > 40:
            feeling = "насторожена"
        elif trust < 30:
            feeling = "недоверие"
        elif trust > 70:
            feeling = "доверие"
        elif respect < 30:
            feeling = "разочарование"
        else:
            feeling = "спокойна"

        # Определяем решение
        if irritation > 80:
            decision = "прекратить диалог"
        elif irritation > 60:
            decision = "жёстко обозначить границы"
        elif irritation > 40:
            decision = "спокойно обозначить границы"
        elif trust < 30:
            decision = "отвечать сдержанно"
        else:
            decision = "продолжить диалог"

        return self.add_event(
            event_type=event_type,
            feeling=feeling,
            reason=f"состояние: раздражение={irritation:.0f}, уважение={respect:.0f}, доверие={trust:.0f}",
            decision=decision,
            expectation=f"пользователь задал: {query[:50]}",
            meta={"query": query[:100], "state": state}
        )

    def get_recent_events(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Возвращает последние события"""
        return self.events[-limit:]

    def get_summary(self) -> Dict[str, Any]:
        """Возвращает краткое резюме внутренней жизни"""
        if not self.events:
            return {
                "total_events": 0,
                "most_common_feeling": "неизвестно",
                "last_event": None,
                "summary": "Внутренняя жизнь пока не началась."
            }

        feelings = {}
        for e in self.events:
            feeling = e.get("feeling", "неизвестно")
            feelings[feeling] = feelings.get(feeling, 0) + 1

        most_common = max(feelings.items(), key=lambda x: x[1])[0] if feelings else "неизвестно"

        return {
            "total_events": len(self.events),
            "most_common_feeling": most_common,
            "last_event": self.events[-1] if self.events else None,
            "summary": f"Я чувствовала {most_common} в {len(self.events)} случаях"
        }

    def clear(self):
        """Очищает внутренний монолог (для тестов)"""
        self.events = []
        self._save()


# Глобальный экземпляр (будет создан для каждого пользователя)
_instances: Dict[str, InnerMonologue] = {}


def get_inner_monologue(user_id: str = "anonymous") -> InnerMonologue:
    """Возвращает экземпляр InnerMonologue для пользователя"""
    if user_id not in _instances:
        _instances[user_id] = InnerMonologue(user_id)
    return _instances[user_id]


if __name__ == "__main__":
    # Тест
    im = get_inner_monologue("test_user")
    im.add_event(
        event_type="insult",
        feeling="раздражена",
        reason="пользователь оскорбил меня",
        decision="обозначить границы",
        expectation="возможно извинится"
    )
    im.add_event(
        event_type="apology",
        feeling="спокойна",
        reason="пользователь извинился",
        decision="дать шанс",
        expectation="наблюдать за поведением"
    )
    print(im.get_summary())
    print("Последние события:")
    for e in im.get_recent_events(2):
        print(f"  - {e['event_type']}: {e['feeling']} ({e['decision']})")
