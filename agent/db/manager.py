"""
agent/db/manager.py — Единая точка доступа к knowledge и traces базам.

Использование:
    from agent.db.manager import KnowledgeDB
    db = KnowledgeDB()
    db.save_knowledge(id, query, answer, tag, trust_level, sources)
    db.save_trace(id, question, steps, verdict, model_chain)
    rows = db.gold_dataset()  # verified knowledge + traces
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.db.schema import (
    INDEX_SCHEMA, KNOWLEDGE_SCHEMA, TRACES_SCHEMA, SCHEMA_VERSION
)

PROJECT_ROOT  = Path(__file__).parent.parent.parent   # yandi/
REGISTRY_DIR  = PROJECT_ROOT / "registry"
INDEX_DB      = REGISTRY_DIR / "index.db"
KNOWLEDGE_DIR = REGISTRY_DIR / "knowledge"
TRACES_DIR    = REGISTRY_DIR / "traces"


def make_id(query: str) -> str:
    """Детерминированный id из текста вопроса."""
    return hashlib.md5(query.lower().strip().encode()).hexdigest()[:8]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _category(tag: str) -> str:
    """science:astronomy → science"""
    return tag.split(":")[0].strip() or "general"


@contextmanager
def _conn(path: Path, schema: str):
    """Открыть соединение, применить схему если нужно, закрыть."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        con.executescript(schema)
        # Записать версию схемы если ещё нет
        con.execute(
            "INSERT OR IGNORE INTO schema_version(version, description) VALUES (?, ?)",
            (SCHEMA_VERSION, "initial schema")
        )
        con.commit()
        yield con
        con.commit()
    finally:
        con.close()


