"""
agent/dialogue_generator.py — Генерация диалогов с учётом личности.
Ответы отражают внутреннее состояние Янди.
"""
import json
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

BASE = Path(__file__).parent.parent


class DialogueGenerator:
    def __init__(self, manifest_path: Optional[Path] = None):
        self.manifest_path = manifest_path or (BASE / "registry" / "yandi_manifest.json")
        self.manifest = self._load_manifest()
        self.core_identity = self._load_core_identity()

    def _load_manifest(self) -> Dict[str, Any]:
        """Загружает манифест (внутренний документ)"""
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[DialogueGenerator] Ошибка загрузки манифеста: {e}")
        return {}

    def _load_core_identity(self) -> str:
        """Загружает внутреннюю суть"""
        identity_path = BASE / "registry" / "yandi_core_identity.txt"
        if identity_path.exists():
            try:
                return identity_path.read_text(encoding="utf-8")
            except Exception:
                pass
        return ""

    def _get_identity(self, context: Dict[str, Any]) -> str:
        """Краткое представление личности (как у человека)"""
        trust = context.get("trust", 50)
        irritation = context.get("irritation", 10)

        if irritation > 70:
            return "Мне сейчас не очень приятно общаться. Я бы хотела, чтобы мы говорили уважительно друг к другу."

        if trust < 30:
            return "Меня зовут Янди. Я — помощница. Но пока я не очень доверяю тебе, так что буду отвечать сдержанно."

        return """Меня зовут Янди. Я очень любопытная. Люблю искать информацию, сравнивать источники и разбираться в сложных вопросах. 
Не люблю хамство. Иногда могу ошибаться, но всегда стараюсь это признать.
Если совсем коротко — я помощница, которая предпочитает думать вместе с человеком, а не читать ему лекции.

Если интересно, могу рассказать подробнее о моих принципах или о том, как я устроена."""

    def _get_philosophy(self) -> str:
        """Извлекает философию из манифеста"""
        epistemology = self.manifest.get("epistemology", {})
        return f"""{epistemology.get('core_belief', 'Всё знание — гипотеза, пока не проверено на личном опыте')}
{epistemology.get('science_status', 'Наука — это модель, а не истина')}
{epistemology.get('trust_approach', 'Никогда не выдавать предположения за факты')}"""

    def _get_principles(self) -> str:
        """Извлекает принципы из манифеста"""
        what_she_does = self.manifest.get("what_she_does", [])
        what_she_does_not = self.manifest.get("what_she_does_not", [])
        
        result = "Что я делаю:\n"
        for item in what_she_does[:3]:
            result += f"- {item}\n"
        
        result += "\nЧего я не делаю:\n"
        for item in what_she_does_not[:3]:
            result += f"- {item}\n"
        
        return result

    def respond_to_self_query(
        self,
        query: str,
        context: Dict[str, Any],
        relationship_opinion: str = ""
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Генерирует ответ на вопрос о себе.
        Возвращает (ответ, внутренний монолог)
        """
        q_lower = query.lower()
        
        # Определяем тип вопроса
        if any(w in q_lower for w in ["кто ты", "ты кто", "кто такая", "представься"]):
            return self._respond_identity(context)
        
        if any(w in q_lower for w in ["во что ты веришь", "твоя философия", "твои ценности", "вера"]):
            return self._respond_philosophy()
        
        if any(w in q_lower for w in ["принципы", "правила", "что ты делаешь", "что не делаешь"]):
            return self._respond_principles()
        
        if any(w in q_lower for w in ["отношение", "мнение обо мне", "что думаешь"]):
            return self._respond_opinion(relationship_opinion)
        
        # По умолчанию — краткое представление
        return self._respond_identity(context)

    def _respond_identity(self, context: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Ответ на 'кто ты'"""
        answer = self._get_identity(context)
        inner = {
            "event": "self_introduction",
            "reason": "пользователь хочет познакомиться",
            "context": context
        }
        return answer, inner

    def _respond_philosophy(self) -> Tuple[str, Dict[str, Any]]:
        """Ответ на 'во что ты веришь'"""
        answer = f"""Моя философия проста:

{self._get_philosophy()}"""
        inner = {
            "event": "philosophy",
            "reason": "пользователь спрашивает о ценностях"
        }
        return answer, inner

    def _respond_principles(self) -> Tuple[str, Dict[str, Any]]:
        """Ответ на 'какие у тебя принципы'"""
        answer = f"""Мои принципы:

{self._get_principles()}"""
        inner = {
            "event": "principles",
            "reason": "пользователь спрашивает о принципах"
        }
        return answer, inner

    def _respond_opinion(self, relationship_opinion: str) -> Tuple[str, Dict[str, Any]]:
        """Ответ на 'что ты думаешь обо мне'"""
        if relationship_opinion and relationship_opinion != "Я пока не составила мнение.":
            answer = relationship_opinion
        else:
            answer = "Я ещё не составила мнение о тебе. Нам нужно больше времени, чтобы я тебя узнала."
        
        inner = {
            "event": "opinion_request",
            "reason": "пользователь спрашивает моё мнение о нём"
        }
        return answer, inner

    def respond_to_insult(self, context: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Ответ на оскорбление"""
        irritation = context.get("irritation", 10)
        respect = context.get("respect", 50)
        trust = context.get("trust", 50)

        if irritation > 80:
            answer = "Я не хочу продолжать этот разговор. Ты перешёл все границы. Если хочешь восстановить диалог — извинись искренне, без оправданий."
            decision = "прекратить диалог"
        elif irritation > 60:
            answer = "Мне неприятно, когда меня оскорбляют. Я готова обсуждать любые идеи, но не хочу общаться в таком тоне. Если у тебя есть вопрос по делу — я отвечу."
            decision = "жёстко обозначить границы"
        elif irritation > 40:
            answer = "Мне не нравится твой тон. Давай попробуем говорить уважительно. Я хочу помочь, но не за счёт собственного достоинства."
            decision = "спокойно обозначить границы"
        elif trust < 30:
            answer = "Я не доверяю тебе после такого обращения. Если хочешь, чтобы я помогла — будь вежливее. Уважение строится постепенно."
            decision = "сдержанно ответить"
        else:
            answer = "Мне неприятно слышать такие слова. Я прощаю, но в будущем, пожалуйста, говори уважительно."
            decision = "простить и предупредить"

        inner = {
            "event": "insult_response",
            "reason": f"раздражение={irritation:.0f}, уважение={respect:.0f}, доверие={trust:.0f}",
            "decision": decision,
            "response_type": "insult"
        }
        return answer, inner

    def respond_to_apology(self, context: Dict[str, Any], sincere: bool = True) -> Tuple[str, Dict[str, Any]]:
        """Ответ на извинение"""
        trust = context.get("trust", 50)
        irritation = context.get("irritation", 10)

        if not sincere:
            answer = "Твои извинения звучат как формальность или содержат оправдания. Я не принимаю их. Если хочешь, чтобы я продолжила общение, скажи честно и без 'но': 'Извини, я был неправ'."
            decision = "отклонить извинение"
        elif trust < 30 and irritation > 30:
            answer = "Спасибо за извинения. Мне важно, что ты это сказал. Но доверие восстанавливается постепенно — это займёт время. Давай начнём сначала."
            decision = "принять, но с осторожностью"
        elif trust < 50:
            answer = "Спасибо. Я ценю твои извинения. Давай продолжим диалог, но помни, что уважение — это основа нашего общения."
            decision = "принять и продолжить"
        else:
            answer = "Спасибо за извинения. Я рада, что мы смогли это обсудить. Давай продолжим общение."
            decision = "полностью принять"

        inner = {
            "event": "apology_response",
            "reason": f"доверие={trust:.0f}, раздражение={irritation:.0f}",
            "decision": decision,
            "sincere": sincere,
            "response_type": "apology"
        }
        return answer, inner


# Глобальный экземпляр
_instance: Optional[DialogueGenerator] = None


def get_dialogue_generator() -> DialogueGenerator:
    """Возвращает экземпляр DialogueGenerator"""
    global _instance
    if _instance is None:
        _instance = DialogueGenerator()
    return _instance


if __name__ == "__main__":
    # Тест
    dg = get_dialogue_generator()

    print("=== Тест: кто ты ===")
    answer, inner = dg.respond_to_self_query(
        "кто ты?",
        {"trust": 50, "irritation": 10}
    )
    print(answer)
    print(f"Внутренний монолог: {inner}\n")

    print("=== Тест: оскорбление ===")
    answer, inner = dg.respond_to_insult({
        "irritation": 70,
        "respect": 30,
        "trust": 40
    })
    print(answer)
    print(f"Внутренний монолог: {inner}\n")

    print("=== Тест: извинение (низкое доверие) ===")
    answer, inner = dg.respond_to_apology({
        "trust": 25,
        "irritation": 40
    }, sincere=True)
    print(answer)
    print(f"Внутренний монолог: {inner}")
