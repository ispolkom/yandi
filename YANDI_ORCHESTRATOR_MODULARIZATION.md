# YANDI Orchestrator Modularization — Living Status

Strangler-fig migration of `agent/orchestrator_v2.py` into `agent/orchestrator/`.
This document is updated as the migration progresses. See
`YANDI_ORCHESTRATOR_MODULARIZATION_MAP.md` for the one-time structural audit
this migration is executing against.

`orchestrator_v2.py` remains the canonical production entry point, CLI, and
behavioral reference throughout the migration. It is **not** deleted. Each
extraction moves ownership of one bounded block of logic into
`agent/orchestrator/*`; `orchestrator_v2.py` calls the moved function instead
of running the logic inline. There is one implementation per block, never two.

---

## STATUS

```
ORCHESTRATOR MODULARIZATION STATUS: PARTIAL
LEGACY orchestrator_v2.py:           PRIMARY (still owns process(), the CLI, and ~85% of pipeline logic)
BEHAVIORAL EQUIVALENCE:              CONFIRMED (13/13 pre-existing regression suites green after every commit; 48/48 new equivalence checks green)
READY FOR EPISTEMIC ARCHITECTURE WORK: PARTIALLY (boundaries exist for epistemic/*, more extraction needed before it's comfortable)
```

## MILESTONE 1 — package skeleton + first low-risk extractions

**Result: DONE.**

- `agent/orchestrator/` created with `epistemic/`, `runtime/`, `response/`,
  `claims/` subpackages (no `registry/` subpackage yet — nothing extracted
  into it so far; not created empty per the "no files just to match a
  template" rule).
- 6 blocks extracted (see MOVED COMPONENTS below).
- `orchestrator_v2.py`: 5620 → 4751 lines (−869, −15.5%).
- `agent/orchestrator/`: 1079 lines across 11 files.
- All 13 pre-existing regression suites green before and after every single
  commit (never batched — one extraction, one full regression run, one
  commit).
- New `agent/orchestrator_modularization_regression_test.py`: 48 deterministic
  checks pinning the extracted units' behavior (see EQUIVALENCE STATUS below
  for why this is a behavioral pin, not an old-vs-new branch comparison).
- No duplicate implementations introduced.
- `git status` clean after each commit.

## CURRENT LEGACY STRUCTURE

```
agent/orchestrator_v2.py (4751 lines)
    lines 1-568   top-level helpers (mostly pure; a few still to extract)
    lines ~570-750  process() body starts around here (was 843 pre-extraction)
    process()     still one unbroken function, ~3900 lines
                  (was 4703 lines / 84% of the file before this milestone)
    interactive() + CLI entrypoint (untouched, P3 per the migration brief)
```

## TARGET STRUCTURE

```
agent/orchestrator/
    __init__.py
    pipeline.py            (not yet created — P3, process() itself moves last)
    context.py              (not yet created — deferred per the brief: don't
                             introduce a PipelineContext until several more
                             modules are proven out)
    pre_pipeline.py         (not yet created — P1/P2 candidate)
    discovery.py            (not yet created — P2 candidate)
    synthesis.py             (not yet created — P1/P2 candidate)
    claims/
        __init__.py
        status.py           ✅ DONE (this milestone)
        lifecycle.py        (not yet created — P1)
        validation.py       (not yet created — P1, thin wrapper over claim_validator.py)
        mapping.py          (not yet created — P1, needs _run_claim_evidence_batch de-closured first)
        retrieval.py        (not yet created — P2, thin wrapper over claim_evidence_retriever.py)
        disagreement.py     (not yet created — P2/P3, large, self-contained)
    epistemic/
        __init__.py
        existence_contract.py  ✅ DONE
        final_coverage.py      ✅ DONE
        trust_gate.py           ✅ DONE
    runtime/
        __init__.py
        profiling.py         ✅ DONE
        timeout.py            (dropped from target — agent/orch_timeout.py
                               already owns this; no orchestrator_v2-specific
                               logic left to extract, confirmed during audit)
        shared_work.py        (not yet created — candidate for
                               generate_local_answer/blind_analysis, P1/P2)
    registry/                (not yet created — nothing extracted into it yet)
        integration.py
    response/
        __init__.py
        assembly.py          ✅ DONE
```

## MOVED COMPONENTS (this milestone, in commit order)

| # | Commit | Extracted | From (orig. lines, pre-migration) | To |
|---|---|---|---|---|
| 1 | `e35ddb7` | Existence Query Contract | 5025-5104 | `agent/orchestrator/epistemic/existence_contract.py` |
| 2 | `a613e84` | Final Claim Coverage orchestration | 4453-4562 | `agent/orchestrator/epistemic/final_coverage.py` |
| 3 | `1f1b166` | Pipeline wall-clock `[PROFILE]` report | 5010-5117 | `agent/orchestrator/runtime/profiling.py` |
| 4 | `9174dc9` | `TRUST_STATES`/`_TRUST_ORDER`/`_calculate_delta_factors`/`_apply_trust_cap` + epistemic trust adjustment block | 357-472 (helpers) + 4464-4651 (block) | `agent/orchestrator/epistemic/trust_gate.py` |
| 5 | `1cf3fa7` | `build_self_answer`, `_generate_character_response`, `_generate_apology_response`, `_adapt_answer_to_style`, `_generate_vulgar_response` (dead, moved with siblings) | 618-773 | `agent/orchestrator/response/assembly.py` |
| 6 | `f37be5f` | Claim epistemic status classification (`---- CLAIM EPISTEMIC STATUS ----` block) | 3267-3476 | `agent/orchestrator/claims/status.py` |

