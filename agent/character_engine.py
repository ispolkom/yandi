"""
agent/character_engine.py — Полноценная модель личности YANDI.
Использует Inner State вместо разрозненных переменных.
"""

import json
import time
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

from agent.criticism_detector import get_criticism_detector, CriticismAnalysis
from agent.inner_state import get_inner_state
from agent.boundaries import detect_toxicity, ToxicityLevel, is_apology

BASE = Path(__file__).parent.parent


class CharacterEngine:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.inner = get_inner_state(user_id)
        self.critic = get_criticism_detector()
        
    def get_context(self) -> Dict[str, Any]:
        """Возвращает контекст для orchestrator"""
        ctx = self.inner.get_response_context()
        return {
            "irritation": 100 - ctx.get("patience", 50),
            "trust": ctx.get("trust", 50),
            "respect": ctx.get("respect", 50),
            "forgiveness": ctx.get("forgiveness", 50),
            "mood": ctx.get("mood", "neutral"),
            "style": self.get_response_style(),
            "total_insults": len([e for e in self.inner.state.relationship.history if "insult" in e.event_type]),
            "total_apologies": len([e for e in self.inner.state.relationship.history if "apology" in e.event_type]),
            "total_help": len([e for e in self.inner.state.relationship.history if e.event_type == "help"]),
            "total_conversations": len(self.inner.state.relationship.history),
            "events_count": len(self.inner.state.relationship.history),
            "pattern": ctx.get("pattern", "unknown"),
            "feeling": ctx.get("feeling", "neutral"),
            "intent": ctx.get("intent", "listen"),
            "tone": ctx.get("tone", "neutral"),
        }
    
    def get_response_style(self) -> Dict[str, Any]:
        """Возвращает стиль ответа на основе состояния"""
        ctx = self.inner.get_response_context()
        
        style = {
            "mood": ctx.get("mood", "neutral"),
            "verbosity": "normal",
            "warmth": "neutral",
            "curiosity": "normal",
            "patience": "normal",
            "will_help": True,
            "will_explain": True,
            "will_ask_back": False,
        }
        
        trust = ctx.get("trust", 50)
        respect = ctx.get("respect", 50)
        energy = ctx.get("energy", 70)
        patience = ctx.get("patience", 50)
        mood = ctx.get("mood", "neutral")
        tone = ctx.get("tone", "neutral")
        
        # Тон из Inner State
        if tone == "warm":
            style["warmth"] = "warm"
        elif tone == "cold":
            style["warmth"] = "cold"
            style["verbosity"] = "brief"
        elif tone == "firm":
            style["warmth"] = "cold"
            style["verbosity"] = "brief"
        elif tone == "thoughtful":
            style["verbosity"] = "detailed"
            style["curiosity"] = "high"
        
        # Настроение
        if mood == "tired" or energy < 40:
            style["verbosity"] = "brief"
            style["patience"] = "low"
        
        if mood == "curious":
            style["curiosity"] = "high"
            style["will_ask_back"] = True
        
        # Доверие
        if trust > 70:
            style["warmth"] = "warm"
            style["will_help"] = True
        elif trust < 30:
            style["warmth"] = "cold"
            style["will_help"] = False
        
        # Уважение
        if respect > 70:
            style["verbosity"] = "detailed"
        elif respect < 30:
            style["verbosity"] = "brief"
        
        return style
    
    def get_mood(self) -> str:
        """Возвращает текущее настроение"""
        return self.inner.get_response_context().get("mood", "neutral")
    
    def should_block(self) -> Tuple[bool, str]:
        """Определяет, нужно ли блокировать диалог"""
        ctx = self.inner.get_response_context()
        trust = ctx.get("trust", 50)
        respect = ctx.get("respect", 50)
        patience = ctx.get("patience", 50)
        energy = ctx.get("energy", 70)
        
        # Если доверие и уважение критически низкие
        if trust < 10 and respect < 10:
            return True, f"trust={trust:.1f}, respect={respect:.1f}"
        
        # Если терпение закончилось
        if patience < 10:
            return True, f"patience={patience:.1f}"
        
        # Если энергия на нуле
        if energy < 20:
            return True, f"energy={energy:.1f}"
        
        return False, ""
    
    # ---- ОБРАБОТКА СОБЫТИЙ ----
    
    def process_insult(self, text: str, level: str, words: List[str]) -> Dict[str, Any]:
        """Обрабатывает оскорбление"""
        event_type = f"{level}_insult" if level in ["mild", "moderate", "severe"] else "moderate_insult"
        
        self.inner.add_event(
            event_type=event_type,
            description=text[:100],
            sincerity=0.1,
        )
        
        return self.get_context()
    
    def process_apology(self, text: str, sincere: bool) -> Dict[str, Any]:
        """Обрабатывает извинение"""
        event_type = "sincere_apology" if sincere else "formal_apology"
        
        self.inner.add_event(
            event_type=event_type,
            description=text[:100],
            sincerity=0.9 if sincere else 0.3,
        )
        
        return self.get_context()
    
    def process_thanks(self, text: str = "") -> Dict[str, Any]:
        """Обрабатывает благодарность"""
        self.inner.add_event(
            event_type="thanks",
            description=text[:100] if text else "благодарность",
            sincerity=0.8,
        )
        
        return self.get_context()
    
    def process_help(self, text: str = "") -> Dict[str, Any]:
        """Обрабатывает помощь"""
        self.inner.add_event(
            event_type="help",
            description=text[:100] if text else "помощь",
            sincerity=0.7,
        )
        
        return self.get_context()
    
    def process_normal(self, text: str = "") -> Dict[str, Any]:
        """Обрабатывает нормальный диалог"""
        # Не добавляем событие, чтобы не засорять историю
        return self.get_context()
    
    def get_inner_monologue(self) -> str:
        """Возвращает внутренний монолог"""
        return self.inner.get_inner_monologue()


_state_cache: Dict[str, CharacterEngine] = {}

def get_character(user_id: str) -> CharacterEngine:
    if user_id not in _state_cache:
        _state_cache[user_id] = CharacterEngine(user_id)
    return _state_cache[user_id]
