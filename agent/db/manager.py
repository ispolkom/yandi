"""
agent/db/manager.py — Единая точка доступа к knowledge query archive.

"ТОЧКА НОЛЬ" v14 (owner mandate, 2026-09): the old sqlite backend
(registry/index.db + registry/knowledge/{category}.db files) is retired,
not migrated — every call below now goes straight to SQL
(knowledge_query_archive, agent/db/sql/schema.py). FAIL LOUD: there is
no more file-based fallback for SqlUnavailable to degrade to.

save_trace()/get_trace()/gold_dataset() (the old `traces` table) are
REMOVED, not ported — confirmed zero live callers (agent/orchestrator/
response/writeback.py's archive_query() only ever calls save_knowledge();
the only other caller was agent/db/migrate.py, a one-time, already-
disposable import tool for a pre-"точка ноль" external dataset drive).

Usage (unchanged for every live caller):
    from agent.db.manager import KnowledgeDB
    db = KnowledgeDB()
    db.save_knowledge(query, answer, tag, trust_level, confidence, sources)
    rows = db.list_unverified(limit=30)
"""
from __future__ import annotations

import hashlib

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


def make_id(query: str) -> str:
    """Детерминированный id из текста вопроса."""
    return hashlib.md5(query.lower().strip().encode()).hexdigest()[:8]


def _category(tag: str) -> str:
    """science:astronomy → science"""
    return tag.split(":")[0].strip() or "general"


class KnowledgeDB:
    """Основной интерфейс к базе знаний (SQL-backed, "точка ноль" v14)."""

    def save_knowledge(
        self,
        query: str,
        answer: str,
        tag: str,
        trust_level: str = "UNVERIFIED",
        confidence: float = 0.0,
        sources: list[str] | None = None,
        node_id: str = "",
        meta: dict | None = None,
        entry_id: str | None = None,
    ) -> str:
        """Сохранить Q&A. Возвращает id записи."""
        rid = entry_id or make_id(query)
        cat = _category(tag)
        with get_connection() as conn:
            repo.record_knowledge_query(
                conn, rid, query, answer, tag, cat,
                trust_level=trust_level, confidence=confidence,
                sources=sources, node_id=node_id, meta=meta,
            )
            conn.commit()
        return rid

    def get_knowledge(self, query: str) -> dict | None:
        """Найти ответ по тексту вопроса."""
        return self.get_by_id(make_id(query))

    def get_by_id(self, rid: str) -> dict | None:
        """Найти ответ по id."""
        with get_connection() as conn:
            return repo.get_knowledge_query(conn, rid)

    def verify(self, rid: str) -> bool:
        """Поставить отметку VERIFIED на запись."""
        with get_connection() as conn:
            ok = repo.set_knowledge_query_verified(conn, rid)
            conn.commit()
        return ok

    def list_unverified(self, limit: int = 30) -> list[dict]:
        """Список неверифицированных записей для review queue."""
        with get_connection() as conn:
            return repo.list_unverified_knowledge_queries(conn, limit=limit)

    def delete(self, rid: str) -> bool:
        """Удалить запись."""
        with get_connection() as conn:
            ok = repo.delete_knowledge_query(conn, rid)
            conn.commit()
        return ok

    def update_answer(self, rid: str, answer: str, trust_level: str = "VERIFIED") -> bool:
        """Обновить ответ и установить trust_level."""
        with get_connection() as conn:
            ok = repo.update_knowledge_query_answer(conn, rid, answer, trust_level=trust_level)
            conn.commit()
        return ok

    def list_by_tag(
        self,
        tag: str,
        limit: int = 100,
        min_confidence: float = 0.0,
    ) -> list[dict]:
        """Список записей по тегу (точный тег или все теги категории)."""
        with get_connection() as conn:
            return repo.list_knowledge_queries_by_tag(
                conn, tag, limit=limit, min_confidence=min_confidence,
            )

    def list_tags(self) -> list[str]:
        """Все теги из архива."""
        with get_connection() as conn:
            return repo.list_knowledge_query_tags(conn)

    def tag_stats(self, tag: str) -> dict:
        """Статистика по тегу: количество, avg confidence."""
        rows = self.list_by_tag(tag, limit=10_000)
        if not rows:
            return {"tag": tag, "count": 0, "unique": 0, "avg_confidence": 0.0}
        avg_conf = sum(r.get("confidence", 0.0) for r in rows) / len(rows)
        return {
            "tag":            tag,
            "count":          len(rows),
            "unique":         len(rows),   # PRIMARY KEY гарантирует уникальность
            "avg_confidence": round(avg_conf, 3),
        }

    def stats(self) -> dict:
        """Статистика по всему архиву."""
        with get_connection() as conn:
            return repo.knowledge_query_archive_stats(conn)
