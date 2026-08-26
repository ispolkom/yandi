"""
agent/strategy_router.py — Адаптивный роутер стратегий поиска.
Меняет стратегию в зависимости от результата.
"""

from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum


class SearchStrategy(Enum):
    """Стратегии поиска"""
    EXACT_ENTITY = "exact_entity"          # Точное совпадение сущности
    GAME_PROFILE = "game_profile"          # Игровые источники
    MEDIA_PROFILE = "media_profile"        # Медиа (фильмы, книги)
    TECH_PROFILE = "tech_profile"          # Технические источники
    GENERAL_WEB = "general_web"            # Обычный веб-поиск
    USER_URL = "user_url"                  # Ссылка пользователя
    SEMANTIC = "semantic"                  # Семантический поиск
    CLARIFY = "clarify"                    # Запрос уточнения


@dataclass
class StrategyResult:
    """Результат применения стратегии"""
    strategy: SearchStrategy
    queries: List[str]
    sources: List[str]
    confidence: float
    reason: str


class StrategyRouter:
    def __init__(self):
        self.strategy_history: List[Dict[str, Any]] = []
        self.failed_strategies: List[SearchStrategy] = []
        self.current_strategy: Optional[SearchStrategy] = None
        self.attempts = 0
        self.max_attempts = 4

    def select_strategy(
        self,
        query: str,
        entity_info: Dict[str, Any],
        intent_type: str,
        search_result: Optional[Dict[str, Any]] = None,
        user_url: Optional[str] = None,
        user_hint: Optional[str] = None,
    ) -> StrategyResult:
        """
        Выбирает стратегию на основе контекста и предыдущих результатов.
        """
        self.attempts += 1
        entity_type = entity_info.get("type", "unknown")
        entity_confidence = entity_info.get("confidence", 0.0)
        is_proper_name = entity_info.get("is_proper_name", False)

        # ---- 1. ЕСЛИ ЕСТЬ URL ПОЛЬЗОВАТЕЛЯ ----
        if user_url:
            self.current_strategy = SearchStrategy.USER_URL
            return StrategyResult(
                strategy=SearchStrategy.USER_URL,
                queries=[user_url],
                sources=[user_url],
                confidence=0.95,
                reason="пользователь предоставил ссылку — приоритет"
            )

        # ---- 2. ЕСЛИ ПОЛЬЗОВАТЕЛЬ ДАЛ ПОДСКАЗКУ ----
        if user_hint and ("x3" in user_hint.lower() or "игра" in user_hint.lower()):
            self.current_strategy = SearchStrategy.GAME_PROFILE
            return self._game_strategy(query, entity_info, user_hint)

        # ---- 3. ЕСЛИ ЭТО ИГРОВОЙ ОБЪЕКТ ----
        if entity_type in ["game_location", "game_entity"] and entity_confidence > 0.6:
            self.current_strategy = SearchStrategy.GAME_PROFILE
            return self._game_strategy(query, entity_info)

        # ---- 4. ЕСЛИ ЭТО СОБСТВЕННОЕ ИМЯ С ВЫСОКОЙ УВЕРЕННОСТЬЮ ----
        if is_proper_name and entity_confidence > 0.7:
            self.current_strategy = SearchStrategy.EXACT_ENTITY
            return self._exact_strategy(query, entity_info)

        # ---- 5. ЕСЛИ ЭТО МЕДИА ----
        if "media" in entity_info.get("categories", []):
            self.current_strategy = SearchStrategy.MEDIA_PROFILE
            return self._media_strategy(query, entity_info)

        # ---- 6. ЕСЛИ БЫЛИ НЕУДАЧНЫЕ ПОПЫТКИ ----
        if self.failed_strategies:
            # Пробуем следующую стратегию
            if SearchStrategy.GAME_PROFILE not in self.failed_strategies:
                self.current_strategy = SearchStrategy.GAME_PROFILE
                return self._game_strategy(query, entity_info)
            if SearchStrategy.EXACT_ENTITY not in self.failed_strategies:
                self.current_strategy = SearchStrategy.EXACT_ENTITY
                return self._exact_strategy(query, entity_info)

        # ---- 7. ПО УМОЛЧАНИЮ ----
        self.current_strategy = SearchStrategy.GENERAL_WEB
        return StrategyResult(
            strategy=SearchStrategy.GENERAL_WEB,
            queries=[query],
            sources=["general_search"],
            confidence=0.5,
            reason="общий веб-поиск"
        )

    def _exact_strategy(self, query: str, entity_info: Dict[str, Any]) -> StrategyResult:
        """Стратегия точного совпадения"""
        entity_name = entity_info.get("canonical_name", query)
        queries = [
            f'"{entity_name}"',
            f'"{entity_name}" X3',
            f'"{entity_name}" игра',
            f'"{entity_name}" сектор',
        ]
        sources = ["exact_search"]
        return StrategyResult(
            strategy=SearchStrategy.EXACT_ENTITY,
            queries=queries,
            sources=sources,
            confidence=0.85,
            reason=f"точный поиск сущности: {entity_name}"
        )

    def _game_strategy(self, query: str, entity_info: Dict[str, Any], hint: str = "") -> StrategyResult:
        """Стратегия поиска по игровым источникам"""
        entity_name = entity_info.get("canonical_name", query)
        
        # Определяем игру
        game = "X3"
        if hint:
            import re
            game_match = re.search(r'X\d|X3|XB|Albion', hint, re.IGNORECASE)
            if game_match:
                game = game_match.group(0).upper()
        
        queries = [
            f'"{entity_name}" {game} сектор',
            f'"{entity_name}" {game} sector',
            f'{game} sector "{entity_name}"',
            f'{entity_name} {game} wiki',
        ]
        sources = [
            "x3tc.allnetic.com",
            "x-universe.fandom.com",
            "x3wiki.com",
            "forum.egosoft.com",
            "steamcommunity.com/app/2820",
        ]
        return StrategyResult(
            strategy=SearchStrategy.GAME_PROFILE,
            queries=queries,
            sources=sources,
            confidence=0.8,
            reason=f"игровой профиль: {game}, сущность: {entity_name}"
        )

    def _media_strategy(self, query: str, entity_info: Dict[str, Any]) -> StrategyResult:
        """Стратегия поиска по медиа (фильмы, книги)"""
        entity_name = entity_info.get("canonical_name", query)
        queries = [
            f'"{entity_name}" фильм',
            f'"{entity_name}" книга',
            f'"{entity_name}" сериал',
            f'"{entity_name}" IMDb',
        ]
        sources = [
            "imdb.com",
            "kinopoisk.ru",
            "goodreads.com",
            "wikipedia.org",
        ]
        return StrategyResult(
            strategy=SearchStrategy.MEDIA_PROFILE,
            queries=queries,
            sources=sources,
            confidence=0.7,
            reason=f"медиа профиль: {entity_name}"
        )

    def record_result(self, strategy: SearchStrategy, found_count: int, was_successful: bool):
        """Запоминает результат попытки"""
        if not was_successful and strategy not in self.failed_strategies:
            self.failed_strategies.append(strategy)
        
        self.strategy_history.append({
            "strategy": strategy.value,
            "found_count": found_count,
            "success": was_successful,
            "attempt": self.attempts,
        })

    def should_switch_strategy(self) -> bool:
        """Проверяет, нужно ли сменить стратегию"""
        if self.attempts >= self.max_attempts:
            return True
        if len(self.failed_strategies) >= 3:
            return True
        return False

    def get_status(self) -> Dict[str, Any]:
        """Возвращает статус роутера"""
        return {
            "current_strategy": self.current_strategy.value if self.current_strategy else None,
            "attempts": self.attempts,
            "failed_strategies": [s.value for s in self.failed_strategies],
            "history": self.strategy_history[-5:],
        }


# Глобальный экземпляр
_router: Optional[StrategyRouter] = None


def get_strategy_router() -> StrategyRouter:
    global _router
    if _router is None:
        _router = StrategyRouter()
    return _router


if __name__ == "__main__":
    router = get_strategy_router()

    # Тест
    test_entity = {
        "type": "game_location",
        "canonical_name": "Буря Окраккоа",
        "confidence": 0.85,
        "is_proper_name": True,
    }

    # Первая попытка
    result = router.select_strategy(
        query="Буря Окраккоа сектор",
        entity_info=test_entity,
        intent_type="objective_information",
    )
    print(f"Стратегия: {result.strategy.value}")
    print(f"Запросы: {result.queries}")
    print(f"Источники: {result.sources}")

    # Запоминаем неудачу
    router.record_result(result.strategy, 0, False)

    # Вторая попытка
    result = router.select_strategy(
        query="Буря Окраккоа сектор",
        entity_info=test_entity,
        intent_type="objective_information",
        user_hint="Это сектор X3",
    )
    print(f"\nПосле подсказки:")
    print(f"Стратегия: {result.strategy.value}")
    print(f"Запросы: {result.queries}")
    print(f"Источники: {result.sources}")
    print(f"\nСтатус: {router.get_status()}")
