"""
assistant/orch_tool_registry.py — реестр инструментов оркестратора.
Хранит доступные инструменты с метриками: latency, reliability, cost.
Planning Engine смотрит сюда при построении плана.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from agent.orch_schemas import StepName

BASE     = Path(__file__).parent.parent
REG_FILE = BASE / "registry" / "orch_tools.json"
REG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Дефолтный реестр инструментов
_DEFAULT_TOOLS: dict[str, dict] = {
    "cache_check": {
        "enabled": True,
        "latency_avg": 0.05,
        "reliability": 1.0,
        "cost_score": 0.0,   # бесплатно (Redis)
        "description": "Семантический кэш Redis + FAISS",
    },
    "risk_assess": {
        "enabled": True,
        "latency_avg": 0.01,
        "reliability": 1.0,
        "cost_score": 0.0,
        "description": "Оценка риска (hardcoded правила)",
    },
    "plan": {
        "enabled": True,
        "latency_avg": 3.0,
        "reliability": 0.95,
        "cost_score": 0.2,   # 7B
        "description": "Planning Engine (Qwen3:14b → 7B в будущем)",
        "model": "qwen3:14b",
    },
    "intent": {
        "enabled": True,
        "latency_avg": 5.0,
        "reliability": 0.95,
        "cost_score": 0.5,   # 14B
        "description": "Intent Analyzer (Qwen3:14b)",
        "model": "qwen3:14b",
    },
    "clarify": {
        "enabled": True,
        "latency_avg": 3.0,
        "reliability": 0.9,
        "cost_score": 0.2,
        "description": "Clarification Engine (Qwen3:14b → 7B в будущем)",
        "model": "qwen3:14b",
    },
    "enrich": {
        "enabled": True,
        "latency_avg": 4.0,
        "reliability": 0.95,
        "cost_score": 0.5,
        "description": "Query Enricher (Qwen3:14b)",
        "model": "qwen3:14b",
    },
    "local_search": {
        "enabled": True,
        "latency_avg": 0.3,
        "reliability": 1.0,
        "cost_score": 0.1,
        "description": "Local Registry Search (nomic-embed + FAISS)",
        "model": "nomic-embed-text",
    },
    "web_query": {
        "enabled": True,
        "latency_avg": 4.0,
        "reliability": 0.9,
        "cost_score": 0.5,
        "description": "Internet Query Formulator (Qwen3:14b)",
        "model": "qwen3:14b",
    },
    "web_scrape": {
        "enabled": True,
        "latency_avg": 8.0,
        "reliability": 0.8,
        "cost_score": 0.1,
        "description": "Web Scraper (DuckDuckGo + trafilatura)",
    },
    "synthesize": {
        "enabled": True,
        "latency_avg": 15.0,
        "reliability": 0.95,
        "cost_score": 0.8,
        "description": "Answer Synthesizer (Qwen3:14b)",
        "model": "qwen3:14b",
    },
    "optimistic_respond": {
        "enabled": True,
        "latency_avg": 0.01,
        "reliability": 1.0,
        "cost_score": 0.0,
        "description": "Optimistic Responder (код)",
    },
    "validate": {
        "enabled": True,
        "latency_avg": 30.0,
        "reliability": 0.85,
        "cost_score": 0.6,
        "description": "Parallel Validator (локальные Ollama-ноды)",
        "model": "qwen3:14b",
    },
    "arbitrate": {
        "enabled": True,
        "latency_avg": 20.0,
        "reliability": 0.9,
        "cost_score": 0.9,
        "description": "Consensus Arbiter (Qwen3:14b)",
        "model": "qwen3:14b",
    },
}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._load()

    def _load(self):
        if REG_FILE.exists():
            try:
                self._tools = json.loads(REG_FILE.read_text())
                return
            except Exception:
                pass
        self._tools = dict(_DEFAULT_TOOLS)
        self._save()

    def _save(self):
        REG_FILE.write_text(json.dumps(self._tools, indent=2, ensure_ascii=False))

    def get(self, step: str) -> Optional[dict]:
        return self._tools.get(step)

    def is_enabled(self, step: str) -> bool:
        t = self._tools.get(step)
        return bool(t and t.get("enabled", True))

    def get_model(self, step: str) -> str:
        t = self._tools.get(step, {})
        return t.get("model", "qwen3:14b")

    def update_latency(self, step: str, latency: float):
        """Обновить среднюю латентность (скользящее среднее)."""
        if step not in self._tools:
            return
        old = self._tools[step].get("latency_avg", latency)
        self._tools[step]["latency_avg"] = round(old * 0.8 + latency * 0.2, 3)
        self._save()

    def update_reliability(self, step: str, success: bool):
        """Обновить reliability (скользящее среднее)."""
        if step not in self._tools:
            return
        old = self._tools[step].get("reliability", 1.0)
        val = 1.0 if success else 0.0
        self._tools[step]["reliability"] = round(old * 0.9 + val * 0.1, 3)
        self._save()

    def stats(self) -> list[dict]:
        return [
            {"step": k, **v}
            for k, v in sorted(self._tools.items(), key=lambda x: -x[1].get("cost_score", 0))
        ]

    def all_steps(self) -> list[str]:
        return list(self._tools.keys())


# Синглтон
_registry: Optional[ToolRegistry] = None

def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


if __name__ == "__main__":
    r = get_registry()
    print("Tool Registry:")
    for s in r.stats():
        en = "✓" if s.get("enabled") else "✗"
        print(f"  {en} {s['step']:22s} lat={s.get('latency_avg',0):5.1f}s  "
              f"rel={s.get('reliability',0):.2f}  cost={s.get('cost_score',0):.1f}")
