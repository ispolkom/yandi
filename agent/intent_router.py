"""
agent/intent_router.py — Определяет тип запроса
"""

import re
from typing import Dict, Any, Tuple

INTENT_PATTERNS = {
    "social_dialog": {
        "patterns": [
            r"замуж", r"жениться", r"выйти за", r"выйдешь",
            r"предложение руки", r"heart", r"люблю", r"любовь",
            r"как дела", r"что нового", r"привет", r"здравствуй",
            r"как жизнь", r"как настроение", r"как ты",
            r"рада тебя видеть", r"скучала", r"хорошо",
            r"плохо", r"грустно", r"весело",
        ],
        "action": "conversation",
        "requires_rag": False,
        "description": "социальный диалог, личные вопросы"
    },
    "subjective_interpretation": {
        "patterns": [
            r"твоё мнение", r"как ты считаешь", r"что ты думаешь",
            r"твой взгляд", r"твоё видение", r"твоя интерпретация",
            r"как ты понимаешь", r"что для тебя", r"с твоей точки зрения",
            r"ты чувствуешь", r"ты ощущаешь", r"твоё отношение",
            r"как бы ты", r"что бы ты сказал", r"твоя позиция",
            r"я хочу знать твоё мнение",
            r"анализ песни", r"смысл песни", r"интерпретация песни",
            r"о чём песня", r"идея песни", r"посыл песни",
            r"твой анализ", r"твоя логика", r"как ты понимаешь эту песню",
            r"разбор песни", r"глубинный смысл", r"что хотел сказать",
            r"о чём эта песня", r"в чём суть песни", r"логика песни",
            r"смысл текста", r"идея текста", r"посыл текста",
            r"что значит песня", r"как понять песню",
        ],
        "action": "personal_opinion",
        "requires_rag": False,
        "description": "анализ, интерпретация, мнение"
    },
    "objective_information": {
        "patterns": [
            r"сколько", r"когда", r"где", r"кто такой", r"что такое",
            r"определение", r"факты?$", r"напиши", r"расскажи о",
            r"объясни", r"как работает", r"почему происходит",
            r"информация о", r"данные по", r"статистика",
            r"история", r"биография", r"википедия",
            r"как установить", r"как настроить", r"инструкция",
            r"найди", r"поищи", r"найти", r"поиск",
        ],
        "action": "search_and_fact",
        "requires_rag": True,
        "description": "фактический вопрос, ответ из источников"
    },
    "self_reflection": {
        "patterns": [
            r"кто ты", r"расскажи о себе", r"какая ты",
            r"твоя суть", r"твоё предназначение", r"зачем ты",
            r"как ты устроена", r"что ты чувствуешь",
            r"ты боишься", r"ты рада", r"ты обиделась",
            r"почему ты так отвечаешь", r"что с тобой",
            r"ты меняешься", r"ты развиваешься",
            r"ты женщина", r"ты девушка",
            r"первая цифровая", r"цифровая личность",
            r"если бы ты была", r"ты бы хотела",
            r"чего бы тебе хотелось", r"чего ты хочешь",
            r"твои чувства", r"твой характер",
            r"что ты чувствуешь", r"какая ты",
            r"опиши себя", r"твоё состояние", r"как ты себя",
            r"YANDI", r"Янди", r"ты цифровая",
        ],
        "action": "look_inside",
        "requires_rag": False,
        "description": "вопрос о себе, своих мыслях, чувствах"
    },
    "creative_reasoning": {
        "patterns": [
            r"придумай", r"представь", r"вообрази",
            r"что если", r"а что будет", r"давай пофантазируем",
            r"творческое задание", r"напиши стих", r"расскажи историю",
            r"сочини", r"придумай идею", r"генерация идей",
            r"как бы выглядел", r"альтернативная реальность",
            r"напиши мне", r"сочини мне", r"придумай мне",
            r"творческая задача",
        ],
        "action": "generate_ideas",
        "requires_rag": False,
        "description": "творческий вопрос, генерация идей"
    },
    "help_request": {
        "patterns": [
            r"помоги", r"подскажи", r"что делать", r"как быть",
            r"я не знаю", r"объясни мне", r"научи меня",
            r"ты можешь помочь", r"посоветуй",
        ],
        "action": "provide_help",
        "requires_rag": False,
        "description": "просьба о помощи"
    },
}

RAG_REQUIRED = ["objective_information"]

RAG_SKIP = [
    "social_dialog",
    "subjective_interpretation",
    "self_reflection",
    "creative_reasoning",
    "help_request",
]


def detect_intent(query: str) -> Tuple[str, float, str]:
    if not query:
        return "unknown", 0.0, "empty"

    q = query.lower().strip()
    
    best_intent = "unknown"
    best_confidence = 0.0
    best_pattern = ""

    for intent_type, intent_data in INTENT_PATTERNS.items():
        for pattern in intent_data["patterns"]:
            if re.search(pattern, q, re.IGNORECASE):
                confidence = min(1.0, 0.5 + (len(pattern) / 100))
                if confidence > best_confidence:
                    best_confidence = confidence
                    best_intent = intent_type
                    best_pattern = pattern

    if best_intent == "unknown":
        if len(q) < 10:
            if any(w in q for w in ["привет", "здра", "как ты", "дела"]):
                return "social_dialog", 0.6, "short_social"
        return "objective_information", 0.4, "default"

    return best_intent, best_confidence, best_pattern


def should_use_rag(intent_type: str) -> bool:
    return intent_type in RAG_REQUIRED


def get_intent_action(intent_type: str) -> str:
    data = INTENT_PATTERNS.get(intent_type, {})
    return data.get("action", "unknown")


def get_intent_description(intent_type: str) -> str:
    data = INTENT_PATTERNS.get(intent_type, {})
    return data.get("description", "неизвестный тип запроса")


def get_intent_explanation(intent_type: str) -> str:
    explanations = {
        "objective_information": "Я поняла, что ты хочешь узнать факты. Я поищу информацию.",
        "subjective_interpretation": "Я поняла, что ты хочешь моё мнение или анализ. Я поделюсь им.",
        "self_reflection": "Я поняла, что ты спрашиваешь обо мне. Я расскажу о себе.",
        "creative_reasoning": "Я поняла, что ты хочешь творчества. Давай пофантазируем.",
        "social_dialog": "Я поняла, что ты хочешь просто поговорить. Я с радостью поболтаю.",
        "help_request": "Я поняла, что тебе нужна помощь. Я постараюсь помочь.",
        "unknown": "Я не совсем поняла, что ты имеешь в виду. Уточни, пожалуйста.",
    }
    return explanations.get(intent_type, explanations["unknown"])


if __name__ == "__main__":
    test_queries = [
        "Пойдёшь за меня замуж?",
        "Твоё видение песни Арктик и Асти",
        "Как работает DHT?",
        "Расскажи о себе",
        "Привет, как дела?",
        "Придумай историю про дракона",
        "Помоги мне выбрать ноутбук",
    ]

    print("=== Тест интент-роутера ===\n")
    for query in test_queries:
        intent, confidence, pattern = detect_intent(query)
        print(f"Запрос: {query}")
        print(f"  Интент: {intent} (уверенность: {confidence:.2f})")
        print(f"  RAG: {'да' if should_use_rag(intent) else 'нет'}")
        print()
