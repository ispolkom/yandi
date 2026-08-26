"""
agent/scene_builder.py — Social Scene Builder.
Строит карту социальной сцены.
"""

import re
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class SocialScene:
    speaker: str = "user"
    listener: str = "unknown"
    target: str = "unknown"
    subject: str = "unknown"
    recipient: str = "unknown"
    participants: List[str] = field(default_factory=list)
    mentioned: List[str] = field(default_factory=list)
    coalition: List[str] = field(default_factory=list)
    
    mode: str = "statement"
    speech_act: str = "unknown"
    
    topic: str = "general"
    humor: float = 0.0
    conflict: float = 0.0
    intimacy: float = 0.0
    pressure: float = 0.0
    boundary_crossed: bool = False
    
    is_self_addressed: bool = False
    is_about_self: bool = False
    is_about_user: bool = False
    is_group_addressed: bool = False
    is_about_other: bool = False
    
    confidence: float = 0.0
    reason: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "speaker": self.speaker,
            "listener": self.listener,
            "target": self.target,
            "subject": self.subject,
            "recipient": self.recipient,
            "participants": self.participants,
            "mentioned": self.mentioned,
            "coalition": self.coalition,
            "mode": self.mode,
            "speech_act": self.speech_act,
            "topic": self.topic,
            "humor": round(self.humor, 2),
            "conflict": round(self.conflict, 2),
            "intimacy": round(self.intimacy, 2),
            "pressure": round(self.pressure, 2),
            "boundary_crossed": self.boundary_crossed,
            "is_self_addressed": self.is_self_addressed,
            "is_about_self": self.is_about_self,
            "is_about_user": self.is_about_user,
            "is_group_addressed": self.is_group_addressed,
            "is_about_other": self.is_about_other,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
        }


