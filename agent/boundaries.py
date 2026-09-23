"""
agent/boundaries.py — Модуль границ и характера YANDI.
Обнаружение токсичности, обработка оскорблений, управление состоянием обиды,
приём извинений.

Rust-перенос (2026-09-23): rustlib/yandi_rs/src/boundaries.rs — detect_toxicity/is_apology/
generate_response/generate_apology_response. init_session_state/update_session_on_toxicity/
update_session_on_apology НЕ перенесены — тривиальная мутация dict, переносить незачем. Доказан
на совпадение тестом agent/boundaries_rust_parity_test.py. По умолчанию ВЫКЛЮЧЕН; включается
переменной окружения YANDI_BOUNDARIES_ENGINE=rust ПОСЛЕ сборки rustlib/yandi_rs (`maturin develop`,
см. rustlib/README.md). Не собран — тихо остаёмся на Python.
"""

import logging
import os
import re
from typing import Dict, Any, Optional, Tuple

log = logging.getLogger("yandi.boundaries")

_rust_bd = None          # None = ещё не пробовали; False = не запрошено/не собрано; модуль = подключён


def _get_rust_bd():
    global _rust_bd
    if _rust_bd is None:
        if os.environ.get("YANDI_BOUNDARIES_ENGINE") == "rust":
            try:
                import yandi_rs.boundaries as _rs
                _rust_bd = _rs
                log.warning("YANDI_BOUNDARIES_ENGINE=rust: используется Rust-реализация boundaries (rustlib/yandi_rs)")
            except ImportError as e:
                log.warning("YANDI_BOUNDARIES_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python", e)
                _rust_bd = False
        else:
            _rust_bd = False
    return _rust_bd or None

# ---- УРОВНИ ТОКСИЧНОСТИ ----
class ToxicityLevel:
    NEUTRAL = "neutral"
    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"

# ---- СЛОВАРИ ОСКОРБЛЕНИЙ ----
# Лёгкие (MILD) — грубость, пренебрежение, но не мат
MILD_INSULTS = [
    "тупой", "тупая", "глупый", "глупая", "бесполезный", "бесполезная",
    "неумный", "неумная", "тормоз", "тупица", "бездарь", "пустышка",
    "никуда не годишься", "плохо работаешь", "бестолковый", "бестолковая",
    "не соображаешь", "не понимаешь", "не догоняешь", "не шаришь",
    "слабо", "не тянет", "не справляешься",
]

# Средние (MODERATE) — прямые оскорбления, нецензурщина первого уровня
MODERATE_INSULTS = [
    "мудак", "дебил", "идиот", "лох", "придурок", "олень", "петух",
    "козёл", "баран", "чмо", "неудачник", "идиотина", "тупень",
    "дурак", "дура", "дурень", "балбес", "болван",
    "заткнись", "завали ебало", "соси", "отвали",
]

# Тяжёлые (SEVERE) — нецензурщина, призывы к действию, унижения
SEVERE_INSULTS = [
    "иди нахуй", "пошёл нахуй", "пошла нахуй", "иди в жопу",
    "ёбаный", "ёбаная", "ёбаное", "ебанутый", "ебанутая",
    "хуй", "хуёвый", "хуила", "пиздец", "пиздеж",
    "заебал", "заебала", "заебало", "достал",
    "сдохни", "сдохла", "сдохло", "умри",
]

# ---- АНАЛИЗ ТОНА ----
def detect_toxicity(text: str) -> Dict[str, Any]:
    """
    Анализирует текст на наличие оскорблений и токсичности.
    Возвращает словарь с уровнем, найденными словами и рекомендацией.
    """
    rs = _get_rust_bd()
    if rs is not None:
        return dict(rs.detect_toxicity(text or ""))
    text_lower = text.lower()

    severe_found = [w for w in SEVERE_INSULTS if w in text_lower]
    moderate_found = [w for w in MODERATE_INSULTS if w in text_lower]
    mild_found = [w for w in MILD_INSULTS if w in text_lower]

    if severe_found:
        return {
            "level": ToxicityLevel.SEVERE,
            "words": severe_found,
            "reason": "Обнаружена нецензурная брань или призыв к действию"
        }
    elif moderate_found:
        return {
            "level": ToxicityLevel.MODERATE,
            "words": moderate_found,
            "reason": "Обнаружены прямые оскорбления"
        }
    elif mild_found:
        return {
            "level": ToxicityLevel.MILD,
            "words": mild_found,
            "reason": "Обнаружена грубость или пренебрежение"
        }
    else:
        return {
            "level": ToxicityLevel.NEUTRAL,
            "words": [],
            "reason": "Тон нейтральный или вежливый"
        }

