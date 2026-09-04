"""
agent/disagreement_engine.py — Спор как обучающий эпизод для YANDI V6.

Формат:
old_position → challenge → analysis → new_position

Система меняет мнение под влиянием контраргументов.
Каждый спор — это обучение.

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/disagreements.json is
retired, not migrated. State now lives in disagreement (class B,
append-only) — agent/db/sql/schema.py.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here.
"""

from __future__ import annotations

import uuid
from typing import Optional, Dict, Any, List

from agent.belief_manager import get_belief_manager
from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


class DisagreementEngine:
    """
    Двигатель спора — обучение через контраргументы.
    """

    def __init__(self):
        self.belief_manager = get_belief_manager()

    def challenge(
        self,
        topic: str,
        old_position: str,
        challenge: str,
        analysis: str,
        new_position: str,
        confidence_before: float,
        confidence_after: float,
        related_belief_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Зафиксировать эпизод спора.
        """
        disagreement_id = f"dag_{uuid.uuid4().hex[:8]}"
        with get_connection() as conn:
            repo.create_disagreement(
                conn, disagreement_id, topic, old_position, challenge, analysis, new_position,
                confidence_before, confidence_after, resolved=True, related_belief_id=related_belief_id,
            )
            conn.commit()

        if related_belief_id:
            self.belief_manager.challenge_belief(
                belief_id=related_belief_id,
                counter_evidence=challenge,
                new_confidence=confidence_after,
                reason=f"спор: {analysis[:50]}",
            )

        return {
            "id": disagreement_id, "topic": topic, "old_position": old_position,
            "challenge": challenge, "analysis": analysis, "new_position": new_position,
            "confidence_before": confidence_before, "confidence_after": confidence_after,
            "resolved": True, "related_belief_id": related_belief_id,
        }

    def get_recent(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Получить последние споры."""
        with get_connection() as conn:
            disagreements = repo.list_disagreements(conn)
        return disagreements[-limit:][::-1]

    def get_by_topic(self, topic: str) -> List[Dict[str, Any]]:
        """Получить споры по теме."""
        with get_connection() as conn:
            disagreements = repo.list_disagreements(conn)
        return [d for d in disagreements if d["topic"] == topic]

    def get_stats(self) -> Dict[str, Any]:
        """Статистика споров."""
        with get_connection() as conn:
            disagreements = repo.list_disagreements(conn)

        topics: Dict[str, int] = {}
        for d in disagreements:
            topics[d["topic"]] = topics.get(d["topic"], 0) + 1

        total_conf_change = sum(d["confidence_before"] - d["confidence_after"] for d in disagreements)
        avg_change = total_conf_change / len(disagreements) if disagreements else 0

        return {
            "total": len(disagreements),
            "topics": topics,
            "avg_confidence_change": round(avg_change, 2),
            "resolved": len([d for d in disagreements if d["resolved"]]),
        }

    def summary(self) -> str:
        stats = self.get_stats()
        recent = self.get_recent(3)

        return f"""
=== DISAGREEMENT ENGINE ===
Всего споров: {stats['total']}
Разрешено: {stats['resolved']}
Среднее изменение уверенности: {stats['avg_confidence_change']}
Темы: {', '.join(f'{k}={v}' for k, v in stats['topics'].items())}

Последние споры:
{chr(10).join(f"  - [{d['confidence_before']:.2f}→{d['confidence_after']:.2f}] {d['topic']}: {d['old_position'][:30]} → {d['new_position'][:30]}" for d in recent) if recent else '  нет'}
"""


_inst: Optional[DisagreementEngine] = None

def get_disagreement_engine() -> DisagreementEngine:
    global _inst
    if _inst is None:
        _inst = DisagreementEngine()
    return _inst


if __name__ == "__main__":
    de = get_disagreement_engine()
    print(de.summary())

    d = de.challenge(
        topic="consciousness",
        old_position="Сознание — это только нейронная активность",
        challenge="Трудная проблема сознания показывает, что субъективный опыт не сводится к нейронным процессам",
        analysis="Физикализм не объясняет квалиа, нужна дополнительная теория",
        new_position="Сознание связано с нейронной активностью, но имеет субъективный компонент",
        confidence_before=0.8,
        confidence_after=0.55,
    )
    print(f"\n✅ Спор зафиксирован: {d['topic']}")
    print(f"   {d['old_position']} → {d['new_position']}")
    print(f"   Уверенность: {d['confidence_before']:.2f} → {d['confidence_after']:.2f}")

    print(de.summary())
