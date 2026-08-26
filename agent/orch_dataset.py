#!/usr/bin/env python3
"""
assistant/orch_dataset.py — конвертер трейсов оркестратора в SFT-датасет.

Поток:
  registry/dataset/orch_traces/*.jsonl
  → фильтр (success + quality ≥ 0.7)
  → дедупликация (Jaccard < 0.85)
  → ChatML SFT формат
  → registry/dataset/orch_sft/

Два типа датасетов:
  orch_train.jsonl  — все трейсы (для обучения оркестратора 14B)
  exec_<skill>.jsonl — разбивка по скиллу (для исполнителей 7B)

CLI:
  python3 assistant/orch_dataset.py stats        — статистика + целевые метрики
  python3 assistant/orch_dataset.py export       — собрать SFT-датасеты
  python3 assistant/orch_dataset.py review [N]   — показать N примеров
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))
TRACES_DIR = BASE / "registry" / "dataset" / "orch_traces"
SFT_DIR    = BASE / "registry" / "dataset" / "orch_sft"
SFT_DIR.mkdir(parents=True, exist_ok=True)

QUALITY_THRESHOLD = 0.7
JACCARD_THRESHOLD = 0.85
MIN_RESULT_LEN    = 50

# Целевые метрики для старта fine-tuning
TARGETS = {
    "orchestrator": {"need": 500,  "skills": None},
    "exec_search":  {"need": 300,  "skills": ["search"]},
    "exec_analysis":{"need": 200,  "skills": ["reasoning"]},
}


def _tokens(text: str) -> set[str]:
    return set(text.lower().split())


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class OrchDatasetBuilder:
    """Пайплайн трейсов оркестратора → ChatML SFT."""

    def _load_traces(self) -> list[dict]:
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

    def _filter(self, traces: list[dict]) -> list[dict]:
        out = []
        for t in traces:
            if t.get("outcome") != "success":
                continue
            if t.get("quality", 0) < QUALITY_THRESHOLD:
                continue
            if len(t.get("result", "")) < MIN_RESULT_LEN:
                continue
            out.append(t)
        return out

    def _dedup(self, traces: list[dict]) -> list[dict]:
        seen: list[str] = []
        out: list[dict] = []
        for t in traces:
            task   = t.get("task", "")
            is_dup = any(_jaccard(task, s) >= JACCARD_THRESHOLD for s in seen)
            if not is_dup:
                seen.append(task)
                out.append(t)
        return out

    def _to_chatml(self, trace: dict) -> dict:
        return {
            "messages":  trace.get("messages", []),
            "quality":   trace.get("quality", 0.0),
            "outcome":   trace.get("outcome", "unknown"),
            "skill":     trace.get("skill", "unknown"),
            "ts":        trace.get("ts", ""),
            "task_type": trace.get("task_type", "unknown"),
        }

    def export(self, verbose: bool = True) -> dict:
        """Собрать и экспортировать SFT-датасеты."""
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw = self._load_traces()

        if verbose:
            print(f"Загружено трейсов: {len(raw)}")

        filtered = self._filter(raw)
        if verbose:
            print(f"После фильтра (success + quality≥{QUALITY_THRESHOLD}): {len(filtered)}")

        deduped = self._dedup(filtered)
        if verbose:
            print(f"После дедупликации (Jaccard≥{JACCARD_THRESHOLD}): {len(deduped)}")

        if not deduped:
            return {"status": "empty", "raw": len(raw), "filtered": 0, "deduped": 0, "orch_rows": 0}

        # ── Оркестратор SFT ───────────────────────────────────────────────────
        orch_rows = [self._to_chatml(t) for t in deduped]
        orch_file = SFT_DIR / f"orch_train_{ts}.jsonl"
        with orch_file.open("w", encoding="utf-8") as f:
            for row in orch_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        latest = SFT_DIR / "orch_train.jsonl"
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(orch_file.name)

        if verbose:
            print(f"Оркестратор SFT: {len(orch_rows)} строк → {orch_file.name}")

        # ── Исполнители SFT (по скиллу) ──────────────────────────────────────
        by_skill: dict[str, list[dict]] = defaultdict(list)
        for t in deduped:
            by_skill[t.get("skill", "general")].append(t)

        exec_files: dict[str, str] = {}
        for skill, skill_traces in by_skill.items():
            exec_rows = [self._to_chatml(t) for t in skill_traces]
            exec_file = SFT_DIR / f"exec_{skill}_{ts}.jsonl"
            with exec_file.open("w", encoding="utf-8") as f:
                for row in exec_rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

            sym = SFT_DIR / f"exec_{skill}.jsonl"
            if sym.is_symlink() or sym.exists():
                sym.unlink()
            sym.symlink_to(exec_file.name)

            exec_files[skill] = exec_file.name
            if verbose:
                print(f"  exec/{skill}: {len(exec_rows)} строк")

        result = {
            "status":     "ok",
            "ts":         ts,
            "raw":        len(raw),
            "filtered":   len(filtered),
            "deduped":    len(deduped),
            "orch_rows":  len(orch_rows),
            "exec_files": exec_files,
            "orch_file":  str(orch_file),
            "skills":     list(by_skill.keys()),
        }

        mf = SFT_DIR / "manifest.json"
        manifest: list[dict] = []
        if mf.exists():
            try:
                manifest = json.loads(mf.read_text())
            except Exception:
                manifest = []
        manifest.append(result)
        mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

        return result

    def stats(self) -> dict:
        """Статистика: трейсы, SFT-файлы, целевые метрики."""
        from agent.orch_tracer import OrchestratorTracer
        tracer_st = OrchestratorTracer().stats()

        sft_files = [f for f in SFT_DIR.glob("*.jsonl") if not f.is_symlink()]
        sft_rows  = 0
        for f in sft_files:
            sft_rows += sum(1 for l in f.read_text(encoding="utf-8").splitlines() if l.strip())

        manifest: list[dict] = []
        mf = SFT_DIR / "manifest.json"
        if mf.exists():
            try:
                manifest = json.loads(mf.read_text())
            except Exception:
                pass

        by_skill = tracer_st.get("by_skill", {})
        targets  = {
            "orchestrator": {
                "need": 500, "have": tracer_st["total"],
                "ready": tracer_st["total"] >= 500,
            },
            "exec_search": {
                "need": 300, "have": by_skill.get("search", 0),
                "ready": by_skill.get("search", 0) >= 300,
            },
            "exec_analysis": {
                "need": 200, "have": by_skill.get("reasoning", 0),
                "ready": by_skill.get("reasoning", 0) >= 200,
            },
        }

        return {
            "traces":      tracer_st,
            "sft_files":   len(sft_files),
            "sft_rows":    sft_rows,
            "exports":     len(manifest),
            "last_export": manifest[-1]["ts"] if manifest else None,
            "targets":     targets,
        }

    def review(self, n: int = 3) -> None:
        """Показать последние N трейсов."""
        from agent.orch_tracer import OrchestratorTracer
        traces = OrchestratorTracer().tail(n)
        if not traces:
            print("Трейсов нет.")
            return
        for i, t in enumerate(traces, 1):
            print(f"\n── Трейс {i} ──────────────────────────────────────")
            print(f"  ts:        {t['ts'][:19]}")
            print(f"  задача:    {t['task'][:100]}")
            print(f"  скилл:     {t.get('skill','?')}  модель: {t.get('model','?')}")
            print(f"  исход:     {t.get('outcome','?')}  качество: {t.get('quality','?')}")
            print(f"  результат: {t.get('result','')[:150]}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    builder = OrchDatasetBuilder()
    sub     = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if sub == "stats":
        st = builder.stats()
        tr = st["traces"]
        print(f"Трейсы: {tr['total']} всего  success_rate={tr['success_rate']}  avg_quality={tr['avg_quality']}")
        print(f"  по скиллам: {tr.get('by_skill', {})}")
        print(f"SFT файлы: {st['sft_files']}  строк: {st['sft_rows']}")
        print(f"Экспортов: {st['exports']}  последний: {st['last_export']}")
        print(f"\nЦелевые метрики (для старта fine-tuning):")
        for k, v in st["targets"].items():
            mark = "✅" if v["ready"] else f"❌ ({v['have']}/{v['need']})"
            print(f"  {k}: {mark}")

    elif sub == "export":
        result = builder.export(verbose=True)
        if result["status"] == "empty":
            print("Нет данных для экспорта.")
        else:
            print(f"\nГотово: {result['orch_rows']} строк оркестратор, скиллы: {result.get('skills', [])}")

    elif sub == "review":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        builder.review(n)

    else:
        print("Команды: stats, export, review [N]")
