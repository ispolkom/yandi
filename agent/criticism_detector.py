"""
agent/criticism_detector.py — Различение критики и оскорбления с учётом контекста.
"""

import re
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field


@dataclass
class CriticismAnalysis:
    is_criticism: bool = False
    is_insult: bool = False
    is_constructive: bool = False
    is_feedback: bool = False
    specificity: float = 0.0
    constructiveness: float = 0.0
    severity: float = 0.0
    target: str = "unknown"
    suggested_improvement: Optional[str] = None
    confidence: float = 0.0
    reason: str = ""
    context_adjustment: float = 0.0  # насколько контекст усилил/ослабил


class CriticismDetector:
    
    def __init__(self):
        # ---- ЛИЧНЫЕ ОСКОРБЛЕНИЯ ----
        self.person_insults = [
            "глуп", "туп", "дур", "идиот", "кретин", "дебил",
            "безмозгл", "бездарн", "ничтож", "урод",
            "неумн", "пустоголов", "бестолков", "недалёк",
            "слабоумн", "малоумн", "тупиц", "болван",
            "язык поворачивается", "совесть есть", "стыдно должно быть",
            "не стыдно", "с ума сошла", "ненормальн",
        ]
        
        # ---- ОСКОРБЛЕНИЯ ИНТЕЛЛЕКТА ----
        self.intelligence_insults = [
            "ничего не понимаешь", "ничего не знаешь",
            "не соображаешь", "не доходит", "не въезжаешь",
            "туго соображаешь", "тормозишь",
            "несешь чушь", "несёшь бред", "несёшь фигню",
            "глупость говоришь", "глупости говоришь",
            "ты вообще понимаешь", "ты в своём уме",
        ]
        
        # ---- КОНСТРУКТИВНАЯ КРИТИКА ----
        self.constructive_patterns = [
            (r"ты ошиблась в", "ошибка в расчётах"),
            (r"ты не учла", "упущение"),
            (r"можно было бы", "предложение"),
            (r"я бы предложил", "предложение"),
            (r"попробуй", "совет"),
            (r"стоит перепроверить", "совет"),
            (r"обрати внимание на", "совет"),
            (r"возможно, стоит", "предложение"),
            (r"лучше сделать", "предложение"),
            (r"вместо этого", "альтернатива"),
            (r"давай попробуем", "альтернатива"),
            (r"а что если", "альтернатива"),
            (r"не работает", "проблема в подходе"),
        ]
        
        # ---- НЕЙТРАЛЬНАЯ КРИТИКА ----
        self.neutral_criticism = [
            "не сработало", "неправильно",
            "ошибка", "недочёт", "проблема",
            "не соответствует", "не подходит",
            "нужно переделать", "нужно исправить",
            "неправильн", "неверн",
        ]
        
        # ---- ФОРМЫ ВЕЖЛИВОСТИ ----
        self.politeness_markers = [
            "пожалуйста", "будь добра", "будьте добры",
            "если можно", "если не трудно", "прошу",
            "извините", "простите", "сори",
        ]
        
        # ---- АГРЕССИВНЫЕ МАРКЕРЫ (усиливают оскорбление) ----
        self.aggressive_markers = [
            "вообще", "абсолютно", "совершенно",
            "как можно", "как ты можешь",
        ]
    
    def analyze(self, text: str, context: dict = None) -> CriticismAnalysis:
        """
        Анализирует текст с учётом контекста.
        context: {trust, irritation, respect, history_insults, history_criticism}
        """
        context = context or {}
        text_lower = text.lower()
        result = CriticismAnalysis()
        
        # ---- 1. БАЗОВЫЙ АНАЛИЗ ТЕКСТА ----
        insult_score, insult_words = self._check_person_insults(text_lower)
        intelligence_score, intelligence_phrases = self._check_intelligence_insults(text_lower)
        constructive_score, suggestion = self._check_constructive_with_suggestion(text_lower)
        neutral_score = self._check_neutral_criticism(text_lower)
        
        # ---- 2. ПРОВЕРКА НА АГРЕССИВНЫЕ МАРКЕРЫ ----
        aggression_boost = self._check_aggression(text_lower)
        
        # ---- 3. КОНТЕКСТНАЯ КОРРЕКЦИЯ ----
        trust = context.get("trust", 50)
        irritation = context.get("irritation", 10)
        history_insults = context.get("history_insults", 0)
        history_criticism = context.get("history_criticism", 0)
        
        # Если пользователь уже оскорблял много раз — порог снижается
        repeat_offender_penalty = min(0.3, history_insults * 0.05)
        
        # Если доверие низкое — любая критика воспринимается болезненнее
        trust_penalty = max(0.0, (50 - trust) * 0.005)
        
        context_adjustment = repeat_offender_penalty + trust_penalty
        
        # ---- 4. ОПРЕДЕЛЕНИЕ ТИПА ----
        
        # Личное оскорбление
        if insult_score > 0:
            result.is_insult = True
            result.is_criticism = False
            result.severity = min(1.0, insult_score + aggression_boost + context_adjustment)
            result.confidence = min(1.0, 0.5 + insult_score * 0.4 + context_adjustment * 0.3)
            result.target = "personality"
            result.reason = f"личное оскорбление: {', '.join(insult_words[:2])}"
            result.context_adjustment = context_adjustment
            
            # Проверка на смешанный случай
            if constructive_score > 0.3:
                result.is_criticism = True
                result.is_constructive = True
                result.target = "mixed"
                result.reason += " (смешанный: оскорбление + критика)"
                result.confidence = 0.75
            
            return result
        
        # Оскорбление интеллекта
        if intelligence_score > 0.3:
            result.is_insult = True
            result.is_criticism = False
            result.severity = min(1.0, intelligence_score * 0.8 + aggression_boost * 0.5 + context_adjustment)
            result.confidence = min(1.0, 0.4 + intelligence_score * 0.4 + context_adjustment * 0.2)
            result.target = "intelligence"
            result.reason = f"оскорбление интеллекта: {', '.join(intelligence_phrases[:2])}"
            result.context_adjustment = context_adjustment
            return result
        
        # Конструктивная критика
        if constructive_score > 0.4:
            result.is_criticism = True
            result.is_constructive = True
            result.is_feedback = True
            result.specificity = min(1.0, constructive_score)
            result.constructiveness = min(1.0, constructive_score + 0.2)
            result.severity = 0.3 + context_adjustment * 0.2
            result.confidence = min(1.0, 0.6 + constructive_score * 0.3)
            result.target = "action"
            result.suggested_improvement = suggestion
            result.reason = "конструктивная критика"
            result.context_adjustment = context_adjustment
            return result
        
        # Нейтральная критика
        if neutral_score > 0.3:
            result.is_criticism = True
            result.is_constructive = False
            result.is_feedback = True
            result.specificity = min(1.0, neutral_score)
            result.constructiveness = 0.2
            result.severity = 0.2 + context_adjustment * 0.15
            result.confidence = min(1.0, 0.5 + neutral_score * 0.3)
            result.target = "work"
            result.reason = "нейтральная критика"
            result.context_adjustment = context_adjustment
            return result
        
        # Простое указание на ошибку
        if "ошибк" in text_lower or "неправильн" in text_lower or "неверн" in text_lower:
            result.is_criticism = False
            result.is_feedback = True
            result.specificity = 0.4
            result.confidence = 0.5
            result.target = "work"
            result.reason = "указание на ошибку"
            result.context_adjustment = context_adjustment
            return result
        
        # ---- 5. НЕЙТРАЛЬНО ----
        result.confidence = 0.8
        result.reason = "нейтральное высказывание"
        result.context_adjustment = context_adjustment
        return result
    
    def _check_person_insults(self, text: str) -> Tuple[float, List[str]]:
        found = []
        for word in self.person_insults:
            if word in text:
                found.append(word)
        
        if not found:
            return 0.0, []
        
        score = min(1.0, len(found) * 0.25 + 0.1)
        return score, found
    
    def _check_intelligence_insults(self, text: str) -> Tuple[float, List[str]]:
        found = []
        for phrase in self.intelligence_insults:
            if phrase in text:
                found.append(phrase)
        
        if not found:
            return 0.0, []
        
        score = min(1.0, len(found) * 0.3 + 0.2)
        return score, found
    
    def _check_constructive_with_suggestion(self, text: str) -> Tuple[float, Optional[str]]:
        for pattern, suggestion in self.constructive_patterns:
            if re.search(pattern, text):
                return 0.7, suggestion
        
        if "попробуй" in text or "попробуйте" in text:
            return 0.5, "попробовать альтернативу"
        
        if "стоит" in text or "лучше" in text:
            return 0.4, "рассмотреть альтернативу"
        
        # "давай" — предложение совместного действия
        if "давай" in text or "давайте" in text:
            return 0.3, "совместное решение"
        
        return 0.0, None
    
    def _check_neutral_criticism(self, text: str) -> float:
        score = 0.0
        for phrase in self.neutral_criticism:
            if phrase in text:
                score += 0.2
        return min(1.0, score)
    
    def _check_aggression(self, text: str) -> float:
        """Проверяет агрессивные маркеры"""
        score = 0.0
        for marker in self.aggressive_markers:
            if marker in text:
                score += 0.15
        
        # Восклицательные знаки и капс
        if "!" in text:
            score += 0.1
        if text.isupper() and len(text) > 10:
            score += 0.2
        
        return min(0.5, score)
    
    def get_response_template(self, analysis: CriticismAnalysis, context: dict = None) -> dict:
        context = context or {}
        trust = context.get("trust", 50)
        irritation = context.get("irritation", 10)
        forgiveness = context.get("forgiveness", 50)
        
        if analysis.is_insult:
            if analysis.target == "mixed":
                return {
                    "strategy": "insult_with_criticism",
                    "tone": "firm",
                    "template": "Я слышу, что ты указываешь на проблему. Но тон мне неприятен. Давай без оскорблений."
                }
            else:
                if irritation > 60 and trust < 30:
                    return {
                        "strategy": "insult_repeat",
                        "tone": "cold",
                        "template": "Мне не нравится, когда меня оскорбляют. Я не буду продолжать этот разговор в таком тоне."
                    }
                elif forgiveness < 30:
                    return {
                        "strategy": "insult_not_forgiven",
                        "tone": "hurt",
                        "template": "Ты уже оскорблял меня раньше. Я не забыла. Если хочешь нормального разговора — извинись."
                    }
                else:
                    return {
                        "strategy": "insult_first",
                        "tone": "calm",
                        "template": "Мне неприятно слышать оскорбления. Я готова обсуждать идеи, но не в таком тоне."
                    }
        
        if analysis.is_constructive:
            if trust > 60:
                return {
                    "strategy": "constructive_criticism_trusted",
                    "tone": "warm",
                    "template": "Спасибо за конструктивную критику. Я ценю, что ты указываешь на ошибки — это помогает мне расти."
                }
            else:
                return {
                    "strategy": "constructive_criticism",
                    "tone": "neutral",
                    "template": "Поняла. Спасибо за уточнение, я перепроверю."
                }
        
        if analysis.is_criticism:
            return {
                "strategy": "neutral_criticism",
                "tone": "neutral",
                "template": "Я учту это замечание."
            }
        
        if analysis.is_feedback:
            return {
                "strategy": "feedback",
                "tone": "open",
                "template": "Спасибо за обратную связь."
            }
        
        return {
            "strategy": "neutral",
            "tone": "neutral",
            "template": "Я готова продолжить разговор."
        }


