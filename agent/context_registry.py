"""
agent/context_registry.py — Реестр контекста для Янди.
Хранит темы обсуждений с датами, чтобы понимать, есть ли у неё контекст для критики.
"""

import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

BASE = Path(__file__).parent.parent
REGISTRY_DIR = BASE / "registry" / "context"
REGISTRY_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ContextInstance:
    """Один экземпляр обсуждения темы"""
    timestamp: float
    query: str
    response: str
    topic: str
    type: str  # calculation, explanation, analysis, etc.
    source: str  # user, yandi


@dataclass
class TopicContext:
    """Контекст по одной теме"""
    topic: str
    instances: List[ContextInstance] = field(default_factory=list)
    last_activity: float = 0.0
    total_instances: int = 0
    
    def add_instance(self, instance: ContextInstance):
        self.instances.append(instance)
        self.last_activity = max(self.last_activity, instance.timestamp)
        self.total_instances += 1
        
        # Ограничиваем историю
        if len(self.instances) > 100:
            self.instances = self.instances[-100:]
    
    def get_recent(self, limit: int = 5) -> List[ContextInstance]:
        """Возвращает последние N экземпляров"""
        return sorted(self.instances, key=lambda x: x.timestamp, reverse=True)[:limit]
    
    def is_recent(self, hours: float = 2) -> bool:
        """Была ли активность по теме в последние N часов"""
        if self.last_activity == 0:
            return False
        age_hours = (time.time() - self.last_activity) / 3600
        return age_hours < hours
    
    def to_dict(self) -> Dict:
        return {
            "topic": self.topic,
            "last_activity": self.last_activity,
            "total_instances": self.total_instances,
            "instances": [
                {
                    "timestamp": i.timestamp,
                    "query": i.query[:100],
                    "response": i.response[:100],
                    "type": i.type,
                    "source": i.source,
                }
                for i in self.instances[-10:]  # храним только последние 10 в JSON
            ]
        }


