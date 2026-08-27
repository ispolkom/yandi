"""
assistant/orch_monitoring.py — Monitoring.
Сбор метрик: latency_p95, token_usage, success_rate, timeout_count.
"""
from __future__ import annotations

import atexit
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

BASE         = Path(__file__).parent.parent
METRICS_FILE = BASE / "registry" / "orch_metrics.jsonl"
METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)

_buffer: list[dict] = []
_flush_every = 10  # записывать каждые N событий


def record(
    step: str,
    latency: float,
    success: bool,
    tokens: int = 0,
    timed_out: bool = False,
    extra: Optional[dict] = None,
):
    """Записать метрику одного шага."""
    event = {
        "step":      step,
        "latency":   round(latency, 3),
        "success":   success,
        "tokens":    tokens,
        "timed_out": timed_out,
        "ts":        time.time(),
        **(extra or {}),
    }
    _buffer.append(event)
    if len(_buffer) >= _flush_every:
        flush()


def flush():
    """Сбросить буфер на диск."""
    if not _buffer:
        return
    with open(METRICS_FILE, "a", encoding="utf-8") as f:
        for e in _buffer:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    _buffer.clear()


# Foundation Repair (YANDI_SELF_LEARNING_RECONCILIATION_AUDIT.md P1
# "orch_metrics.jsonl silently near-dead"): record() only ever flushed
# every _flush_every events — proven root cause of data loss: any process
# restart (frequent during active development, confirmed by this repo's
# own commit history) drops up to _flush_every - 1 buffered events
# silently, with no error. Registering flush() at interpreter exit closes
# this for a plain script/CLI process exiting normally (proven: covered
# by this module's own regression test, a real subprocess that records
# under _flush_every events and exits cleanly).
#
# IMPORTANT, verified by direct isolated reproduction (a minimal
# standalone FastAPI+uvicorn app: register this exact flush() via atexit,
# record one event, uvicorn.run(), then `kill -TERM` the process) — this
# atexit registration does NOT fire when the real production process
# (pet/council_chat_server.py, run via uvicorn.run(..., reload=False))
# receives SIGTERM. uvicorn's graceful-shutdown path does not go through
# a route that triggers atexit handlers in this setup. So for the actual
# production server, this fix is INCOMPLETE: it correctly fixes the
# mechanism this module owns, but does not fully close the loop for the
# real deployment's shutdown path. A complete fix needs an explicit
# shutdown hook wired into pet/council_chat_server.py (e.g. a FastAPI
# `@app.on_event("shutdown")` handler calling this module's flush()) —
# that file is outside agent/, outside this Foundation Repair's audited
# scope. Left as documented, verified-incomplete P1 debt rather than
# silently claimed fixed; see
# YANDI_SELF_LEARNING_FOUNDATION_REPAIR_REPORT.md.
atexit.register(flush)


def get_stats(last_n: int = 1000) -> dict:
    """Статистика за последние N событий."""
    if not METRICS_FILE.exists():
        return {}

    lines = METRICS_FILE.read_text().splitlines()[-last_n:]
    events = [json.loads(l) for l in lines if l.strip()]

    by_step: dict[str, list] = defaultdict(list)
    for e in events:
        by_step[e["step"]].append(e)

    stats = {}
    for step, evs in by_step.items():
        latencies = sorted(e["latency"] for e in evs)
        n         = len(latencies)
        successes = sum(1 for e in evs if e.get("success"))
        timeouts  = sum(1 for e in evs if e.get("timed_out"))
        p95_idx   = min(int(n * 0.95), n - 1)
        stats[step] = {
            "count":        n,
            "success_rate": round(successes / n, 3) if n else 0,
            "timeout_rate": round(timeouts / n, 3)  if n else 0,
            "latency_avg":  round(sum(latencies) / n, 2) if n else 0,
            "latency_p95":  round(latencies[p95_idx], 2) if latencies else 0,
            "latency_max":  round(max(latencies), 2) if latencies else 0,
        }

    return stats


def print_stats():
    stats = get_stats()
    if not stats:
        print("Нет метрик")
        return
    print(f"{'Шаг':<22} {'N':>5} {'Success':>8} {'Timeout':>8} {'Avg':>7} {'P95':>7} {'Max':>7}")
    print("─" * 70)
    for step, s in sorted(stats.items()):
        print(
            f"{step:<22} {s['count']:>5} "
            f"{s['success_rate']:>7.1%} {s['timeout_rate']:>7.1%} "
            f"{s['latency_avg']:>6.1f}s {s['latency_p95']:>6.1f}s {s['latency_max']:>6.1f}s"
        )


if __name__ == "__main__":
    # Симуляция метрик
    for step in ["intent", "enrich", "local_search", "synthesize"]:
        for _ in range(5):
            record(step, latency=5.0 + step.__hash__() % 10, success=True, tokens=200)
    flush()
    print_stats()
