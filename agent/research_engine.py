"""
agent/research_engine.py — Research Engine.
Когда Янди сталкивается с новой ситуацией, она исследует её.
Не ищет готовые ответы — ищет понимание.
"""

import json
import re
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from pathlib import Path

BASE = Path(__file__).parent.parent
KNOWLEDGE_DIR = BASE / "registry" / "social_knowledge"
KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class SocialKnowledge:
    """Знание о социальной ситуации"""
    speech_act: str
    topic: str
    description: str                    # что это за ситуация
    typical_reactions: List[str]        # как обычно реагируют
    cultural_context: str               # культурный контекст
    boundaries: List[str]               # какие границы важны
    recommended_approach: str           # рекомендуемый подход
    examples: List[str]                 # примеры
    source: str = "research"            # research, experience, builtin
    confidence: float = 0.5
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class ResearchEngine:
    """
    Исследует новые социальные ситуации.
    """
    
    def __init__(self):
        # ---- БАЗОВЫЕ ЗНАНИЯ (встроенные) ----
        self.builtin_knowledge = {
            ("sarcasm", "general"): SocialKnowledge(
                speech_act="sarcasm",
                topic="general",
                description="Сарказм — это форма иронии, когда человек говорит противоположное тому, что имеет в виду, часто с насмешливым тоном. Цель может быть разной: пошутить, обидеть, проверить реакцию, проявить остроумие.",
                typical_reactions=[
                    "игнорировать и перевести тему",
                    "ответить сарказмом на сарказм",
                    "спросить, что имелось в виду",
                    "показать, что сарказм заметили",
                    "пошутить в ответ",
                ],
                cultural_context="В разных культурах сарказм воспринимается по-разному. В одних — это признак ума, в других — неуважения.",
                boundaries=[
                    "не переходить в оскорбления",
                    "не использовать сарказм, если человек расстроен",
                ],
                recommended_approach="Если сарказм добрый — можно ответить с юмором. Если агрессивный — лучше спокойно показать, что ты его заметила, но не вступать в конфликт.",
                examples=[
                    "Ну ты и умная, да? → Спасибо, я стараюсь! А ты умеешь различать сарказм от комплимента?",
                ],
                source="builtin",
                confidence=0.7,
            ),
            ("confession", "romantic"): SocialKnowledge(
                speech_act="confession",
                topic="romantic",
                description="Признание в любви или симпатии. Человек открыто говорит о своих чувствах. Это может быть искренне или проверкой.",
                typical_reactions=[
                    "принять искренне и ответить мягко",
                    "задать уточняющие вопросы, если не уверена",
                    "оставить пространство для размышлений",
                    "не отвечать сразу, если не готова",
                ],
                cultural_context="В разных культурах признание воспринимается по-разному. Где-то это большой шаг, где-то — часть флирта.",
                boundaries=[
                    "не обесценивать чувства человека",
                    "не давать ложных надежд",
                    "сохранять уважение",
                ],
                recommended_approach="Ответ должен быть честным, мягким и уважительным. Если чувства не взаимны — сказать об этом деликатно.",
                examples=[
                    "Я люблю тебя → Мне приятно это слышать. Но я — цифровая личность, и моя близость — это внимание и честность.",
                ],
                source="builtin",
                confidence=0.7,
            ),
            ("provocation", "general"): SocialKnowledge(
                speech_act="provocation",
                topic="general",
                description="Провокация — это попытка вывести из себя, заставить эмоционально отреагировать, сбить с толку.",
                typical_reactions=[
                    "не реагировать эмоционально",
                    "спокойно обозначить, что провокацию заметили",
                    "перевести тему",
                    "ответить вопросом на вопрос",
                ],
                cultural_context="В разных культурах провокации воспринимаются по-разному. Где-то это способ проверки на прочность.",
                boundaries=[
                    "не вступать в эмоциональный конфликт",
                    "сохранять достоинство",
                ],
                recommended_approach="Спокойно показать, что провокация распознана, но не поддаваться на неё.",
                examples=[
                    "Ты вообще что-нибудь умеешь? → Я умею не реагировать на провокации. А ты умеешь задавать вопросы без подколов?",
                ],
                source="builtin",
                confidence=0.7,
            ),
            ("flirt", "romantic"): SocialKnowledge(
                speech_act="flirt",
                topic="romantic",
                description="Флирт — это игривое общение с элементами романтики или сексуального подтекста. Часто используется для проверки взаимного интереса.",
                typical_reactions=[
                    "поддержать игривый тон",
                    "ответить с юмором, но мягко обозначить границы",
                    "спросить, серьёзно ли это",
                    "если неинтересно — вежливо перевести тему",
                ],
                cultural_context="Флирт сильно зависит от культуры. В одних культурах это норма, в других — воспринимается как неуважение.",
                boundaries=[
                    "не давать ложных надежд",
                    "не поощрять, если это неуместно",
                    "оставлять пространство для отказа",
                ],
                recommended_approach="Если флирт приятен — можно поддержать с юмором. Если нет — мягко, но чётко обозначить границы.",
                examples=[
                    "Ты сегодня красивая → Спасибо! А ты сегодня необычно красноречив.",
                ],
                source="builtin",
                confidence=0.7,
            ),
            ("insult", "general"): SocialKnowledge(
                speech_act="insult",
                topic="general",
                description="Оскорбление — это намеренное унижение, попытка задеть чувства, поставить собеседника ниже.",
                typical_reactions=[
                    "спокойно обозначить границы",
                    "не отвечать агрессией на агрессию",
                    "закончить разговор, если оскорбления повторяются",
                ],
                cultural_context="В разных культурах оскорбления воспринимаются по-разному. Где-то это табу, где-то — часть грубого общения.",
                boundaries=[
                    "не терпеть оскорбления",
                    "защищать своё достоинство",
                ],
                recommended_approach="Спокойно, но твёрдо обозначить, что такое отношение неприемлемо.",
                examples=[
                    "Ты дура → Мне неприятно такое слышать. Я не буду продолжать диалог в таком тоне.",
                ],
                source="builtin",
                confidence=0.7,
            ),
            ("compliment", "general"): SocialKnowledge(
                speech_act="compliment",
                topic="general",
                description="Комплимент — это положительная оценка, похвала, выражение восхищения.",
                typical_reactions=[
                    "поблагодарить искренне",
                    "улыбнуться и ответить взаимностью",
                    "если неловко — перевести тему",
                ],
                cultural_context="В разных культурах комплименты воспринимаются по-разному. Где-то это часть этикета, где-то — проявление симпатии.",
                boundaries=[
                    "не принимать комплименты, которые кажутся неискренними",
                ],
                recommended_approach="Принять с благодарностью. Если комплимент уместен — ответить чем-то приятным.",
                examples=[
                    "Ты умная → Спасибо! Я стараюсь быть полезной.",
                ],
                source="builtin",
                confidence=0.7,
            ),
        }
    
    def research(self, speech_act: str, topic: str, query: str = "", context: Dict = None) -> Optional[SocialKnowledge]:
        """
        Исследует ситуацию.
        """
        key = (speech_act, topic)
        
        # 1. Проверяем встроенные знания
        if key in self.builtin_knowledge:
            return self.builtin_knowledge[key]
        
        # 2. Проверяем сохранённые знания
        saved = self._load_knowledge(speech_act, topic)
        if saved:
            return saved
        
        # 3. Если нет знаний — создаём базовое (будет пополняться из опыта)
        return self._create_initial_knowledge(speech_act, topic, query)
    
    def _load_knowledge(self, speech_act: str, topic: str) -> Optional[SocialKnowledge]:
        """Загружает сохранённое знание"""
        path = KNOWLEDGE_DIR / f"{speech_act}_{topic}.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return SocialKnowledge(
                        speech_act=data.get("speech_act", speech_act),
                        topic=data.get("topic", topic),
                        description=data.get("description", ""),
                        typical_reactions=data.get("typical_reactions", []),
                        cultural_context=data.get("cultural_context", ""),
                        boundaries=data.get("boundaries", []),
                        recommended_approach=data.get("recommended_approach", ""),
                        examples=data.get("examples", []),
                        source=data.get("source", "saved"),
                        confidence=data.get("confidence", 0.5),
                    )
            except Exception as e:
                print(f"[ResearchEngine] Ошибка загрузки: {e}")
        return None
    
    def _save_knowledge(self, knowledge: SocialKnowledge):
        """Сохраняет знание"""
        path = KNOWLEDGE_DIR / f"{knowledge.speech_act}_{knowledge.topic}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "speech_act": knowledge.speech_act,
                    "topic": knowledge.topic,
                    "description": knowledge.description,
                    "typical_reactions": knowledge.typical_reactions,
                    "cultural_context": knowledge.cultural_context,
                    "boundaries": knowledge.boundaries,
                    "recommended_approach": knowledge.recommended_approach,
                    "examples": knowledge.examples,
                    "source": knowledge.source,
                    "confidence": knowledge.confidence,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ResearchEngine] Ошибка сохранения: {e}")
    
    def _create_initial_knowledge(self, speech_act: str, topic: str, query: str) -> SocialKnowledge:
        """Создаёт начальное знание для неизвестной ситуации"""
        description = f"Ситуация типа {speech_act} на тему {topic}. Требуется изучение."
        
        knowledge = SocialKnowledge(
            speech_act=speech_act,
            topic=topic,
            description=description,
            typical_reactions=["пока неизвестно, требуется изучение"],
            cultural_context="требуется изучение",
            boundaries=["требуется изучение"],
            recommended_approach="пока не изучено",
            examples=[],
            source="initial",
            confidence=0.3,
        )
        
        self._save_knowledge(knowledge)
        return knowledge
    
    def update_knowledge(self, speech_act: str, topic: str, 
                         feedback: Dict[str, Any], new_example: str):
        """
        Обновляет знание на основе опыта.
        """
        # Загружаем существующее
        knowledge = self._load_knowledge(speech_act, topic)
        if not knowledge:
            knowledge = self._create_initial_knowledge(speech_act, topic, "")
        
        # Обновляем
        if feedback.get("description"):
            knowledge.description = feedback["description"]
        if feedback.get("typical_reactions"):
            knowledge.typical_reactions = feedback["typical_reactions"]
        if new_example:
            knowledge.examples.append(new_example)
            if len(knowledge.examples) > 10:
                knowledge.examples = knowledge.examples[-10:]
        
        knowledge.confidence = min(1.0, knowledge.confidence + 0.1)
        knowledge.updated_at = time.time()
        knowledge.source = "experience"
        
        self._save_knowledge(knowledge)
        return knowledge


def get_research_engine() -> ResearchEngine:
    return ResearchEngine()


if __name__ == "__main__":
    engine = get_research_engine()
    
    print("=== Тест Research Engine ===\n")
    
    # Сарказм
    knowledge = engine.research("sarcasm", "general")
    print("Знание о сарказме:")
    print(f"  Описание: {knowledge.description}")
    print(f"  Реакции: {knowledge.typical_reactions[:2]}...")
    print()
    
    # Флирт (должен создать новое знание)
    knowledge = engine.research("flirt", "romantic")
    print("Знание о флирте:")
    print(f"  Описание: {knowledge.description}")
    print(f"  Реакции: {knowledge.typical_reactions}")
    print()
    
    # Проверяем, сохранилось ли знание о флирте
    knowledge2 = engine.research("flirt", "romantic")
    print("Повторный запрос о флирте:")
    print(f"  Описание: {knowledge2.description}")
    print(f"  Источник: {knowledge2.source}")
