"""
agent/orch_knowledge_writer.py — Knowledge Writer.

Структура хранилища:
  registry/knowledge/
    {uuid8}.jsonl     — одна запись на файл, всё внутри
    index.db          — SQLite индекс для быстрых запросов

Поля записи:
  id, question, answer, trust_level, verdict, topic, tags,
  sources, created_at, updated_at, meta
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

import requests as _requests
_sync_session = _requests.Session()
_sync_session.trust_env = False

from agent.orch_schemas import SynthesisResult, ArbiterResult, TrustLevel

BASE    = Path(__file__).parent.parent
KW_DIR  = BASE / "registry" / "knowledge"
KW_DIR.mkdir(parents=True, exist_ok=True)
IDX_DB  = KW_DIR / "index.db"

VERDICT_TO_TRUST: dict[str, TrustLevel] = {
    "VERIFIED":           "VERIFIED",
    "PARTIALLY_VERIFIED": "HYPOTHESIS",
    "CONFLICT_DETECTED":  "HYPOTHESIS",
    "REJECTED":           "PERSONAL",
}


# ── SQLite индекс ──────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    con = sqlite3.connect(IDX_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id          TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            trust_level TEXT NOT NULL,
            verdict     TEXT,
            topic       TEXT,
            tags        TEXT,
            query       TEXT,
            created_at  TEXT,
            updated_at  TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_trust ON knowledge(trust_level)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_topic ON knowledge(topic)")
    con.commit()
    return con


def _index_record(con: sqlite3.Connection, rec: dict):
    con.execute("""
        INSERT OR REPLACE INTO knowledge
          (id, filename, trust_level, verdict, topic, tags, query, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        rec["id"],
        rec["_filename"],
        rec["trust_level"],
        rec.get("verdict", ""),
        rec.get("topic", "general"),
        json.dumps(rec.get("tags", []), ensure_ascii=False),
        rec.get("question", "")[:500],
        rec.get("created_at", ""),
        rec.get("updated_at", ""),
    ))
    con.commit()


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
    filename   = f"{rec_id}.jsonl"
    now_iso    = time.strftime("%Y-%m-%dT%H:%M:%S")
    topic_real = tags[0] if tags else topic

    record = {
        "id":          rec_id,
        "question":    question,
        "answer":      answer,
        "trust_level": trust,
        "verdict":     verdict,
        "topic":       topic_real,
        "tags":        tags or ([topic] if topic != "general" else []),
        "sources":     sources or [],
        "created_at":  now_iso,
        "updated_at":  now_iso,
        "_filename":   filename,
        **(meta or {}),
    }

    # Файл записи
    (KW_DIR / filename).write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # SQLite индекс
    con = _get_db()
    _index_record(con, record)
    con.close()

    # Синхронизация на пиры (только VERIFIED)
    if trust in ("VERIFIED", "HYPOTHESIS"):
        import threading
        threading.Thread(target=sync_to_peers, args=(record,), daemon=True).start()

    return rec_id


def update_trust_level(rec_id: str, trust_level: str, verdict: str = "") -> bool:
    """Обновить trust_level существующей записи (после перепроверки)."""
    filename = f"{rec_id}.jsonl"
    path = KW_DIR / filename
    if not path.exists():
        return False

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        record["trust_level"] = trust_level
        record["updated_at"]  = now_iso
        if verdict:
            record["verdict"] = verdict
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

        con = _get_db()
        con.execute(
            "UPDATE knowledge SET trust_level=?, verdict=?, updated_at=? WHERE id=?",
            (trust_level, verdict or record.get("verdict",""), now_iso, rec_id),
        )
        con.commit()
        con.close()
        return True
    except Exception:
        return False


# ── Запрос ────────────────────────────────────────────────────────────────────

def get_by_trust(trust_level: str, limit: int = 100) -> list[dict]:
    """Получить записи по статусу (из SQLite, быстро)."""
    con = _get_db()
    rows = con.execute(
        "SELECT id, query, topic, created_at FROM knowledge WHERE trust_level=? ORDER BY created_at DESC LIMIT ?",
        (trust_level, limit),
    ).fetchall()
    con.close()
    return [{"id": r[0], "query": r[1], "topic": r[2], "created_at": r[3]} for r in rows]


def load_record(rec_id: str) -> Optional[dict]:
    """Загрузить полную запись по id."""
    path = KW_DIR / f"{rec_id}.jsonl"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def get_stats() -> dict:
    con = _get_db()
    total = con.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    rows  = con.execute("SELECT trust_level, COUNT(*) FROM knowledge GROUP BY trust_level").fetchall()
    con.close()
    return {"total": total, "by_trust": {r[0]: r[1] for r in rows}}


# ── Миграция старого knowledge.jsonl ─────────────────────────────────────────

def migrate_old(old_file: Path | None = None) -> int:
    """Перенести записи из старого monolith-файла в новый формат."""
    if old_file is None:
        old_file = BASE / "registry" / "verified_knowledge" / "knowledge.jsonl"
    if not old_file.exists():
        return 0

    migrated = 0
    con = _get_db()
    for line in old_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            old = json.loads(line)
            rec_id   = uuid.uuid4().hex[:8]
            filename = f"{rec_id}.jsonl"
            now_iso  = old.get("ts_iso") or time.strftime("%Y-%m-%dT%H:%M:%S")
            record = {
                "id":          rec_id,
                "question":    old.get("question", ""),
                "answer":      old.get("answer", ""),
                "trust_level": old.get("trust_level", "HYPOTHESIS"),
                "verdict":     old.get("verdict", ""),
                "topic":       old.get("topic", "general"),
                "tags":        old.get("tags", []),
                "sources":     old.get("sources", []),
                "created_at":  now_iso,
                "updated_at":  now_iso,
                "_filename":   filename,
            }
            (KW_DIR / filename).write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _index_record(con, record)
            migrated += 1
        except Exception:
            continue
    con.close()
    return migrated


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
    """Загрузить список пиров из registry/peers.json."""
    peers_file = BASE / "registry" / "peers.json"
    if not peers_file.exists():
        return [], "", False
    try:
        data = json.loads(peers_file.read_text(encoding="utf-8"))
        return (
            data.get("peers", []),
            data.get("sync_token", ""),
            data.get("sync_enabled", False),
        )
    except Exception:
        return [], "", False


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
    # Миграция + статистика
    n = migrate_old()
    if n:
        print(f"Мигрировано записей: {n}")
    print("Stats:", get_stats())
