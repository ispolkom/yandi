#!/usr/bin/env python3
"""
assistant/orchestrator.py — единый оркестратор + секретарь.

Один класс управляет всем:
  - TaskClassifier  : правила в RAM, без LLM, мгновенно
  - VRAMManager     : единственный владелец GPU
  - DecisionRegistry: логирует ПОЧЕМУ выбрана модель
  - Secretary       : scribe / search / summarize — встроены сюда же
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

import sys
import requests
import yaml
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent.local_http import local_post

BASE        = Path(__file__).parent.parent
CONFIG_PATH = BASE / "reader" / "config.yaml"
REGISTRY    = BASE / "registry"
DECISIONS   = REGISTRY / "decisions"
FLOOD_DIR   = REGISTRY / "flood"
DECISIONS.mkdir(parents=True, exist_ok=True)
FLOOD_DIR.mkdir(parents=True, exist_ok=True)

OLLAMA = "http://localhost:11434"

_MODEL_DISPLAY = {"claude": "Claude", "gpt": "GPT", "deepseek": "DeepSeek", "human": "Человек"}


def _flood(text: str, tag: str = "secretary"):
    ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = FLOOD_DIR / f"{ts}_{tag}.md"
    path.write_text(
        f"---\ndate: {datetime.now().strftime('%Y-%m-%d')}\ntag: {tag}\n---\n\n{text}\n",
        encoding="utf-8",
    )


# ── TaskClassifier — правила, без LLM ────────────────────────────────────────

TASK_RULES = [
    (["почему", "объясни", "разбери", "compare", "сравни",
      "плюсы", "минусы", "pros", "cons", "анализ", "analyze",
      "рассуди", "chain", "логика", "докажи"],    "reasoning",   "deepseek-r1:14b"),

    (["напиши", "создай", "сгенерируй", "json", "yaml",
      "список", "план", "модули", "структуру", "код",
      "write", "generate", "format", "schema"],   "structured",  "qwen3:14b"),

    (["найди", "поищи", "search", "fetch", "погугли",
      "что такое", "what is", "как работает",
      "документация", "docs", "пример"],          "search",      "search"),

    (["summary", "суммари", "кратко", "итог",
      "о чём", "что было", "recap", "сохрани",
      "запиши", "в реестр", "scribe"],            "memory",      "qwen3:14b"),

    (["совет", "broadcast", "спроси модели",
      "что думают", "мнение моделей"],            "council",     "council"),
]


def classify(text: str) -> tuple[str, str]:
    """Вернуть (тип_задачи, модель). Никакого LLM."""
    lower  = text.lower()
    scores: dict[str, int] = {}
    for patterns, task_type, model in TASK_RULES:
        hit = sum(1 for p in patterns if p in lower)
        if hit:
            scores[task_type] = scores.get(task_type, 0) + hit
    if not scores:
        return "general", "qwen3:14b"
    best  = max(scores, key=scores.get)
    model = next(m for _, t, m in TASK_RULES if t == best)
    return best, model


# ── VRAMManager ───────────────────────────────────────────────────────────────

class VRAMManager:
    def __init__(self):
        self._lock    = Lock()
        self._current = None

    def acquire(self, model: str) -> bool:
        with self._lock:
            if self._current == model:
                return True
            if self._current:
                self._unload(self._current)
            ok = self._load(model)
            if ok:
                self._current = model
            return ok

    def release(self): pass

    def current(self) -> str | None:
        return self._current

    def _load(self, model: str) -> bool:
        try:
            r = local_post(f"{OLLAMA}/api/chat",
                json={"model": model, "messages": [{"role": "user", "content": "ping"}],
                      "stream": False, "keep_alive": "10m"},
                timeout=60)
            return r.status_code == 200
        except Exception:
            return False

    def _unload(self, model: str):
        try:
            local_post(f"{OLLAMA}/api/chat",
                json={"model": model, "messages": [], "keep_alive": 0},
                timeout=10)
        except Exception:
            pass


# ── DecisionRegistry ──────────────────────────────────────────────────────────

class DecisionRegistry:
    def log(self, task: str, task_type: str, model: str, reason: str, result_preview: str = ""):
        ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        slug = re.sub(r"[^\w]", "_", task[:30].lower())
        path = DECISIONS / f"{ts}_{slug}.json"
        path.write_text(json.dumps({
            "ts": ts, "task": task[:200], "task_type": task_type,
            "model_chosen": model, "reason": reason,
            "result_preview": result_preview[:200],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def recent(self, n: int = 10) -> list[dict]:
        files = sorted(DECISIONS.glob("*.json"), reverse=True)[:n]
        out = []
        for f in files:
            try:
                out.append(json.loads(f.read_text()))
            except Exception:
                pass
        return out


# ── StepDecomposer ────────────────────────────────────────────────────────────

SIMPLE_THRESHOLD = 50

def decompose(task: str) -> list[str]:
    words = task.split()
    if len(words) < SIMPLE_THRESHOLD:
        return [task]
    parts = re.split(r'(?<=[.!?])\s+(?=[А-ЯA-Z])', task)
    parts = [p.strip() for p in parts if len(p.strip()) > 20]
    return parts if len(parts) > 1 else [task]


# ── Orchestrator + Secretary ──────────────────────────────────────────────────

class Orchestrator:
    """Единая точка входа: роутинг задач + секретарь (scribe/search/summarize)."""

    def __init__(self):
        cfg = {}
        try:
            cfg = yaml.safe_load(open(CONFIG_PATH))["ollama"]
        except Exception:
            pass
        self.cfg       = cfg
        self.vram      = VRAMManager()
        self.decisions = DecisionRegistry()
        self.model_map = {
            "reasoning":  cfg.get("reasoner_model",  "deepseek-r1:14b"),
            "structured": cfg.get("secretary_model", "qwen3:14b"),
            "memory":     cfg.get("secretary_model", "qwen3:14b"),
            "general":    cfg.get("model",           "qwen3:14b"),
            "search":     "search",
            "council":    "council",
        }

    # ── вызов Ollama ──────────────────────────────────────────────────────────

    def _call(self, model: str, prompt: str, system: str = None, timeout: int = 180) -> str:
        self.vram.acquire(model)
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        try:
            r = local_post(f"{OLLAMA}/api/chat",
                json={"model": model, "messages": msgs, "stream": False},
                timeout=timeout)
            raw = r.json()["message"]["content"].strip()
            return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        except Exception as e:
            return f"[error: {e}]"

    # ── Secretary: scribe ─────────────────────────────────────────────────────

    def scribe(self, text: str, verbose: bool = False) -> str:
        """Записать мысль в реестр через qwen3 → структурированный markdown."""
        model  = self.cfg.get("secretary_model", "qwen3:14b")
        if verbose:
            print(f"  [scribe] {model}: {text[:60]}...")
        prompt = (
            f"Запиши мысль в реестр. Верни JSON:\n"
            f'{{ "topic": "...", "project": "claude", "key_point": "...", '
            f'"reasoning": "...", "status": "open" }}\n\nМысль: {text}'
        )
        out = self._call(model, prompt)
        m   = re.search(r"\{.*\}", out, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
                ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                slug = re.sub(r"[^\w]", "_", data.get("topic", "note").lower())[:30]
                path = FLOOD_DIR / f"{ts}_scribe_{slug}.md"
                path.write_text(
                    f"---\ntopic: {data.get('topic','note')}\nproject: claude\n"
                    f"date: {datetime.now().strftime('%Y-%m-%d')}\nstatus: open\n"
                    f"generated_by: orchestrator-scribe\n---\n\n"
                    f"## Рассуждение\n{data.get('reasoning','')}\n\n"
                    f"## Ключевая мысль\n{data.get('key_point','')}\n",
                    encoding="utf-8",
                )
                return f"✓ записано: {path.name}"
            except Exception:
                pass
        _flood(f"scribe raw:\n{out}", tag="scribe")
        return "✓ во flood (JSON не распознан)"

    # ── Secretary: search ─────────────────────────────────────────────────────

    def search(self, query: str, verbose: bool = False) -> str:
        """Веб-поиск: gemma4 переформулирует → DDG → gemma4 извлекает сигнал."""
        try:
            from ddgs import DDGS
        except ImportError:
            return "[search error: ddgs не установлен — pip install duckduckgo-search]"

        model = self.cfg.get("fallback_model", "gemma4:e4b")
        if verbose:
            print(f"  [search] reformulate via {model}: {query[:50]}...")

        raw     = self._call(model,
            f'Дай 2 коротких English технических поисковых запроса JSON: {{"queries":["...","..."]}}\nЗапрос: {query}',
            timeout=60)
        queries = [query]
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                queries = json.loads(m.group()).get("queries", [query])
            except Exception:
                pass

        results = []
        env_bak = {k: os.environ.pop(k) for k in list(os.environ) if "proxy" in k.lower()}
        try:
            with DDGS() as d:
                for q in queries[:2]:
                    results.extend(d.text(q, max_results=5))
        except Exception as e:
            return f"[search error: {e}]"
        finally:
            os.environ.update(env_bak)

        if not results:
            return "[search: нет результатов]"

        ctx = "\n".join(f"[{r['title']}] {r.get('body','')[:200]}" for r in results[:6])
        out = self._call(model,
            f"Вопрос: {query}\n\nРезультаты:\n{ctx}\n\nИзвлеки ключевые факты (≤150 слов), убери рекламу.",
            timeout=60)

        _flood(f"search query: {query}\n\nresult:\n{out}", tag="search")
        if verbose:
            print(f"  [search] → {out[:200]}")
        return out

    # ── Secretary: summarize ──────────────────────────────────────────────────

    def summarize(self, messages: list[dict], verbose: bool = False) -> str:
        """Краткое summary диалога через qwen3."""
        model = self.cfg.get("model", "qwen3:14b")
        if not messages:
            return "(нет сообщений)"
        text = "\n".join(
            f"{_MODEL_DISPLAY.get(m.get('from','?'), m.get('from','?'))}: {m.get('text','')[:300]}"
            for m in messages[-20:]
        )
        if verbose:
            print(f"  [summarize] {model}, {len(messages)} сообщений...")
        return self._call(model, f"Сделай краткое summary (5-7 предложений) этого диалога:\n\n{text}")

    # ── run — главная точка входа ─────────────────────────────────────────────

    def run(self, task: str, context: str = "") -> dict:
        """Выполнить задачу: classify → route → execute → log."""
        task_type, model_key = classify(task)
        model = self.model_map.get(task_type, "qwen3:14b")
        steps = decompose(task)

        results = []
        for step in steps:
            prompt = f"{context}\n\n{step}".strip() if context else step

            if task_type == "search":
                result = self.search(step, verbose=True)
            elif task_type == "memory":
                result = self.scribe(step, verbose=True)
            elif model == "council":
                result = f"[council: broadcast — {step}]"
            else:
                result = self._call(model, prompt)

            results.append({"step": step, "result": result})

        final  = results[-1]["result"] if results else ""
        reason = f"task_type={task_type} → {model} по правилам classify(). steps={len(steps)}"
        self.decisions.log(task, task_type, model, reason, final)

        return {"task_type": task_type, "model": model,
                "steps": steps, "results": results, "final": final}

    # ── вспомогательные ───────────────────────────────────────────────────────

    def show_decisions(self, n: int = 5) -> str:
        rows = self.decisions.recent(n)
        if not rows:
            return "нет записей"
        lines = []
        for d in rows:
            lines.append(
                f"[{d['ts']}] {d['task_type']} → {d['model_chosen']}\n"
                f"  задача: {d['task'][:80]}\n"
                f"  результат: {d['result_preview'][:80]}"
            )
        return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    task = " ".join(sys.argv[1:]) or "сделай summary последних записей реестра"
    print(f"\n[orchestrator] задача: {task}")
    task_type, model = classify(task)
    print(f"  classify → {task_type} / {model}")
    o      = Orchestrator()
    result = o.run(task)
    print(f"\n  final → {result['final'][:300]}")
