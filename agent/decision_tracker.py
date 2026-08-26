#!/usr/bin/env python3
"""
assistant/decision_tracker.py — трекер архитектурных решений.

Каждое решение фиксирует:
  - что решено
  - почему (причина)
  - ожидаемый результат
  - фактический результат (заполняется позже)
  - дата, статус (open/closed/cancelled)

Хранение: registry/decisions/decisions.jsonl (append-only)
Индексация: через KnowledgeGraph → decision-узлы

API:
  dt = DecisionTracker()
  dt.add("Использовать NetworkX + SQLite для KG", reason="...", expected="...")
  dt.list(status="open")
  dt.close(decision_id, outcome="Работает, граф строится за 8s")
  dt.report()

Команды:
  python3 assistant/decision_tracker.py add "текст" [reason] [expected]
  python3 assistant/decision_tracker.py list [open|closed|all]
  python3 assistant/decision_tracker.py close <id> "результат"
  python3 assistant/decision_tracker.py report
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis

BASE          = Path(__file__).parent.parent
DECISIONS_DIR = BASE / "registry" / "decisions"
DECISIONS_FILE = DECISIONS_DIR / "decisions.jsonl"
REPORT_KEY    = "council:skill:reports"
REPORT_CH     = "council:skill:report"

DECISIONS_DIR.mkdir(parents=True, exist_ok=True)


# ── helpers ───────────────────────────────────────────────────────────────────

def _r() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def _publish(r: redis.Redis, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    r.lpush(REPORT_KEY, data)
    r.ltrim(REPORT_KEY, 0, 49)
    r.publish(REPORT_CH, data)


def _make_id(text: str) -> str:
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = hashlib.md5(text.encode()).hexdigest()[:6]
    return f"dec_{ts}_{slug}"


# ── DecisionTracker ───────────────────────────────────────────────────────────

class DecisionTracker:
    """Append-only JSONL трекер архитектурных решений."""

    STATUS_OPEN      = "open"
    STATUS_CLOSED    = "closed"
    STATUS_CANCELLED = "cancelled"

    def __init__(self, r: Optional[redis.Redis] = None):
        self.r    = r or _r()
        self._cache: list[dict] = []
        self._load()

    # ── загрузка ─────────────────────────────────────────────────────────────

    def _load(self):
        self._cache = []
        if not DECISIONS_FILE.exists():
            return
        with open(DECISIONS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        self._cache.append(json.loads(line))
                    except Exception:
                        pass

    def _save_record(self, record: dict):
        with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._cache.append(record)

    # ── добавление ────────────────────────────────────────────────────────────

    def add(self, text: str, reason: str = "", expected: str = "",
            tags: Optional[list] = None, auto_kg: bool = True) -> dict:
        """
        Записать новое решение. Возвращает запись.
        auto_kg=True — автоматически добавить в Knowledge Graph.
        """
        did = _make_id(text)
        record = {
            "id"      : did,
            "text"    : text,
            "reason"  : reason,
            "expected": expected,
            "outcome" : None,
            "status"  : self.STATUS_OPEN,
            "tags"    : tags or [],
            "created" : datetime.now().isoformat(),
            "closed"  : None,
        }
        self._save_record(record)

        if auto_kg:
            try:
                from agent.knowledge_graph import KnowledgeGraph
                kg = KnowledgeGraph(r=self.r)
                kg.add_decision(did, text, reason, expected)
            except Exception:
                pass

        _publish(self.r, {
            "skill"    : "decision_tracker",
            "action"   : "add",
            "id"       : did,
            "text"     : text[:80],
            "reason"   : reason[:60],
            "timestamp": record["created"],
        })

        return record

    # ── закрытие ─────────────────────────────────────────────────────────────

    def close(self, decision_id: str, outcome: str,
              status: str = STATUS_CLOSED) -> Optional[dict]:
        """Закрыть решение с фактическим результатом."""
        # найти запись
        target = next((d for d in self._cache if d["id"] == decision_id), None)
        if not target:
            # пробуем частичное совпадение
            matches = [d for d in self._cache
                       if decision_id.lower() in d["id"].lower()
                       or decision_id.lower() in d["text"].lower()]
            if not matches:
                return None
            target = matches[-1]

        # записать closure-запись (патч поверх оригинала)
        patch = {
            "_patch_for" : target["id"],
            "id"         : target["id"],
            "outcome"    : outcome,
            "status"     : status,
            "closed"     : datetime.now().isoformat(),
        }
        self._save_record(patch)

        # обновить кеш
        target["outcome"] = outcome
        target["status"]  = status
        target["closed"]  = patch["closed"]

        _publish(self.r, {
            "skill"    : "decision_tracker",
            "action"   : "close",
            "id"       : target["id"],
            "outcome"  : outcome[:80],
            "timestamp": patch["closed"],
        })

        return target

    # ── просмотр ─────────────────────────────────────────────────────────────

    def list(self, status: str = "open", limit: int = 20) -> list[dict]:
        """Вернуть список решений. status='all' → все."""
        # применить патчи
        records = self._effective_records()
        if status != "all":
            records = [r for r in records if r.get("status") == status]
        return sorted(records, key=lambda x: x.get("created", ""), reverse=True)[:limit]

    def _effective_records(self) -> list[dict]:
        """Смёрживает оригиналы и патчи в единый список."""
        base:   dict[str, dict] = {}
        patches: list[dict]     = []

        for rec in self._cache:
            if "_patch_for" in rec:
                patches.append(rec)
            else:
                base[rec["id"]] = dict(rec)

        for patch in patches:
            target_id = patch["_patch_for"]
            if target_id in base:
                base[target_id].update({
                    k: v for k, v in patch.items() if not k.startswith("_")
                })

        return list(base.values())

    def get(self, decision_id: str) -> Optional[dict]:
        records = self._effective_records()
        return next((r for r in records if r["id"] == decision_id), None)

    # ── отчёт ─────────────────────────────────────────────────────────────────

    def report(self) -> dict:
        records  = self._effective_records()
        by_status = {}
        for r in records:
            s = r.get("status", "unknown")
            by_status.setdefault(s, []).append(r)

        return {
            "total"    : len(records),
            "open"     : len(by_status.get("open", [])),
            "closed"   : len(by_status.get("closed", [])),
            "cancelled": len(by_status.get("cancelled", [])),
            "open_list": [
                {"id": r["id"], "text": r["text"][:60], "created": r["created"]}
                for r in by_status.get("open", [])
            ],
        }

    def stats_str(self) -> str:
        rpt = self.report()
        return (f"Решения: всего={rpt['total']} открытых={rpt['open']} "
                f"закрытых={rpt['closed']}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    dt  = DecisionTracker()

    if cmd == "add":
        text     = sys.argv[2] if len(sys.argv) > 2 else ""
        reason   = sys.argv[3] if len(sys.argv) > 3 else ""
        expected = sys.argv[4] if len(sys.argv) > 4 else ""
        if not text:
            print("Укажи текст решения: python3 decision_tracker.py add 'текст' [причина] [ожидание]")
            sys.exit(1)
        rec = dt.add(text, reason=reason, expected=expected)
        print(f"✓ Добавлено: {rec['id']}")
        print(f"  Текст:     {rec['text']}")
        print(f"  Причина:   {rec['reason'] or '—'}")
        print(f"  Ожидание:  {rec['expected'] or '—'}")

    elif cmd == "list":
        status = sys.argv[2] if len(sys.argv) > 2 else "open"
        records = dt.list(status=status)
        if not records:
            print(f"Нет решений со статусом: {status}")
        for r in records:
            icon = "✅" if r["status"] == "closed" else ("❌" if r["status"] == "cancelled" else "🔄")
            print(f"{icon} [{r['id']}]  {r['text'][:70]}")
            if r.get("reason"):
                print(f"   ↳ Причина: {r['reason'][:60]}")
            if r.get("outcome"):
                print(f"   ↳ Итог:    {r['outcome'][:60]}")

    elif cmd == "close":
        did     = sys.argv[2] if len(sys.argv) > 2 else ""
        outcome = sys.argv[3] if len(sys.argv) > 3 else ""
        if not did or not outcome:
            print("Использование: python3 decision_tracker.py close <id> 'результат'")
            sys.exit(1)
        rec = dt.close(did, outcome)
        if rec:
            print(f"✓ Закрыто: {rec['id']}")
            print(f"  Итог: {rec['outcome']}")
        else:
            print(f"Решение не найдено: {did}")

    elif cmd == "report":
        rpt = dt.report()
        print(json.dumps(rpt, ensure_ascii=False, indent=2))

    else:
        print(f"Неизвестная команда: {cmd}")
        print("Доступно: add <текст> [причина] [ожидание] | list [open|closed|all] | close <id> <итог> | report")
        sys.exit(1)
