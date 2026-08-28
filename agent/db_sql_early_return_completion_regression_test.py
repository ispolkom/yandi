"""
agent/db_sql_early_return_completion_regression_test.py — Этап 5 (SQL
persistence migration) regression: MIGRATION_STATUS.md §41 gap closed —
"pre_pipeline.py's ~11 early-return short-circuits do not call
shadow_complete_run() — a run that exits early via one of those paths
stays status='running' in SQL until reconciled."

Fix scope, widened beyond the literal §41 wording after re-checking the
real production source (mandate §50 — don't treat the prior audit as
gospel): agent/orchestrator_v2.py's process() has TWO structurally
identical `if early_response is not None: return early_response`
points — one after run_pre_pipeline() (pre_pipeline.py's 11
short-circuits), one after run_standard_pipeline() (that module's own
early-return short-circuits, e.g. a cache-hit direct answer). Both have
the identical "stuck at status='running' forever" gap; both are fixed
the same way, at the single call site in process() itself (not inside
either extracted function — avoids duplicating the fix across ~20+
individual return statements).

Full end-to-end functional testing of process() itself is deliberately
NOT attempted here — agent/claim_lifecycle_regression_test.py already
documents why: process() has too many live dependencies (session,
registry, self_model, memory, ...) to construct offline. This suite
uses the same structural-position technique agent/db_sql_wiring_
regression_test.py's checks A/B already established for exactly this
class of "call graph position in the real production source" claim.

Covers:
    A. shadow_complete_run( appears AFTER the run_pre_pipeline() early-
       return check, BEFORE `return early_response`.
    B. that call passes early_response.answer/trust_level (the SAME
       values this branch already decided) — not fabricated ones.
    C. same two checks for the run_standard_pipeline() early-return
       point.
    D. sanity: shadow_complete_run is passed run_id=trace_id and
       question_id=_sql_question_id — the same identity pair used at
       every other SQL call site in this file (no parallel identity
       scheme invented for this fix).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_early_return_completion_regression_test
"""
from __future__ import annotations

import inspect

import agent.orchestrator_v2 as orch_v2_mod

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


_src = inspect.getsource(orch_v2_mod)

# ============================================================
# A/B. run_pre_pipeline()'s early_response branch.
# ============================================================

_pos_pre_pipeline_call = _src.find("early_response, _pre_pipeline_state = run_pre_pipeline(")
_pos_pre_pipeline_if = _src.find("if early_response is not None:", _pos_pre_pipeline_call)
_pos_pre_pipeline_shadow = _src.find("shadow_complete_run(", _pos_pre_pipeline_if)
_pos_pre_pipeline_return = _src.find("return early_response", _pos_pre_pipeline_if)

check(
    "A: shadow_complete_run( is called between run_pre_pipeline()'s "
    "`if early_response is not None:` and its `return early_response`",
    -1 < _pos_pre_pipeline_if < _pos_pre_pipeline_shadow < _pos_pre_pipeline_return,
    f"if={_pos_pre_pipeline_if} shadow={_pos_pre_pipeline_shadow} return={_pos_pre_pipeline_return}",
)

_pre_pipeline_block = _src[_pos_pre_pipeline_if:_pos_pre_pipeline_return]
check(
    "B: the call uses early_response.answer/trust_level (this branch's OWN decided "
    "values), not a fabricated or hardcoded canonical_trust",
    "delivered_answer_text=early_response.answer" in _pre_pipeline_block
    and "canonical_trust=early_response.trust_level" in _pre_pipeline_block,
    f"{_pre_pipeline_block}",
)

# ============================================================
# C. run_standard_pipeline()'s early_response branch — same gap, same fix.
# ============================================================

_pos_std_call = _src.find("early_response, _pipeline_state = run_standard_pipeline(")
_pos_std_if = _src.find("if early_response is not None:", _pos_std_call)
_pos_std_shadow = _src.find("shadow_complete_run(", _pos_std_if)
_pos_std_return = _src.find("return early_response", _pos_std_if)

check(
    "C: shadow_complete_run( is ALSO called between run_standard_pipeline()'s "
    "`if early_response is not None:` and its `return early_response` "
    "(same gap the mandate documented only for pre_pipeline, found to apply here too)",
    _pos_std_call > _pos_pre_pipeline_call  # sanity: really the second call site
    and -1 < _pos_std_if < _pos_std_shadow < _pos_std_return,
    f"if={_pos_std_if} shadow={_pos_std_shadow} return={_pos_std_return}",
)

_std_block = _src[_pos_std_if:_pos_std_return]
check(
    "C: the second call site ALSO uses early_response.answer/trust_level, not fabricated",
    "delivered_answer_text=early_response.answer" in _std_block
    and "canonical_trust=early_response.trust_level" in _std_block,
    f"{_std_block}",
)

# ============================================================
# D. Identity consistency: run_id=trace_id, question_id=_sql_question_id
# — the same pair shadow_record_question_and_run() established earlier
# in this same function, not a parallel identity scheme.
# ============================================================

check(
    "D: pre_pipeline branch's shadow_complete_run uses run_id=trace_id, "
    "question_id=_sql_question_id (the identity pair already established earlier)",
    "run_id=trace_id, question_id=_sql_question_id" in _pre_pipeline_block,
    f"{_pre_pipeline_block}",
)
check(
    "D: standard_pipeline branch's shadow_complete_run ALSO uses the same identity pair",
    "run_id=trace_id, question_id=_sql_question_id" in _std_block,
    f"{_std_block}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
