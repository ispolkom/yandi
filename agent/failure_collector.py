#!/usr/bin/env python3
"""
assistant/failure_collector.py — коллектор провальных примеров.

Собирает:
  - REJECT-примеры из DatasetValidator (с причиной)
  - Низкокачественные примеры из QualityFilter (score < 35)
  - Примеры с явными ошибками (опечатки, дубли, пустые ответы)

Хранение: registry/dataset/failures/failures_YYYYMMDD.jsonl
Формат каждой записи:
  {
    "bad_sample": "...",      # исходный текст
    "reason"   : "...",       # почему плохой
    "lesson"   : "...",       # что правильно
    "corrected": "...",       # исправленный вариант (если есть)
    "source"   : "...",       # validator | filter | manual
    "topic"    : "...",
    "timestamp": "..."
  }

API:
  fc = FailureCollector()
  fc.add(bad_sample, reason, lesson="", corrected="", source="manual", topic="")
  fc.stats()
  fc.export_training_pairs()  → список {prompt, rejected, accepted}
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis

BASE         = Path(__file__).parent.parent
FAILURES_DIR = BASE / "registry" / "dataset" / "failures"
REPORT_KEY   = "council:skill:reports"
REPORT_CH    = "council:skill:report"

FAILURES_DIR.mkdir(parents=True, exist_ok=True)


def _r() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def _today_file() -> Path:
    stamp = datetime.now().strftime("%Y%m%d")
    return FAILURES_DIR / f"failures_{stamp}.jsonl"


class FailureCollector:
    """Append-only коллектор плохих примеров для датасета."""

    def __init__(self, r: Optional[redis.Redis] = None):
        self.r = r or _r()

    def add(self, bad_sample: str, reason: str,
            lesson: str = "", corrected: str = "",
            source: str = "manual", topic: str = "unknown") -> dict:
        record = {
            "bad_sample": bad_sample[:500],
            "reason"    : reason[:200],
            "lesson"    : lesson[:200],
            "corrected" : corrected[:500],
            "source"    : source,
            "topic"     : topic,
            "timestamp" : datetime.now().isoformat(),
        }
        with open(_today_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def add_rejected(self, record: dict, votes: list[str], reason: str = ""):
        """Добавить запись, отклонённую DatasetValidator."""
        text = record.get("content", record.get("text", ""))
        self.add(
            bad_sample=text,
            reason    =reason or f"Validator REJECT ({len(votes)} голосов)",
            lesson    ="Запись не прошла валидацию советом",
            source    ="validator",
            topic     =record.get("topic", "unknown"),
        )

    def add_low_quality(self, record: dict, score: int, issues: list[str]):
        """Добавить низкокачественную запись из QualityFilter."""
        text = record.get("content", record.get("text", ""))
        self.add(
            bad_sample=text,
            reason    =f"Низкое качество (score={score}): {', '.join(issues[:3])}",
            lesson    ="Нужен более информативный/уникальный контент",
            source    ="filter",
            topic     =record.get("topic", "unknown"),
        )

    def load_all(self) -> list[dict]:
        rows = []
        for f in sorted(FAILURES_DIR.glob("failures_*.jsonl")):
            with open(f, encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
        return rows

    def stats(self) -> dict:
        rows   = self.load_all()
        topics  = Counter(r.get("topic", "?") for r in rows)
        sources = Counter(r.get("source", "?") for r in rows)
        return {
            "total"  : len(rows),
            "topics" : dict(topics),
            "sources": dict(sources),
            "files"  : len(list(FAILURES_DIR.glob("failures_*.jsonl"))),
        }

    def export_training_pairs(self, with_corrected_only: bool = False) -> list[dict]:
        """
        Экспортирует пары для обучения: {bad_sample, reason, corrected/lesson}.
        with_corrected_only=True → только записи с исправлениями.
        """
        rows = self.load_all()
        pairs = []
        for r in rows:
            if with_corrected_only and not r.get("corrected"):
                continue
            pairs.append({
                "input"   : r["bad_sample"],
                "label"   : "bad",
                "reason"  : r["reason"],
                "improved": r.get("corrected") or r.get("lesson", ""),
                "topic"   : r.get("topic", ""),
                "source"  : r.get("source", ""),
            })
        return pairs


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    fc  = FailureCollector()

    if cmd == "stats":
        print(json.dumps(fc.stats(), ensure_ascii=False, indent=2))
    elif cmd == "add":
        bad     = sys.argv[2] if len(sys.argv) > 2 else ""
        reason  = sys.argv[3] if len(sys.argv) > 3 else ""
        corrected = sys.argv[4] if len(sys.argv) > 4 else ""
        r = fc.add(bad, reason, corrected=corrected, source="manual")
        print(f"Добавлено: {r['timestamp']}")
    elif cmd == "export":
        pairs = fc.export_training_pairs()
        print(json.dumps(pairs, ensure_ascii=False, indent=2))
    else:
        print("Команды: stats | add <bad> <reason> [corrected] | export")
