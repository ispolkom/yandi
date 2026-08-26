"""
assistant/orch_feedback.py — Feedback Loop.
Собирает 👍/👎 от пользователей, обновляет репутацию нод.
Хранит в JSONL для последующего обучения.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

BASE         = Path(__file__).parent.parent
FEEDBACK_DIR = BASE / "registry" / "feedback"
FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
FEEDBACK_FILE = FEEDBACK_DIR / "feedback.jsonl"

FeedbackType = Literal["positive", "negative", "neutral"]


def record_feedback(
    question: str,
    answer: str,
    feedback: FeedbackType,
    session_id: str = "",
    validation_id: str = "",
    trust_level: str = "UNVERIFIED",
    sources: list[str] | None = None,
    liked_version: str | None = None,
    deepseek_verdict: str = "",
    deepseek_correction: str = "",
) -> dict:
    event = {
        "question":           question[:500],
        "answer":             answer[:1000],
        "feedback":           feedback,
        "liked_version":      liked_version,       # "web"|"deepseek"|"both"|null
        "deepseek_verdict":   deepseek_verdict,    # VERIFIED/PARTIALLY_VERIFIED/REJECTED
        "deepseek_correction": deepseek_correction[:300] if deepseek_correction else "",
        "session_id":         session_id,
        "validation_id":      validation_id,
        "trust_level":        trust_level,
        "sources":            sources or [],
        "ts":                 time.time(),
        "ts_iso":             time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def update_reputation_from_feedback(
    feedback: FeedbackType,
    node_ids: list[str],
    domain: str = "general",
):
    """
    Обновить репутацию нод на основе обратной связи.
    Positive → correct=True; Negative → correct=False.
    """
    if not node_ids or feedback == "neutral":
        return
    try:
        from agent.orch_reputation import update_node
        correct = feedback == "positive"
        for node_id in node_ids:
            update_node(node_id, correct=correct, latency=0.0, domain=domain)
    except Exception:
        pass


def get_feedback_stats(last_n: int = 500) -> dict:
    """Статистика по обратной связи."""
    if not FEEDBACK_FILE.exists():
        return {"total": 0, "positive": 0, "negative": 0, "neutral": 0, "rate": 0.0}

    lines = FEEDBACK_FILE.read_text(encoding="utf-8").splitlines()[-last_n:]
    events = [json.loads(l) for l in lines if l.strip()]

    total    = len(events)
    positive = sum(1 for e in events if e.get("feedback") == "positive")
    negative = sum(1 for e in events if e.get("feedback") == "negative")
    neutral  = total - positive - negative

    return {
        "total":    total,
        "positive": positive,
        "negative": negative,
        "neutral":  neutral,
        "rate":     round(positive / total, 3) if total else 0.0,
    }


def get_recent_feedback(n: int = 20) -> list[dict]:
    """Получить последние N отзывов."""
    if not FEEDBACK_FILE.exists():
        return []
    lines = FEEDBACK_FILE.read_text(encoding="utf-8").splitlines()
    events = [json.loads(l) for l in lines if l.strip()]
    return events[-n:]


if __name__ == "__main__":
    record_feedback(
        question="Что такое DHT?",
        answer="DHT — распределённая хеш-таблица...",
        feedback="positive",
        trust_level="VERIFIED",
    )
    print("Stats:", get_feedback_stats())
