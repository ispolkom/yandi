"""
agent/experience_memory.py — Память опыта.
Хранит, как Янди реагировала на разные ситуации,
чтобы использовать этот опыт в будущем.
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

BASE = Path(__file__).parent.parent
EXPERIENCE_DIR = BASE / "registry" / "experiences"
EXPERIENCE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Experience:
    """Один опыт — ситуация + реакция"""
    id: str
    speech_act: str          # insult, sarcasm, confession, etc.
    topic: str               # general, romantic, work, etc.
    query: str               # что сказал пользователь
    response: str            # как ответила Янди
    user_reaction: str       # как отреагировал пользователь (позже)
    timestamp: float
    success: float = 0.5     # насколько удачным был ответ (будет обновляться)
    used_count: int = 0      # сколько раз использовался
    context: Dict = field(default_factory=dict)


class ExperienceMemory:
    """
    Хранит опыт Янди — ситуации и её реакции.
    """
    
    def __init__(self, user_id: str = "global"):
        self.user_id = user_id
        self.file_path = EXPERIENCE_DIR / f"{user_id}.json"
        self.experiences: List[Experience] = []
        self._load()
    
    def _load(self):
        """Загружает опыт из файла"""
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for exp_data in data.get("experiences", []):
                        self.experiences.append(Experience(
                            id=exp_data.get("id", ""),
                            speech_act=exp_data.get("speech_act", "unknown"),
                            topic=exp_data.get("topic", "general"),
                            query=exp_data.get("query", ""),
                            response=exp_data.get("response", ""),
                            user_reaction=exp_data.get("user_reaction", ""),
                            timestamp=exp_data.get("timestamp", time.time()),
                            success=exp_data.get("success", 0.5),
                            used_count=exp_data.get("used_count", 0),
                            context=exp_data.get("context", {}),
                        ))
            except Exception as e:
                print(f"[ExperienceMemory] Ошибка загрузки: {e}")
    
    def _save(self):
        """Сохраняет опыт в файл"""
        try:
            data = {
                "user_id": self.user_id,
                "updated_at": time.time(),
                "experiences": [
                    {
                        "id": e.id,
                        "speech_act": e.speech_act,
                        "topic": e.topic,
                        "query": e.query,
                        "response": e.response,
                        "user_reaction": e.user_reaction,
                        "timestamp": e.timestamp,
                        "success": e.success,
                        "used_count": e.used_count,
                        "context": e.context,
                    }
                    for e in self.experiences[-200:]  # храним последние 200
                ]
            }
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ExperienceMemory] Ошибка сохранения: {e}")
    
    def add_experience(self, speech_act: str, topic: str, query: str, 
                       response: str, context: Dict = None) -> str:
        """
        Добавляет новый опыт.
        Возвращает ID опыта.
        """
        exp_id = f"exp_{int(time.time())}_{len(self.experiences)}"
        
        experience = Experience(
            id=exp_id,
            speech_act=speech_act,
            topic=topic,
            query=query,
            response=response,
            user_reaction="",
            timestamp=time.time(),
            success=0.5,
            used_count=0,
            context=context or {},
        )
        
        self.experiences.append(experience)
        self._save()
        return exp_id
    
    def get_experience(self, speech_act: str, topic: str = None) -> Optional[Experience]:
        """
        Возвращает лучший опыт для ситуации.
        """
        # Ищем подходящие опыты
        candidates = []
        for exp in self.experiences:
            if exp.speech_act == speech_act:
                if topic is None or exp.topic == topic:
                    candidates.append(exp)
        
        if not candidates:
            return None
        
        # Выбираем с наибольшим success и used_count
        best = max(candidates, key=lambda e: (e.success, e.used_count))
        best.used_count += 1
        self._save()
        return best
    
    def update_success(self, exp_id: str, user_reaction: str, success: float):
        """
        Обновляет успешность опыта на основе реакции пользователя.
        """
        for exp in self.experiences:
            if exp.id == exp_id:
                exp.user_reaction = user_reaction
                exp.success = (exp.success + success) / 2  # среднее
                self._save()
                return
    
    
    
    def get_lessons(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Возвращает список уроков из опыта.
        Уроки ищутся в context.lessons каждой записи.
        """
        lessons = []
        for exp in self.experiences:
            context = getattr(exp, 'context', {})
            if context and 'lessons' in context and context['lessons']:
                lessons.append({
                    "query": getattr(exp, 'query', ''),
                    "domain": context.get('domain', 'unknown'),
                    "trust": context.get('trust', 'UNVERIFIED'),
                    "confidence": context.get('confidence', 0.0),
                    "mistakes": context.get('mistakes', []),
                    "lessons": context.get('lessons', []),
                    "policy_changes": context.get('policy_changes', []),
                    "timestamp": context.get('timestamp', ''),
                })
        # Сортируем по убыванию confidence
        lessons.sort(key=lambda x: x.get('confidence', 0.0), reverse=True)
    def get_relevant_lessons(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Возвращает уроки, релевантные текущему запросу.
        Использует простое сопоставление ключевых слов.
        """
        if not query:
            return self.get_lessons(limit)
        
        query_words = set(query.lower().split())
        scored_lessons = []
        
        for exp in self.experiences:
            context = getattr(exp, "context", {})
            if context and "lessons" in context and context["lessons"]:
                exp_query = getattr(exp, "query", "").lower()
                exp_words = set(exp_query.split())
                overlap = len(query_words & exp_words)
                score = overlap / max(len(query_words), 1)
                
                scored_lessons.append({
                    "score": score,
                    "lesson": {
                        "query": getattr(exp, "query", ""),
                        "domain": context.get("domain", "unknown"),
                        "trust": context.get("trust", "UNVERIFIED"),
                        "confidence": context.get("confidence", 0.0),
                        "mistakes": context.get("mistakes", []),
                        "lessons": context.get("lessons", []),
                        "policy_changes": context.get("policy_changes", []),
                        "timestamp": context.get("timestamp", ""),
                    }
                })
        
        scored_lessons.sort(key=lambda x: (x["score"], x["lesson"]["confidence"]), reverse=True)
        return [item["lesson"] for item in scored_lessons[:limit]]
    def get_relevant_lessons(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Возвращает уроки, релевантные текущему запросу.
        Использует простое сопоставление ключевых слов.
        """
        if not query:
            return self.get_lessons(limit)
        
        query_words = set(query.lower().split())
        scored_lessons = []
        
        for exp in self.experiences:
            context = getattr(exp, "context", {})
            if context and "lessons" in context and context["lessons"]:
                exp_query = getattr(exp, "query", "").lower()
                # Считаем пересечение слов
                exp_words = set(exp_query.split())
                overlap = len(query_words & exp_words)
                score = overlap / max(len(query_words), 1)
                
                scored_lessons.append({
                    "score": score,
                    "lesson": {
                        "query": getattr(exp, "query", ""),
                        "domain": context.get("domain", "unknown"),
                        "trust": context.get("trust", "UNVERIFIED"),
                        "confidence": context.get("confidence", 0.0),
                        "mistakes": context.get("mistakes", []),
                        "lessons": context.get("lessons", []),
                        "policy_changes": context.get("policy_changes", []),
                        "timestamp": context.get("timestamp", ""),
                    }
                })
        
        # Сортируем по score и confidence
        scored_lessons.sort(key=lambda x: (x["score"], x["lesson"]["confidence"]), reverse=True)
        return [item["lesson"] for item in scored_lessons[:limit]]
        return lessons[:limit]
    def get_stats(self) -> Dict:
        """Возвращает статистику по опыту"""
        acts = {}
        for exp in self.experiences:
            if exp.speech_act not in acts:
                acts[exp.speech_act] = 0
            acts[exp.speech_act] += 1
        
        return {
            "total_experiences": len(self.experiences),
            "speech_acts": acts,
            "avg_success": sum(e.success for e in self.experiences) / len(self.experiences) if self.experiences else 0,
        }


def get_experience_memory(user_id: str = "global") -> ExperienceMemory:
    return ExperienceMemory(user_id)


if __name__ == "__main__":
    memory = get_experience_memory("test_user")
    
    # Добавляем опыт
    memory.add_experience(
        speech_act="sarcasm",
        topic="general",
        query="Ну ты и умная, да?",
        response="Спасибо! Я стараюсь. А ты умеешь отличать сарказм от комплимента?",
        context={"trust": 50}
    )
    
    # Получаем опыт
    exp = memory.get_experience("sarcasm")
    if exp:
        print(f"Найден опыт: {exp.query[:50]} → {exp.response[:50]}")
    
    print(f"Статистика: {memory.get_stats()}")
