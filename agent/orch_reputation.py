"""
assistant/orch_reputation.py — Reputation Tracker.
Хранит историю точности нод по доменам. SQLite + JSONL.
Распределённая синхронизация: публикует обновления в Redis,
принимает обновления от других инстансов оркестратора.
"""
from __future__ import annotations

import json
import sqlite3
import time
import threading
from pathlib import Path
from typing import Optional

BASE     = Path(__file__).parent.parent
REP_DIR  = BASE / "registry" / "nodes"
REP_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE  = REP_DIR / "reputation.db"
LOG_FILE = REP_DIR / "reputation_log.jsonl"

# Redis-канал для распределённой репутации
REDIS_REP_CH  = "orch:reputation:updates"
REDIS_HOST    = "127.0.0.1"
REDIS_PORT    = 6379

# Глобальный слушатель (запускается один раз)
_listener_started = False
_listener_lock    = threading.Lock()


def _try_redis():
    """Получить Redis-клиент или None если недоступен."""
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def _publish_reputation_update(node_id: str, correct: bool, latency: float, domain: str):
    """Опубликовать обновление репутации в Redis (best-effort)."""
    r = _try_redis()
    if not r:
        return
    try:
        payload = json.dumps({
            "node_id": node_id, "correct": correct,
            "latency": latency, "domain": domain,
            "ts": time.time(), "source": "local",
        })
        r.publish(REDIS_REP_CH, payload)
    except Exception:
        pass
    finally:
        try:
            r.close()
        except Exception:
            pass


def _apply_remote_update(data: dict):
    """Применить обновление репутации от удалённого инстанса."""
    node_id = data.get("node_id", "")
    correct = bool(data.get("correct", False))
    latency = float(data.get("latency", 10.0))
    domain  = data.get("domain", "general")
    if not node_id:
        return
    # Применить как локальное обновление (без повторной публикации)
    _update_node_local(node_id, correct, latency, domain)


def _start_listener_daemon():
    """Запустить фоновый daemon для получения репутации от других нод."""
    global _listener_started
    with _listener_lock:
        if _listener_started:
            return
        _listener_started = True

    def _listen():
        while True:
            try:
                import redis
                r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
                pub = r.pubsub()
                pub.subscribe(REDIS_REP_CH)
                for msg in pub.listen():
                    if msg["type"] != "message":
                        continue
                    try:
                        data = json.loads(msg["data"])
                        if data.get("source") != "local":  # только чужие обновления
                            _apply_remote_update(data)
                    except Exception:
                        pass
            except Exception:
                time.sleep(30)  # Redis недоступен — подождать

    t = threading.Thread(target=_listen, daemon=True, name="rep-sync-listener")
    t.start()

DOMAINS = [
    "cooking", "medical", "legal", "financial",
    "coding", "science", "tech", "ai_ml", "general",
]


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_FILE))
    c.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
            node_id      TEXT PRIMARY KEY,
            model        TEXT,
            endpoint     TEXT,
            total        INTEGER DEFAULT 0,
            correct      INTEGER DEFAULT 0,
            reputation   REAL    DEFAULT 0.7,
            speed_avg    REAL    DEFAULT 10.0,
            updated_at   REAL    DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS domain_scores (
            node_id  TEXT,
            domain   TEXT,
            total    INTEGER DEFAULT 0,
            correct  INTEGER DEFAULT 0,
            score    REAL    DEFAULT 0.7,
            PRIMARY KEY (node_id, domain)
        )
    """)
    c.commit()
    return c


def register_node(node_id: str, model: str, endpoint: str):
    """Зарегистрировать ноду (если ещё нет)."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO nodes (node_id, model, endpoint, updated_at) VALUES (?,?,?,?)",
            (node_id, model, endpoint, time.time()),
        )


