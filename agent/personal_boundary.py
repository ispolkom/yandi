"""
agent/personal_boundary.py — Определяет границы личности YANDI.
Отличает личные запросы от деловых.
Различает искренние извинения от провокаций.

Rust-перенос (2026-09-24): rustlib/yandi_rs/src/personal_boundary.rs — PersonalBoundary.analyze и
get_response_template (класс и датакласс остаются здесь; Rust отдаёт данные). Доказан на совпадение
тестом agent/personal_boundary_rust_parity_test.py. По умолчанию ВЫКЛЮЧЕН; включается переменной
окружения YANDI_PERSONAL_BOUNDARY_ENGINE=rust ПОСЛЕ сборки rustlib/yandi_rs (`maturin develop`, см.
rustlib/README.md). Делегирует, только пока списки паттернов экземпляра не изменены; иначе — Python.
"""

import logging
import os
import re
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

log = logging.getLogger("yandi.personal_boundary")

_rust_pb = None          # None = ещё не пробовали; False = не запрошено/не собрано; модуль = подключён


def _get_rust_pb():
    global _rust_pb
    if _rust_pb is None:
        if os.environ.get("YANDI_PERSONAL_BOUNDARY_ENGINE") == "rust":
            try:
                import yandi_rs.personal_boundary as _rs
                _rust_pb = _rs
                log.warning("YANDI_PERSONAL_BOUNDARY_ENGINE=rust: используется Rust-реализация personal_boundary (rustlib/yandi_rs)")
            except ImportError as e:
                log.warning("YANDI_PERSONAL_BOUNDARY_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python", e)
                _rust_pb = False
        else:
            _rust_pb = False
    return _rust_pb or None


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
        self._default_patterns = self._pattern_snapshot()

    def _pattern_snapshot(self):
        return (
            list(self.personal_patterns), list(self.sincere_apology_patterns), list(self.formal_apology_patterns),
            list(self.provocation_patterns), list(self.deep_question_patterns),
        )

    def _patterns_unchanged(self) -> bool:
        """Rust знает только паттерны по умолчанию: если списки экземпляра изменили — считает Python."""
        return self._pattern_snapshot() == self._default_patterns

    def analyze(self, query: str, context: Dict = None) -> BoundaryAnalysis:
        """
        Анализирует запрос на предмет личного характера.
        """
        rs = _get_rust_pb()
        if rs is not None and isinstance(query, str) and self._patterns_unchanged():
            return BoundaryAnalysis(**rs.analyze(query))

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

        rs = _get_rust_pb()
        if (rs is not None and isinstance(analysis, BoundaryAnalysis)
                and isinstance(trust, (int, float)) and isinstance(irritation, (int, float))):
            try:
                kind, tone, template = rs.get_response_template(
                    bool(analysis.is_provocation), bool(analysis.is_apology), bool(analysis.is_sincere),
                    bool(analysis.is_personal), bool(analysis.is_deep_question), bool(analysis.is_social),
                    float(trust), float(irritation),
                )
                return {"type": kind, "tone": tone, "template": template}
            except OverflowError:
                pass        # число, не помещающееся в float, — обычный Python-путь
        
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
