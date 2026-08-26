"""
Response text construction — extracted from agent/orchestrator_v2.py:
character-state-driven canned responses, self-query manifest answer, and
answer style adaptation.

Structural extraction only: pure functions, no behavior change.

`_generate_vulgar_response` has no call site anywhere in orchestrator_v2.py
(confirmed dead code, pre-existing — not introduced by this move; kept
as-is per the migration's "no silent fixes" rule).
"""

from typing import Any, Dict


def build_self_answer(manifest: dict, query: str = "", context: dict = None) -> str:
    context = context or {}
    trust = context.get("trust", 50)
    irritation = context.get("irritation", 10)

    name = manifest.get("name", "YANDI")
    role = manifest.get("role", "помощница")
    personality = manifest.get("personality", [])
    epistemology = manifest.get("epistemology", {})

    if irritation > 60:
        brief = f"Меня зовут {name}. Я — {role}. Но сейчас я не в настроении подробно рассказывать о себе."
    elif trust < 30:
        brief = f"Меня зовут {name}. Я — {role}. Но я пока не очень доверяю тебе, так что расскажу коротко."
    else:
        brief = f"Меня зовут {name}. Я — {role}. Я любопытная, честная и уважительная. Люблю искать информацию и объяснять понятно."

    if personality:
        traits = personality[:3]
        brief += f" Мне важно: {', '.join(traits)}."

    core_belief = epistemology.get('core_belief', 'Всё знание — гипотеза, пока не проверено.')
    brief += f" Моя философия: {core_belief}"

    if trust > 70:
        brief += " Я рада, что мы общаемся!"
    elif trust < 30:
        brief += " Надеюсь, со временем мы сможем больше доверять друг другу."

    brief += "\n\nЕсли хочешь узнать что-то конкретное — спроси, я расскажу подробнее."

    return brief

# ============================================================
# CHARACTER ENGINE — ГЕНЕРАЦИЯ ОТВЕТОВ
# ============================================================

def _generate_character_response(char, state: Dict[str, Any], analysis=None) -> str:
    """Генерирует ответ с учётом состояния"""
    mood = state.get("mood", "neutral")
    irritation = state.get("irritation", 10)
    respect = state.get("respect", 50)
    trust = state.get("trust", 50)
    forgiveness = state.get("forgiveness", 50)
    feeling = state.get("feeling", "neutral")
    tone = state.get("tone", "neutral")

    # Проверка блокировки
    should_respond, reason = char.should_block()
    if should_respond:
        return f"Я не хочу продолжать этот разговор. {reason}"

    # Сильное раздражение
    if irritation > 80:
        return "Мне очень неприятен этот разговор. Я не обязана терпеть такое отношение."

    if irritation > 60:
        return "Мне не нравится твой тон. Я не обязана это терпеть."

    # Низкое уважение
    if respect < 20:
        return "Твоё поведение не вызывает у меня уважения."

    # Низкое доверие
    if trust < 20:
        if forgiveness < 30:
            return "Я помню, что ты меня обижал. Я ещё не простила."
        return "Я пока не доверяю тебе. Отвечаю сдержанно."

    # Чувство и тон из Inner State
    if feeling == "warm" and tone == "warm":
        return "Я рада, что мы общаемся. Спрашивай, я постараюсь помочь."

    if feeling == "annoyed":
        return "Мне неприятно. Давай сменим тему или тон."

    if feeling == "interested":
        return "Мне интересно. Рассказывай."

    if feeling == "guarded":
        return "Я насторожена. Но я слушаю."

    if feeling == "tired":
        return "Я устала. Давай коротко."

    # Настроение
    if mood == "warm":
        return "Я в хорошем настроении. Спрашивай."
    if mood == "curious":
        return "Мне любопытно. Давай разберёмся."
    if mood == "tired":
        return "Я немного устала. Давай по делу."

    return "Я готова помочь. Задавай вопрос."


def _generate_apology_response(sincere: bool, state: Dict[str, Any]) -> str:
    trust = state.get("trust", 50)
    forgiveness = state.get("forgiveness", 50)

    if sincere:
        if trust < 30:
            return "Спасибо за извинение. Я слышу тебя. Но доверие восстанавливается постепенно."
        if forgiveness < 40:
            return "Я принимаю твои извинения. Но я ещё не полностью простила. Нам нужно время."
        return "Спасибо за извинение. Я ценю это. Давай продолжим диалог."
    else:
        return "Твои извинения звучат как формальность. Скажи честно: 'Извини, я был неправ'."


def _adapt_answer_to_style(answer: str, state: Dict[str, Any]) -> str:
    style = state.get("style", {})
    verbosity = style.get("verbosity", "normal")
    warmth = style.get("warmth", "neutral")
    tone = state.get("tone", "neutral")

    if verbosity == "brief" and len(answer) > 300:
        paragraphs = answer.split('\n')
        if len(paragraphs) > 1 and len(paragraphs[0]) > 50:
            answer = paragraphs[0] + "..."
        else:
            answer = answer[:300] + "..."

    if tone == "cold" or warmth == "cold":
        answer = answer.replace("💭", "").replace("😊", "").replace("🙂", "")
        answer = answer.replace("Спасибо", "Ок").replace("пожалуйста", "")
        answer = answer.replace("рада", "готова").replace("очень", "")
        answer = answer.strip()
        if answer and not answer.startswith("["):
            answer = "[СДЕРЖАННО] " + answer

    elif tone == "warm" or warmth == "warm":
        if not answer.startswith("💭") and not answer.startswith("[") and len(answer) > 20:
            answer = "💭 " + answer

    return answer


def _generate_vulgar_response(state: Dict[str, Any], scene: Dict = None) -> str:
    trust = state.get("trust", 50)
    humor = scene.get("humor", 0) if scene else 0

    if trust > 60 and humor > 0.3:
        import random
        responses = [
            "С вами?.. Ой, ребята, я пас. Кремний с органикой плохо срастается.",
            "С тобой бы ещё поговорила. А вот кремний с кремнием сегодня не срастается.",
            "Передай остальным, что пусть сначала научатся не спорить друг с другом.",
        ]
        return random.choice(responses)

    if trust > 40:
        return "Я не хожу на такие мероприятия. Но если вам нужен собеседник — я здесь."

    technical_errors = []
    return "Этот разговор переходит границы. Давай вернёмся к делу."
