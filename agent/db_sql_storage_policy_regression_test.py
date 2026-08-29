"""
agent/db_sql_storage_policy_regression_test.py — Этап 5E-S2 §3/§4:
disk-space state machine + pre-write reserve guard. Closes
SECURITY_THREAT_MODEL.md's T27 ("NOT ADDRESSED, open").

Fully proven OFFLINE — pure arithmetic, no filesystem/DB needed.

Covers:
    - state classification considers absolute bytes, percentage, AND
      inode percentage (mandate: never a single number).
    - deteriorating state is IMMEDIATE (no hysteresis delay reacting to
      a disk that's actually filling up).
    - improving state requires clearing the EXIT threshold, not just
      failing to meet the ENTER threshold — no LOW<->NORMAL flapping at
      the boundary (mandate's own explicit example).
    - a large genuine improvement (this session's own real event: 96%
      free -> 40% used after manual cleanup) can jump straight from a
      severe state to NORMAL in one reading, without getting stuck
      waiting through intermediate hysteresis bands one at a time.
    - CORE INVARIANT: nothing in this module ever proposes deleting
      history — checked structurally (no delete-shaped identifier
      anywhere in the module).
    - check_pre_write_reserve(): CRITICAL/EXHAUSTED states block new
      writes outright; NORMAL/LOW_SPACE states still individually check
      the predicted operation against the hard reserve.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_storage_policy_regression_test
"""
from __future__ import annotations

import inspect

