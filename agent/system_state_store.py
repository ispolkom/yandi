"""
agent/system_state_store.py — SYSTEM AWARENESS V1: STORE + COMPARE
(mandate §6/§7).

AUDIT-informed reuse: this codebase already has TWO established
persistence conventions this module reuses rather than inventing a
third —
    1. a mutable "current state" JSON file, load-on-init/save-on-write
       (agent.belief_manager's beliefs.json, agent.claim_family_
       registry's claim_families.json) — `latest.json` here follows the
       exact same shape and fail-safe-on-corruption `_load()` contract.
    2. an append-only JSONL file, one record per line, opened in "a"
       mode (agent.orch_tracer's day-based trace files, registry/
       orch_metrics.jsonl) — `history.jsonl` here is the same pattern.

No SQL dependency anywhere in this module (mandate: "SYSTEM STATE V1
НЕ ЗАВИСИТ ОТ SQL" — agent/db/sql/* is not imported; migrating system
observations into SQL canonical memory is explicitly future work, not
started here, and does not reopen 5E-S/5E-S2/5F).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.system_awareness import build_snapshot, fingerprint, compare

BASE = Path(__file__).parent.parent
STATE_DIR = BASE / "registry" / "system_state"
LATEST_PATH = STATE_DIR / "latest.json"
HISTORY_PATH = STATE_DIR / "history.jsonl"


def _load_latest(path: Path = LATEST_PATH) -> Optional[Dict[str, Any]]:
    """Fail-safe on corruption — matches belief_manager.py/claim_family_
    registry.py's own established `_load()` convention exactly: a
    corrupt/unreadable file is treated as "no prior state" (never
    crashes the probe, mandate §14's explicit "corrupted latest
    recovery" test), and is left UNTOUCHED on disk until the next
    successful save (never blindly overwritten by a hasty "fix")."""
    path = Path(path)  # accept a plain str path too (e.g. tempfile.mktemp()'s return value)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_latest(snapshot: Dict[str, Any], snapshot_fingerprint: str, path: Path = LATEST_PATH) -> None:
    """Atomic write (temp file + rename) — a crash mid-write must never
    leave a half-written, unparseable latest.json."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"snapshot": snapshot, "fingerprint": snapshot_fingerprint, "saved_at": time.time()}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp_path.replace(path)


def _append_history(
    snapshot: Dict[str, Any], snapshot_fingerprint: str, delta: Dict[str, Any], path: Path = HISTORY_PATH,
) -> None:
    """APPEND-ONLY (mandate §6/§7) — never truncates, never rewrites a
    previous line. One JSON object per line, matching orch_tracer.py's
    established JSONL convention."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "fingerprint": snapshot_fingerprint,
        "recorded_at": time.time(),
        "snapshot": snapshot,
        "delta_from_previous": delta,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def update_state(
    probe_source: str = "agent_local_probe",
    latest_path: Path = LATEST_PATH,
    history_path: Path = HISTORY_PATH,
) -> Dict[str, Any]:
    """OBSERVE -> NORMALIZE -> STORE -> COMPARE, in one call (mandate's
    own pipeline name).

    `latest.json` is ALWAYS overwritten with the freshest full snapshot
    (mandate: "latest: derived/current projection, может
    перезаписываться" — it should reflect the newest observation in
    full, not just have its timestamp bumped, so a reader always sees
    current free-memory/disk numbers even between material changes).

    `history.jsonl` ONLY gets a new line when the snapshot's
    FINGERPRINT changed from the previously stored one (mandate §7:
    never flood history from routine numeric noise — the fingerprint
    already excludes timestamps and buckets disk-usage percentages,
    see agent.system_awareness.fingerprint()/_material_view()).

    Returns {"snapshot", "fingerprint", "material_change": bool,
    "delta": {...}} — `delta` is empty on the very first observation
    (nothing to compare against) and non-empty exactly when material_
    change is True and a prior observation existed.
    """
    new_snapshot = build_snapshot(probe_source=probe_source)
    new_fp = fingerprint(new_snapshot)

    prior = _load_latest(latest_path)
    material_change = prior is None or prior.get("fingerprint") != new_fp

    delta: Dict[str, Any] = {}
    if material_change:
        if prior is not None and isinstance(prior.get("snapshot"), dict):
            delta = compare(prior["snapshot"], new_snapshot)
        _append_history(new_snapshot, new_fp, delta, path=history_path)

    _save_latest(new_snapshot, new_fp, path=latest_path)

    return {"snapshot": new_snapshot, "fingerprint": new_fp, "material_change": material_change, "delta": delta}


def get_latest(path: Path = LATEST_PATH) -> Optional[Dict[str, Any]]:
    return _load_latest(path)


def read_history(limit: int = 50, path: Path = HISTORY_PATH) -> List[Dict[str, Any]]:
    """Reads up to the last `limit` history entries. A single corrupted
    line is skipped, not fatal to reading the rest (same fail-safe
    posture as _load_latest())."""
    path = Path(path)
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def summary_line(result: Dict[str, Any]) -> str:
    """One concise, human-readable log line for AGENT startup (mandate
    §12) — never the full snapshot dict."""
    snap = result["snapshot"]
    os_ = snap.get("os", {})
    cpu = snap.get("cpu", {})
    mem = snap.get("memory", {})
    gpu = snap.get("gpu", {})

    gpu_desc = "none/unknown"
    if gpu.get("status") == "PRESENT" and gpu.get("gpus"):
        gpu_desc = ", ".join(f"{g.get('model')} ({g.get('driver_version')})" for g in gpu["gpus"])

    mem_total = mem.get("total_bytes")
    mem_gb = round(mem_total / 1e9, 1) if isinstance(mem_total, (int, float)) else "?"

    state_word = "CHANGED" if result.get("material_change") else "unchanged"

    return (
        f"[SystemAwareness] {os_.get('distro', '?')} kernel={os_.get('kernel', '?')} "
        f"cpu={cpu.get('model', '?')} mem={mem_gb}GB gpu={gpu_desc} "
        f"fingerprint={result['fingerprint'][:12]} state={state_word}"
    )
