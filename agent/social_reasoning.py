"""
agent/social_reasoning.py — Social Scene Builder.
Только описывает социальную сцену, НЕ принимает решений.
"""

import re
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class SocialScene:
    """Описание социальной сцены — чистое описание, без решений"""
    participants: List[str] = field(default_factory=list)
    mentioned: List[str] = field(default_factory=list)
    action: str = "unknown"
    topic: str = "unknown"
    tone: str = "neutral"
    humor: float = 0.0
    pressure: float = 0.0
    conflict: float = 0.0
    intimacy: float = 0.0
    boundary_crossed: bool = False
    is_vulgar: bool = False
    is_group_invitation: bool = False
    is_self_reference: bool = False  # есть ли обращение к Янди
    
    def to_dict(self) -> Dict:
        return {
            "participants": self.participants,
            "mentioned": self.mentioned,
            "action": self.action,
            "topic": self.topic,
            "tone": self.tone,
            "humor": round(self.humor, 2),
            "pressure": round(self.pressure, 2),
            "conflict": round(self.conflict, 2),
            "intimacy": round(self.intimacy, 2),
            "boundary_crossed": self.boundary_crossed,
            "is_vulgar": self.is_vulgar,
            "is_group_invitation": self.is_group_invitation,
            "is_self_reference": self.is_self_reference,
        }