import agent.db.sql.storage_policy as sp_mod
from agent.db.sql.storage_policy import (
    StorageState, classify_storage_state, check_pre_write_reserve,
    LOW_ENTER_BYTES, CRITICAL_ENTER_BYTES, EXHAUSTED_ENTER_BYTES,
    LOW_EXIT_BYTES, CRITICAL_EXIT_BYTES, GIB,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


TOTAL = 218 * GIB  # this session's own real root filesystem size

# ============================================================
# Basic classification — multi-signal, not a single percentage.
# ============================================================

check(
    "NORMAL: plenty of absolute bytes AND plenty of percentage (this session's "
    "own real post-cleanup reading: 126G free of 218G)",
    classify_storage_state(free_bytes=126 * GIB, total_bytes=TOTAL) == StorageState.NORMAL,
)
check(
    "LOW_SPACE: absolute bytes below LOW_ENTER even though percentage alone "
    "would look fine (15G of a 90G disk is 16.7% — above the 15% PCT threshold — "
    "but 15G absolute is still below the 20G BYTES threshold, so LOW correctly wins)",
    classify_storage_state(free_bytes=15 * GIB, total_bytes=90 * GIB) == StorageState.LOW_SPACE,
    "15G absolute must still be flagged LOW even when the percentage alone looks fine",
)
check(
    "LOW_SPACE: percentage below LOW_ENTER_PCT even with more absolute bytes "
    "than a smaller disk's LOW threshold (a small disk needs the percentage axis)",
    classify_storage_state(free_bytes=21 * GIB, total_bytes=200 * GIB) == StorageState.LOW_SPACE,
    "21G is only 10.5% of 200G — below the 15% percentage threshold",
)
check(
    "CRITICAL_SPACE: below the critical absolute threshold",
    classify_storage_state(free_bytes=3 * GIB, total_bytes=TOTAL) == StorageState.CRITICAL_SPACE,
)
check(
    "STORAGE_EXHAUSTED: below the hard reserve floor",
    classify_storage_state(free_bytes=int(0.5 * GIB), total_bytes=TOTAL) == StorageState.STORAGE_EXHAUSTED,
)
check(
    "inode exhaustion forces at least CRITICAL_SPACE even with plenty of byte space",
    classify_storage_state(free_bytes=100 * GIB, total_bytes=TOTAL, free_inodes_pct=0.02) == StorageState.CRITICAL_SPACE,
    "2% free inodes with 100G free bytes must still be CRITICAL",
)


# ============================================================
# Deteriorating -> immediate, no hysteresis delay.
# ============================================================

check(
    "NORMAL -> CRITICAL_SPACE happens IMMEDIATELY on a bad reading (no delay "
    "reacting to a disk that's actually filling up)",
    classify_storage_state(free_bytes=3 * GIB, total_bytes=TOTAL, previous_state=StorageState.NORMAL) == StorageState.CRITICAL_SPACE,
)
check(
    "LOW_SPACE -> STORAGE_EXHAUSTED happens immediately (worsening is never delayed)",
    classify_storage_state(free_bytes=int(0.5 * GIB), total_bytes=TOTAL, previous_state=StorageState.LOW_SPACE) == StorageState.STORAGE_EXHAUSTED,
)


# ============================================================
# Improving -> requires clearing the EXIT threshold (hysteresis).
#
# Uses a 100G total specifically so the ABSOLUTE-BYTES thresholds are
# the binding constraint, not the percentage ones (20G/100G = 20% >
# 15% PCT-enter threshold; 30G/100G = 30% > 22.5% PCT-exit threshold) —
# isolates the byte-threshold hysteresis behavior cleanly, uncoupled
# from the percentage axis's own (already separately tested) behavior.
# ============================================================

TOTAL_HYST = 100 * GIB
just_above_low_enter = LOW_ENTER_BYTES + 1 * GIB  # 21G: clears ENTER (20G) but not EXIT (30G)
check(
    "LOW_SPACE precondition: a reading just above the ENTER threshold alone "
    "would classify as NORMAL from a cold start (no hysteresis without history)",
    classify_storage_state(free_bytes=just_above_low_enter, total_bytes=TOTAL_HYST) == StorageState.NORMAL,
)
check(
    "HYSTERESIS: the SAME reading, coming FROM LOW_SPACE, does NOT immediately "
    "flap back to NORMAL — it clears ENTER but not EXIT",
    classify_storage_state(free_bytes=just_above_low_enter, total_bytes=TOTAL_HYST, previous_state=StorageState.LOW_SPACE) == StorageState.LOW_SPACE,
    "mandate's own explicit anti-flapping example: LOW -> NORMAL -> LOW must not happen at the boundary",
)

well_above_low_exit = LOW_EXIT_BYTES + 5 * GIB  # 35G: clears EXIT (30G) with margin
check(
    "once the reading genuinely clears the EXIT threshold (with margin), LOW_SPACE "
    "DOES transition back to NORMAL",
    classify_storage_state(free_bytes=well_above_low_exit, total_bytes=TOTAL_HYST, previous_state=StorageState.LOW_SPACE) == StorageState.NORMAL,
)

check(
    "a reading between LOW_EXIT and CRITICAL_EXIT, coming from CRITICAL_SPACE, "
    "lands on LOW_SPACE (one step at a time), not a jump straight to NORMAL",
    classify_storage_state(
        free_bytes=CRITICAL_EXIT_BYTES + 1 * GIB, total_bytes=TOTAL_HYST, previous_state=StorageState.CRITICAL_SPACE,
    ) == StorageState.LOW_SPACE,
)


# ============================================================
# A LARGE genuine improvement (this session's own real event: the
# owner manually freeing disk space, 96% -> 40% used) jumps straight
# to NORMAL, not stuck waiting through each hysteresis band one poll
# at a time.
# ============================================================

check(
    "REAL EVENT (this session): CRITICAL_SPACE -> a massive real cleanup (126G "
    "free of 218G) -> NORMAL in one reading, not stuck at LOW_SPACE first",
    classify_storage_state(free_bytes=126 * GIB, total_bytes=TOTAL, previous_state=StorageState.CRITICAL_SPACE) == StorageState.NORMAL,
)
check(
    "same real event, starting from the most severe state (STORAGE_EXHAUSTED) "
    "also jumps straight to NORMAL on a sufficiently large genuine improvement",
    classify_storage_state(free_bytes=126 * GIB, total_bytes=TOTAL, previous_state=StorageState.STORAGE_EXHAUSTED) == StorageState.NORMAL,
)


# ============================================================
# CORE INVARIANT: never proposes deleting history.
# ============================================================

_src = inspect.getsource(sp_mod)
# Precise: looks for an actual CALL or DEFINITION shape, not any mention
# in prose — this module's own docstrings legitimately use words like
# "delete"/"truncate" while explaining that it never does either
# (the same class of false positive an earlier check in this session's
# work already hit once with a "generate_kek" docstring mention).
_forbidden_call_patterns = (
    "def delete", ".delete(", "DELETE FROM", "def truncate", "TRUNCATE ",
    "drop_oldest(", "def purge", ".purge(", "def evict", ".evict(", "os.remove(", "os.unlink(",
)
for forbidden in _forbidden_call_patterns:
    check(
        f"CORE INVARIANT: no {forbidden!r}-shaped call/definition anywhere in "
        f"storage_policy.py (LOW SPACE MUST NEVER CAUSE HISTORY DELETION)",
        forbidden not in _src,
    )


# ============================================================
# check_pre_write_reserve().
# ============================================================

allowed, reason = check_pre_write_reserve(
    free_bytes=100 * GIB, predicted_operation_bytes=1 * GIB, current_state=StorageState.NORMAL,
)
check("pre-write guard: NORMAL state with a small predicted operation -> allowed", allowed, reason)

blocked_critical, reason_critical = check_pre_write_reserve(
    free_bytes=100 * GIB, predicted_operation_bytes=1 * GIB, current_state=StorageState.CRITICAL_SPACE,
)
check(
    "pre-write guard: CRITICAL_SPACE blocks a new heavy write outright, even with "
    "plenty of raw bytes free right now",
    blocked_critical is False,
    reason_critical,
)

blocked_exhausted, reason_exhausted = check_pre_write_reserve(
    free_bytes=100 * GIB, predicted_operation_bytes=1 * GIB, current_state=StorageState.STORAGE_EXHAUSTED,
)
check("pre-write guard: STORAGE_EXHAUSTED blocks unconditionally", blocked_exhausted is False, reason_exhausted)

blocked_would_exhaust, reason_would_exhaust = check_pre_write_reserve(
    free_bytes=int(1.5 * GIB), predicted_operation_bytes=int(0.6 * GIB), current_state=StorageState.LOW_SPACE,
)
check(
    "pre-write guard: an operation that would leave less than the hard reserve "
    "is refused BEFORE it starts (never discovered mid-write)",
    blocked_would_exhaust is False,
    reason_would_exhaust,
)

allowed_low_ok, _ = check_pre_write_reserve(
    free_bytes=15 * GIB, predicted_operation_bytes=int(0.1 * GIB), current_state=StorageState.LOW_SPACE,
)
check(
    "pre-write guard: LOW_SPACE (not CRITICAL) still allows a SMALL operation that "
    "leaves plenty of reserve — LOW_SPACE is a warning state, not a write-blocking one",
    allowed_low_ok is True,
)

try:
    classify_storage_state(free_bytes=1, total_bytes=0)
    zero_total_raised = False
except ValueError:
    zero_total_raised = True
check("classify_storage_state() rejects total_bytes<=0 rather than dividing by zero", zero_total_raised)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
