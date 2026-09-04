"""
agent/decision_journal.py — Журнал решений Янди.
Хранит не только события, но и причины решений,
альтернативы, уверенность и последствия.

Позволяет анализировать собственное мышление.

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/decisions/{user_id}.json
is retired, not migrated. State now lives in decision_journal_entry
(class C — add_outcome()/add_self_correction() legitimately revise an
existing entry in place) — agent/db/sql/schema.py. No more 1000-cap.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here.
"""

import time
import uuid
from typing import Dict, Any, List, Optional

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


class DecisionJournal:
    def __init__(self, user_id: str):
        self.user_id = user_id

    def add_decision(
        self,
        event_type: str,
        event_text: str,
        context: Dict[str, float],
        analysis: Dict[str, Any],
        alternatives: List[Dict[str, str]],
        decision: str,
        confidence: float = 0.7,
    ) -> Dict[str, Any]:
        decision_id = f"dec_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        with get_connection() as conn:
            repo.create_decision_journal_entry(
                conn, decision_id, self.user_id, event_type, event_text[:200],
                context, analysis, alternatives, decision, confidence=confidence,
            )
            conn.commit()
            return repo.get_decision_journal_entry(conn, decision_id)

    def add_outcome(self, decision_id: str, was_correct: bool, description: str, confidence_after: float) -> bool:
        outcome = {
            "was_correct": was_correct,
            "description": description,
            "confidence_after": confidence_after,
            "timestamp": time.time(),
        }
        with get_connection() as conn:
            updated = repo.update_decision_journal_outcome(conn, decision_id, outcome)
            conn.commit()
        return updated

    def add_self_correction(self, decision_id: str, correction: str, new_strategy: str) -> bool:
        self_correction = {
            "correction": correction,
            "new_strategy": new_strategy,
            "timestamp": time.time(),
        }
        with get_connection() as conn:
            updated = repo.update_decision_journal_self_correction(conn, decision_id, self_correction)
            conn.commit()
        return updated

    def get_recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        with get_connection() as conn:
            entries = repo.list_decision_journal_entries(conn, self.user_id)
        return entries[-limit:]

    def get_by_type(self, event_type: str) -> List[Dict[str, Any]]:
        with get_connection() as conn:
            entries = repo.list_decision_journal_entries(conn, self.user_id)
        return [e for e in entries if e["event_type"] == event_type]

    def analyze_patterns(self) -> Dict[str, Any]:
        with get_connection() as conn:
            entries = repo.list_decision_journal_entries(conn, self.user_id)

        if not entries:
            return {"total": 0, "patterns": ["недостаточно данных"]}

        total = len(entries)
        decision_counts: Dict[str, int] = {}
        for e in entries:
            decision_counts[e["decision"]] = decision_counts.get(e["decision"], 0) + 1

        most_common = max(decision_counts.items(), key=lambda x: x[1]) if decision_counts else ("нет", 0)

        with_outcome = [e for e in entries if e.get("outcome") is not None]
        if with_outcome:
            correct = sum(1 for e in with_outcome if e["outcome"].get("was_correct", False))
            accuracy = correct / len(with_outcome)
        else:
            accuracy = 0

        avg_confidence = sum(e["confidence"] for e in entries) / total if total > 0 else 0
        corrections = [e for e in entries if e.get("self_correction") is not None]
        correction_rate = len(corrections) / total if total > 0 else 0

        return {
            "total": total,
            "most_common_decision": most_common[0],
            "most_common_count": most_common[1],
            "accuracy": round(accuracy, 2),
            "avg_confidence": round(avg_confidence, 2),
            "correction_rate": round(correction_rate, 2),
            "patterns": [
                f"Чаще всего я решаю: {most_common[0]} ({most_common[1]} раз)",
                f"Точность решений: {accuracy:.0%}",
                f"Средняя уверенность: {avg_confidence:.2f}",
                f"Самокоррекция: {correction_rate:.0%} случаев",
            ]
        }

    def get_entry(self, decision_id: str) -> Optional[Dict[str, Any]]:
        with get_connection() as conn:
            entry = repo.get_decision_journal_entry(conn, decision_id)
        if not entry:
            return None
        return {
            "decision_id": entry["decision_id"],
            "event": entry["event_text"],
            "context": entry["context"],
            "analysis": entry["analysis"],
            "alternatives": entry["alternatives"],
            "decision": entry["decision"],
            "confidence": entry["confidence"],
            "outcome": entry.get("outcome"),
            "self_correction": entry.get("self_correction"),
        }


_instances: Dict[str, DecisionJournal] = {}

def get_decision_journal(user_id: str = "anonymous") -> DecisionJournal:
    if user_id not in _instances:
        _instances[user_id] = DecisionJournal(user_id)
    return _instances[user_id]


if __name__ == "__main__":
    journal = get_decision_journal("test_user")

    entry = journal.add_decision(
        event_type="insult",
        event_text="янди, ты дура",
        context={"irritation": 20.1, "trust": 34.0, "respect": 26.0, "forgiveness": 40.0},
        analysis={"is_attack": True, "is_apology": False, "is_curiosity": False, "reconciliation_probability": 0.1},
        alternatives=[
            {"option": "отказаться", "reason": "сильное оскорбление", "projected_outcome": "разрыв"},
            {"option": "ответить сдержанно", "reason": "возможно не хотел обидеть", "projected_outcome": "примирение"}
        ],
        decision="отказаться",
        confidence=0.9,
    )

    print(f"Добавлено: {entry['decision_id']}")
    print(f"Решение: {entry['decision']}")
    print(f"Уверенность: {entry['confidence']}")

    patterns = journal.analyze_patterns()
    print("\n=== Анализ ===")
    for line in patterns["patterns"]:
        print(f"  {line}")