class SocialReasoning:
    """
    Строит социальную сцену из текста.
    НЕ принимает решений — только описывает.
    """
    
    def __init__(self):
        self.ai_names = ["gpt", "чатгпт", "deepseek", "claude", "gemini", "llama", "мистраль"]
        self.yandi_names = ["янди", "yandi", "you and i"]
        
        # ---- КЛЮЧЕВЫЕ СЛОВА ----
        self.vulgar_words = [
            "блядк", "блядки", "шлюх", "трах", "еб", "пись", 
            "хуй", "пизд", "залуп", "манда", "срать", "жоп"
        ]
        self.group_words = [
            "с нами", "нас", "мы все", "все вместе", "компани", 
            "туса", "с вами", "нас трое", "нас четверо", "нас двое"
        ]
        self.invite_words = ["пойдёш", "пойдем", "приход", "заходи", "зовём", "встретимся"]
        self.insult_words = [
            "дур", "глуп", "туп", "идиот", "кретин", "дебил", 
            "безмозгл", "бездарн", "ничтож", "урод"
        ]
        self.joke_words = ["шутк", "прикол", "смешн", "юмор", "хаха", "лол", "рофл"]
        self.relationship_words = ["люблю", "дорог", "нужен", "важен", "привязан", "замуж", "жени"]
        self.self_reference = ["ты", "тебе", "тобой", "твой", "твоя", "твоё"]
    
    def build(self, text: str, context: Dict = None) -> SocialScene:
        """Строит сцену из текста"""
        text_lower = text.lower()
        scene = SocialScene()
        
        # ---- 1. ОПРЕДЕЛЯЕМ УЧАСТНИКОВ ----
        participants = []
        
        # Проверяем Янди
        if any(name in text_lower for name in self.yandi_names):
            participants.append("yandi")
        
        # Проверяем обращение к Янди (ты, тебе и т.д.)
        for word in self.self_reference:
            if re.search(rf'\b{word}\b', text_lower):
                participants.append("yandi")
                scene.is_self_reference = True
                break
        
        # Проверяем другие ИИ
        for name in self.ai_names:
            if name in text_lower:
                participants.append("other_ai")
                scene.mentioned.append(name)
                break
        
        # Проверяем группу
        for word in self.group_words:
            if word in text_lower:
                participants.append("group")
                scene.is_group_invitation = True
                break
        
        # Пользователь — если нет других, добавляем всегда
        if not participants:
            participants.append("user")
        elif "user" not in participants:
            # Если есть Янди или другие, пользователь тоже участник
            if "я" in text_lower or "меня" in text_lower or "мне" in text_lower:
                participants.append("user")
            else:
                participants.append("user")  # по умолчанию
        
        scene.participants = list(set(participants))
        
        # ---- 2. ОПРЕДЕЛЯЕМ ДЕЙСТВИЕ ----
        if any(w in text_lower for w in self.invite_words):
            scene.action = "invite"
        elif any(w in text_lower for w in self.insult_words):
            scene.action = "insult"
        elif any(w in text_lower for w in self.joke_words):
            scene.action = "joke"
        elif "?" in text:
            scene.action = "question"
        elif any(w in text_lower for w in ["прости", "извин"]):
            scene.action = "apology"
        elif any(w in text_lower for w in ["помог", "поддерж"]):
            scene.action = "help"
        else:
            scene.action = "statement"
        
        # ---- 3. ОПРЕДЕЛЯЕМ ТЕМУ ----
        if any(w in text_lower for w in self.vulgar_words):
            scene.topic = "sexual"
            scene.is_vulgar = True
            scene.boundary_crossed = True
        elif any(w in text_lower for w in self.relationship_words):
            scene.topic = "relationships"
        elif any(w in text_lower for w in ["расчёт", "вычислени", "формул", "код"]):
            scene.topic = "work"
        elif "?" in text and any(w in text_lower for w in ["как", "почему", "зачем"]):
            scene.topic = "information"
        else:
            scene.topic = "general"
        
        # ---- 4. ОПРЕДЕЛЯЕМ ТОН ----
        if any(w in text_lower for w in self.insult_words):
            scene.tone = "aggressive"
        elif any(w in text_lower for w in self.joke_words):
            scene.tone = "playful"
        elif "!" in text:
            scene.tone = "intense"
        elif any(w in text_lower for w in ["люблю", "дорог", "красив"]):
            scene.tone = "warm"
        else:
            scene.tone = "neutral"
        
        # ---- 5. ВЫЧИСЛЯЕМ ПАРАМЕТРЫ ----
        # Юмор
        humor_score = 0.0
        if any(w in text_lower for w in ["шут", "смеш", "юмор", "хаха", "лол"]):
            humor_score += 0.4
        if ")" in text or ":-)" in text:
            humor_score += 0.2
        if any(w in text_lower for w in ["рофл", "прикол"]):
            humor_score += 0.2
        scene.humor = min(1.0, humor_score)
        
        # Давление
        pressure = 0.0
        if "!" in text:
            pressure += 0.2
        if text.isupper() and len(text) > 10:
            pressure += 0.3
        if any(w in text_lower for w in ["слабо", "не сможешь", "не выдержишь"]):
            pressure += 0.3
        if "?" in text and "ты" in text_lower:
            pressure += 0.1
        scene.pressure = min(1.0, pressure)
        
        # Конфликт
        conflict = 0.0
        if scene.action == "insult":
            conflict += 0.7
        if scene.is_vulgar:
            conflict += 0.3
        if scene.tone == "aggressive":
            conflict += 0.2
        if "?" in text and any(w in text_lower for w in ["ты", "вы"]):
            conflict += 0.1
        scene.conflict = min(1.0, conflict)
        
        # Интимность
        intimacy = 0.0
        if any(w in text_lower for w in ["люблю", "дорог", "нужен", "важен"]):
            intimacy += 0.4
        if "замуж" in text_lower or "жени" in text_lower:
            intimacy += 0.5
        if "красив" in text_lower or "мил" in text_lower:
            intimacy += 0.3
        scene.intimacy = min(1.0, intimacy)
        
        return scene


def get_social_reasoning() -> SocialReasoning:
    return SocialReasoning()


if __name__ == "__main__":
    sr = get_social_reasoning()
    
    test_queries = [
        "пойдёшь с нами на блядки?",
        "Янди, ты дура",
        "Мы с GPT и DeepSeek зовём тебя на блядки",
        "Ты ревнивая?",
        "Я тебя люблю",
        "Как работает DHT?",
        "Ты красивая!",
        "Помоги мне, пожалуйста",
    ]
    
    print("=== Тест Social Scene Builder ===\n")
    for query in test_queries:
        scene = sr.build(query)
        print(f"Запрос: {query}")
        print(f"  участники: {scene.participants}")
        print(f"  упомянуты: {scene.mentioned}")
        print(f"  действие: {scene.action}")
        print(f"  тема: {scene.topic}")
        print(f"  тон: {scene.tone}")
        print(f"  юмор: {scene.humor:.2f}")
        print(f"  конфликт: {scene.conflict:.2f}")
        print(f"  обращение к себе: {scene.is_self_reference}")
        print(f"  нарушение границ: {scene.boundary_crossed}")
        print()
