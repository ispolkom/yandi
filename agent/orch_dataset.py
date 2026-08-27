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


def _safe_outcome(t: dict) -> dict:
    """trace["outcome"] is a dict on every real trace (OutcomeRecord); a
    malformed/legacy record could carry a stray non-dict value (e.g. the
    old, never-real "success" string this module used to check against) —
    guard against that instead of crashing on .get()."""
    outcome = t.get("outcome")
    return outcome if isinstance(outcome, dict) else {}


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

    # Foundation Repair P1 (YANDI_SELF_LEARNING_RECONCILIATION_AUDIT.md
    # "Dataset readiness" / orch_dataset.py SFT filter): the filter, dedup
    # key, and ChatML projection below previously referenced a trace schema
    # ("task"/"skill"/"model"/"messages"/"result"/"outcome"-as-string/
    # "quality"/"ts"/"task_type") that the real trace producer
    # (agent/orch_tracer.py::DecisionTracer/Trace) has never written —
    # root cause: schema divergence, not a miscalibrated threshold. 0/434
    # (later 0/440) real traces ever matched, silently, no exception.
    #
    # Fixed against the REAL schema (verified on live persisted traces):
    # top-level query/trust/timestamp/trace_id, nested
    # outcome{final_answer,trust_label,trust_score,coverage_ratio}, nested
    # epistemic{domain,...}.
    #
    # QUALITY_THRESHOLD is intentionally NOT applied as a second filter
    # gate anymore (measured, not guessed, on the real 2026-08 dataset):
    # outcome.trust_score (== the synthesizer's raw confidence) never
    # exceeded 0.4 even for STRONGLY_SUPPORTED traces, and
    # outcome.coverage_ratio only ever takes the two hardcoded values
    # {0.0, 0.5} it is literally assigned as (writeback.py:
    # `coverage_ratio=0.5 if len(answer)>100 else 0.0` — not a real
    # coverage computation). Neither field is an honest, independent
    # "quality" signal distinct from the trust label today. Gating on
    # either would either (a) silently discard every trace, which is what
    # the original bug already did, unnoticed, or (b) require picking an
    # arbitrary lower threshold with no real justification — exactly what
    # this repair was told not to do ("не ослаблять фильтр просто ради
    # ненулевого результата"). The trust label itself (STRONGLY_SUPPORTED /
    # PARTIALLY_SUPPORTED) IS the project's real, already-computed,
    # canonical epistemic quality signal — kept as the primary gate below.
    # A genuine numeric SFT-quality score would need coverage_ratio (or an
    # equivalent) to be computed for real first — new-feature work, out of
    # this bug-fix's scope; flagged in the Foundation Repair report instead
    # of silently worked around.
    def _filter(self, traces: list[dict]) -> list[dict]:
        out = []
        for t in traces:
            outcome = _safe_outcome(t)
            trust = t.get("trust") or outcome.get("trust_label", "UNVERIFIED")
            # "success" for SFT purposes: an answer with genuine epistemic
            # support, not merely "the pipeline didn't crash". WEAKLY_SUPPORTED
            # and UNVERIFIED are explicitly low-confidence outcomes (see
            # ROADMAP_v7 canonical Trust ordering) and are not good positive
            # reasoning examples.
            if trust not in ("STRONGLY_SUPPORTED", "PARTIALLY_SUPPORTED"):
                continue
            final_answer = outcome.get("final_answer") or t.get("final_answer", "")
            if len(final_answer) < MIN_RESULT_LEN:
                continue
            out.append(t)
        return out

    def _dedup(self, traces: list[dict]) -> list[dict]:
        seen: list[str] = []
        out: list[dict] = []
        for t in traces:
            query  = t.get("query", "")
            is_dup = any(_jaccard(query, s) >= JACCARD_THRESHOLD for s in seen)
            if not is_dup:
                seen.append(query)
                out.append(t)
        return out

    def _to_chatml(self, trace: dict) -> dict:
        outcome = _safe_outcome(trace)
        query = trace.get("query", "")
        final_answer = outcome.get("final_answer") or trace.get("final_answer", "")
        epistemic = trace.get("epistemic") or {}
        return {
            "messages": [
                {"role": "user", "content": query},
                {"role": "assistant", "content": final_answer},
            ],
            "quality":  outcome.get("trust_score", 0.0) or 0.0,
            "outcome":  outcome.get("trust_label") or trace.get("trust", "unknown"),
            "domain":   epistemic.get("domain", "unknown"),
            "ts":       trace.get("timestamp", ""),
            "trace_id": trace.get("trace_id", ""),
        }

    def export(self, verbose: bool = True) -> dict:
        """Собрать и экспортировать SFT-датасеты."""
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw = self._load_traces()

        if verbose:
            print(f"Загружено трейсов: {len(raw)}")

        filtered = self._filter(raw)
        if verbose:
            print(f"После фильтра (trust ∈ {{STRONGLY_SUPPORTED, PARTIALLY_SUPPORTED}} + answer≥{MIN_RESULT_LEN} chars): {len(filtered)}")

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

        # ── Исполнители SFT (по домену) ──────────────────────────────────────
        # Foundation Repair P1: was grouped by a "skill" field that does not
        # exist anywhere in the real trace schema (always "general" for
        # every record, via the .get() default — never a real grouping).
        # epistemic.domain (factual/philosophical/interpretive/...) is the
        # real, populated dimension closest to this grouping's original
        # intent. Nothing outside this module reads the "exec_<x>.jsonl"
        # files or the "skills"/"domains" manifest key by name (checked),
        # so this rename is safe.
        by_domain: dict[str, list[dict]] = defaultdict(list)
        for t in deduped:
            domain = (t.get("epistemic") or {}).get("domain") or "general"
            by_domain[domain].append(t)

        exec_files: dict[str, str] = {}
        for domain, domain_traces in by_domain.items():
            exec_rows = [self._to_chatml(t) for t in domain_traces]
            exec_file = SFT_DIR / f"exec_{domain}_{ts}.jsonl"
            with exec_file.open("w", encoding="utf-8") as f:
                for row in exec_rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

            sym = SFT_DIR / f"exec_{domain}.jsonl"
            if sym.is_symlink() or sym.exists():
                sym.unlink()
            sym.symlink_to(exec_file.name)

            exec_files[domain] = exec_file.name
            if verbose:
                print(f"  exec/{domain}: {len(exec_rows)} строк")

        result = {
            "status":     "ok",
            "ts":         ts,
            "raw":        len(raw),
            "filtered":   len(filtered),
            "deduped":    len(deduped),
            "orch_rows":  len(orch_rows),
            "exec_files": exec_files,
            "orch_file":  str(orch_file),
            "domains":    list(by_domain.keys()),
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
        """Статистика: трейсы, SFT-файлы, целевые метрики.

        Foundation Repair P1: previously called OrchestratorTracer().stats()
        — OrchestratorTracer (agent/orch_tracer.py) is a no-op stub with a
        single `trace(*a, **kw): pass` method, no `stats()`/`tail()` at all,
        so this crashed with AttributeError on every invocation, unrelated
        to the separate _filter() schema bug. Computed directly from the
        real trace files instead (same source _load_traces() already reads
        for export()), no stub dependency.
        """
        raw = self._load_traces()
        total = len(raw)
        success = 0
        trust_scores = []
        by_domain: dict[str, int] = defaultdict(int)
        for t in raw:
            outcome = _safe_outcome(t)
            trust = t.get("trust") or outcome.get("trust_label", "UNVERIFIED")
            if trust in ("STRONGLY_SUPPORTED", "PARTIALLY_SUPPORTED"):
                success += 1
            score = outcome.get("trust_score")
            if isinstance(score, (int, float)):
                trust_scores.append(score)
            domain = (t.get("epistemic") or {}).get("domain") or "general"
            by_domain[domain] += 1

        tracer_st = {
            "total": total,
            "success_rate": round(success / total, 3) if total else 0.0,
            "avg_quality": round(sum(trust_scores) / len(trust_scores), 3) if trust_scores else 0.0,
            "by_domain": dict(by_domain),
        }

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

        # exec_search/exec_analysis: the roadmap's own future per-skill
        # executor split. No "skill" dimension exists in the real trace
        # schema (STILL_MISSING per the audit) — reporting 0/need here is
        # honest, not a fabricated mapping onto an unrelated dimension
        # (domain != skill).
        targets = {
            "orchestrator": {
                "need": 500, "have": total,
                "ready": total >= 500,
            },
            "exec_search": {
                "need": 300, "have": 0,
                "ready": False,
            },
            "exec_analysis": {
                "need": 200, "have": 0,
                "ready": False,
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
        """Показать последние N трейсов.

        Foundation Repair P1: previously called OrchestratorTracer().tail(n)
        (crashes, see stats() above) and read fields ("task"/"skill"/
        "model"/"outcome"-as-string/"quality"/"result") that don't exist in
        real traces. Reads the real files directly and prints real fields.
        """
        raw = self._load_traces()
        if not raw:
            print("Трейсов нет.")
            return
        for i, t in enumerate(raw[-n:], 1):
            outcome = _safe_outcome(t)
            epistemic = t.get("epistemic") or {}
            ts = t.get("timestamp")
            ts_str = datetime.fromtimestamp(ts).isoformat() if isinstance(ts, (int, float)) else str(ts)
            print(f"\n── Трейс {i} ──────────────────────────────────────")
            print(f"  ts:      {ts_str[:19]}")
            print(f"  запрос:  {t.get('query', '')[:100]}")
            print(f"  домен:   {epistemic.get('domain', '?')}")
            trust = t.get("trust") or outcome.get("trust_label", "?")
            score = outcome.get("trust_score", "?")
            print(f"  trust:   {trust}  trust_score: {score}")
            print(f"  ответ:   {(outcome.get('final_answer') or t.get('final_answer', ''))[:150]}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    builder = OrchDatasetBuilder()
    sub     = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if sub == "stats":
        st = builder.stats()
        tr = st["traces"]
        print(f"Трейсы: {tr['total']} всего  success_rate={tr['success_rate']}  avg_quality={tr['avg_quality']}")
        print(f"  по доменам: {tr.get('by_domain', {})}")
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
            print(f"\nГотово: {result['orch_rows']} строк оркестратор, домены: {result.get('domains', [])}")

    elif sub == "review":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        builder.review(n)

    else:
        print("Команды: stats, export, review [N]")
