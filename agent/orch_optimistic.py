"""
assistant/orch_optimistic.py — Optimistic Responder.
Немедленно выдаёт предварительный ответ пользователю.
Параллельно стартует фоновую валидацию (не блокирует UI).
Чистый код — без LLM.
"""
from __future__ import annotations

import time
import uuid
from typing import Callable, Optional

from agent.orch_schemas import SynthesisResult, OptimisticResponse


def format_preliminary(synthesis: SynthesisResult, validation_id: str = "") -> str:
    """Форматировать предварительный ответ для отображения в чате."""
    lines = []

    trust_badge = {
        "VERIFIED":           "✅ Проверено",
        "PARTIALLY_VERIFIED": "🔶 Частично проверено",
        "HYPOTHESIS":         "🔬 Гипотеза",
        "PERSONAL":           "👤 Персональное",
        "UNVERIFIED":         "⏳ На проверке",
    }.get(synthesis.trust_level, "⏳ На проверке")

    lines.append(f"[ПРЕДВАРИТЕЛЬНЫЙ • {trust_badge}]")
    lines.append("")
    lines.append(synthesis.answer)

    if synthesis.sources:
        lines.append("")
        lines.append("Источники:")
        for src in synthesis.sources[:3]:
            lines.append(f"- {src}")

    if validation_id:
        lines.append("")
        lines.append(f"🔄 Отправлен на проверку через доверенные ноды (ID: {validation_id[:8]})")

    return "\n".join(lines)


def format_verified(synthesis: SynthesisResult, verdict: str, explanation: str) -> str:
    """Форматировать финальный ответ после валидации."""
    badge = {
        "VERIFIED":           "✅ Ответ прошёл проверку",
        "PARTIALLY_VERIFIED": "🔶 Ответ частично проверен",
        "CONFLICT_DETECTED":  "⚠️ Обнаружены расхождения",
        "REJECTED":           "❌ Ответ не прошёл проверку",
    }.get(verdict, "ℹ️ Проверка завершена")

    lines = [badge]
    if explanation:
        lines.append(explanation)
    return "\n".join(lines)


class OptimisticResponder:
    """
    Выдаёт предварительный ответ, запускает валидацию в фоне.
    """

    def __init__(self, on_update: Optional[Callable[[str, str], None]] = None):
        """
        Args:
            on_update: callback(validation_id, update_text) — вызывается когда
                       фоновая валидация завершена. Используется для обновления UI.
        """
        self._on_update = on_update
        self._pending: dict[str, dict] = {}

    def respond(
        self,
        synthesis: SynthesisResult,
        start_validation: Optional[Callable[[str], None]] = None,
    ) -> OptimisticResponse:
        """
        Немедленно вернуть предварительный ответ.

        Args:
            synthesis:         результат синтеза
            start_validation:  функция запуска фоновой валидации (опционально)

        Returns:
            OptimisticResponse — можно сразу показать пользователю
        """
        val_id = str(uuid.uuid4())
        text   = format_preliminary(synthesis, val_id)

        self._pending[val_id] = {
            "synthesis": synthesis,
            "ts":        time.time(),
        }

        # Запустить валидацию в фоне если передана функция
        if start_validation:
            try:
                start_validation(val_id)
            except Exception:
                pass

        return OptimisticResponse(
            text=text,
            preliminary=True,
            validation_id=val_id,
        )

    def on_validation_done(self, validation_id: str, verdict: str, explanation: str):
        """Вызвать когда фоновая валидация завершена."""
        entry = self._pending.pop(validation_id, None)
        if not entry:
            return
        update_text = format_verified(entry["synthesis"], verdict, explanation)
        if self._on_update:
            self._on_update(validation_id, update_text)


# Простой синглтон для использования в оркестраторе
_responder: Optional[OptimisticResponder] = None

def get_responder(on_update: Optional[Callable] = None) -> OptimisticResponder:
    global _responder
    if _responder is None:
        _responder = OptimisticResponder(on_update=on_update)
    return _responder


def quick_respond(synthesis: SynthesisResult) -> OptimisticResponse:
    """Быстрый вызов без фоновой валидации."""
    return get_responder().respond(synthesis)


if __name__ == "__main__":
    from agent.orch_schemas import SynthesisResult
    s = SynthesisResult(
        answer="Kademlia — оптимальный выбор для DHT в P2P-сети с LLM-нодами. "
               "Он обеспечивает O(log N) поиск и хорошо работает при частой смене нод.",
        confidence=0.62,
        sources=["local:claude_20260517.jsonl"],
        trust_level="HYPOTHESIS",
    )
    resp = quick_respond(s)
    print(resp.text)
    print(f"\nValidation ID: {resp.validation_id}")
