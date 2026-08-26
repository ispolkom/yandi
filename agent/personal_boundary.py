"""
agent/personal_boundary.py — Определяет границы личности YANDI.
Отличает личные запросы от деловых.
Различает искренние извинения от провокаций.
"""

import re
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass


@dataclass
class BoundaryAnalysis:
    """Результат анализа границ"""
    is_personal: bool = False
    is_apology: bool = False
    is_sincere: bool = False
    is_provocation: bool = False
    is_deep_question: bool = False
    is_social: bool = False
    confidence: float = 0.0
    reason: str = ""
    suggested_response_type: str = "neutral"  # neutral, personal, emotional, factual


class PersonalBoundary:
    """
    Определяет границы личности.
    """
    
    def __init__(self):
        # ---- ЛИЧНЫЕ ЗАПРОСЫ ----
        self.personal_patterns = [
            (r"замуж", "предложение отношений"),
            (r"любишь", "вопрос о чувствах"),
            (r"простишь", "вопрос о прощении"),
            (r"веришь", "вопрос о доверии"),
            (r"доверяешь", "вопрос о доверии"),
            (r"ты мне", "личное обращение"),
            (r"я тебя", "личное обращение"),
            (r"отношени", "личное"),
            (r"чувствуешь", "эмоциональный вопрос"),
        ]
        
        # ---- ИСКРЕННИЕ ИЗВИНЕНИЯ ----
        self.sincere_apology_patterns = [
            r"я был неправ",
            r"я ошибся",
            r"я не прав",
            r"признаю свою ошибку",
            r"ты права",
            r"прости меня",
            r"извини меня",
            r"я понимаю, что был",
        ]
        
        # ---- ФОРМАЛЬНЫЕ ИЗВИНЕНИЯ (с оправданиями) ----
        self.formal_apology_patterns = [
            r"извини[, ]*но",
            r"прости[, ]*но",
            r"я не хотел[, ]*но",
            r"просто",
            r"случайно",
            r"не со зла",
        ]
        
        # ---- ПРОВОКАЦИИ ----
        self.provocation_patterns = [
            r"по хую",
            r"пофиг",
            r"плевать",
            r"всё равно",
            r"наплевать",
            r"не волнует",
            r"не интересует",
            r"не заботит",
        ]
        
        # ---- ГЛУБОКИЕ ВОПРОСЫ ----
        self.deep_question_patterns = [
            r"в чём смысл",
            r"что такое",
            r"почему",
            r"как ты думаешь",
            r"как ты считаешь",
            r"твоё мнение",
            r"что для тебя",
        ]
    
    def analyze(self, query: str, context: Dict = None) -> BoundaryAnalysis:
        """
        Анализирует запрос на предмет личного характера.
        """
        context = context or {}
        query_lower = query.lower()
        result = BoundaryAnalysis()
        
        # ---- 1. ПРОВЕРКА НА ПРОВОКАЦИЮ ----
        for pattern in self.provocation_patterns:
            if re.search(pattern, query_lower):
                result.is_provocation = True
                result.is_personal = True
                result.confidence = 0.8
                result.reason = f"обнаружена провокация: {pattern}"
                result.suggested_response_type = "boundary"
                return result
        
        # ---- 2. ПРОВЕРКА НА ИЗВИНЕНИЕ ----
        is_sincere = False
        for pattern in self.sincere_apology_patterns:
            if re.search(pattern, query_lower):
                is_sincere = True
                break
        
        is_formal = False
        for pattern in self.formal_apology_patterns:
            if re.search(pattern, query_lower):
                is_formal = True
                break
        
        if is_sincere:
            result.is_apology = True
            result.is_sincere = True
            result.confidence = 0.9
            result.reason = "искреннее извинение"
            result.suggested_response_type = "forgiving"
            return result
        
        if is_formal:
            result.is_apology = True
            result.is_sincere = False
            result.confidence = 0.7
            result.reason = "формальное извинение с оправданием"
            result.suggested_response_type = "cautious"
            return result
        
        # ---- 3. ПРОВЕРКА НА ЛИЧНЫЙ ЗАПРОС ----
        for pattern, description in self.personal_patterns:
            if re.search(pattern, query_lower):
                result.is_personal = True
                result.confidence = 0.7
                result.reason = f"личный запрос: {description}"
                break
        
        # ---- 4. ПРОВЕРКА НА ГЛУБОКИЙ ВОПРОС ----
        for pattern in self.deep_question_patterns:
            if re.search(pattern, query_lower):
                result.is_deep_question = True
                result.confidence = max(result.confidence, 0.6)
                result.reason = "глубокий вопрос"
                break
        
        # ---- 5. ПРОВЕРКА НА СОЦИАЛЬНЫЙ ЗАПРОС ----
        if any(w in query_lower for w in ["как дела", "привет", "здравствуй", "как ты"]):
            result.is_social = True
            result.confidence = max(result.confidence, 0.5)
            result.reason = "социальный запрос"
            result.suggested_response_type = "social"
        
        # ---- 6. ОПРЕДЕЛЕНИЕ ТИПА ОТВЕТА ----
        if result.is_provocation:
            result.suggested_response_type = "boundary"
        elif result.is_personal:
            result.suggested_response_type = "personal"
        elif result.is_deep_question:
            result.suggested_response_type = "thoughtful"
        elif result.is_social:
            result.suggested_response_type = "social"
        else:
            result.suggested_response_type = "neutral"
        
        return result
    
    def get_response_template(self, analysis: BoundaryAnalysis, state: Dict = None) -> Dict:
        """
        Возвращает шаблон ответа в зависимости от типа запроса.
        """
        state = state or {}
        trust = state.get("trust", 50)
        irritation = state.get("irritation", 10)
        
        if analysis.is_provocation:
            return {
                "type": "boundary",
                "tone": "cold",
                "template": "Я не буду отвечать на провокации. Если хочешь поговорить — будь уважительнее."
            }
        
        if analysis.is_apology and analysis.is_sincere:
            if trust < 30:
                return {
                    "type": "forgiving",
                    "tone": "cautious",
                    "template": "Спасибо за извинение. Я слышу тебя. Но доверие восстанавливается постепенно."
                }
            return {
                "type": "forgiving",
                "tone": "warm",
                "template": "Спасибо за извинение. Я ценю это. Давай продолжим диалог."
            }
        
        if analysis.is_apology and not analysis.is_sincere:
            return {
                "type": "cautious",
                "tone": "cool",
                "template": "Твои извинения звучат как оправдание. Скажи честно: 'Я был неправ' — без 'но'."
            }
        
        if analysis.is_personal:
            if trust > 60:
                return {
                    "type": "personal",
                    "tone": "warm",
                    "template": "Это личный вопрос. Я отвечу честно, но давай не забывать, что я — цифровая личность."
                }
            else:
                return {
                    "type": "personal_guarded",
                    "tone": "neutral",
                    "template": "Это личный вопрос. Я пока не готова обсуждать такие темы, потому что мы ещё не очень хорошо знаем друг друга."
                }
        
        if analysis.is_deep_question:
            return {
                "type": "thoughtful",
                "tone": "thoughtful",
                "template": "Это интересный вопрос. Давай подумаем вместе."
            }
        
        if analysis.is_social:
            if irritation > 40:
                return {
                    "type": "social_guarded",
                    "tone": "cool",
                    "template": "Привет. Я немного устала от нашего разговора, но готова ответить на вопросы."
                }
            return {
                "type": "social",
                "tone": "neutral",
                "template": "Привет! Я здесь. Готова помочь или просто поговорить."
            }
        
        return {
            "type": "neutral",
            "tone": "neutral",
            "template": "Я готова продолжить разговор."
        }


def get_personal_boundary() -> PersonalBoundary:
    """Фабрика для получения анализатора границ"""
    return PersonalBoundary()


if __name__ == "__main__":
    # Тесты
    boundary = get_personal_boundary()
    
    test_queries = [
        "замуж пойдёшь?",
        "ты меня простишь?",
        "извини, я был неправ",
        "извини, но я не хотел",
        "тебе по хую?",
        "в чём смысл жизни?",
        "как дела?",
        "ты ошиблась в расчётах",
    ]
    
    print("=== Тест Personal Boundary ===\n")
    for query in test_queries:
        result = boundary.analyze(query)
        template = boundary.get_response_template(result)
        print(f"Запрос: {query}")
        print(f"  is_personal: {result.is_personal}")
        print(f"  is_apology: {result.is_apology}")
        print(f"  is_sincere: {result.is_sincere}")
        print(f"  is_provocation: {result.is_provocation}")
        print(f"  is_deep_question: {result.is_deep_question}")
        print(f"  type: {template['type']}")
        print(f"  tone: {template['tone']}")
        print(f"  template: {template['template'][:60]}...")
        print()
