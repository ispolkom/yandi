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

# ── Relationship state machine ──────────────────────────────────────────────
#
# Owner's own framing (2026-09-13): a node's standing with another node
# should not be only a slowly-drifting accuracy percentage — a percentage
# can be eroded by many small negatives with no single moment that ever
# crosses an alarm threshold, and it forgets old betrayals exactly as fast
# as it forgets old good behavior. Real trust has asymmetric memory:
# betrayal is remembered far longer than it takes to cause, and earning
# your way back out of distrust costs much more than falling into it did.
#
# This adds a THIRD dimension alongside the existing accuracy/reputation
# score: a qualitative relationship STATE (NEUTRAL / LOVE / HATE) with its
# own accumulated, asymmetric evidence — not a replacement for the
# accuracy score, a companion to it.
REL_NEUTRAL = "neutral"
REL_LOVE = "love"
REL_HATE = "hate"

# How much accumulated positive evidence it takes to earn LOVE from
# NEUTRAL. Deliberately large — this must reflect sustained good behavior,
# never a single interaction.
LOVE_ENTER_THRESHOLD = 15.0

# How much accumulated negative evidence it takes to fall into HATE from
# NEUTRAL. Lower than LOVE_ENTER_THRESHOLD on purpose: distrust is
# reasonable to develop faster than deep trust — the classic asymmetry
# between "quick to distrust, slow to trust deeply".
HATE_ENTER_THRESHOLD = 8.0

# A node that was LOVEd and then betrays falls into HATE more easily than
# one that was already neutral — betrayal from someone trusted cuts
# deeper. Applied as a multiplier on HATE_ENTER_THRESHOLD while in LOVE.
LOVE_BETRAYAL_MULTIPLIER = 0.5

# How much accumulated positive evidence, earned AFTER falling into HATE,
# it takes to climb back out to NEUTRAL. Deliberately much larger than
# HATE_ENTER_THRESHOLD — redemption costs more than the betrayal did, and
# HATE can only ever soften back to NEUTRAL, never straight to LOVE: love
# has to be re-earned separately from a clean, neutral start.
HATE_EXIT_THRESHOLD = 25.0

# LOVE fades slowly from pure inactivity (no interaction at all) — even a
# good relationship needs continued contact — but HATE does NOT decay on
# its own with time; a grudge doesn't evaporate just because nothing new
# happened. Points per day of total silence.
LOVE_DECAY_PER_DAY = 0.05


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
    c.execute("""
        CREATE TABLE IF NOT EXISTS relationship (
            node_id       TEXT PRIMARY KEY,
            state         TEXT    DEFAULT 'neutral',
            love_score    REAL    DEFAULT 0.0,
            hate_score    REAL    DEFAULT 0.0,
            entered_at    REAL    DEFAULT 0,
            last_event_at REAL    DEFAULT 0
        )
    """)
    c.commit()
    return c


def _evaluate_transition(state: str, love_score: float, hate_score: float) -> str:
    """Pure state-transition function — no I/O, easy to test exhaustively.

    Encodes the asymmetry: entering HATE is easier than entering LOVE;
    leaving HATE is harder than entering it; HATE never softens straight
    into LOVE, only back to NEUTRAL.
    """
    if state == REL_LOVE:
        if hate_score >= HATE_ENTER_THRESHOLD * LOVE_BETRAYAL_MULTIPLIER:
            return REL_HATE
        return REL_LOVE
    if state == REL_HATE:
        if love_score >= HATE_EXIT_THRESHOLD:
            return REL_NEUTRAL
        return REL_HATE
    # NEUTRAL
    if hate_score >= HATE_ENTER_THRESHOLD:
        return REL_HATE
    if love_score >= LOVE_ENTER_THRESHOLD:
        return REL_LOVE
    return REL_NEUTRAL


