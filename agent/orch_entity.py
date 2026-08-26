"""
assistant/orch_entity.py — Entity Resolution для Orchestrator v2.

Извлекает сущности из запроса (фильмы, сериалы, книги, игры, люди).
Ищет их в локальном реестре и через web.
Возвращает структурированную информацию о сущности.

Цель: перед синтезом ответа — понять, о чём именно спрашивает пользователь.
"""
from __future__ import annotations

import re
import json
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field

# Добавляем путь для импорта
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))


@dataclass
class EntityResult:
    """Результат разрешения сущности."""
    entity_id: str
    title: str
    original_title: Optional[str] = None
    year: Optional[int] = None
    director: Optional[str] = None
    type: str = "unknown"  # movie | series | book | game | person | other
    synopsis: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    confidence: float = 0.0
    source: str = "local"  # local | web | both
    url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    is_ambiguous: bool = False
    alternatives: List[Dict[str, Any]] = field(default_factory=list)


# Паттерны для извлечения названия
_TITLE_PATTERNS = [
    # "смысл фильма Эскортницы" → "Эскортницы"
    r"(?:смысл|о чем|разбор|объясни)\s+(?:фильма|сериала|книги|игры|фильм|сериал|книг|игр)\s+[\"']?([^\"'.,!?]+)[\"']?",
    # "о чем фильм Интерстеллар" → "Интерстеллар"
    r"о\s+чем\s+(?:фильм|сериал)\s+[\"']?([^\"'.,!?]+)[\"']?",
    # "объясни концовку сериала Очень странные дела" → "Очень странные дела"
    r"объясни\s+концовку\s+(?:фильма|сериала|книги|игры)\s+[\"']?([^\"'.,!?]+)[\"']?",
    # "смысл фильма Интерстеллар" → "Интерстеллар"
    r"смысл\s+фильма\s+[\"']?([^\"'.,!?]+)[\"']?",
    # "что хотел сказать режиссёр фильма Интерстеллар" → "Интерстеллар"
    r"режисс[ёе]р\s+(?:фильма|сериала)\s+[\"']?([^\"'.,!?]+)[\"']?",
    # "Эскортницы" (просто название)
    r"^([А-ЯЁ][а-яё]+\s*[А-ЯЁ]?[а-яё]*)$",
]

# Известные фильмы для быстрого локального поиска (заглушка)
_LOCAL_DB: Dict[str, Dict[str, Any]] = {
    "интерстеллар": {
        "title": "Интерстеллар",
        "original_title": "Interstellar",
        "year": 2014,
        "director": "Кристофер Нолан",
        "type": "movie",
        "genres": ["научная фантастика", "драма", "приключения"],
        "synopsis": "Группа исследователей отправляется через червоточину в поисках нового дома для человечества.",
        "url": "https://www.kinopoisk.ru/film/258687/",
    },
    "эскортницы": {
        "title": "Эскортницы",
        "original_title": "Escort Girls",
        "year": 2020,
        "director": "Неизвестен",
        "type": "movie",
        "genres": ["драма", "комедия"],
        "synopsis": "Фильм о жизни девушек, работающих в эскорт-услугах.",
        "url": "",
    },
    "очень странные дела": {
        "title": "Очень странные дела",
        "original_title": "Stranger Things",
        "year": 2016,
        "director": "Братья Даффер",
        "type": "series",
        "genres": ["научная фантастика", "ужасы", "драма"],
        "synopsis": "В маленьком городке мальчик исчезает, и его друзья начинают расследование, сталкиваясь с таинственными силами.",
        "url": "https://www.kinopoisk.ru/series/931311/",
    },
}


def extract_title(query: str) -> Optional[str]:
    """
    Извлечь название произведения из запроса.
    """
    q = query.lower().strip()
    
    for pattern in _TITLE_PATTERNS:
        match = re.search(pattern, q, re.IGNORECASE)
        if match:
            title = match.group(1).strip()
            # Убираем возможные артикли и предлоги в конце
            title = re.sub(r'\s+(?:фильм|сериал|книг|игр|год|года|вышел|сезон|серии?)$', '', title, flags=re.IGNORECASE)
            return title
    
    # Если запрос короткий и похож на название
    if len(q.split()) <= 4 and not any(w in q for w in ["что", "как", "почему", "зачем", "когда", "где"]):
        # Убираем стоп-слова
        stop_words = ["смысл", "о чем", "разбор", "объясни", "фильм", "сериал", "книг", "игр"]
        cleaned = q
        for sw in stop_words:
            cleaned = cleaned.replace(sw, "").strip()
        if cleaned and len(cleaned) > 2:
            return cleaned.title()
    
    return None


def search_local(title: str) -> Optional[Dict[str, Any]]:
    """
    Поиск в локальной базе знаний.
    """
    title_lower = title.lower().strip()
    
    # Прямое совпадение
    if title_lower in _LOCAL_DB:
        return _LOCAL_DB[title_lower]
    
    # Частичное совпадение
    for key, data in _LOCAL_DB.items():
        if title_lower in key or key in title_lower:
            return data
        
        # Проверяем оригинальное название
        if data.get("original_title", "").lower() in title_lower:
            return data
    
    return None