class ContextRegistry:
    """
    Реестр контекста для Янди.
    Хранит все темы, которые она обсуждала.
    """
    
    def __init__(self, user_id: str = "anonymous"):
        self.user_id = user_id
        self.file_path = REGISTRY_DIR / f"{user_id}.json"
        self.topics: Dict[str, TopicContext] = {}
        self._load()
    
    def _load(self):
        """Загружает реестр из файла"""
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for topic_name, topic_data in data.get("topics", {}).items():
                        topic = TopicContext(topic=topic_name)
                        topic.last_activity = topic_data.get("last_activity", 0)
                        topic.total_instances = topic_data.get("total_instances", 0)
                        
                        for inst_data in topic_data.get("instances", []):
                            instance = ContextInstance(
                                timestamp=inst_data.get("timestamp", time.time()),
                                query=inst_data.get("query", ""),
                                response=inst_data.get("response", ""),
                                topic=topic_name,
                                type=inst_data.get("type", "unknown"),
                                source=inst_data.get("source", "unknown"),
                            )
                            topic.instances.append(instance)
                        
                        self.topics[topic_name] = topic
            except Exception as e:
                print(f"[ContextRegistry] Ошибка загрузки: {e}")
    
    def _save(self):
        """Сохраняет реестр в файл"""
        try:
            data = {
                "user_id": self.user_id,
                "updated_at": time.time(),
                "topics": {
                    name: topic.to_dict()
                    for name, topic in self.topics.items()
                }
            }
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ContextRegistry] Ошибка сохранения: {e}")
    
    def register(self, query: str, response: str, topic: str, 
                 type: str = "unknown", source: str = "yandi"):
        """
        Регистрирует новый контекст.
        """
        if topic not in self.topics:
            self.topics[topic] = TopicContext(topic=topic)
        
        instance = ContextInstance(
            timestamp=time.time(),
            query=query,
            response=response,
            topic=topic,
            type=type,
            source=source,
        )
        
        self.topics[topic].add_instance(instance)
        self._save()
    
    def get_topic(self, topic: str) -> Optional[TopicContext]:
        """Возвращает контекст по теме"""
        return self.topics.get(topic)
    
    def get_topics(self) -> List[str]:
        """Возвращает список всех тем"""
        return list(self.topics.keys())
    
    def has_context_for(self, query: str, hours: float = 2) -> Tuple[bool, str, Optional[str]]:
        """
        Проверяет, есть ли у Янди контекст для запроса.
        Возвращает (есть_контекст, причина, название_темы).
        """
        query_lower = query.lower()
        
        # Определяем тему запроса
        topic = self._detect_topic(query_lower)
        
        if not topic:
            return False, "не удалось определить тему запроса", None
        
        # Проверяем, есть ли такая тема в реестре
        topic_context = self.topics.get(topic)
        if not topic_context:
            return False, f"тема '{topic}' не обсуждалась ранее", topic
        
        # Проверяем, была ли активность по этой теме недавно
        if not topic_context.is_recent(hours):
            return False, f"тема '{topic}' обсуждалась давно (последняя активность > {hours}ч)", topic
        
        # Проверяем, была ли Янди автором последнего ответа по этой теме
        recent = topic_context.get_recent(3)
        if recent:
            # Проверяем, что хотя бы один из последних ответов был от Янди
            yandi_responses = [i for i in recent if i.source == "yandi"]
            if not yandi_responses:
                return False, f"Янди не отвечала на тему '{topic}' в последних обсуждениях", topic
        
        return True, f"есть контекст по теме '{topic}'", topic
    
    def _detect_topic(self, query: str) -> Optional[str]:
        """
        Определяет тему запроса.
        """
        topic_map = {
            "calculation": ["расчёт", "вычислени", "подсчёт", "сумма", "сложи", "умнож"],
            "programming": ["код", "программ", "функци", "класс", "алгоритм", "python", "javascript"],
            "physics": ["физик", "гравитаци", "квант", "электричеств", "магнит"],
            "mathematics": ["математ", "уравнени", "формул", "числ", "геометри"],
            "text_analysis": ["текст", "статья", "пост", "сообщени", "анализ"],
            "song_analysis": ["песн", "музык", "трек", "композици"],
            "movie_analysis": ["фильм", "кино", "сериал"],
            "help": ["помощ", "объясни", "расскажи"],
        }
        
        for topic, keywords in topic_map.items():
            if any(kw in query for kw in keywords):
                return topic
        
        # Если не определили — пытаемся найти по контексту
        # Проверяем, есть ли темы в реестре, которые упоминаются в запросе
        for existing_topic in self.topics:
            if existing_topic in query:
                return existing_topic
        
        return None
    
    def get_summary(self) -> Dict:
        """Возвращает краткую сводку по реестру"""
        return {
            "total_topics": len(self.topics),
            "topics": [
                {
                    "topic": name,
                    "instances": topic.total_instances,
                    "last_activity": topic.last_activity,
                    "is_recent": topic.is_recent(),
                }
                for name, topic in self.topics.items()
            ]
        }


def get_context_registry(user_id: str = "anonymous") -> ContextRegistry:
    """Фабрика для получения реестра контекста"""
    return ContextRegistry(user_id)


if __name__ == "__main__":
    # Тесты
    print("=== Тест Context Registry ===\n")
    
    registry = get_context_registry("test_user")
    
    # Регистрируем контекст
    registry.register(
        query="посчитай 2+2",
        response="2+2=4",
        topic="calculation",
        type="calculation",
        source="yandi"
    )
    
    registry.register(
        query="напиши код на python",
        response="print('Hello')",
        topic="programming",
        type="code",
        source="yandi"
    )
    
    print("Зарегистрированы темы:", registry.get_topics())
    print("\nСводка:", registry.get_summary())
    
    # Проверяем контекст
    has_context, reason, topic = registry.has_context_for("ты ошиблась в расчётах", hours=24)
    print(f"\nКонтекст для 'ты ошиблась в расчётах': {has_context} — {reason} (тема: {topic})")
    
    has_context, reason, topic = registry.has_context_for("напиши код на javascript", hours=24)
    print(f"Контекст для 'напиши код на javascript': {has_context} — {reason} (тема: {topic})")
    
    has_context, reason, topic = registry.has_context_for("что такое гравитация", hours=24)
    print(f"Контекст для 'что такое гравитация': {has_context} — {reason} (тема: {topic})")