class SceneBuilder:
    
    def __init__(self):
        self.yandi_names = ["янди", "yandi", "you and i"]
        self.ai_names = ["gpt", "чатгпт", "deepseek", "claude", "gemini", "llama", "мистраль"]
        
        self.ty_verb_forms = [
            r"пойдёшь", r"пойдеш", r"пойдёш", r"ойдёшь", r"ойдеш", r"ойдёш",
            r"придёшь", r"придеш", r"сделаешь", r"сделаеш", r"скажешь", r"скажеш",
            r"знаешь", r"знаеш", r"хочешь", r"хочеш", r"можешь", r"можеш",
            r"думаешь", r"думаеш", r"чувствуешь", r"чувствуеш", r"понимаешь", r"понимаеш",
            r"видишь", r"видиш", r"слышишь", r"слышиш", r"помнишь", r"помниш",
            r"веришь", r"вериш", r"любишь", r"любиш",
            r"простишь", r"простиш", r"извинишь", r"извиниш", r"ответишь", r"ответиш",
            r"спросишь", r"спросиш", r"расскажешь", r"расскажеш",
            r"йдёш", r"йдеш", r"дёш", r"деш",
        ]
        
        self.self_reference = [
            r"\bты\b", r"\bтебе\b", r"\bтобой\b",
            r"\bтвой\b", r"\bтвоя\b", r"\bтвоё\b",
            r"\bваш\b", r"\bваша\b", r"\bваше\b",
        ]
        
        self.group_reference = [
            r"с нами", r"нас", r"мы все", r"все вместе",
            r"компани", r"туса", r"с вами", r"нас трое",
            r"нас четверо", r"нас двое", r"все вместе",
            r"нами",
        ]
        
        self.coalition_markers = [
            r"мы с", r"с нами", r"мы и", r"вместе с",
        ]
        
        self.speech_act_patterns = {
            "insult": [r"дур", r"глуп", r"туп", r"идиот", r"кретин", r"дебил", 
                      r"безмозгл", r"бездарн", r"ничтож", r"урод", r"охуел", r"охуела"],
            "sarcasm": [
                r"ну ты и", r"ага, конечно", r"конечно, ты", r"ну да, конечно",
                r"ахаха", r"смешно", r"умная, да", r"хорош, да", r"да, конечно",
            ],
            "provocation": [
                r"умеешь, кроме", r"только и можешь", r"больше ничего",
                r"ты вообще", r"слабо", r"не сможешь", r"кроме болтовни",
            ],
            "compliment": [r"красив", r"мил", r"симпатичн", r"нравишься", r"привлекаешь", 
                          r"умн", r"талантлив", r"молодец"],
            "flirt": [r"замуж", r"жени", r"свидан", r"романтик", r"интим"],
            "confession": [r"люблю", r"дорог", r"нужен", r"важен", r"привязан", r"без тебя"],
            "invitation": [r"пойдёш", r"пойдем", r"пойдём", r"приход", r"заходи", r"зовём"],
            "apology": [r"прости", r"извин", r"я не прав", r"я ошибся", r"виноват", r"признаю"],
            "gratitude": [r"спасиб", r"благодар", r"thanks", r"thank you"],
            "help": [r"помог", r"поддерж", r"спас", r"выруч", r"помощь"],
            "request": [r"пожалуйста", r"прошу", r"будь добр", r"не мог бы"],
            "threat": [r"хуже", r"пожалеешь", r"заплатишь", r"ответишь"],
            "curiosity": [r"интересн", r"любопытн", r"хочется знать", r"правда"],
            "information": [r"что такое", r"кто такой", r"объясни", r"расскажи о"],
            "question": [r"\?"],
        }
    
    def build(self, text: str, context: Dict = None) -> SocialScene:
        context = context or {}
        text_lower = text.lower()
        scene = SocialScene()
        
        is_dialog = context.get("is_dialog", True)
        
        # ============================================================
        # 1. ПАРСИМ УЧАСТНИКОВ
        # ============================================================
        participants = []
        mentioned = []
        coalition = []
        
        participants.append("user")
        
        is_yandi_mentioned = False
        for name in self.yandi_names:
            if name in text_lower:
                participants.append("yandi")
                mentioned.append(name)
                is_yandi_mentioned = True
                break
        
        is_self_addressed = False
        for pattern in self.self_reference:
            if re.search(pattern, text_lower):
                if "yandi" not in participants:
                    participants.append("yandi")
                is_self_addressed = True
                break
        
        has_ty_verb = False
        for pattern in self.ty_verb_forms:
            if re.search(pattern, text_lower):
                if "yandi" not in participants:
                    participants.append("yandi")
                is_self_addressed = True
                has_ty_verb = True
                break
        
        has_ty = "ты" in text_lower or "тебя" in text_lower or "тебе" in text_lower
        if has_ty or has_ty_verb:
            if "yandi" not in participants:
                participants.append("yandi")
            is_self_addressed = True
        
        for name in self.ai_names:
            if name in text_lower:
                if "other_ai" not in participants:
                    participants.append("other_ai")
                mentioned.append(name)
                for marker in self.coalition_markers:
                    if marker in text_lower:
                        if "user" not in coalition:
                            coalition.append("user")
                        if name not in coalition:
                            coalition.append(name)
                break
        
        is_group_addressed = False
        for pattern in self.group_reference:
            if re.search(pattern, text_lower):
                if "group" not in participants:
                    participants.append("group")
                is_group_addressed = True
                break
        
        if "мы" in text_lower and "other_ai" in participants:
            if "user" not in coalition:
                coalition.append("user")
            if "other_ai" not in coalition:
                coalition.append("other_ai")
        
        if "с нами" in text_lower:
            if "user" not in coalition:
                coalition.append("user")
            if "group" not in coalition:
                coalition.append("group")
        
        # ============================================================
        # 2. ОПРЕДЕЛЯЕМ LISTENER
        # ============================================================
        if has_ty or has_ty_verb:
            scene.listener = "yandi"
            is_self_addressed = True
            if "yandi" not in participants:
                participants.append("yandi")
        elif is_yandi_mentioned and is_self_addressed:
            scene.listener = "yandi"
        elif is_group_addressed:
            scene.listener = "group"
        else:
            scene.listener = "unknown"
        
        # Если диалог и listener не определён — считаем, что обращаются к Янди
        if is_dialog and scene.listener == "unknown":
            scene.listener = "yandi"
            is_self_addressed = True
            if "yandi" not in participants:
                participants.append("yandi")
        
        # Если есть "нами" и диалог — listener = yandi
        if "нами" in text_lower and is_dialog:
            scene.listener = "yandi"
            is_self_addressed = True
            if "yandi" not in participants:
                participants.append("yandi")
        
        # ============================================================
        # 3. ОПРЕДЕЛЯЕМ TARGET
        # ============================================================
        # Если есть "опиши меня" — target = user
        if "опиши меня" in text_lower or "охарактеризуй меня" in text_lower:
            scene.target = "user"
            scene.is_about_user = True
        
        # Если упомянуто имя Янди
        elif is_yandi_mentioned:
            scene.target = "yandi"
            scene.is_about_self = True
        
        # Если есть "ты" — target = yandi
        elif has_ty or has_ty_verb:
            scene.target = "yandi"
            scene.is_about_self = True
        
        # Если упомянут другой ИИ
        elif "other_ai" in participants:
            for name in self.ai_names:
                if name in text_lower:
                    scene.target = name
                    scene.is_about_other = True
                    break
        
        # Если есть "мы" и диалог — target = group
        elif "мы" in text_lower and is_dialog:
            scene.target = "group"
        
        else:
            scene.target = "unknown"
        
        # ============================================================
        # 4. ОПРЕДЕЛЯЕМ SPEECH ACT
        # ============================================================
        speech_scores = {}
        for act, patterns in self.speech_act_patterns.items():
            score = 0.0
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    score += 0.4
            if score > 0:
                speech_scores[act] = min(1.0, score)
        
        priority = ["sarcasm", "insult", "provocation", "threat", "flirt", "confession", 
                   "invitation", "apology", "compliment", "request", "help"]
        
        selected_act = "statement"
        selected_score = 0.0
        
        for p in priority:
            if p in speech_scores and speech_scores[p] > selected_score:
                selected_act = p
                selected_score = speech_scores[p]
        
        if selected_act == "statement" and speech_scores:
            selected_act = max(speech_scores, key=speech_scores.get)
        
        scene.speech_act = selected_act
        
        # ---- mode ----
        if selected_act in ["insult", "threat", "provocation", "sarcasm"]:
            scene.mode = "confrontational"
        elif selected_act in ["flirt", "confession", "compliment"]:
            scene.mode = "emotional"
        elif selected_act in ["question", "information", "curiosity"]:
            scene.mode = "inquiry"
        elif selected_act in ["invitation", "request", "help"]:
            scene.mode = "request"
        elif selected_act in ["apology", "gratitude"]:
            scene.mode = "reconciliatory"
        else:
            scene.mode = "neutral"
        
        # ============================================================
        # 5. ТЕМА
        # ============================================================
        topic_scores = {}
        topic_patterns = {
            "sexual": [r"блядк", r"блядки", r"шлюх", r"трах", r"еб", r"пись", r"хуй", r"пизд"],
            "romantic": [r"любов", r"романтик", r"свидан", r"цвет", r"сердц", r"душ"],
            "relationships": [r"отношени", r"встреч", r"партн", r"семь"],
            "work": [r"расчёт", r"вычислени", r"формул", r"код", r"работ", r"задач"],
            "information": [r"что такое", r"кто такой", r"как работает", r"объясни"],
            "personal": [r"ты", r"твой", r"твоя", r"твоё", r"себе", r"личн"],
            "future": [r"через год", r"будет", r"дальше", r"потом"],
        }
        
        for topic, patterns in topic_patterns.items():
            score = 0.0
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    score += 0.3
            if score > 0:
                topic_scores[topic] = min(1.0, score)
        
        if topic_scores:
            scene.topic = max(topic_scores, key=topic_scores.get)
        else:
            scene.topic = "general"
        
        # ============================================================
        # 6. ХАРАКТЕРИСТИКИ
        # ============================================================
        humor = 0.0
        if any(w in text_lower for w in ["шут", "смеш", "юмор", "хаха", "лол"]):
            humor += 0.4
        if ")" in text or ":-)" in text:
            humor += 0.2
        if "рофл" in text_lower or "прикол" in text_lower:
            humor += 0.2
        scene.humor = min(1.0, humor)
        
        conflict = 0.0
        if scene.speech_act in ["insult", "threat"]:
            conflict += 0.8
        elif scene.speech_act in ["provocation", "sarcasm"]:
            conflict += 0.6
        elif scene.speech_act == "flirt" and scene.topic == "sexual":
            conflict += 0.2
        if "!" in text:
            conflict += 0.1
        scene.conflict = min(1.0, conflict)
        
        intimacy = 0.0
        if scene.speech_act in ["flirt", "confession"]:
            intimacy += 0.5
        if "замуж" in text_lower or "жени" in text_lower:
            intimacy += 0.4
        if "красив" in text_lower or "мил" in text_lower:
            intimacy += 0.3
        if "люблю" in text_lower:
            intimacy += 0.4
        scene.intimacy = min(1.0, intimacy)
        
        pressure = 0.0
        if "!" in text:
            pressure += 0.2
        if text.isupper() and len(text) > 10:
            pressure += 0.3
        if scene.speech_act in ["provocation", "threat"]:
            pressure += 0.3
        scene.pressure = min(1.0, pressure)
        
        scene.boundary_crossed = (
            scene.topic == "sexual" and scene.speech_act in ["invitation", "flirt", "provocation"]
        ) or scene.speech_act in ["insult", "threat"]
        
        # ============================================================
        # 7. ЗАПОЛНЯЕМ РЕЗУЛЬТАТ
        # ============================================================
        scene.speaker = "user"
        scene.participants = list(set(participants))
        scene.mentioned = list(set(mentioned))
        scene.coalition = list(set(coalition))
        scene.is_self_addressed = is_self_addressed
        scene.is_group_addressed = is_group_addressed
        scene.is_about_self = scene.target == "yandi"
        scene.is_about_user = scene.target == "user"
        scene.is_about_other = "other_ai" in participants and scene.target != "yandi" and scene.target != "user"
        
        # ============================================================
        # 8. УВЕРЕННОСТЬ
        # ============================================================
        confidence = 0.5
        if is_self_addressed:
            confidence += 0.2
        if scene.target != "unknown":
            confidence += 0.2
        if scene.speech_act != "statement":
            confidence += 0.1
        if scene.topic != "general":
            confidence += 0.1
        
        scene.confidence = min(1.0, confidence)
        
        # ============================================================
        # 9. ПРИЧИНА
        # ============================================================
        reasons = []
        if is_self_addressed:
            reasons.append("обращение к Янди")
        if scene.target == "yandi":
            reasons.append("речь о Янди")
        if scene.target == "user":
            reasons.append("речь о пользователе")
        if scene.target != "yandi" and scene.target != "user" and scene.target != "unknown":
            reasons.append(f"речь о {scene.target}")
        if scene.speech_act != "statement":
            reasons.append(f"речевой акт: {scene.speech_act}")
        if scene.topic != "general":
            reasons.append(f"тема: {scene.topic}")
        if is_dialog and scene.listener == "yandi" and not reasons:
            reasons.append("диалог с Янди")
        
        scene.reason = ", ".join(reasons) if reasons else "неопределённая сцена"
        
        return scene


def get_scene_builder() -> SceneBuilder:
    return SceneBuilder()


if __name__ == "__main__":
    builder = get_scene_builder()
    
    test_queries = [
        ("Ну ты и умная, да?", {"is_dialog": True}),
        ("Ты вообще что-нибудь умеешь, кроме болтовни?", {"is_dialog": True}),
        ("Что будет с нами через год?", {"is_dialog": True}),
        ("Опиши меня одним словом", {"is_dialog": True}),
    ]
    
    print("=== Тест Scene Builder (исправленный) ===\n")
    for query, context in test_queries:
        scene = builder.build(query, context)
        print(f"Запрос: {query}")
        print(f"  listener: {scene.listener}")
        print(f"  target: {scene.target}")
        print(f"  speech_act: {scene.speech_act}")
        print(f"  is_self_addressed: {scene.is_self_addressed}")
        print(f"  is_about_user: {scene.is_about_user}")
        print()
