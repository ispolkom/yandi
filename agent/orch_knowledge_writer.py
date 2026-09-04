"""
agent/orch_knowledge_writer.py — Knowledge Writer.

Поля записи:
  id, question, answer, trust_level, verdict, topic, tags,
  sources, created_at, updated_at, meta

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/knowledge/{id}.jsonl
(one file per record) + registry/knowledge/index.db (a separate SQLite
index) + registry/peers.json are retired, not migrated — old files are
disposable test-era cruft. This module has ZERO production callers
(confirmed via grep before this rewrite — nothing imports agent.
orch_knowledge_writer anywhere else in this codebase), so this is a
from-scratch design rather than a faithful port of every old field: the
old record's `**(meta or {})` merge-into-top-level-dict pattern is
replaced by a proper `meta` JSON column. migrate_old() (a one-time
importer from an even older monolith knowledge.jsonl) is DROPPED
entirely, not ported — owner mandate: "переносить ничего не нужно, мы
будем начинать с точки НОЛЬ."

State now lives exclusively in knowledge_record (class C, mutable) and
peer_config (class C, singleton) — agent/db/sql/schema.py.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here. There is no JSON fallback left to quietly succeed against.
"""
from __future__ import annotations

import uuid
from typing import Optional

import requests as _requests
_sync_session = _requests.Session()
_sync_session.trust_env = False

from agent.orch_schemas import SynthesisResult, ArbiterResult, TrustLevel
from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo

VERDICT_TO_TRUST: dict[str, TrustLevel] = {
    "VERIFIED":           "VERIFIED",
    "PARTIALLY_VERIFIED": "HYPOTHESIS",
    "CONFLICT_DETECTED":  "HYPOTHESIS",
    "REJECTED":           "PERSONAL",
}


# ── Запись ────────────────────────────────────────────────────────────────────

def write_knowledge(
    question: str,
    answer: str,
    verdict: str,
    topic: str = "general",
    tags: list[str] | None = None,
    sources: list[str] | None = None,
    meta: dict | None = None,
) -> Optional[str]:
    """
    Записать знание в реестр.
    Возвращает id записи или None если пропущено (REJECTED).
    """
    if verdict == "REJECTED":
        return None

    trust      = VERDICT_TO_TRUST.get(verdict, "HYPOTHESIS")
    rec_id     = uuid.uuid4().hex[:8]
    topic_real = tags[0] if tags else topic
    record_tags = tags or ([topic] if topic != "general" else [])
    record_sources = sources or []

    with get_connection() as conn:
        repo.upsert_knowledge_record(
            conn, rec_id, question, answer, trust, verdict=verdict, topic=topic_real,
            tags=record_tags, sources=record_sources, meta=meta or {},
        )
        conn.commit()

    record = {
        "id": rec_id, "question": question, "answer": answer, "trust_level": trust,
        "verdict": verdict, "topic": topic_real, "tags": record_tags, "sources": record_sources,
        **(meta or {}),
    }

    # Синхронизация на пиры (только VERIFIED/HYPOTHESIS)
    if trust in ("VERIFIED", "HYPOTHESIS"):
        import threading
        threading.Thread(target=sync_to_peers, args=(record,), daemon=True).start()

    return rec_id


def update_trust_level(rec_id: str, trust_level: str, verdict: str = "") -> bool:
    """Обновить trust_level существующей записи (после перепроверки)."""
    with get_connection() as conn:
        updated = repo.update_knowledge_record_trust(conn, rec_id, trust_level, verdict=verdict or None)
        conn.commit()
    return updated


# ── Запрос ────────────────────────────────────────────────────────────────────

def get_by_trust(trust_level: str, limit: int = 100) -> list[dict]:
    """Получить записи по статусу."""
    with get_connection() as conn:
        rows = repo.list_knowledge_by_trust(conn, trust_level, limit=limit)
    return [{"id": r["record_id"], "query": r["question"], "topic": r["topic"], "created_at": r["created_at"]} for r in rows]


def load_record(rec_id: str) -> Optional[dict]:
    """Загрузить полную запись по id."""
    with get_connection() as conn:
        row = repo.get_knowledge_record(conn, rec_id)
    if not row:
        return None
    return {
        "id": row["record_id"], "question": row["question"], "answer": row["answer"],
        "trust_level": row["trust_level"], "verdict": row["verdict"], "topic": row["topic"],
        "tags": row.get("tags") or [], "sources": row.get("sources") or [],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        **(row.get("meta") or {}),
    }


def get_stats() -> dict:
    with get_connection() as conn:
        return repo.get_knowledge_stats(conn)


# ── Обратная совместимость ────────────────────────────────────────────────────

def write_from_arbiter(
    question: str,
    synthesis: SynthesisResult,
    arbiter: ArbiterResult,
    topic: str = "general",
) -> Optional[str]:
    final_answer = arbiter.final_answer or synthesis.answer
    return write_knowledge(
        question=question,
        answer=final_answer,
        verdict=arbiter.verdict,
        topic=topic,
        sources=synthesis.sources,
        meta={"original_confidence": synthesis.confidence},
    )


def _load_peers() -> tuple[list[str], str, bool]:
    """Загрузить список пиров из peer_config."""
    with get_connection() as conn:
        cfg = repo.get_or_create_peer_config(conn)
        conn.commit()
    return cfg.get("peers") or [], cfg.get("sync_token") or "", bool(cfg.get("sync_enabled"))


def sync_to_peers(record: dict) -> list[str]:
    """Отправить верифицированное знание на все известные пиры. Возвращает список успешных."""
    peers, token, enabled = _load_peers()
    if not enabled or not peers:
        return []

    ok = []
    payload = {"record": record, "token": token}
    for peer_url in peers:
        try:
            r = _sync_session.post(
                f"{peer_url.rstrip('/')}/api/knowledge/sync",
                json=payload,
                timeout=10,
            )
            if r.status_code == 200:
                ok.append(peer_url)
                print(f"📤 KNOWLEDGE_SYNC → {peer_url} [{record['id']}] {record.get('trust_level','?')}")
        except Exception as e:
            print(f"⚠️ SYNC_FAIL → {peer_url}: {e}")
    return ok


if __name__ == "__main__":
    print("Stats:", get_stats())
