"""
agent/orch_query_archive.py — Query Archive (SQLite backend).

Сохраняет запросы пользователей через KnowledgeDB.
Поддерживает тот же внешний API что и старая JSONL-версия.

CLI:
  python3 -m agent.orch_query_archive stats
  python3 -m agent.orch_query_archive list
  python3 -m agent.orch_query_archive get <tag> [limit]
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime

from agent.db.manager import KnowledgeDB

_ANON_PATTERNS = [
    (re.compile(r'\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b', re.I), '[EMAIL]'),
    (re.compile(r'\b\+?[\d][\d\s\-\(\)]{7,}\d\b'),          '[PHONE]'),
    (re.compile(r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b'), '[CARD]'),
    (re.compile(r'\b(?:ул\.|улица|пр\.|проспект|д\.|дом)\s+\S+[^.!?]*\d+\b', re.I), '[ADDRESS]'),
]


def anonymize(text: str) -> str:
    for pattern, replacement in _ANON_PATTERNS:
        text = pattern.sub(replacement, text)
    return text.strip()


def record_query(
    query: str,
    tag: str,
    answer: str = "",
    confidence: float = 0.0,
    trust_level: str = "UNVERIFIED",
    session_id: str = "",
    sources: list[str] | None = None,
) -> dict:
    """
    Записать запрос в базу знаний.
    Возвращает dict, совместимый со старым JSONL-форматом.
    """
    query_anon  = anonymize(query)
    answer_anon = anonymize(answer) if answer else ""
    session_hash = hashlib.sha256(session_id.encode()).hexdigest()[:12] if session_id else ""
    query_hash   = hashlib.md5(query_anon.lower().encode()).hexdigest()[:8]

    meta = {
        "session_hash": session_hash,
        "date":         datetime.now().strftime("%Y-%m-%d"),
    }

    db = KnowledgeDB()
    rid = db.save_knowledge(
        query       = query_anon,
        answer      = answer_anon,
        tag         = tag,
        trust_level = trust_level,
        confidence  = round(confidence, 3),
        sources     = sources or [],
        meta        = meta,
    )

    return {
        "query":        query_anon,
        "query_hash":   query_hash,
        "tag":          tag,
        "answer":       answer_anon,
        "confidence":   round(confidence, 3),
        "trust_level":  trust_level,
        "session_hash": session_hash,
        "sources":      sources or [],
        "ts":           time.time(),
        "date":         meta["date"],
        "id":           rid,
    }


def get_queries(tag: str, limit: int = 100, min_confidence: float = 0.0) -> list[dict]:
    """Получить записи по тегу."""
    rows = KnowledgeDB().list_by_tag(tag, limit=limit, min_confidence=min_confidence)
    result = []
    for r in rows:
        meta = r.get("meta") or {}
        result.append({
            "query":        r["query"],
            "query_hash":   r["id"],
            "tag":          r["tag"],
            "answer":       r["answer"],
            "confidence":   r["confidence"],
            "trust_level":  r["trust_level"],
            "session_hash": meta.get("session_hash", ""),
            "sources":      r["sources"],
            "ts":           0.0,
            "date":         meta.get("date", r["created_at"][:10]),
            "id":           r["id"],
        })
    return result


def get_all_tags() -> list[str]:
    return KnowledgeDB().list_tags()


def get_tag_stats(tag: str) -> dict:
    return KnowledgeDB().tag_stats(tag)


def get_archive_stats() -> dict:
    s = KnowledgeDB().stats()
    return {
        "total":      s["knowledge"],
        "tags_count": 0,          # заполняется ниже
        "by_tag":     {},
        "verified":   s["verified"],
        "traces":     s["traces"],
    }


def dedup_tag(tag: str) -> int:
    """Дедупликация не нужна — PRIMARY KEY в SQLite гарантирует уникальность."""
    return 0


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "stats":
        s = KnowledgeDB().stats()
        print(f"Query Archive (SQLite)")
        print(f"  Категории: {s['categories']}")
        print(f"  Knowledge: {s['knowledge']}")
        print(f"  Verified:  {s['verified']}")
        print(f"  Traces:    {s['traces']}")
        print(f"  Gold pairs:{s['gold_pairs']}")

    elif cmd == "list":
        tags = get_all_tags()
        print(f"Теги ({len(tags)}):")
        for t in tags:
            print(f"  {t}")

    elif cmd == "get":
        tag   = sys.argv[2] if len(sys.argv) > 2 else "general:general"
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        qs    = get_queries(tag, limit=limit)
        print(f"Запросы [{tag}] ({len(qs)}):")
        for q in qs:
            print(f"  [{q.get('date','')}] conf={q.get('confidence',0):.2f} | {q['query'][:80]}")

    elif cmd == "test":
        r = record_query(
            query="Как лечить кашель? Email: test@mail.ru тел. +7 999 123-45-67",
            tag="health:medicine",
            answer="Кашель лечится...",
            confidence=0.75,
            trust_level="HYPOTHESIS",
        )
        print("Записано:", r["query"])
        print("ID:", r["id"])
        print("Тег:", r["tag"])
