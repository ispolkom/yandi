"""
agent/object_resolver.py — Определяет тип объекта для субъективных запросов.

Rust-перенос (2026-09-24): rustlib/yandi_rs/src/object_resolver.rs — ObjectResolver.resolve. Доказан на
совпадение тестом agent/object_resolver_rust_parity_test.py. По умолчанию ВЫКЛЮЧЕН; включается переменной
окружения YANDI_OBJECT_RESOLVER_ENGINE=rust ПОСЛЕ сборки rustlib/yandi_rs (`maturin develop`, см.
rustlib/README.md). Делегирует, только пока `self.patterns` экземпляра не изменён; иначе — Python.
"""

import copy
import logging
import os
import re
from typing import Dict, Any, Tuple

log = logging.getLogger("yandi.object_resolver")

_rust_or = None          # None = ещё не пробовали; False = не запрошено/не собрано; модуль = подключён


def _get_rust_or():
    global _rust_or
    if _rust_or is None:
        if os.environ.get("YANDI_OBJECT_RESOLVER_ENGINE") == "rust":
            try:
                import yandi_rs.object_resolver as _rs
                _rust_or = _rs
                log.warning("YANDI_OBJECT_RESOLVER_ENGINE=rust: используется Rust-реализация object_resolver (rustlib/yandi_rs)")
            except ImportError as e:
                log.warning("YANDI_OBJECT_RESOLVER_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python", e)
                _rust_or = False
        else:
            _rust_or = False
    return _rust_or or None


class ObjectResolver:
    def __init__(self):
        self.patterns = {
            "song": {
                "patterns": [
                    r"песн", r"song", r"трек", r"композиц",
                    r"музык", r"мелоди", r"don't cry", r"i don't cry",
                    r"guns n roses", r"ганз энд роузез",
                    r"виагра", r"via gra",
                ],
                "type": "song",
                "confidence": 0.7,
                "analyzer": "SongAnalyzer"
            },
            "movie": {
                "patterns": [
                    r"фильм", r"movie", r"кино", r"сериал",
                    r"interstellar", r"интерстеллар",
                    r"matrix", r"матрица",
                ],
                "type": "movie",
                "confidence": 0.7,
                "analyzer": "MovieAnalyzer"
            },
            "book": {
                "patterns": [
                    r"книг", r"book", r"роман", r"повест",
                    r"рассказ", r"story",
                ],
                "type": "book",
                "confidence": 0.7,
                "analyzer": "BookAnalyzer"
            },
            "person": {
                "patterns": [
                    r"ницше", r"nietzsche",
                    r"пушкин", r"pushkin",
                    r"достоевск", r"dostoevsky",
                    r"толстой", r"tolstoy",
                    r"человек", r"person", r"личность",
                ],
                "type": "person",
                "confidence": 0.6,
                "analyzer": "CharacterAnalyzer"
            },
            "idea": {
                "patterns": [
                    r"свобод", r"justice", r"справедлив",
                    r"любов", r"love", r"смысл", r"meaning",
                    r"жизн", r"life", r"смерт", r"death",
                    r"философи", r"philosophy",
                ],
                "type": "idea",
                "confidence": 0.5,
                "analyzer": "IdeaAnalyzer"
            },
            "self_reflection": {
                "patterns": [
                    r"ты женщина", r"ты девушка",
                    r"первая цифровая", r"цифровая личность",
                    r"если бы ты была", r"ты бы хотела",
                    r"чего бы тебе хотелось", r"чего ты хочешь",
                    r"твои чувства", r"твой характер",
                    r"что ты чувствуешь", r"какая ты",
                    r"расскажи о себе", r"опиши себя",
                    r"твоё состояние", r"как ты себя",
                    r"YANDI", r"Янди", r"ты цифровая",
                ],
                "type": "self_reflection",
                "confidence": 0.7,
                "analyzer": "SelfReflectionAnalyzer"
            },
            "game": {
                "patterns": [
                    r"игр", r"game", r"сектор", r"x3",
                    r"игра", r"gaming",
                ],
                "type": "game",
                "confidence": 0.6,
                "analyzer": "GameAnalyzer"
            },
        }
        self._default_patterns = copy.deepcopy(self.patterns)

    def resolve(self, query: str) -> Dict[str, Any]:
        """
        Определяет тип объекта в запросе.
        Возвращает: {type, confidence, analyzer, matched_pattern}
        """
        rs = _get_rust_or()
        if rs is not None and isinstance(query, str) and self.patterns == self._default_patterns:
            return dict(rs.resolve(query))

        q = query.lower().strip()
        
        best_match = {
            "type": "unknown",
            "confidence": 0.0,
            "analyzer": "GeneralSubjective",
            "matched_pattern": "none"
        }
        
        for obj_type, obj_data in self.patterns.items():
            for pattern in obj_data["patterns"]:
                if re.search(pattern, q, re.IGNORECASE):
                    # Чем длиннее паттерн, тем выше уверенность
                    confidence = min(1.0, obj_data.get("confidence", 0.5) + (len(pattern) / 200))
                    
                    # Проверка на self_reflection — самый высокий приоритет
                    if obj_type == "self_reflection":
                        confidence += 0.2
                    
                    if confidence > best_match["confidence"]:
                        best_match = {
                            "type": obj_data["type"],
                            "confidence": min(1.0, confidence),
                            "analyzer": obj_data["analyzer"],
                            "matched_pattern": pattern
                        }
        
        return best_match


def get_object_resolver() -> ObjectResolver:
    return ObjectResolver()
