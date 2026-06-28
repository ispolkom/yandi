#!/usr/bin/env python3
"""
assistant/orch_tracer.py — перехватчик решений оркестратора.

Записывает каждое решение classify() + execute() как обучающий пример
в registry/dataset/orch_traces/YYYYMMDD.jsonl.

Используется из daemon.py при обработке action="orch".

CLI:
  python3 assistant/orch_tracer.py stats      — статистика трейсов
  python3 assistant/orch_tracer.py tail [N]   — последние N трейсов
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE       = Path(__file__).parent.parent
TRACES_DIR = BASE / "registry" / "dataset" / "orch_traces"
TRACES_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_PROMPT = (
    "Ты оркестратор задач. Получаешь запрос пользователя и контекст сессии. "
    "Анализируешь и возвращаешь JSON-решение: какой скилл использовать, "
    "с какими параметрами и почему."
)


def _quality(task: str, result: str, outcome: str) -> float:
    """Оценка качества трейса: 0.0–1.0."""
    if outcome == "timeout":
        return 0.2
    if outcome == "fail":
        return 0.3
    score = 0.6
    if len(task) > 20:
        score += 0.1
    if len(result) > 100:
        score += 0.1
    if len(result) > 500:
        score += 0.1
    if not result.startswith("[error"):
        score += 0.1
    return min(1.0, round(score, 2))


class OrchestratorTracer:
    """Записывает решения оркестратора как обучающие примеры."""

    def trace(
        self,
        task: str,
        task_type: str,
        model: str,
        result: str,
        context: str = "",
        outcome: str = "success",
        elapsed_ms: int = 0,
        steps: Optional[list] = None,
    ) -> dict:
        """Записать трейс. Возвращает записанный объект."""
        ts = datetime.now()

        if outcome == "success" and result.startswith("[error"):
            outcome = "fail"

        quality = _quality(task, result, outcome)

        orch_decision = json.dumps({
            "skill":  task_type,
            "model":  model,
            "args":   {"task": task[:200]},
            "reason": f"classify() → task_type={task_type}, модель={model}",
        }, ensure_ascii=False)

        user_content = task
        if context:
            user_content = f"{task}\n\n[Контекст: {context[:300]}]"

        trace = {
            "ts":         ts.isoformat(),
            "date":       ts.strftime("%Y-%m-%d"),
            "task":       task[:500],
            "task_type":  task_type,
            "model":      model,
            "result":     result[:1000],
            "outcome":    outcome,
            "elapsed_ms": elapsed_ms,
            "quality":    quality,
            "steps":      len(steps) if steps else 1,
            "skill":      task_type,
            "messages": [
                {"role": "system",    "content": SYSTEM_PROMPT},
                {"role": "user",      "content": user_content},
                {"role": "assistant", "content": orch_decision},
            ],
        }

        day_file = TRACES_DIR / f"{ts.strftime('%Y%m%d')}.jsonl"
        with day_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trace, ensure_ascii=False) + "\n")

        return trace

    def stats(self) -> dict:
        """Статистика по всем трейсам."""
        files = sorted(TRACES_DIR.glob("*.jsonl"))
        total = 0
        by_skill: dict[str, int]   = {}
        by_outcome: dict[str, int] = {}
        quality_sum = 0.0

        for f in files:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    t = json.loads(line)
                    total += 1
                    skill   = t.get("skill", "unknown")
                    outcome = t.get("outcome", "unknown")
                    by_skill[skill]     = by_skill.get(skill, 0) + 1
                    by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
                    quality_sum        += t.get("quality", 0.0)
                except Exception:
                    pass

        success      = by_outcome.get("success", 0)
        success_rate = round(success / total, 2) if total else 0.0
        avg_quality  = round(quality_sum / total, 2) if total else 0.0

        return {
            "total":        total,
            "files":        len(files),
            "by_skill":     by_skill,
            "by_outcome":   by_outcome,
            "success_rate": success_rate,
            "avg_quality":  avg_quality,
        }

    def tail(self, n: int = 10) -> list[dict]:
        """Последние N трейсов (от новых к старым)."""
        files = sorted(TRACES_DIR.glob("*.jsonl"), reverse=True)
        out: list[dict] = []
        for f in files:
            lines = [l for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
            for line in reversed(lines):
                try:
                    out.append(json.loads(line))
                    if len(out) >= n:
                        return out
                except Exception:
                    pass
        return out

    def all_traces(self) -> list[dict]:
        """Все трейсы из всех файлов."""
        out: list[dict] = []
        for f in sorted(TRACES_DIR.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tracer = OrchestratorTracer()
    sub    = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if sub == "stats":
        st = tracer.stats()
        print(f"Трейсы оркестратора: {st['total']} всего, {st['files']} файлов")
        print(f"  success_rate={st['success_rate']}  avg_quality={st['avg_quality']}")
        print(f"  по скиллам: {st['by_skill']}")
        print(f"  по исходам: {st['by_outcome']}")

    elif sub == "tail":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        for t in tracer.tail(n):
            print(f"[{t['ts'][:19]}] {t['task_type']} → {t['model']} [{t['outcome']}] q={t['quality']}")
            print(f"  задача:    {t['task'][:80]}")
            print(f"  результат: {t['result'][:100]}")
            print()

    else:
        print("Команды: stats, tail [N]")