Each row was verified against the pre-move source with an exact whitespace-
normalized diff before the call site was rewired (see each commit body for
the specific diff notes — e.g. free-variable renames like `_belief_manager`
→ `belief_manager` parameter, or module-level constant hoisting for
`HARD_BLOCKED_SOURCE_CLASSES`/`DIRECTNESS_SUPPORT_THRESHOLD`).

## REMAINING COMPONENTS (still inline in `process()`)

Per the structural audit map, largest remaining undifferentiated regions,
highest risk first:

- **`[10]` Optimistic respond** (~440 lines): background-validation kickoff
  (`nonlocal` closure), cache write, query archive write, V3 memory/
  reflection/dataset write-back (8+ ordered side-effecting calls in one
  try/except), trust banner selection, final `OrchestratorResponse` return.
  HIGH risk — dense side-effect ordering, forward-reads from earlier phases.
- **`[8]` Synthesize + claim/evidence lifecycle** (~2500 lines): frame
  construction, `synthesize()` call, evidence pool assembly, claim identity/
  validation/mapping (pass1+pass2), claim retrieval, belief update,
  claim↔answer linking, claim↔claim disagreement (own raw HTTP session).
  Contains the still-closured `_run_claim_evidence_batch` (2 call sites,
  must be de-closured before extraction — P1 prerequisite, not yet done).
- **Pre-pipeline** (~600 lines, 13 short-circuit early-return branches):
  scene/target/intent/self-query/entity/strategy/swear/criticism/boundary/
  song-social-reflection/context/early-gate. MEDIUM risk — each branch is
  simple but ordering between them is behaviorally load-bearing.
- **Discovery fan-out** (`ThreadPoolExecutor(max_workers=4)`, registry/web/
  refutation/local-answer parallel submit): not yet touched.

## IMPORT DEPENDENCY GRAPH

```
agent.orchestrator_v2
    → agent.orchestrator.epistemic.existence_contract
    → agent.orchestrator.epistemic.final_coverage
    → agent.orchestrator.epistemic.trust_gate
    → agent.orchestrator.runtime.profiling
    → agent.orchestrator.response.assembly
    → agent.orchestrator.claims.status
        (each of the above → existing domain modules only:
         agent.claim_evidence_retriever, agent.final_claim_coverage,
         agent.orch_registry_search; no orchestrator.* module imports
         another orchestrator.* module yet, and none import orchestrator_v2)
```

No circular imports found or introduced. Direction holds cleanly:
`agent.orchestrator.* → domain modules`, never the reverse, and
`agent.orchestrator_v2 → agent.orchestrator.*`, never the reverse.

## GLOBAL/SINGLETON HANDLING

No global/singleton ownership changed in this milestone. `_belief_manager`
(V6 singleton, owned by `_init_v3()` in orchestrator_v2.py) is passed into
`trust_gate.apply_epistemic_trust_adjustment()` and `claims/status.py`'s
counting logic as an explicit parameter rather than read as a module global
inside the new modules — this is the only structural change needed to make
the extracted code independent of orchestrator_v2.py's module globals, and
it does not change how many times any singleton is constructed or when.

## EQUIVALENCE STATUS

**CONFIRMED**, with a caveat on methodology: because this migration moves
code (single source of truth) rather than duplicating it, there is no
separate "old" implementation left to diff against once a block is
extracted — the old inline code is gone, replaced by a call into the new
module. So equivalence is established two ways:

1. **At extraction time**: an exact whitespace-normalized diff between the
   pre-move inline source and the new module's function body, done before
   the call site is rewired (documented per-block in each commit message).
2. **After extraction**: the full 13-suite pre-existing regression baseline,
   run end-to-end through `orchestrator_v2.py` (which now delegates to the
   new modules), green before and after every commit — this is the
   behavioral-equivalence proof for the system as a whole.
3. **Ongoing regression net**: `agent/orchestrator_modularization_regression_test.py`
   (48 checks) pins each extracted unit's behavior going forward, so future
   changes to `agent/orchestrator/*` get caught the same way the other 13
   suites catch domain-module regressions.

## NEXT EXTRACTION TARGET

Per the audit's P1 tier: de-closure `_run_claim_evidence_batch` (2 call
sites already prove its needed signature: explicit `log`/`verbose` params
instead of closing over `process()`'s locals) as a standalone prerequisite
step, then extract `claims/mapping.py` around it. In parallel,
`claims/validation.py` (thin wrapper over `agent.claim_validator.filter_claims`)
and `claims/lifecycle.py` (claim identity assignment + evidence pool
assembly) are similarly bounded P1 candidates that don't require the
de-closuring prerequisite.
