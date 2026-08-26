"""
agent/decision_journal.py — Журнал решений Янди.
Хранит не только события, но и причины решений,
альтернативы, уверенность и последствия.

Позволяет анализировать собственное мышление.
"""

import time
import json
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict

BASE = Path(__file__).parent.parent
JOURNAL_DIR = BASE / "registry" / "decisions"
JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class DecisionEntry:
    decision_id: str
    timestamp: float
    event_type: str
    event_text: str
    context: Dict[str, float]
    analysis: Dict[str, Any]
    alternatives: List[Dict[str, str]]
    decision: str
    confidence: float
    outcome: Optional[Dict[str, Any]] = None
    self_correction: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DecisionEntry":
        return cls(**data)


class DecisionJournal:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.file_path = JOURNAL_DIR / f"{user_id}.json"
        self.entries: List[DecisionEntry] = []
        self._load()

    def _load(self):
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.entries = [DecisionEntry.from_dict(e) for e in data.get("entries", [])]
            except Exception as e:
                print(f"[DecisionJournal] Ошибка загрузки: {e}")
                self.entries = []

    def _save(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump({
                    "user_id": self.user_id,
                    "entries": [e.to_dict() for e in self.entries],
                    "total": len(self.entries),
                    "last_updated": time.time()
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[DecisionJournal] Ошибка сохранения: {e}")

    def add_decision(
        self,
        event_type: str,
        event_text: str,
        context: Dict[str, float],
        analysis: Dict[str, Any],
        alternatives: List[Dict[str, str]],
        decision: str,
        confidence: float = 0.7,
    ) -> DecisionEntry:
        entry = DecisionEntry(
            decision_id=f"dec_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            timestamp=time.time(),
            event_type=event_type,
            event_text=event_text[:200],
            context=context,
            analysis=analysis,
            alternatives=alternatives,
            decision=decision,
            confidence=confidence,
            outcome=None,
            self_correction=None,
        )
        self.entries.append(entry)
        if len(self.entries) > 1000:
            self.entries = self.entries[-1000:]
        self._save()
        return entry

    def add_outcome(self, decision_id: str, was_correct: bool, description: str, confidence_after: float):
        for entry in self.entries:
            if entry.decision_id == decision_id:
                entry.outcome = {
                    "was_correct": was_correct,
                    "description": description,
                    "confidence_after": confidence_after,
                    "timestamp": time.time(),
                }
                self._save()
                return True
        return False

    def add_self_correction(self, decision_id: str, correction: str, new_strategy: str):
        for entry in self.entries:
            if entry.decision_id == decision_id:
                entry.self_correction = {
                    "correction": correction,
                    "new_strategy": new_strategy,
                    "timestamp": time.time(),
                }
                self._save()
                return True
        return False

    def get_recent(self, limit: int = 10) -> List[DecisionEntry]:
        return self.entries[-limit:]

    def get_by_type(self, event_type: str) -> List[DecisionEntry]:
        return [e for e in self.entries if e.event_type == event_type]

    def analyze_patterns(self) -> Dict[str, Any]:
        if not self.entries:
            return {"total": 0, "patterns": ["недостаточно данных"]}

        total = len(self.entries)
        decision_counts = {}
        for e in self.entries:
            decision_counts[e.decision] = decision_counts.get(e.decision, 0) + 1

        most_common = max(decision_counts.items(), key=lambda x: x[1]) if decision_counts else ("нет", 0)

        with_outcome = [e for e in self.entries if e.outcome is not None]
        if with_outcome:
            correct = sum(1 for e in with_outcome if e.outcome.get("was_correct", False))
            accuracy = correct / len(with_outcome)
        else:
            accuracy = 0

        avg_confidence = sum(e.confidence for e in self.entries) / total if total > 0 else 0
        corrections = [e for e in self.entries if e.self_correction is not None]
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
        for entry in self.entries:
            if entry.decision_id == decision_id:
                return {
                    "decision_id": entry.decision_id,
                    "event": entry.event_text,
                    "context": entry.context,
                    "analysis": entry.analysis,
                    "alternatives": entry.alternatives,
                    "decision": entry.decision,
                    "confidence": entry.confidence,
                    "outcome": entry.outcome,
                    "self_correction": entry.self_correction,
                }
        return None

    def clear(self):
        self.entries = []
        self._save()


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

    print(f"Добавлено: {entry.decision_id}")
    print(f"Решение: {entry.decision}")
    print(f"Уверенность: {entry.confidence}")

    patterns = journal.analyze_patterns()
    print("\n=== Анализ ===")
    for line in patterns["patterns"]:
        print(f"  {line}")