def record_relationship_signal(node_id: str, positive: bool, severity: float = 1.0) -> dict:
    """Feed one interaction's outcome into the relationship state machine.

    `severity` scales the weight of this one signal — an ordinary
    right/wrong answer should stay near 1.0; a confirmed severe violation
    (proven deception, an attack, a broken protocol guarantee) should be
    called with a much higher severity so it can matter immediately rather
    than needing many repeats to add up. Returns the relationship row
    after the update (state may or may not have changed).
    """
    ts = time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT state, love_score, hate_score, entered_at, last_event_at FROM relationship WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if row:
            state, love_score, hate_score, entered_at, last_event_at = row
        else:
            state, love_score, hate_score, entered_at, last_event_at = REL_NEUTRAL, 0.0, 0.0, ts, ts

        # LOVE fades with pure inactivity; HATE never does on its own.
        if state == REL_LOVE and last_event_at:
            idle_days = max(0.0, (ts - last_event_at) / 86400.0)
            love_score = max(0.0, love_score - LOVE_DECAY_PER_DAY * idle_days)

        if positive:
            love_score += severity
        else:
            hate_score += severity

        new_state = _evaluate_transition(state, love_score, hate_score)

        if new_state != state:
            # Crossing into a new state resets BOTH accumulators — the
            # evidence that caused this transition has done its job; the
            # next transition (in either direction) needs its own fresh
            # evidence, not leftover momentum from the last one.
            love_score = 0.0
            hate_score = 0.0
            entered_at = ts

        c.execute(
            """
            INSERT INTO relationship (node_id, state, love_score, hate_score, entered_at, last_event_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET
                state=excluded.state, love_score=excluded.love_score,
                hate_score=excluded.hate_score, entered_at=excluded.entered_at,
                last_event_at=excluded.last_event_at
            """,
            (node_id, new_state, love_score, hate_score, entered_at, ts),
        )

        return {
            "node_id": node_id, "state": new_state,
            "love_score": round(love_score, 3), "hate_score": round(hate_score, 3),
            "entered_at": entered_at,
        }


def record_severe_violation(node_id: str, reason: str, severity: float = 4.0) -> dict:
    """Explicit entry point for confirmed serious misconduct (proven lie,
    attack, broken guarantee) — distinct from an ordinary wrong answer.
    Default severity is well above HATE_ENTER_THRESHOLD/2 so a single
    confirmed violation can matter on its own, without needing repeats."""
    result = record_relationship_signal(node_id, positive=False, severity=severity)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps({
            "node_id": node_id, "event": "severe_violation", "reason": reason,
            "severity": severity, "ts": time.time(),
        }) + "\n")
    return result


def get_relationship(node_id: str) -> dict:
    """Current relationship state for a node — NEUTRAL if never recorded."""
    with _conn() as c:
        row = c.execute(
            "SELECT state, love_score, hate_score, entered_at FROM relationship WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if not row:
            return {"node_id": node_id, "state": REL_NEUTRAL, "love_score": 0.0, "hate_score": 0.0, "entered_at": None}
        state, love_score, hate_score, entered_at = row
        return {"node_id": node_id, "state": state, "love_score": love_score, "hate_score": hate_score, "entered_at": entered_at}


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

    # Ordinary interactions feed the relationship state too, at a low
    # weight (0.3) — LOVE/HATE should mostly come from either sustained
    # patterns over many interactions or an explicit severe violation
    # (record_severe_violation, weight 4.0+), never from being merely
    # wrong a handful of times.
    record_relationship_signal(node_id, positive=correct, severity=0.3)


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

def add_decision_event(event_type: str, trace_id: str, entity_type: str, entity_id: str,
                       verdict: str, reason: str = "", domain: str = "general",
                       confidence: float = 0.0, delta: float = 0.0,
                       delta_factors: dict = None, meta: dict = None):
    """Заглушка для совместимости с Decision Ledger."""
    pass


def get_trace(trace_id: str) -> dict:
    """Заглушка для совместимости."""
    return {}


def get_ledger() -> dict:
    """Заглушка для совместимости."""
    return {}
