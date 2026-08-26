"""
agent/entity_resolver.py — Распознавание сущностей.
Определяет, что именно ищет пользователь.
"""

import re
import json
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

BASE = Path(__file__).parent.parent

# Словари известных сущностей (можно расширять)
KNOWN_GAMES = ["x3", "x3 terran conflict", "x3 albion prelude", "x4", "x4 foundations"]
KNOWN_GAME_TERMS = ["сектор", "звездная система", "корабль", "станция", "гонка", "фракция"]
KNOWN_MEDIA = ["фильм", "сериал", "аниме", "книга", "игра", "песня"]


class EntityResolver:
    def __init__(self):
        self.known_games = set(KNOWN_GAMES)
        self.known_game_terms = set(KNOWN_GAME_TERMS)
        self.known_media = set(KNOWN_MEDIA)

    def resolve(self, query: str) -> Dict[str, Any]:
        """
        Определяет тип сущности в запросе.
        Возвращает:
        {
            "type": "game_location" | "media" | "person" | "place" | "unknown",
            "game": "X3" | None,
            "canonical_name": "Легенда Форнема" | None,
            "confidence": 0.82,
            "is_proper_name": True/False,
            "needs_exact_search": True/False
        }
        """
        q = query.strip()
        q_lower = q.lower()

        result = {
            "type": "unknown",
            "game": None,
            "canonical_name": q,
            "confidence": 0.5,
            "is_proper_name": False,
            "needs_exact_search": True,
            "categories": [],
        }

        # ---- 1. Проверка: это собственное имя? ----
        # Если слова с большой буквы — вероятно, имя
        words = q.split()
        capital_count = sum(1 for w in words if w and w[0].isupper())
        if capital_count >= len(words) * 0.6:
            result["is_proper_name"] = True
            result["confidence"] += 0.2

        # ---- 2. Проверка: это игровой термин? ----
        for term in self.known_game_terms:
            if term in q_lower:
                result["categories"].append("game")
                result["confidence"] += 0.1

        for game in self.known_games:
            if game in q_lower:
                result["game"] = game.upper()
                result["type"] = "game_location"
                result["confidence"] += 0.3

        # ---- 3. Проверка: это медиа? ----
        for media in self.known_media:
            if media in q_lower:
                result["categories"].append(media)
                result["confidence"] += 0.1

        # ---- 4. Если есть явные признаки названия ----
        # Два слова, оба с большой буквы → скорее всего название
        if len(words) >= 2 and all(w and w[0].isupper() for w in words):
            result["is_proper_name"] = True
            result["type"] = "proper_name"
            result["confidence"] += 0.3

        # ---- 5. Итоговая уверенность ----
        result["confidence"] = min(1.0, result["confidence"])

        # ---- 6. Решение: нужен ли точный поиск ----
        result["needs_exact_search"] = result["confidence"] > 0.4

        return result

    def get_search_strategy(self, entity: Dict[str, Any]) -> str:
        """Возвращает стратегию поиска на основе сущности"""
        if entity["type"] in ["game_location", "proper_name"]:
            return "exact_match_first"
        if entity["confidence"] > 0.7:
            return "exact_match_first"
        if entity["is_proper_name"]:
            return "exact_match_first"
        return "semantic_first"


# Глобальный экземпляр
_resolver = None


def get_entity_resolver() -> EntityResolver:
    global _resolver
    if _resolver is None:
        _resolver = EntityResolver()
    return _resolver


if __name__ == "__main__":
    resolver = get_entity_resolver()

    test_queries = [
        "Легенда Форнема",
        "Кто такой Пушкин?",
        "X3 сектор Легенда Форнема",
        "Как установить Python?",
        "Фильм Матрица",
        "Смысл жизни",
    ]

    print("=== Entity Resolver Test ===\n")
    for q in test_queries:
        result = resolver.resolve(q)
        strategy = resolver.get_search_strategy(result)
        print(f"Запрос: {q}")
        print(f"  Тип: {result['type']}")
        print(f"  Уверенность: {result['confidence']:.2f}")
        print(f"  Собственное имя: {result['is_proper_name']}")
        print(f"  Стратегия: {strategy}")
        print(f"  Категории: {result['categories']}")
        print()
