"""
agent/orch_external_evidence.py — Delayed External Evidence.

PET_AGENT_BOUNDARY_AUDIT.md Phase 4C: minimal AGENT-owned adapter for
recording a validation result that arrives AFTER a request's response was
already returned and its trace persisted (pet's post-hoc DeepSeek/P2P/
local-model validation in chat_orch.py::_bg_validate). Agent owns the
interpretation and persistence of this event; pet performs only the
transport (asking external validators, already implemented via existing
agent/ functions - orch_node_selector/orch_validator/orch_arbiter/
orch_ai_validator) and reports the raw result here, linked by trace_id.

Scope, deliberately minimal (per the customer's own Phase 4C decision,
and "НЕ ТРОГАТЬ ПОКА: ... Self-learning"): this records the delayed event
and links it to the original trace by trace_id, capturing the original
canonical Trust for later before/after comparison. It does NOT recompute
canonical Trust automatically and does NOT mutate the original trace
file - that is roadmap Phase I-2/I-3 (Delayed Supervision / Outcome
Revision) territory, explicitly out of scope here. "Trust не меняется
только ради consensus" (the customer's own words) is satisfied trivially
in this minimal version: nothing in this module ever changes Trust: it
only appends an immutable, trace-linked observation for a future,
separately-scoped Delayed Supervision mechanism to consume.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

BASE       = Path(__file__).parent.parent
TRACES_DIR = BASE / "registry" / "dataset" / "orch_traces"
EVENTS_DIR = BASE / "registry" / "dataset" / "delayed_validation"
EVENTS_DIR.mkdir(parents=True, exist_ok=True)


def find_trace_by_id(trace_id: str, max_files: int = 14) -> Optional[dict]:
    """Найти persisted trace по trace_id.

    Сканирует последние `max_files` day-bucketed файлов
    (registry/dataset/orch_traces/*.jsonl), самые свежие первыми -
    delayed evidence обычно приходит минуты/часы, редко дни спустя.
    Линейный скан, без индекса: приемлемо для текущего объёма
    (сотни записей на файл); если объём вырастет на порядки, потребуется
    отдельный index - не строится здесь заранее без доказанной нужды.
    """
    if not trace_id:
        return None
    files = sorted(TRACES_DIR.glob("*.jsonl"), reverse=True)[:max_files]
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("trace_id") == trace_id:
                return d
    return None


def record_delayed_validation(
    trace_id: str,
    source: str,
    verdict: str,
    reason: str = "",
    raw: str = "",
) -> dict:
    """Persist a delayed external validation event, linked to its
    originating trace by id (if found - trace_id may be empty or the
    trace may have aged out of the scanned window; both are recorded
    honestly via trace_found, not silently dropped or faked).

    Never mutates the original trace file and never recomputes canonical
    Trust - see this module's docstring for why that boundary is
    deliberate, not an oversight.

    Returns the recorded event dict, so the caller (pet) can project it
    into a UI without re-deriving anything or computing its own verdict.
    """
    trace = find_trace_by_id(trace_id)
    event = {
        "event_id":       f"dv_{int(time.time())}_{uuid.uuid4().hex[:8]}",
        "trace_id":       trace_id,
        "trace_found":    trace is not None,
        "original_trust": trace.get("trust") if trace else None,
        "source":         source,
        "verdict":        verdict,
        "reason":         reason,
        "raw":            (raw or "")[:2000],
        "recorded_at":    time.time(),
    }

    day      = time.strftime("%Y%m%d")
    out_file = EVENTS_DIR / f"{day}.jsonl"
    with open(out_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    return event


def get_delayed_validations(trace_id: str, max_files: int = 30) -> list[dict]:
    """Вернуть все delayed-validation события для данного trace_id
    (для UI/будущего self-learning: 'что происходило с этой трассой
    после того, как ответ был отдан')."""
    if not trace_id:
        return []
    out: list[dict] = []
    files = sorted(EVENTS_DIR.glob("*.jsonl"), reverse=True)[:max_files]
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("trace_id") == trace_id:
                out.append(d)
    return out
