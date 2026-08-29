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


# V1.1 (mandate §4/§5/§6): state words distinguish WHY a fingerprint
# is being treated as new/unchanged, not just whether it is —
# specifically so "latest.json was corrupted, but we recovered" (a
# genuinely different situation, worth a different startup log line)
# is never silently reported as an ordinary NEW.
STATE_NEW = "NEW"
STATE_UNCHANGED = "UNCHANGED"
STATE_CHANGED = "CHANGED"
STATE_RECOVERED = "RECOVERED"


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
    FINGERPRINT changed from the previously stored one, OR when latest.
    json could not be trusted at all (mandate §5: a corrupted latest.json
    is NEVER treated as a valid "previous snapshot" to diff against —
    the only honest thing to do is record the current, successfully-
    probed state as a fresh history entry, marked RECOVERED, not silently
    assume nothing changed).

    Returns {"snapshot", "fingerprint", "material_change": bool,
    "delta": {...}, "state": one of STATE_NEW/UNCHANGED/CHANGED/
    RECOVERED}. `material_change` is kept for backward compatibility
    with existing callers/tests — it is True for NEW/CHANGED/RECOVERED,
    False only for UNCHANGED.
    """
    new_snapshot = build_snapshot(probe_source=probe_source)
    new_fp = fingerprint(new_snapshot)

    latest_path = Path(latest_path)
    file_existed_before = latest_path.exists()
    prior = _load_latest(latest_path)
    latest_was_corrupted = file_existed_before and prior is None

    delta: Dict[str, Any] = {}

    if prior is None:
        state = STATE_RECOVERED if latest_was_corrupted else STATE_NEW
        material_change = True
        # delta stays {} — a corrupted prior state genuinely cannot be
        # diffed against (mandate §5: never treat it as a trustworthy
        # previous snapshot), and a first-ever observation has nothing
        # to compare against either. Both are honest emptiness, not a
        # claim that "nothing changed".
    elif prior.get("fingerprint") == new_fp:
        state = STATE_UNCHANGED
        material_change = False
    else:
        state = STATE_CHANGED
        material_change = True
        if isinstance(prior.get("snapshot"), dict):
            delta = compare(prior["snapshot"], new_snapshot)

    if material_change:
        _append_history(new_snapshot, new_fp, delta, path=history_path)

    _save_latest(new_snapshot, new_fp, path=latest_path)

    return {
        "snapshot": new_snapshot, "fingerprint": new_fp,
        "material_change": material_change, "delta": delta, "state": state,
    }


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
    §4/§12) — NEVER the full snapshot dict (mandate: "Не печатай весь
    snapshot при каждом startup").

    NEW/RECOVERED (the two states where a human benefits from full
    context — a fresh install, or a just-repaired corrupted state file)
    get the one-time descriptive line. UNCHANGED/CHANGED (the routine
    case on every subsequent startup) get the SHORT form the mandate's
    own examples show verbatim: "[SystemAwareness] CHANGED fp=...
    delta=3" / "[SystemAwareness] UNCHANGED fp=..." — a change lists
    which TOP-LEVEL SECTIONS changed (e.g. "gpu, storage"), never the
    full before/after values, and never a causal explanation (mandate
    §8: no "GPU driver changed caused X" — just the fact that section
    changed)."""
    fp_short = result["fingerprint"][:12]
    state = result.get("state") or (STATE_CHANGED if result.get("material_change") else STATE_UNCHANGED)

    if state in (STATE_NEW, STATE_RECOVERED):
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

        return (
            f"[SystemAwareness] {state} {os_.get('distro', '?')} kernel={os_.get('kernel', '?')} "
            f"cpu={cpu.get('model', '?')} mem={mem_gb}GB gpu={gpu_desc} fingerprint={fp_short}"
        )

    if state == STATE_CHANGED:
        changed_sections = sorted((result.get("delta") or {}).keys())
        sections_desc = f" ({', '.join(changed_sections)})" if changed_sections else ""
        return f"[SystemAwareness] CHANGED fp={fp_short} delta={len(changed_sections)}{sections_desc}"

    return f"[SystemAwareness] UNCHANGED fp={fp_short}"
