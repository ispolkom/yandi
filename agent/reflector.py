#!/usr/bin/env python3
"""
assistant/reflector.py — Reflection Daemon.

Периодически анализирует состояние системы и генерирует отчёт:
  - Незакрытые решения (открытые > N дней)
  - Слабые темы в датасете (< MIN_EXAMPLES примеров)
  - Противоречия в KG (концепты с рёбром contradicts)
  - Открытые вопросы без ответа (TODO из сессий)
  - Топ концептов по упоминаниям (самые активные)
  - Аномалии: нет ответа от модели давно, пустые кластеры

Вывод: registry/reflections/YYYY-MM-DD.md + Redis-отчёт

Команды:
  python3 assistant/reflector.py run
  python3 assistant/reflector.py summary
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import redis

BASE         = Path(__file__).parent.parent
REFLECT_DIR  = BASE / "registry" / "reflections"
DATASET_DIR  = BASE / "registry" / "dataset"
SESSION_DIR  = BASE / "registry" / "council" / "sessions"
FINAL_DIR    = DATASET_DIR / "final"
REPORT_KEY   = "council:skill:reports"
REPORT_CH    = "council:skill:report"

REFLECT_DIR.mkdir(parents=True, exist_ok=True)

MIN_EXAMPLES      = 5    # минимум примеров на тему
STALE_DECISION_DAYS = 7  # решение "залежалось" если открыто > 7 дней
TODO_PATTERN      = re.compile(r"\bTODO\b|\[ \]|нужно|осталось|планируется", re.IGNORECASE)


# ── helpers ───────────────────────────────────────────────────────────────────

def _r() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def _publish(r: redis.Redis, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    r.lpush(REPORT_KEY, data)
    r.ltrim(REPORT_KEY, 0, 49)
    r.publish(REPORT_CH, data)


def _load_final_rows() -> list[dict]:
    rows = []
    for f in sorted(FINAL_DIR.glob("*_hf.jsonl")):
        with open(f, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


# ── Reflector ─────────────────────────────────────────────────────────────────

class Reflector:
    """Анализирует состояние системы, генерирует еженедельные отчёты."""

    def __init__(self, r: Optional[redis.Redis] = None):
        self.r = r or _r()

    # ── анализ ───────────────────────────────────────────────────────────────

    def _check_decisions(self) -> dict:
        """Находит незакрытые и залежавшиеся решения."""
        try:
            from agent.decision_tracker import DecisionTracker
            dt = DecisionTracker(r=self.r)
            open_decisions = dt.list(status="open")
            rpt = dt.report()
        except Exception as e:
            return {"error": str(e), "open": 0, "stale": []}

        now   = datetime.now()
        stale = []
        for d in open_decisions:
            try:
                created = datetime.fromisoformat(d["created"])
                age     = (now - created).days
                if age >= STALE_DECISION_DAYS:
                    stale.append({"id": d["id"], "text": d["text"][:60], "age_days": age})
            except Exception:
                pass

        return {
            "total"  : rpt["total"],
            "open"   : rpt["open"],
            "closed" : rpt["closed"],
            "stale"  : stale,
        }

    def _check_dataset(self) -> dict:
        """Анализирует баланс тем в датасете."""
        rows = _load_final_rows()
        if not rows:
            return {"total": 0, "topics": {}, "sparse": []}

        topic_counts: Counter = Counter(r.get("topic", "unknown") for r in rows)
        sparse = [{"topic": t, "count": c}
                  for t, c in topic_counts.items()
                  if c < MIN_EXAMPLES and t not in ("noise", "unknown")]

        return {
            "total"  : len(rows),
            "topics" : dict(topic_counts),
            "sparse" : sparse,
        }

    def _check_kg(self) -> dict:
        """Проверяет Knowledge Graph: изолированные узлы, противоречия, топ-концепты."""
        try:
            from agent.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(r=self.r)
            G  = kg.G
        except Exception as e:
            return {"error": str(e)}

        isolated = [n for n in G.nodes if G.degree(n) == 0]

        contradictions = []
        for u, v, data in G.edges(data=True):
            if data.get("rel") == "contradicts":
                contradictions.append({
                    "a": G.nodes[u].get("label", u),
                    "b": G.nodes[v].get("label", v),
                })

        # топ концептов по степени
        concepts = [(n, G.degree(n)) for n, d in G.nodes(data=True)
                    if d.get("type") == "concept"]
        top_concepts = sorted(concepts, key=lambda x: -x[1])[:10]

        return {
            "nodes"       : G.number_of_nodes(),
            "edges"       : G.number_of_edges(),
            "isolated"    : len(isolated),
            "contradictions": contradictions,
            "top_concepts": [{"node": n, "degree": d} for n, d in top_concepts],
        }

    def _check_sessions(self) -> dict:
        """Ищет TODO в сессиях, незакрытые вопросы."""
        todos = []
        session_count = 0
        for f in sorted(SESSION_DIR.glob("*.md"), reverse=True)[:10]:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
                session_count += 1
                for line in text.splitlines():
                    if TODO_PATTERN.search(line) and len(line.strip()) > 10:
                        todos.append({
                            "session": f.stem[:30],
                            "line"   : line.strip()[:80],
                        })
            except Exception:
                pass
        return {
            "sessions_checked": session_count,
            "todos"           : todos[:20],
        }

    def _check_models(self) -> dict:
        """Проверяет активность моделей из Redis."""
        try:
            token_keys = ["council:tokens:claude", "council:tokens:gpt", "council:tokens:deepseek"]
            model_stats = {}
            for key in token_keys:
                model = key.split(":")[-1]
                val = self.r.get(key)
                model_stats[model] = int(val) if val else 0
            return {"tokens": model_stats}
        except Exception as e:
            return {"error": str(e)}

    # ── генерация отчёта ──────────────────────────────────────────────────────

    def run(self, verbose: bool = True) -> dict:
        """Полный анализ + запись отчёта."""
        now = datetime.now()

        results = {
            "timestamp" : now.isoformat(),
            "decisions" : self._check_decisions(),
            "dataset"   : self._check_dataset(),
            "kg"        : self._check_kg(),
            "sessions"  : self._check_sessions(),
            "models"    : self._check_models(),
        }

        # ── форматируем отчёт в Markdown ─────────────────────────────────────
        lines = [
            f"# Reflection Report — {now.strftime('%Y-%m-%d %H:%M')}",
            "",
            "## Решения",
        ]

        dec = results["decisions"]
        if "error" not in dec:
            lines.append(f"- Всего: {dec['total']}, открытых: {dec['open']}, закрытых: {dec['closed']}")
            if dec["stale"]:
                lines.append(f"\n### ⚠️ Залежавшиеся (>{STALE_DECISION_DAYS}д):")
                for d in dec["stale"]:
                    lines.append(f"  - [{d['id'][-12:]}] {d['text']} (возраст: {d['age_days']}д)")

        lines += ["", "## Датасет"]
        ds = results["dataset"]
        lines.append(f"- Всего строк: {ds['total']}")
        lines.append(f"- Темы: {ds['topics']}")
        if ds["sparse"]:
            lines.append("\n### ⚠️ Слабые темы:")
            for s in ds["sparse"]:
                lines.append(f"  - {s['topic']}: {s['count']} примеров (нужно ≥{MIN_EXAMPLES})")

        lines += ["", "## Knowledge Graph"]
        kg = results["kg"]
        if "error" not in kg:
            lines.append(f"- Узлов: {kg['nodes']}, рёбер: {kg['edges']}")
            if kg["isolated"]:
                lines.append(f"- ⚠️ Изолированных узлов: {kg['isolated']}")
            if kg["contradictions"]:
                lines.append(f"- ⚠️ Противоречий: {len(kg['contradictions'])}")
                for c in kg["contradictions"]:
                    lines.append(f"  - {c['a']} ↔ {c['b']}")
            lines.append("\n### Топ концептов:")
            for c in kg["top_concepts"][:5]:
                lines.append(f"  - {c['node']} (degree={c['degree']})")

        lines += ["", "## Открытые вопросы из сессий"]
        sess = results["sessions"]
        todos = sess.get("todos", [])
        if todos:
            for t in todos[:10]:
                lines.append(f"  - [{t['session'][:20]}] {t['line']}")
        else:
            lines.append("  — открытых вопросов не найдено")

        lines += ["", "## Модели"]
        mdl = results["models"]
        if "tokens" in mdl:
            for m, t in mdl["tokens"].items():
                lines.append(f"  - {m}: {t} токенов")

        # ── сохраняем ─────────────────────────────────────────────────────────
        stamp   = now.strftime("%Y-%m-%d")
        md_path = REFLECT_DIR / f"{stamp}.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")

        if verbose:
            print("\n".join(lines))

        # ── Redis отчёт ───────────────────────────────────────────────────────
        _publish(self.r, {
            "skill"     : "reflector",
            "timestamp" : results["timestamp"],
            "open_decisions": dec.get("open", 0),
            "stale_decisions": len(dec.get("stale", [])),
            "sparse_topics" : len(ds.get("sparse", [])),
            "kg_nodes"  : kg.get("nodes", 0),
            "todos"     : len(todos),
            "report"    : str(md_path),
        })

        results["md_path"] = str(md_path)
        return results

    def summary(self) -> str:
        """Короткий текстовый итог для лога демона."""
        dec = self._check_decisions()
        ds  = self._check_dataset()
        kg  = self._check_kg()
        return (
            f"decisions: {dec.get('open',0)} открытых, {len(dec.get('stale',[]))} залежавшихся | "
            f"dataset: {ds.get('total',0)} строк, {len(ds.get('sparse',[]))} слабых тем | "
            f"KG: {kg.get('nodes',0)} узлов"
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    rf  = Reflector()

    if cmd == "run":
        results = rf.run(verbose=True)
        print(f"\n[saved] {results['md_path']}")
    elif cmd == "summary":
        print(rf.summary())
    else:
        print(f"Неизвестная команда: {cmd}")
        sys.exit(1)