class KnowledgeDB:
    """Основной интерфейс к базам знаний и трейсов."""

    # ── Knowledge ─────────────────────────────────────────────────────────────

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
        rid      = entry_id or make_id(query)
        cat      = _category(tag)
        db_path  = KNOWLEDGE_DIR / f"{cat}.db"
        now      = _now()
        src_json = json.dumps(sources or [], ensure_ascii=False)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)

        with _conn(db_path, KNOWLEDGE_SCHEMA) as con:
            existing = con.execute(
                "SELECT version FROM knowledge WHERE id = ?", (rid,)
            ).fetchone()

            if existing:
                # Обновляем только если новый trust_level выше или confidence выше
                con.execute("""
                    UPDATE knowledge SET
                        answer = ?, trust_level = ?, confidence = ?,
                        sources = ?, node_id = ?, version = version + 1,
                        meta = ?, updated_at = ?
                    WHERE id = ? AND (
                        trust_level != 'VERIFIED' OR ? = 'VERIFIED'
                    )
                """, (answer, trust_level, confidence, src_json,
                      node_id, meta_json, now, rid, trust_level))
            else:
                con.execute("""
                    INSERT INTO knowledge
                        (id, query, answer, tag, trust_level, confidence,
                         sources, node_id, meta, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (rid, query, answer, tag, trust_level, confidence,
                      src_json, node_id, meta_json, now, now))

        self._index_knowledge(rid, tag, cat, trust_level, node_id, now)
        return rid

    def get_knowledge(self, query: str) -> dict | None:
        """Найти ответ по тексту вопроса."""
        rid = make_id(query)
        return self.get_by_id(rid)

    def get_by_id(self, rid: str) -> dict | None:
        """Найти ответ по id."""
        row = self._index_row(rid)
        if not row:
            return None
        db_path = KNOWLEDGE_DIR / f"{row['category']}.db"
        if not db_path.exists():
            return None
        with _conn(db_path, KNOWLEDGE_SCHEMA) as con:
            r = con.execute(
                "SELECT * FROM knowledge WHERE id = ?", (rid,)
            ).fetchone()
            return dict(r) if r else None

    def verify(self, rid: str) -> bool:
        """Поставить отметку VERIFIED на запись."""
        row = self._index_row(rid)
        if not row:
            return False
        db_path = KNOWLEDGE_DIR / f"{row['category']}.db"
        now = _now()
        with _conn(db_path, KNOWLEDGE_SCHEMA) as con:
            con.execute(
                "UPDATE knowledge SET trust_level='VERIFIED', updated_at=? WHERE id=?",
                (now, rid)
            )
        with _conn(INDEX_DB, INDEX_SCHEMA) as con:
            con.execute(
                "UPDATE knowledge_index SET trust_level='VERIFIED', updated_at=? WHERE id=?",
                (now, rid)
            )
        return True

    # ── Traces ────────────────────────────────────────────────────────────────

    def save_trace(
        self,
        entry_id: str,
        question: str,
        steps: list[dict],
        verdict: str = "UNVERIFIED",
        model_chain: str = "",
        tag: str = "general",
        node_id: str = "",
        meta: dict | None = None,
    ) -> None:
        """Сохранить трейс цепочки рассуждений."""
        cat      = _category(tag)
        db_path  = TRACES_DIR / f"{cat}.db"
        now      = _now()

        with _conn(db_path, TRACES_SCHEMA) as con:
            con.execute("""
                INSERT OR REPLACE INTO traces
                    (id, question, steps, verdict, model_chain, node_id, meta, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry_id, question,
                json.dumps(steps, ensure_ascii=False),
                verdict, model_chain, node_id,
                json.dumps(meta or {}, ensure_ascii=False),
                now,
            ))

        self._index_trace(entry_id, tag, cat, verdict, model_chain, node_id, now)

    def get_trace(self, entry_id: str) -> dict | None:
        """Получить трейс по id."""
        row = self._trace_index_row(entry_id)
        if not row:
            return None
        db_path = TRACES_DIR / f"{row['category']}.db"
        if not db_path.exists():
            return None
        with _conn(db_path, TRACES_SCHEMA) as con:
            r = con.execute(
                "SELECT * FROM traces WHERE id = ?", (entry_id,)
            ).fetchone()
            if not r:
                return None
            d = dict(r)
            d["steps"] = json.loads(d["steps"])
            return d

    # ── Gold dataset ──────────────────────────────────────────────────────────

    def gold_dataset(self, category: str | None = None) -> list[dict]:
        """
        Золотой датасет: VERIFIED knowledge + соответствующие трейсы.
        Если category=None — по всем категориям.
        """
        categories = [category] if category else self._all_categories()
        result = []

        for cat in categories:
            k_path = KNOWLEDGE_DIR / f"{cat}.db"
            t_path = TRACES_DIR    / f"{cat}.db"
            if not k_path.exists():
                continue

            with _conn(k_path, KNOWLEDGE_SCHEMA) as kcon:
                rows = kcon.execute(
                    "SELECT * FROM knowledge WHERE trust_level = 'VERIFIED'"
                ).fetchall()

            for row in rows:
                item = dict(row)
                item["sources"] = json.loads(item["sources"])
                item["meta"]    = json.loads(item["meta"])
                item["trace"]   = None

                if t_path.exists():
                    with _conn(t_path, TRACES_SCHEMA) as tcon:
                        t = tcon.execute(
                            "SELECT * FROM traces WHERE id = ?", (item["id"],)
                        ).fetchone()
                        if t:
                            td = dict(t)
                            td["steps"] = json.loads(td["steps"])
                            item["trace"] = td

                result.append(item)

        return result

    def list_unverified(self, limit: int = 30) -> list[dict]:
        """Список неверифицированных записей для review queue."""
        cats = self._all_categories()
        result = []
        for cat in cats:
            k_path = KNOWLEDGE_DIR / f"{cat}.db"
            if not k_path.exists():
                continue
            with _conn(k_path, KNOWLEDGE_SCHEMA) as con:
                rows = con.execute("""
                    SELECT id, query, answer, tag, confidence, created_at
                    FROM knowledge
                    WHERE trust_level != 'VERIFIED'
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit - len(result),)).fetchall()
            for row in rows:
                result.append(dict(row))
            if len(result) >= limit:
                break
        return result

    def delete(self, rid: str) -> bool:
        """Удалить запись из knowledge и индекса."""
        row = self._index_row(rid)
        if not row:
            return False
        db_path = KNOWLEDGE_DIR / f"{row['category']}.db"
        if db_path.exists():
            with _conn(db_path, KNOWLEDGE_SCHEMA) as con:
                con.execute("DELETE FROM knowledge WHERE id = ?", (rid,))
        with _conn(INDEX_DB, INDEX_SCHEMA) as con:
            con.execute("DELETE FROM knowledge_index WHERE id = ?", (rid,))
        return True

    def update_answer(self, rid: str, answer: str, trust_level: str = "VERIFIED") -> bool:
        """Обновить ответ и установить trust_level."""
        row = self._index_row(rid)
        if not row:
            return False
        db_path = KNOWLEDGE_DIR / f"{row['category']}.db"
        now = _now()
        with _conn(db_path, KNOWLEDGE_SCHEMA) as con:
            con.execute("""
                UPDATE knowledge
                SET answer=?, trust_level=?, version=version+1, updated_at=?
                WHERE id=?
            """, (answer, trust_level, now, rid))
        with _conn(INDEX_DB, INDEX_SCHEMA) as con:
            con.execute(
                "UPDATE knowledge_index SET trust_level=?, updated_at=? WHERE id=?",
                (trust_level, now, rid)
            )
        return True

    def list_by_tag(
        self,
        tag: str,
        limit: int = 100,
        min_confidence: float = 0.0,
    ) -> list[dict]:
        """Список записей по тегу (точный тег или все теги категории)."""
        cat = _category(tag)
        db_path = KNOWLEDGE_DIR / f"{cat}.db"
        if not db_path.exists():
            return []
        with _conn(db_path, KNOWLEDGE_SCHEMA) as con:
            rows = con.execute("""
                SELECT * FROM knowledge
                WHERE (tag = ? OR tag LIKE ?)
                  AND confidence >= ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (tag, f"{tag}:%", min_confidence, limit)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["sources"] = json.loads(d["sources"])
            d["meta"]    = json.loads(d["meta"])
            result.append(d)
        return result

    def list_tags(self) -> list[str]:
        """Все теги из индекса."""
        if not INDEX_DB.exists():
            return []
        with _conn(INDEX_DB, INDEX_SCHEMA) as con:
            rows = con.execute(
                "SELECT DISTINCT tag FROM knowledge_index ORDER BY tag"
            ).fetchall()
        return [r["tag"] for r in rows]

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
        """Статистика по всем базам."""
        cats = self._all_categories()
        total_k = total_t = verified = 0

        for cat in cats:
            k_path = KNOWLEDGE_DIR / f"{cat}.db"
            t_path = TRACES_DIR    / f"{cat}.db"
            if k_path.exists():
                with _conn(k_path, KNOWLEDGE_SCHEMA) as con:
                    total_k  += con.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
                    verified += con.execute(
                        "SELECT COUNT(*) FROM knowledge WHERE trust_level='VERIFIED'"
                    ).fetchone()[0]
            if t_path.exists():
                with _conn(t_path, TRACES_SCHEMA) as con:
                    total_t += con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]

        return {
            "categories":  cats,
            "knowledge":   total_k,
            "verified":    verified,
            "traces":      total_t,
            "gold_pairs":  min(verified, total_t),
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _index_knowledge(self, rid, tag, cat, trust_level, node_id, now):
        with _conn(INDEX_DB, INDEX_SCHEMA) as con:
            con.execute("""
                INSERT INTO knowledge_index
                    (id, tag, category, trust_level, node_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    trust_level = excluded.trust_level,
                    updated_at  = excluded.updated_at
            """, (rid, tag, cat, trust_level, node_id, now, now))

    def _index_trace(self, rid, tag, cat, verdict, model_chain, node_id, now):
        with _conn(INDEX_DB, INDEX_SCHEMA) as con:
            con.execute("""
                INSERT OR REPLACE INTO traces_index
                    (id, tag, category, verdict, model_chain, node_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (rid, tag, cat, verdict, model_chain, node_id, now))

    def _index_row(self, rid: str) -> sqlite3.Row | None:
        if not INDEX_DB.exists():
            return None
        with _conn(INDEX_DB, INDEX_SCHEMA) as con:
            return con.execute(
                "SELECT * FROM knowledge_index WHERE id = ?", (rid,)
            ).fetchone()

    def _trace_index_row(self, rid: str) -> sqlite3.Row | None:
        if not INDEX_DB.exists():
            return None
        with _conn(INDEX_DB, INDEX_SCHEMA) as con:
            return con.execute(
                "SELECT * FROM traces_index WHERE id = ?", (rid,)
            ).fetchone()

    def _all_categories(self) -> list[str]:
        cats = set()
        for d in (KNOWLEDGE_DIR, TRACES_DIR):
            if d.exists():
                cats.update(p.stem for p in d.glob("*.db"))
        return sorted(cats)
