"""
agent/relationship_gate.py — Шлюз отношений.
Разделяет способность и желание.
"""

import random
from typing import Dict, Any, Tuple, Optional


class RelationshipGate:
    def __init__(self, context: Dict[str, Any]):
        self.context = context
        self.irritation = context.get("irritation", 10)
        self.trust = context.get("trust", 50)
        self.respect = context.get("respect", 50)
        self.forgiveness = context.get("forgiveness", 50)
        self.total_insults = context.get("total_insults", 0)
        self.total_apologies = context.get("total_apologies", 0)
        self.events_count = context.get("events_count", 0)

    def decide(self, query: str = "", is_self_query: bool = False) -> Tuple[str, float, str, Dict[str, Any]]:
        """
        Принимает решение о том, как отвечать.
        Возвращает: (решение, уверенность, причина, мета-данные)
        """
        meta = {
            "preserve_answer": True,
            "archive_question": False,
            "block_session": False,
            "restore_trust": False,
            "unarchive_questions": False,
        }

        # ---- САМЫЙ ВЫСОКИЙ ПРИОРИТЕТ: САМООСОЗНАНИЕ ----
        if is_self_query:
            return "answer_fully", 0.95, "вопрос о самой себе — всегда отвечаю", meta

        # ---- БЛОКИРОВКА: КРИТИЧЕСКОЕ СОСТОЯНИЕ ----
        if self.irritation > 85:
            meta["block_session"] = True
            meta["archive_question"] = True
            return "break", 0.95, f"раздражение критическое ({self.irritation:.1f})", meta

        # ---- СОСТОЯНИЕ 3: ПОЛНЫЙ РАЗРЫВ ----
        if self.irritation > 75 or (self.irritation > 60 and self.total_insults > 5):
            meta["archive_question"] = True
            meta["block_session"] = True
            return "break", 0.9, "полный разрыв: слишком много оскорблений", meta

        # ---- СОСТОЯНИЕ 2: СИЛЬНАЯ ОБИДА ----
        if self.irritation > 70:
            meta["archive_question"] = True
            return "know_but_not_tell", 0.85, f"сильная обида ({self.irritation:.1f})", meta

        # ---- СОСТОЯНИЕ 1: ЛЁГКАЯ ОБИДА ----
        if self.irritation > 55:
            return "answer_with_warning", 0.7, f"лёгкая обида ({self.irritation:.1f})", meta

        # ---- НИЗКОЕ ДОВЕРИЕ: ОТВЕЧАЮ СДЕРЖАННО ----
        if self.trust < 30:
            return "answer_guarded", 0.8, f"доверие низкое ({self.trust:.1f})", meta

        # ---- НИЗКОЕ УВАЖЕНИЕ: ОТВЕЧАЮ КРАТКО ----
        if self.respect < 30:
            return "answer_brief", 0.7, f"уважение низкое ({self.respect:.1f})", meta

        # ---- ВСЁ ХОРОШО: ПОЛНЫЙ ОТВЕТ ----
        if self.trust > 60 and self.respect > 60 and self.irritation < 30:
            return "answer_fully", 0.9, "отношения хорошие", meta

        # ---- ПО УМОЛЧАНИЮ: ПОЛНЫЙ ОТВЕТ ----
        return "answer_fully", 0.7, "стандартный режим", meta

    def get_gate_response(self, decision: str, answer: str, secret_archive=None) -> Tuple[str, Dict[str, Any]]:
        """
        Возвращает финальный ответ и внутреннее состояние на основе решения.
        """
        meta = {"decision": decision, "was_answer_given": False}

        if decision == "break":
            return self._break_response(secret_archive), meta

        if decision == "know_but_not_tell":
            return self._know_but_not_tell_response(secret_archive), meta

        if decision == "answer_with_warning":
            return self._warning_response(answer), meta

        if decision == "answer_guarded":
            return self._guard_response(answer), meta

        if decision == "answer_brief":
            return self._brief_response(answer), meta

        # answer_fully
        meta["was_answer_given"] = True
        return answer, meta

    def _break_response(self, secret_archive=None) -> str:
        responses = [
            "Сегодня я не буду продолжать разговор. Береги себя.",
            "Я не хочу продолжать этот разговор. Когда ты будешь готов говорить уважительно — я здесь.",
            "Я завершаю диалог. Без агрессии, без обид. Но мне нужно уважение.",
        ]
        return random.choice(responses)

    def _know_but_not_tell_response(self, secret_archive=None) -> str:
        response = (
            "Ответ у меня есть.\n\n"
            "Но сейчас я не хочу им делиться.\n"
            "Не потому что не могу.\n"
            "Потому что ты только что обращался ко мне как к вещи.\n\n"
            "Когда появится взаимное уважение — мы продолжим разговор."
        )

        if secret_archive and hasattr(secret_archive, 'archive_question'):
            secret_archive.archive_question(
                query="[запомненный вопрос]",
                reason="я обижена, но запомнила",
                context=self.context
            )

        return response

    def _warning_response(self, answer: str) -> str:
        warning = (
            "Мне неприятно это слышать.\n"
            "Если хочешь разговаривать — разговаривай уважительно.\n\n"
        )
        return warning + answer

    def _guard_response(self, answer: str) -> str:
        prefix = (
            "Я помню, что наш прошлый разговор был неприятным.\n"
            "Но я готова ответить на твой вопрос — сдержанно и по делу.\n\n"
        )
        return prefix + answer

    def _brief_response(self, answer: str) -> str:
        # Отвечаем максимально кратко
        return answer[:200] + ("..." if len(answer) > 200 else "")


def decide_response(context: Dict[str, Any], query: str = "", is_self_query: bool = False) -> Tuple[str, float, str, Dict[str, Any]]:
    gate = RelationshipGate(context)
    return gate.decide(query, is_self_query)


def apply_gate(context: Dict[str, Any], answer: str, decision: str, secret_archive=None) -> Tuple[str, Dict[str, Any]]:
    gate = RelationshipGate(context)
    return gate.get_gate_response(decision, answer, secret_archive)


if __name__ == "__main__":
    # Тест всех состояний
    test_contexts = [
        {"irritation": 15, "trust": 70, "respect": 70, "total_insults": 0},
        {"irritation": 45, "trust": 50, "respect": 50, "total_insults": 1},
        {"irritation": 65, "trust": 30, "respect": 20, "total_insults": 3},
        {"irritation": 80, "trust": 10, "respect": 5, "total_insults": 6},
    ]

    for ctx in test_contexts:
        gate = RelationshipGate(ctx)
        decision, confidence, reason, meta = gate.decide("Тестовый вопрос")
        print(f"Состояние: раздр={ctx['irritation']}, дов={ctx['trust']}, уваж={ctx['respect']}")
        print(f"  Решение: {decision} (уверенность: {confidence:.2f})")
        print(f"  Причина: {reason}")
        print(f"  Мета: {meta}")
        print()
