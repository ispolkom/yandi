"""
assistant/orch_ledger.py — Decision Ledger V4 (финальная версия).

Теперь:
  - DecisionFinished (SUCCESS | FAILED | CACHED | CANCELLED)
  - duration_ms для каждого шага + total_duration_ms
  - delta_factors заполняются
  - policy_snapshot (порог, web, fallback)
  - orchestrator_version, registry_version
  - stats аналитический (среднее время, % Cache HIT, % VERIFIED, лучшие маршруты/модели/источники)
  - timeline <trace_id> — компактный вывод

CLI:
  python3 assistant/orch_ledger.py stats
  python3 assistant/orch_ledger.py show <trace_id>
  python3 assistant/orch_ledger.py timeline <trace_id>
  python3 assistant/orch_ledger.py events [N]
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
import sys
import uuid

BASE = Path(__file__).parent.parent
LEDGER_DIR = BASE / "registry" / "ledger"
LEDGER_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = LEDGER_DIR / "decision_ledger.db"
EVENTS_LOG = LEDGER_DIR / "events.jsonl"

ORCHESTRATOR_VERSION = "v2.0"
REGISTRY_VERSION = "174"

POLICY_VERSION = "RegistryFirstPolicy v1"


class DecisionEvent:
    def __init__(
        self,
        event_type: str,
        trace_id: str,
        entity_type: str,
        entity_id: str,
        verdict: str = "",
        confidence: float = 0.0,
        delta: float = 0.0,
        delta_factors: Optional[Dict[str, float]] = None,
        reason: str = "",
        domain: str = "general",
        parent_event_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        total_duration_ms: Optional[int] = None,
        policy_snapshot: Optional[Dict[str, Any]] = None,
        meta: Optional[Dict[str, Any]] = None,
    ):
        self.event_id = uuid.uuid4().hex
        self.event_type = event_type
        self.trace_id = trace_id
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.verdict = verdict
        self.confidence = confidence
        self.delta = round(delta, 4)
        self.delta_factors = delta_factors or {}
        self.reason = reason
        self.domain = domain
        self.parent_event_id = parent_event_id
        self.duration_ms = duration_ms
        self.total_duration_ms = total_duration_ms
        self.policy_snapshot = policy_snapshot or {}
        self.meta = meta or {}
        self.timestamp = time.time()
        self.policy_version = POLICY_VERSION
        self.orchestrator_version = ORCHESTRATOR_VERSION
        self.registry_version = REGISTRY_VERSION

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "delta": self.delta,
            "delta_factors": self.delta_factors,
            "reason": self.reason,
            "domain": self.domain,
            "parent_event_id": self.parent_event_id,
            "duration_ms": self.duration_ms,
            "total_duration_ms": self.total_duration_ms,
            "policy_snapshot": self.policy_snapshot,
            "policy_version": self.policy_version,
            "orchestrator_version": self.orchestrator_version,
            "registry_version": self.registry_version,
            "meta": self.meta,
        }


class DecisionLedger:
    def __init__(self):
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(DB_FILE)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reputation_current (
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    score REAL DEFAULT 0.0,
                    trust_level TEXT DEFAULT 'UNVERIFIED',
                    domain TEXT DEFAULT 'general',
                    last_update REAL DEFAULT 0,
                    policy_version TEXT DEFAULT '',
                    PRIMARY KEY (entity_type, entity_id, domain)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decision_events (
                    event_id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    domain TEXT DEFAULT 'general',
                    verdict TEXT DEFAULT '',
                    confidence REAL DEFAULT 0.0,
                    delta REAL DEFAULT 0.0,
                    delta_factors TEXT DEFAULT '{}',
                    reason TEXT NOT NULL,
                    parent_event_id TEXT DEFAULT '',
                    duration_ms INTEGER DEFAULT 0,
                    total_duration_ms INTEGER DEFAULT 0,
                    policy_snapshot TEXT DEFAULT '{}',
                    policy_version TEXT DEFAULT '',
                    orchestrator_version TEXT DEFAULT '',
                    registry_version TEXT DEFAULT '',
                    meta TEXT DEFAULT '{}'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trace ON decision_events(trace_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity ON decision_events(entity_type, entity_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_parent ON decision_events(parent_event_id)")
            conn.commit()

    def add_event(self, event: DecisionEvent):
        with open(EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

        with sqlite3.connect(str(DB_FILE)) as conn:
            conn.execute(
                """
                INSERT INTO decision_events
                (event_id, timestamp, event_type, trace_id, entity_type, entity_id,
                 domain, verdict, confidence, delta, delta_factors, reason, parent_event_id,
                 duration_ms, total_duration_ms, policy_snapshot, policy_version,
                 orchestrator_version, registry_version, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.timestamp,
                    event.event_type,
                    event.trace_id,
                    event.entity_type,
                    event.entity_id,
                    event.domain,
                    event.verdict,
                    event.confidence,
                    event.delta,
                    json.dumps(event.delta_factors, ensure_ascii=False),
                    event.reason,
                    event.parent_event_id or "",
                    event.duration_ms or 0,
                    event.total_duration_ms or 0,
                    json.dumps(event.policy_snapshot, ensure_ascii=False),
                    event.policy_version,
                    event.orchestrator_version,
                    event.registry_version,
                    json.dumps(event.meta, ensure_ascii=False),
                )
            )

            if event.event_type == "ReputationUpdated":
                conn.execute(
                    """
                    INSERT OR REPLACE INTO reputation_current
                    (entity_type, entity_id, domain, score, trust_level, last_update, policy_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.entity_type,
                        event.entity_id,
                        event.domain,
                        event.delta,
                        event.verdict,
                        event.timestamp,
                        event.policy_version,
                    )
                )
            conn.commit()

    def get_trace(self, trace_id: str) -> List[Dict]:
        with sqlite3.connect(str(DB_FILE)) as conn:
            rows = conn.execute(
                """
                SELECT timestamp, event_type, entity_type, entity_id, verdict, confidence, delta, reason,
                       parent_event_id, duration_ms, total_duration_ms, policy_snapshot
                FROM decision_events
                WHERE trace_id = ?
                ORDER BY timestamp ASC
                """,
                (trace_id,)
            ).fetchall()
            return [
                {
                    "timestamp": r[0],
                    "event_type": r[1],
                    "entity_type": r[2],
                    "entity_id": r[3],
                    "verdict": r[4],
                    "confidence": r[5],
                    "delta": r[6],
                    "reason": r[7],
                    "parent_event_id": r[8],
                    "duration_ms": r[9],
                    "total_duration_ms": r[10],
                    "policy_snapshot": json.loads(r[11]) if r[11] else {},
                }
                for r in rows
            ]

    def show_trace(self, trace_id: str) -> str:
        events = self.get_trace(trace_id)
        if not events:
            return f"Trace {trace_id} не найден."

        lines = [f"Trace: {trace_id}"]
        lines.append("")

        for e in events:
            dt = datetime.fromtimestamp(e['timestamp']).strftime("%H:%M:%S")
            delta = e.get('delta', 0.0)
            delta_str = f"{delta:+.3f}" if delta != 0 else ""
            duration = e.get('duration_ms', 0)
            duration_str = f" {duration}ms" if duration else ""

            lines.append(f"  [{dt}{duration_str}] {e['event_type']}")
            lines.append(f"    entity: {e['entity_type']}:{e['entity_id']}")
            if e['verdict']:
                lines.append(f"    verdict: {e['verdict']}")
            if e['confidence']:
                lines.append(f"    confidence: {e['confidence']:.3f}")
            if delta_str:
                lines.append(f"    delta: {delta_str}")
            if e['reason']:
                lines.append(f"    reason: {e['reason']}")
            lines.append("")

        return "\n".join(lines)

    def timeline(self, trace_id: str) -> str:
        events = self.get_trace(trace_id)
        if not events:
            return f"Trace {trace_id} не найден."

        lines = [f"Trace: {trace_id}"]
        lines.append("")

        total_ms = 0
        for e in events:
            duration = e.get('duration_ms', 0)
            total_ms += duration
            dt = datetime.fromtimestamp(e['timestamp']).strftime("%H:%M:%S")
            verdict = e['verdict']
            entity = e['entity_id']
            lines.append(f"  {dt}  {e['event_type']:20} {verdict:12} {entity[:30]}")

        if total_ms > 0:
            lines.append("")
            lines.append(f"  Total: {total_ms}ms")

        return "\n".join(lines)

    def get_events(self, limit: int = 50) -> List[Dict]:
        with sqlite3.connect(str(DB_FILE)) as conn:
            rows = conn.execute(
                """
                SELECT timestamp, event_type, trace_id, entity_type, entity_id, verdict, confidence, delta, reason
                FROM decision_events
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
            return [
                {
                    "timestamp": r[0],
                    "event_type": r[1],
                    "trace_id": r[2],
                    "entity_type": r[3],
                    "entity_id": r[4],
                    "verdict": r[5],
                    "confidence": r[6],
                    "delta": r[7],
                    "reason": r[8],
                }
                for r in rows
            ]

    def stats(self) -> dict:
        with sqlite3.connect(str(DB_FILE)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]

            # По типам
            by_type = {}
            rows = conn.execute(
                "SELECT event_type, COUNT(*) FROM decision_events GROUP BY event_type"
            ).fetchall()
            for r in rows:
                by_type[r[0]] = r[1]

            entities = conn.execute("SELECT COUNT(*) FROM reputation_current").fetchone()[0]

            # Среднее время
            avg_duration = conn.execute(
                "SELECT AVG(duration_ms) FROM decision_events WHERE duration_ms > 0"
            ).fetchone()[0] or 0

            # Cache HIT
            cache_hit = conn.execute(
                "SELECT COUNT(*) FROM decision_events WHERE entity_id='cache_check' AND verdict='hit'"
            ).fetchone()[0]
            cache_miss = conn.execute(
                "SELECT COUNT(*) FROM decision_events WHERE entity_id='cache_check' AND verdict='miss'"
            ).fetchone()[0]
            cache_total = cache_hit + cache_miss
            cache_hit_rate = round(cache_hit / cache_total * 100, 1) if cache_total else 0

            # VERIFIED
            verified = conn.execute(
                "SELECT COUNT(*) FROM decision_events WHERE event_type='ValidationFinished' AND verdict='VERIFIED'"
            ).fetchone()[0]
            total_validations = conn.execute(
                "SELECT COUNT(*) FROM decision_events WHERE event_type='ValidationFinished'"
            ).fetchone()[0]
            verified_rate = round(verified / total_validations * 100, 1) if total_validations else 0

            # Средний confidence
            avg_confidence = conn.execute(
                "SELECT AVG(confidence) FROM decision_events WHERE confidence > 0"
            ).fetchone()[0] or 0

            # Лучшие маршруты
            best_routes = conn.execute(
                """
                SELECT entity_id, AVG(delta) as avg_delta, COUNT(*) as cnt
                FROM decision_events
                WHERE entity_type='route' AND event_type='ReputationUpdated'
                GROUP BY entity_id
                ORDER BY avg_delta DESC
                LIMIT 3
                """
            ).fetchall()

            # Лучшие модели
            best_models = conn.execute(
                """
                SELECT entity_id, AVG(delta) as avg_delta, COUNT(*) as cnt
                FROM decision_events
                WHERE entity_type='model' AND event_type='ReputationUpdated'
                GROUP BY entity_id
                ORDER BY avg_delta DESC
                LIMIT 3
                """
            ).fetchall()

            # Лучшие источники
            best_sources = conn.execute(
                """
                SELECT entity_id, AVG(delta) as avg_delta, COUNT(*) as cnt
                FROM decision_events
                WHERE entity_type='source' AND event_type='ReputationUpdated'
                GROUP BY entity_id
                ORDER BY avg_delta DESC
                LIMIT 3
                """
            ).fetchall()

            return {
                "total_events": total,
                "by_event_type": by_type,
                "total_entities": entities,
                "avg_duration_ms": round(avg_duration, 1),
                "cache_hit_rate": cache_hit_rate,
                "verified_rate": verified_rate,
                "avg_confidence": round(avg_confidence, 3),
                "best_routes": [{"route": r[0], "avg_delta": round(r[1], 3), "count": r[2]} for r in best_routes],
                "best_models": [{"model": r[0], "avg_delta": round(r[1], 3), "count": r[2]} for r in best_models],
                "best_sources": [{"source": r[0], "avg_delta": round(r[1], 3), "count": r[2]} for r in best_sources],
                "events_log": EVENTS_LOG,
                "db_file": DB_FILE,
                "policy_version": POLICY_VERSION,
            }


_ledger: Optional[DecisionLedger] = None


def get_ledger() -> DecisionLedger:
    global _ledger
    if _ledger is None:
        _ledger = DecisionLedger()
    return _ledger


def add_decision_event(
    event_type: str,
    trace_id: str,
    entity_type: str,
    entity_id: str,
    verdict: str = "",
    confidence: float = 0.0,
    delta: float = 0.0,
    delta_factors: Optional[Dict[str, float]] = None,
    reason: str = "",
    domain: str = "general",
    parent_event_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    total_duration_ms: Optional[int] = None,
    policy_snapshot: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
):
    event = DecisionEvent(
        event_type=event_type,
        trace_id=trace_id,
        entity_type=entity_type,
        entity_id=entity_id,
        verdict=verdict,
        confidence=confidence,
        delta=delta,
        delta_factors=delta_factors,
        reason=reason,
        domain=domain,
        parent_event_id=parent_event_id,
        duration_ms=duration_ms,
        total_duration_ms=total_duration_ms,
        policy_snapshot=policy_snapshot,
        meta=meta,
    )
    get_ledger().add_event(event)


def show_trace(trace_id: str) -> str:
    return get_ledger().show_trace(trace_id)


def timeline(trace_id: str) -> str:
    return get_ledger().timeline(trace_id)


def get_events(limit: int = 50) -> List[Dict]:
    return get_ledger().get_events(limit)


def get_stats() -> dict:
    return get_ledger().stats()


if __name__ == "__main__":
    ledger = get_ledger()
    sub = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if sub == "stats":
        st = ledger.stats()
        print(f"Decision Ledger V4")
        print(f"  Всего событий: {st['total_events']}")
        print(f"  По типам: {st['by_event_type']}")
        print(f"  Всего сущностей: {st['total_entities']}")
        print(f"  Среднее время шага: {st['avg_duration_ms']}ms")
        print(f"  Cache HIT: {st['cache_hit_rate']}%")
        print(f"  VERIFIED: {st['verified_rate']}%")
        print(f"  Средний confidence: {st['avg_confidence']}")
        print(f"  Лучшие маршруты: {st['best_routes']}")
        print(f"  Лучшие модели: {st['best_models']}")
        print(f"  Лучшие источники: {st['best_sources']}")
        print(f"  Политика: {st['policy_version']}")
        print(f"  Журнал: {st['events_log']}")

    elif sub == "show":
        trace_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not trace_id:
            print("Использование: python3 orch_ledger.py show <trace_id>")
            sys.exit(1)
        print(show_trace(trace_id))

    elif sub == "timeline":
        trace_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not trace_id:
            print("Использование: python3 orch_ledger.py timeline <trace_id>")
            sys.exit(1)
        print(timeline(trace_id))

    elif sub == "events":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        events = get_events(n)
        for e in events:
            dt = datetime.fromtimestamp(e['timestamp']).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{dt} {e['event_type']}: {e['entity_type']}:{e['entity_id']} {e['verdict']} {e['delta']:+.3f} trace_id={e['trace_id']}")

    else:
        print("Команды: stats, show <trace_id>, timeline <trace_id>, events [N]")
