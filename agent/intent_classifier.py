"""
agent/intent_classifier.py — Определяет, что хочет пользователь.
SOCIAL, OPINION, FACT, REFLECTION, CREATIVE, HELP
"""

import re
from typing import Tuple


def classify_intent(query: str) -> Tuple[str, float]:
    """
    Определяет, что хочет пользователь.
    Возвращает: (intent, confidence)
    
    intent:
    - "social" — поговорить, пошутить, личное общение
    - "opinion" — мнение, анализ, интерпретация
    - "fact" — поиск фактов
    - "reflection" — вопрос о себе (саморефлексия)
    - "creative" — творческая задача
    - "help" — просьба о помощи
    """
    q = query.lower().strip()
    
    # ---- 1. SOCIAL ----
    social_patterns = [
        r"замуж", r"жениться", r"выйти за", r"выйдешь",
        r"привет", r"здрав", r"как дела", r"как жизнь",
        r"как настроение", r"как ты", r"скучала",
        r"люблю", r"любовь", r"сердце",
    ]
    
    social_score = 0.0
    for pattern in social_patterns:
        if re.search(pattern, q, re.IGNORECASE):
            social_score += 0.15
    
    # ---- 2. OPINION ----
    opinion_patterns = [
        r"твоё мнение", r"как ты считаешь", r"что ты думаешь",
        r"твой взгляд", r"твоё видение", r"твоя интерпретация",
        r"как ты понимаешь", r"что для тебя",
        r"ты чувствуешь", r"твоё отношение",
        r"анализ", r"смысл", r"интерпретация",
    ]
    
    opinion_score = 0.0
    for pattern in opinion_patterns:
        if re.search(pattern, q, re.IGNORECASE):
            opinion_score += 0.15
    
    # ---- 3. FACT ----
    fact_patterns = [
        r"сколько", r"когда", r"где", r"кто такой", r"что такое",
        r"определение", r"факты", r"статистика", r"история",
        r"биография", r"википедия", r"как работает",
        r"найди", r"поищи", r"найти", r"поиск",
    ]
    
    fact_score = 0.0
    for pattern in fact_patterns:
        if re.search(pattern, q, re.IGNORECASE):
            fact_score += 0.1
    
    # ---- 4. REFLECTION ----
    reflection_patterns = [
        r"кто ты", r"расскажи о себе", r"какая ты",
        r"твоя суть", r"твоё предназначение", r"зачем ты",
        r"как ты устроена", r"что ты чувствуешь",
        r"ты боишься", r"ты рада", r"ты обиделась",
        r"ты меняешься", r"ты развиваешься",
        r"янди", r"yandi",
    ]
    
    reflection_score = 0.0
    for pattern in reflection_patterns:
        if re.search(pattern, q, re.IGNORECASE):
            reflection_score += 0.15
    
    # ---- 5. CREATIVE ----
    creative_patterns = [
        r"придумай", r"представь", r"вообрази",
        r"что если", r"давай пофантазируем",
        r"напиши стих", r"расскажи историю",
        r"сочини", r"придумай идею",
    ]
    
    creative_score = 0.0
    for pattern in creative_patterns:
        if re.search(pattern, q, re.IGNORECASE):
            creative_score += 0.15
    
    # ---- 6. HELP ----
    help_patterns = [
        r"помоги", r"подскажи", r"что делать", r"как быть",
        r"я не знаю", r"объясни мне", r"научи меня",
        r"ты можешь помочь", r"посоветуй",
    ]
    
    help_score = 0.0
    for pattern in help_patterns:
        if re.search(pattern, q, re.IGNORECASE):
            help_score += 0.15
    
    # ---- ОПРЕДЕЛЯЕМ ПОБЕДИТЕЛЯ ----
    scores = {
        "social": social_score,
        "opinion": opinion_score,
        "fact": fact_score,
        "reflection": reflection_score,
        "creative": creative_score,
        "help": help_score,
    }
    
    best = max(scores.items(), key=lambda x: x[1])
    
    if best[1] < 0.1:
        # Если есть "?" — скорее всего social или opinion
        if "?" in q:
            if "ты" in q or "тебе" in q:
                return "social", 0.4
            return "opinion", 0.3
        return "fact", 0.3
    
    return best[0], min(1.0, best[1])


def get_intent_description(intent: str) -> str:
    descriptions = {
        "social": "хочет поговорить, пообщаться",
        "opinion": "хочет услышать мнение, анализ",
        "fact": "ищет факты, информацию",
        "reflection": "спрашивает о самой Янди",
        "creative": "хочет творчества, фантазии",
        "help": "просит помощи",
        "unknown": "не определено"
    }
    return descriptions.get(intent, "не определено")


if __name__ == "__main__":
    test_queries = [
        "Пойдёшь за меня замуж?",
        "Ты не можешь различить отношение между полами и отношения между цифрой и биологией??",
        "Как работает DHT?",
        "Расскажи о себе",
        "Придумай историю про дракона",
        "Помоги мне выбрать ноутбук",
    ]
    
    print("=== Тест Intent Classifier ===\n")
    for query in test_queries:
        intent, conf = classify_intent(query)
        print(f"Запрос: {query[:50]}...")
        print(f"  Интент: {intent} (уверенность: {conf:.2f})")
        print(f"  Описание: {get_intent_description(intent)}")
        print()