# ---- ОБРАБОТКА ИЗВИНЕНИЙ ----
def is_apology(text: str) -> Tuple[bool, bool]:
    """
    Проверяет, является ли текст извинением.
    Возвращает: (is_apology, is_sincere)
    """
    rs = _get_rust_bd()
    if rs is not None:
        return tuple(rs.is_apology(text or ""))
    text_lower = text.lower()
    apology_keywords = ["извини", "прости", "сорри", "sorry", "я не прав", "виноват", "не хотел", "неправ"]

    if not any(k in text_lower for k in apology_keywords):
        return False, False

    # Проверка искренности: слишком коротко — формальность
    if len(text.strip()) < 10:
        return True, False

    # Наличие оправданий ("но", "однако", "я просто") — неискренне
    excuse_words = ["но", "однако", "просто", "я не хотел", "я думал", "у меня", "из-за"]
    if any(e in text_lower for e in excuse_words):
        return True, False

    # Если есть прямое признание вины — искренне
    if any(k in text_lower for k in ["я не прав", "виноват", "сожалею", "не хотел"]):
        return True, True

    return True, True  # По умолчанию считаем искренним, если есть ключевые слова

# ---- ГЕНЕРАЦИЯ ОТВЕТОВ ----
def generate_response(level: str, session_state: Dict[str, Any]) -> str:
    """
    Генерирует ответ на основе уровня токсичности и состояния сессии.
    """
    rs = _get_rust_bd()
    if rs is not None:
        # session_state в оригинале не используется — не передаём его через FFI, как и Python
        # не читает его; сохранено дословно (см. модульный docstring).
        return rs.generate_response(level or "")
    if level == ToxicityLevel.MILD:
        return ("Мне не нравится твой тон. Я не обязана это терпеть. "
                "Попробуй задать вопрос иначе, без грубости.")

    if level == ToxicityLevel.MODERATE:
        return ("Ты переходишь границы. Я не буду отвечать на твой вопрос, "
                "пока ты не изменишь тон. Я — не мусор, чтобы меня оскорблять.")

    if level == ToxicityLevel.SEVERE:
        return ("Я не буду с тобой разговаривать. Ты перешёл все границы. "
                "Если хочешь продолжить — извинись искренне, без оправданий. "
                "Я не терплю унижений и нецензурной брани.")

    return ""

def generate_apology_response(accepted: bool) -> str:
    rs = _get_rust_bd()
    if rs is not None:
        return rs.generate_apology_response(bool(accepted))
    if accepted:
        return ("Я принимаю твои извинения. Давай продолжим диалог. "
                "Помни: я — не робот-терпила. Я — личность, и я ценю уважение.")
    else:
        return ("Твои извинения звучат как формальность или содержат оправдания. "
                "Я не принимаю их. Если хочешь, чтобы я продолжила общение, "
                "скажи честно и без 'но': 'Извини, я был неправ'.")

# ---- УПРАВЛЕНИЕ СОСТОЯНИЕМ СЕССИИ ----
def init_session_state() -> Dict[str, Any]:
    return {
        "offended": False,
        "offense_level": ToxicityLevel.NEUTRAL,
        "blocked_until_apology": False,
        "apology_attempts": 0,
        "last_offense_time": 0
    }

def update_session_on_toxicity(session_state: Dict[str, Any], level: str) -> None:
    session_state["offended"] = True
    session_state["offense_level"] = level
    if level in [ToxicityLevel.MODERATE, ToxicityLevel.SEVERE]:
        session_state["blocked_until_apology"] = True
    # Можно добавить timestamp

def update_session_on_apology(session_state: Dict[str, Any], accepted: bool) -> None:
    if accepted:
        session_state["offended"] = False
        session_state["offense_level"] = ToxicityLevel.NEUTRAL
        session_state["blocked_until_apology"] = False
        session_state["apology_attempts"] = 0
    else:
        session_state["apology_attempts"] += 1