def search_web(title: str) -> Optional[Dict[str, Any]]:
    """
    Поиск в web (заглушка).
    TODO: Реализовать реальный поиск через API кинопоиска или IMDB.
    """
    # Пока просто возвращаем None, если не нашли в локальной базе
    # В будущем: запрос к TMDB, IMDb, Kinopoisk API
    return None


def resolve_entity(query: str, enable_web: bool = True) -> Optional[EntityResult]:
    """
    Основная функция разрешения сущности.
    """
    # 1. Извлечь название
    title = extract_title(query)
    if not title:
        return None
    
    # 2. Поиск в локальной базе
    local_data = search_local(title)
    
    if local_data:
        return EntityResult(
            entity_id=f"ent_{uuid.uuid4().hex[:8]}",
            title=local_data.get("title", title),
            original_title=local_data.get("original_title"),
            year=local_data.get("year"),
            director=local_data.get("director"),
            type=local_data.get("type", "movie"),
            synopsis=local_data.get("synopsis"),
            genres=local_data.get("genres", []),
            confidence=0.85,
            source="local",
            url=local_data.get("url"),
            metadata=local_data.get("metadata", {}),
            is_ambiguous=False,
        )
    
    # 3. Поиск в web (если включён)
    if enable_web:
        web_data = search_web(title)
        if web_data:
            return EntityResult(
                entity_id=f"ent_{uuid.uuid4().hex[:8]}",
                title=web_data.get("title", title),
                original_title=web_data.get("original_title"),
                year=web_data.get("year"),
                director=web_data.get("director"),
                type=web_data.get("type", "movie"),
                synopsis=web_data.get("synopsis"),
                genres=web_data.get("genres", []),
                confidence=0.7,
                source="web",
                url=web_data.get("url"),
                metadata=web_data.get("metadata", {}),
                is_ambiguous=False,
            )
    
    # 4. Не найдено — возвращаем частичный результат с низкой уверенностью
    return EntityResult(
        entity_id=f"ent_{uuid.uuid4().hex[:8]}",
        title=title,
        type="unknown",
        confidence=0.1,
        source="none",
        is_ambiguous=False,
        metadata={"suggested_title": title},
    )


def disambiguate_entity(title: str, alternatives: List[Dict[str, Any]]) -> EntityResult:
    """
    Разрешить неоднозначность, если найдено несколько кандидатов.
    """
    if not alternatives:
        return None
    
    # Если только один альтернативный вариант — берём его
    if len(alternatives) == 1:
        data = alternatives[0]
        return EntityResult(
            entity_id=f"ent_{uuid.uuid4().hex[:8]}",
            title=data.get("title", title),
            original_title=data.get("original_title"),
            year=data.get("year"),
            director=data.get("director"),
            type=data.get("type", "unknown"),
            synopsis=data.get("synopsis"),
            genres=data.get("genres", []),
            confidence=0.7,
            source=data.get("source", "local"),
            url=data.get("url"),
            metadata=data.get("metadata", {}),
            is_ambiguous=False,
        )
    
    # Если несколько — возвращаем все варианты
    return EntityResult(
        entity_id=f"ent_{uuid.uuid4().hex[:8]}",
        title=title,
        type="unknown",
        confidence=0.3,
        source="ambiguous",
        is_ambiguous=True,
        alternatives=alternatives,
        metadata={"suggested_title": title},
    )


def format_entity_for_prompt(entity: EntityResult) -> str:
    """
    Форматировать сущность для вставки в промпт синтезатора.
    """
    parts = []
    parts.append(f"Название: {entity.title}")
    if entity.original_title:
        parts.append(f"Оригинальное название: {entity.original_title}")
    if entity.year:
        parts.append(f"Год: {entity.year}")
    if entity.director:
        parts.append(f"Режиссёр: {entity.director}")
    if entity.type and entity.type != "unknown":
        parts.append(f"Тип: {entity.type}")
    if entity.genres:
        parts.append(f"Жанры: {', '.join(entity.genres[:3])}")
    if entity.synopsis:
        parts.append(f"Синопсис: {entity.synopsis[:200]}")
    
    return "\n".join(parts)


if __name__ == "__main__":
    test_queries = [
        "Смысл фильма Эскортницы?",
        "О чем фильм Интерстеллар?",
        "Объясни концовку сериала Очень странные дела",
        "Что хотел сказать режиссёр фильма Дюна?",
        "Разбор фильма Паразиты",
        "Смысл книги Преступление и наказание",
        "О чем игра Ведьмак 3?",
    ]
    
    print("=" * 60)
    print("ENTITY RESOLUTION TEST")
    print("=" * 60)
    
    for q in test_queries:
        print(f"\n📝 Запрос: {q}")
        title = extract_title(q)
        print(f"  📌 Извлечено название: {title}")
        
        if title:
            result = resolve_entity(q)
            if result:
                print(f"  🏷️  Результат:")
                print(f"     - Название: {result.title}")
                print(f"     - Оригинал: {result.original_title}")
                print(f"     - Год: {result.year}")
                print(f"     - Режиссёр: {result.director}")
                print(f"     - Тип: {result.type}")
                print(f"     - Уверенность: {result.confidence:.2f}")
                print(f"     - Источник: {result.source}")
            else:
                print(f"  ❌ Не найдено")
        else:
            print(f"  ❌ Не удалось извлечь название")
