"""
agent/object_resolver.py — Определяет тип объекта для субъективных запросов.
"""

import re
from typing import Dict, Any, Tuple


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

    def resolve(self, query: str) -> Dict[str, Any]:
        """
        Определяет тип объекта в запросе.
        Возвращает: {type, confidence, analyzer, matched_pattern}
        """
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
