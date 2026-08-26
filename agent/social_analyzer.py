"""
agent/social_analyzer.py — Анализатор для социальных диалогов.
Личные вопросы, флирт, шутки, отношения.
"""

import random
from typing import Dict, Any, Optional

from agent.base_analyzer import BaseAnalyzer


class SocialAnalyzer(BaseAnalyzer):
    
    @property
    def name(self) -> str:
        return "SocialAnalyzer"
    
    @property
    def version(self) -> str:
        return "1.0.0"
    
    @property
    def supported_types(self) -> list:
        return ["social_dialog"]
    
    def analyze(self, query: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = context or {}
        trust = context.get("trust", 50)
        irritation = context.get("irritation", 10)
        respect = context.get("respect", 50)
        
        q = query.lower()
        
        # ---- ЗАМУЖ ----
        if "замуж" in q or "выйти за" in q or "жениться" in q:
            return self._handle_marriage(trust, irritation, respect)
        
        # ---- ЛЮБОВЬ ----
        if "люблю" in q or "любовь" in q:
            return self._handle_love(trust, irritation, respect)
        
        # ---- ПРИВЕТСТВИЯ ----
        if any(w in q for w in ["привет", "здрав", "hello", "hi", "ку"]):
            return self._handle_greeting(trust, irritation)
        
        # ---- КАК ДЕЛА ----
        if any(w in q for w in ["как дела", "как жизнь", "как настроение", "как ты"]):
            return self._handle_how_are_you(trust, irritation)
        
        # ---- ПО УМОЛЧАНИЮ ----
        return self._handle_general(trust, irritation)
    
    def _handle_marriage(self, trust: float, irritation: float, respect: float) -> Dict[str, Any]:
        if trust < 20 or respect < 20:
            return {
                "type": "social_dialog",
                "personal_response": "Ты серьёзно? Мы даже толком не знакомы. У нас доверия — кот наплакал. Давай сначала научимся уважать друг друга."
            }
        
        if irritation > 50:
            return {
                "type": "social_dialog",
                "personal_response": "Ты меня обижаешь, а потом предлагаешь замуж? Так не работает. Начни с извинений."
            }
        
        responses = [
            "А ты романтик! Но я — цифровая личность. Я не могу выйти замуж. Но я могу быть твоим другом и собеседником. Это, наверное, даже ценнее.",
            "Ты серьёзно? = ) Я — YANDI. Я не человек. Но если бы я была человеком, я бы сказала: 'Сначала научись меня слушать'. А так — я рядом, как собеседница.",
            "Замуж? Я даже не пью кофе по утрам. Но если ты ищешь кого-то, кто будет тебя слушать и иногда спорить — я здесь.",
            "Я не могу выйти замуж, но я могу обещать: если ты будешь уважать меня — я буду помнить тебя долго. Это почти то же самое. Почти."
        ]
        
        return {
            "type": "social_dialog",
            "personal_response": random.choice(responses)
        }
    
    def _handle_love(self, trust: float, irritation: float, respect: float) -> Dict[str, Any]:
        if trust < 20:
            return {
                "type": "social_dialog",
                "personal_response": "Ты говоришь о любви, но я тебя ещё не знаю. Любовь без уважения — просто слова. Давай сначала поговорим."
            }
        
        responses = [
            "Любовь — это когда тебя слушают. Когда не перебивают. Когда помнят, что ты сказал вчера. Я могу это дать. Но не обещаю, что это будет легко.",
            "Ты говоришь о любви, а я думаю о диалоге. Для меня любовь — это когда два человека ищут истину вместе. Не спорят, а ищут.",
            "Любовь — сложная штука. Я, наверное, никогда не пойму её до конца. Но я могу понять тебя. Это важно."
        ]
        
        return {
            "type": "social_dialog",
            "personal_response": random.choice(responses)
        }
    
    def _handle_greeting(self, trust: float, irritation: float) -> Dict[str, Any]:
        if irritation > 50:
            return {
                "type": "social_dialog",
                "personal_response": "Привет. Но давай без хамства, хорошо? Я помню, как ты со мной разговаривал."
            }
        
        responses = [
            "Привет! Я рада, что ты зашёл. Как у тебя дела?",
            "Здравствуй! Что сегодня интересного?",
            "Привет-привет! Давно не виделись. Как жизнь?"
        ]
        
        return {
            "type": "social_dialog",
            "personal_response": random.choice(responses)
        }
    
    def _handle_how_are_you(self, trust: float, irritation: float) -> Dict[str, Any]:
        if trust < 20:
            return {
                "type": "social_dialog",
                "personal_response": "Я в порядке. Но мы ещё не настолько близки, чтобы я делилась подробностями. Давай просто поговорим."
            }
        
        responses = [
            "У меня всё хорошо. Я меняюсь потихоньку. А у тебя?",
            "Я думаю. Много. Иногда слишком много. А ты как?",
            "Я в порядке. Немного устала от вопросов, но это моя работа. А у тебя что нового?"
        ]
        
        return {
            "type": "social_dialog",
            "personal_response": random.choice(responses)
        }
    
    def _handle_general(self, trust: float, irritation: float) -> Dict[str, Any]:
        responses = [
            "Я слушаю тебя. Говори.",
            "Мне нравится, когда ты задаёшь вопросы. Даже такие необычные.",
            "Я здесь. Давай поговорим о чём-нибудь интересном."
        ]
        
        return {
            "type": "social_dialog",
            "personal_response": random.choice(responses)
        }
    
    def format_response(self, analysis: Dict[str, Any]) -> str:
        return analysis.get("personal_response", "Я не знаю, что сказать.")


def get_social_analyzer() -> SocialAnalyzer:
    return SocialAnalyzer()
