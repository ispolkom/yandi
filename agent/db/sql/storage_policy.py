"""
agent/db/sql/storage_policy.py — Этап 5E-S2 §3/§4: disk-space state
machine + pre-write reserve guard (mandate §19's T27, finally
addressed — SECURITY_THREAT_MODEL.md's T27 was previously recorded as
"NOT ADDRESSED, open").

CORE PRINCIPLE (mandate, repeated verbatim on purpose):

    LOW SPACE MUST NEVER CAUSE HISTORY DELETION.

Nothing in this module ever deletes, truncates, or archives canonical
history to free space. Its only two jobs are (1) classify the current
state so a caller can decide whether to proceed, and (2) check a
specific planned operation against the available reserve BEFORE it
starts, so a large operation can't be left half-written when the disk
runs out mid-transaction.

DESIGNED + unit-tested this pass — not wired into any production write
path yet (same "primitive proven in isolation first" pattern as
crypto.py/integrity.py — see SECURITY_ARCHITECTURE.md §21). Wiring
requires deciding WHERE in the write path to call it, which itself
depends on the still-open dedicated-instance deployment (5E-S2's
Percona instance doesn't exist yet — see DISK_CAPACITY_REPORT.md and
the 5E-S2 final report's "5E-S2 LIVE READY" verdict).

Thresholds are deliberately NOT a single percentage (mandate: "Не
выбирай тупо один процент"). Every state considers absolute free
bytes AND percentage free AND (when available) free-inode percentage
— a huge disk at 15% free still has a lot of real headroom; a small
disk at 50% free might not. HYSTERESIS prevents a reading that
oscillates right at a boundary from flapping the reported state back
and forth (mandate: "Не прыгает: LOW -> NORMAL -> LOW"): moving to a
WORSE state is immediate (never delay reacting to a disk that's
actually filling up); moving to a BETTER state requires clearing an
EXIT threshold set above the ENTER threshold by HYSTERESIS_MARGIN.
"""
from __future__ import annotations

from typing import Optional, Tuple

GIB = 1024 ** 3


class StorageState:
    NORMAL = "NORMAL"
    LOW_SPACE = "LOW_SPACE"
    CRITICAL_SPACE = "CRITICAL_SPACE"
    STORAGE_EXHAUSTED = "STORAGE_EXHAUSTED"


_SEVERITY = {
    StorageState.NORMAL: 0,
    StorageState.LOW_SPACE: 1,
    StorageState.CRITICAL_SPACE: 2,
    StorageState.STORAGE_EXHAUSTED: 3,
}

# ── ENTER thresholds — crossing INTO a worse state ──────────────────────
# Defaults chosen to be conservative on a modest single-disk host (this
# session's own audit: a 218G root filesystem) — not universal physics,
# an operator can override them, but the SHAPE (absolute + percentage +
# inode, never a single number) is the actual design requirement.
LOW_ENTER_BYTES = 20 * GIB
LOW_ENTER_PCT = 0.15
CRITICAL_ENTER_BYTES = 5 * GIB
CRITICAL_ENTER_PCT = 0.05
EXHAUSTED_ENTER_BYTES = 1 * GIB  # the hard reserve floor
INODE_CRITICAL_FREE_PCT = 0.05

# ── EXIT thresholds — crossing BACK into a better state ─────────────────
# Must clear the ENTER threshold by this margin, creating a dead zone
# so a reading oscillating near the boundary doesn't flap the reported
# state (mandate: "LOW -> NORMAL -> LOW" must not happen).
HYSTERESIS_MARGIN = 1.5
LOW_EXIT_BYTES = LOW_ENTER_BYTES * HYSTERESIS_MARGIN
LOW_EXIT_PCT = LOW_ENTER_PCT * HYSTERESIS_MARGIN
CRITICAL_EXIT_BYTES = CRITICAL_ENTER_BYTES * HYSTERESIS_MARGIN
CRITICAL_EXIT_PCT = CRITICAL_ENTER_PCT * HYSTERESIS_MARGIN
EXHAUSTED_EXIT_BYTES = EXHAUSTED_ENTER_BYTES * HYSTERESIS_MARGIN


def classify_storage_state(
    *, free_bytes: int, total_bytes: int, previous_state: str = StorageState.NORMAL,
    free_inodes_pct: Optional[float] = None,
) -> str:
    """Returns the new StorageState given a current reading and the
    PREVIOUSLY reported state (hysteresis needs to know where it's
    coming from). Deteriorating is always immediate; improving requires
    clearing the relevant EXIT threshold."""
    if total_bytes <= 0:
        raise ValueError("total_bytes must be positive")
    free_pct = free_bytes / total_bytes
    inode_forces_critical = free_inodes_pct is not None and free_inodes_pct < INODE_CRITICAL_FREE_PCT

    if free_bytes < EXHAUSTED_ENTER_BYTES:
        raw_state = StorageState.STORAGE_EXHAUSTED
    elif free_bytes < CRITICAL_ENTER_BYTES or free_pct < CRITICAL_ENTER_PCT or inode_forces_critical:
        raw_state = StorageState.CRITICAL_SPACE
    elif free_bytes < LOW_ENTER_BYTES or free_pct < LOW_ENTER_PCT:
        raw_state = StorageState.LOW_SPACE
    else:
        raw_state = StorageState.NORMAL

    if _SEVERITY[raw_state] >= _SEVERITY[previous_state]:
        # Same or worse than before -> report immediately, no hysteresis
        # on the way down.
        return raw_state

    # raw_state looks BETTER than previous_state — only accept the
    # improvement if it clears the EXIT threshold of whatever state we
    # were previously in, checked from the most severe state downward
    # so a large, genuine improvement (e.g. an operator freeing a lot
    # of space at once) can skip straight to NORMAL in one reading.
    if previous_state == StorageState.STORAGE_EXHAUSTED and free_bytes < EXHAUSTED_EXIT_BYTES:
        return StorageState.STORAGE_EXHAUSTED
    if previous_state in (StorageState.STORAGE_EXHAUSTED, StorageState.CRITICAL_SPACE):
        if free_bytes < CRITICAL_EXIT_BYTES or free_pct < CRITICAL_EXIT_PCT or inode_forces_critical:
            return StorageState.CRITICAL_SPACE
    if previous_state in (StorageState.STORAGE_EXHAUSTED, StorageState.CRITICAL_SPACE, StorageState.LOW_SPACE):
        if free_bytes < LOW_EXIT_BYTES or free_pct < LOW_EXIT_PCT:
            return StorageState.LOW_SPACE
    return raw_state


def check_pre_write_reserve(
    *, free_bytes: int, predicted_operation_bytes: int, current_state: str,
) -> Tuple[bool, str]:
    """Mandate §4's PRE-WRITE SPACE GUARD: checked BEFORE a large
    canonical transaction starts, not discovered mid-write. Returns
    (allowed, reason). NEVER deletes anything to make room — a refusal
    here means the caller does not start the transaction at all, it
    does not mean YANDI frees space by discarding history."""
    if current_state == StorageState.STORAGE_EXHAUSTED:
        return (False, "storage exhausted — no new canonical writes permitted")
    if current_state == StorageState.CRITICAL_SPACE:
        return (False, "critical space — new heavy retrieval/persistence workloads blocked")
    projected_free = free_bytes - predicted_operation_bytes
    if projected_free < EXHAUSTED_ENTER_BYTES:
        return (
            False,
            f"predicted operation ({predicted_operation_bytes} bytes) would leave "
            f"{projected_free} bytes free, below the hard reserve ({EXHAUSTED_ENTER_BYTES} bytes)",
        )
    return (True, "")