def _update_node_local(node_id: str, correct: bool, latency: float, domain: str = "general"):
    """Обновить репутацию ноды в локальной SQLite (без Redis-публикации)."""
    ts = time.time()
    with _conn() as c:
        row = c.execute("SELECT total, correct, reputation, speed_avg FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        if not row:
            # Авто-регистрация неизвестной ноды
            c.execute(
                "INSERT OR IGNORE INTO nodes (node_id, model, endpoint, updated_at) VALUES (?,?,?,?)",
                (node_id, "unknown", "unknown", ts),
            )
            row = (0, 0, 0.7, 10.0)
        total, corr, rep, speed = row
        total += 1
        corr  += 1 if correct else 0
        rep    = round(corr / total, 3)
        speed  = round(speed * 0.8 + latency * 0.2, 2)
        c.execute(
            "UPDATE nodes SET total=?, correct=?, reputation=?, speed_avg=?, updated_at=? WHERE node_id=?",
            (total, corr, rep, speed, ts, node_id),
        )
        c.execute("INSERT OR IGNORE INTO domain_scores (node_id, domain) VALUES (?,?)", (node_id, domain))
        dr = c.execute(
            "SELECT total, correct FROM domain_scores WHERE node_id=? AND domain=?",
            (node_id, domain),
        ).fetchone()
        dtotal = (dr[0] if dr else 0) + 1
        dcorr  = (dr[1] if dr else 0) + (1 if correct else 0)
        c.execute(
            "UPDATE domain_scores SET total=?, correct=?, score=? WHERE node_id=? AND domain=?",
            (dtotal, dcorr, round(dcorr / dtotal, 3), node_id, domain),
        )

    with open(LOG_FILE, "a") as f:
        f.write(json.dumps({
            "node_id": node_id, "correct": correct,
            "latency": latency, "domain": domain, "ts": ts,
        }) + "\n")


def update_node(node_id: str, correct: bool, latency: float, domain: str = "general"):
    """Обновить репутацию ноды после валидации + синхронизировать через Redis."""
    _update_node_local(node_id, correct, latency, domain)
    _publish_reputation_update(node_id, correct, latency, domain)
    # Запустить слушатель при первом обновлении (lazy init)
    _start_listener_daemon()


def get_node_score(node_id: str, domain: str = "general") -> dict:
    """Получить скоринг ноды."""
    with _conn() as c:
        row = c.execute("SELECT reputation, speed_avg FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        if not row:
            return {"reputation": 0.7, "domain_score": 0.7, "speed": 10.0, "composite": 0.7}
        rep, speed = row
        dr = c.execute("SELECT score FROM domain_scores WHERE node_id=? AND domain=?", (node_id, domain)).fetchone()
        domain_score = dr[0] if dr else rep
        composite = round(rep * 0.5 + domain_score * 0.3 + min(1.0, 5.0 / max(speed, 1)) * 0.2, 3)
        return {"reputation": rep, "domain_score": domain_score, "speed": speed, "composite": composite}


def get_best_nodes(domain: str = "general", n: int = 3) -> list[dict]:
    """Получить топ-N нод по composite score для данного домена."""
    with _conn() as c:
        rows = c.execute("""
            SELECT n.node_id, n.model, n.endpoint, n.reputation, n.speed_avg,
                   COALESCE(d.score, n.reputation) as domain_score
            FROM nodes n
            LEFT JOIN domain_scores d ON n.node_id = d.node_id AND d.domain = ?
            ORDER BY (n.reputation * 0.5 + COALESCE(d.score, n.reputation) * 0.3) DESC
            LIMIT ?
        """, (domain, n)).fetchall()
        return [
            {"node_id": r[0], "model": r[1], "endpoint": r[2],
             "reputation": r[3], "speed": r[4], "domain_score": r[5]}
            for r in rows
        ]


def list_nodes() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT node_id, model, endpoint, reputation, total, speed_avg FROM nodes").fetchall()
        return [{"node_id":r[0],"model":r[1],"endpoint":r[2],"reputation":r[3],"total":r[4],"speed":r[5]}
                for r in rows]


if __name__ == "__main__":
    # Регистрируем тестовые ноды
    register_node("local-qwen14b-a", "qwen3:14b", "http://127.0.0.1:11434")
    register_node("local-qwen14b-b", "qwen3:14b", "http://127.0.0.1:11434")
    register_node("local-deepseek",  "deepseek-r1:14b", "http://127.0.0.1:11434")

    update_node("local-qwen14b-a", correct=True,  latency=8.0,  domain="ai_ml")
    update_node("local-qwen14b-b", correct=True,  latency=12.0, domain="ai_ml")
    update_node("local-deepseek",  correct=False, latency=15.0, domain="ai_ml")

    print("Все ноды:")
    for n in list_nodes():
        print(f"  {n['node_id']}: rep={n['reputation']:.2f} total={n['total']} speed={n['speed']:.1f}s")

    print("\nЛучшие ноды для ai_ml:")
    for n in get_best_nodes("ai_ml"):
        print(f"  {n['node_id']}: domain_score={n['domain_score']:.2f}")
