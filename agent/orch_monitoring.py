"""
assistant/orch_monitoring.py — Monitoring.
Сбор метрик: latency_p95, token_usage, success_rate, timeout_count.
"""
from __future__ import annotations

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