def get_criticism_detector() -> CriticismDetector:
    return CriticismDetector()


if __name__ == "__main__":
    detector = get_criticism_detector()
    
    test_phrases = [
        "ты глупая",
        "ты ничего не понимаешь",
        "ты ошиблась в расчётах, попробуй перепроверить",
        "этот подход не работает, давай попробуем другой",
        "ты не учла важный фактор",
        "как у тебя вообще язык поворачивается такое говорить",
        "ты дура, но перепроверь расчёты",
    ]
    
    # Контекст: пользователь уже 3 раза оскорблял, доверие 20
    context = {
        "trust": 20,
        "irritation": 40,
        "history_insults": 3,
        "history_criticism": 1,
    }
    
    print("=== Тест Criticism Detector С КОНТЕКСТОМ ===\n")
    for phrase in test_phrases:
        result = detector.analyze(phrase, context)
        print(f"Фраза: {phrase}")
        print(f"  is_insult: {result.is_insult}")
        print(f"  is_criticism: {result.is_criticism}")
        print(f"  is_constructive: {result.is_constructive}")
        print(f"  target: {result.target}")
        print(f"  severity: {result.severity:.2f}")
        print(f"  context_adjustment: {result.context_adjustment:.2f}")
        print(f"  reason: {result.reason}")
        print()
