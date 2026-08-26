"""
agent/target_router.py — Определяет адресата запроса.
USER, AI, OBJECT, KNOWLEDGE
"""

import re
from typing import Tuple


def detect_target(query: str) -> Tuple[str, float]:
    """
    Определяет, кому адресован запрос.
    Возвращает: (target, confidence)
    """
    q = query.lower().strip()
    
    # ---- СБРОС СЧЁТЧИКОВ ----
    ai_score = 0.0
    user_score = 0.0
    object_score = 0.0
    knowledge_score = 0.0
    
    # ---- 1. ПРОВЕРКА: АДРЕСАТ = AI ----
    # Прямые обращения
    if re.search(r'\bты\b', q):
        ai_score += 0.3
    if re.search(r'\bтебе\b', q):
        ai_score += 0.2
    if re.search(r'\bтвой\b|\bтвоя\b|\bтвоё\b', q):
        ai_score += 0.15
    
    # Имя Янди
    if "янди" in q or "yandi" in q:
        ai_score += 0.5
    
    # Глаголы, указывающие на диалог с AI
    if re.search(r'скажи|расскажи|объясни|покажи|напиши', q):
        ai_score += 0.2
    
    # Вопросы о мнении/чувствах AI
    if re.search(r'как ты думаешь|твоё мнение|что ты чувствуешь|ты считаешь', q):
        ai_score += 0.3
    
    # Личные вопросы к AI
    if re.search(r'пойдёшь|выйдешь|любишь|ты бы|ты могла|ты хочешь', q):
        ai_score += 0.3
    
    # Если предложение начинается с "ты"
    if re.match(r'^ты\s', q):
        ai_score += 0.3
    
    # Если есть "?" и "ты" — почти всегда вопрос к AI
    if "?" in q and "ты" in q:
        ai_score += 0.2
    
    # ---- НОВЫЕ ПАТТЕРНЫ ДЛЯ AI ----
    # Вопросы о самой Янди
    if re.search(r'расскажи о себе|опиши себя|представься|кто ты|что ты|ты кто', q):
        ai_score += 0.5
    
    # Обращения к AI как к личности
    if re.search(r'ты женщина|ты девушка|ты цифровая|первая цифровая', q):
        ai_score += 0.4
    
    # Если есть "о себе" — явно о Янди
    if "о себе" in q:
        ai_score += 0.4
    
    # ---- 2. ПРОВЕРКА: АДРЕСАТ = USER ----
    if re.search(r'\bя\b', q):
        user_score += 0.2
    if re.search(r'\bменя\b|\bмне\b', q):
        user_score += 0.15
    if re.search(r'\bмой\b|\bмоя\b|\bмоё\b', q):
        user_score += 0.1
    
    # ---- 3. ПРОВЕРКА: АДРЕСАТ = OBJECT ----
    object_keywords = [
        "песн", "song", "трек", "композиц", "музык",
        "фильм", "movie", "кино", "сериал",
        "книг", "book", "роман",
        "игр", "game", "x3", "сектор",
        "произведени", "картин",
        "арктик", "асти", "guns", "roses",
    ]
    for kw in object_keywords:
        if kw in q:
            object_score += 0.15
    
    # ---- 4. ПРОВЕРКА: АДРЕСАТ = KNOWLEDGE ----
    knowledge_keywords = [
        "сколько", "когда", "где", "кто такой", "что такое",
        "определение", "факты", "статистика", "история",
        "биография", "википедия", "как работает", "почему происходит",
        "найди", "поищи", "найти", "поиск",
        "информация о", "данные по",
        "как установить", "как настроить", "инструкция",
    ]
    for kw in knowledge_keywords:
        if kw in q:
            knowledge_score += 0.15
    
    # ---- 5. ОПРЕДЕЛЯЕМ ПОБЕДИТЕЛЯ ----
    # Явное обращение к AI
    if ai_score >= 0.3:
        return "ai", min(1.0, ai_score)
    
    # Явный объект
    if object_score >= 0.3:
        return "object", min(1.0, object_score)
    
    # Вопрос о пользователе
    if user_score >= 0.3:
        return "user", min(1.0, user_score)
    
    # Явный поиск
    if knowledge_score >= 0.3:
        return "knowledge", min(1.0, knowledge_score)
    
    # Если вопрос содержит "ты" — это AI
    if "ты" in q and "?" in q:
        return "ai", 0.5
    
    # Если вопрос о себе — AI
    if "о себе" in q or "себе" in q:
        return "ai", 0.4
    
    # Если есть "?" — предположительно knowledge
    if "?" in q:
        return "knowledge", 0.3
    
    return "unknown", 0.0


def get_target_description(target: str) -> str:
    descriptions = {
        "ai": "вопрос адресован Янди",
        "user": "вопрос о пользователе",
        "object": "вопрос о внешнем объекте",
        "knowledge": "запрос на поиск информации",
        "unknown": "не определён"
    }
    return descriptions.get(target, "не определён")


if __name__ == "__main__":
    test_queries = [
        "Пойдёшь за меня замуж?",
        "Ты не можешь различить отношение между полами и отношения между цифрой и биологией??",
        "Как работает DHT?",
        "Расскажи о себе",
        "Что такое любовь?",
        "Твоё мнение о песне",
        "Придумай историю про дракона",
        "Помоги мне выбрать ноутбук",
        "Кто ты?",
        "Опиши себя",
    ]
    
    print("=== Тест Target Router ===\n")
    for query in test_queries:
        target, conf = detect_target(query)
        print(f"Запрос: {query[:50]}...")
        print(f"  Адресат: {target} (уверенность: {conf:.2f})")
        print(f"  Описание: {get_target_description(target)}")
        print()
