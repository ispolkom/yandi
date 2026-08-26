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
LEGACY orchestrator_v2.py:           PRIMARY (still owns process(), the CLI, and ~70% of pipeline logic)
BEHAVIORAL EQUIVALENCE:              CONFIRMED (14/14 regression suites green after every commit; 145/145 modularization equivalence checks green)
READY FOR EPISTEMIC ARCHITECTURE WORK: PARTIALLY (claims/* and epistemic/* fully bounded; [8] frame-construction/synthesis and [10] write-back still monolithic)
```

## MILESTONE 1 — package skeleton + first low-risk extractions

**Result: DONE.**

## MILESTONE 2 — claims/* lifecycle (identity → validation → mapping → retrieval)

**Result: DONE.**

## MILESTONE 3 — claims/* completed (belief/linker/personality, disagreement)

**Result: DONE.** `claims/*` is now a complete, bounded subpackage covering
the entire claim epistemic lifecycle from identity assignment through
disagreement detection.

- 12 blocks extracted total (see MOVED COMPONENTS below).
- `orchestrator_v2.py`: 5620 → 3410 lines (−2210, −39.3%).
- `agent/orchestrator/`: 2645 lines across 16 files.
- All 14 regression suites (13 pre-existing + the modularization equivalence
  suite) green before and after every single commit (never batched — one
  extraction, one full regression run, one commit).
- `agent/orchestrator_modularization_regression_test.py`: 145 deterministic
  checks (see EQUIVALENCE STATUS below for methodology).
- No duplicate implementations introduced.
- Five live sanity runs with `--web -v` across the milestone, each targeting
  the newly-extracted code path specifically (existence contract/final
  coverage/profiling/trust gate/response assembly/claim status; PASS1+PASS2
  batch NLI; Claim Resolution Gate + retrieval, including the added_count==0
  branch; belief update + personality cycle; claim↔claim disagreement,
  including a real embedding call) — all completed normally end-to-end.
- `git status` clean after each commit.

## CURRENT LEGACY STRUCTURE

```
agent/orchestrator_v2.py (3410 lines)
    lines 1-~450  top-level helpers (mostly pure; a few still to extract)
    process()     still one unbroken function, ~2900 lines
                  (was 4703 lines / 84% of the file before milestone 1)
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
    pre_pipeline.py         (not yet created — next major target: 13 short-
                             circuit early-return branches, ~600 lines)
    discovery.py            (not yet created — fan-out: registry/web/
                             refutation/local-answer, ThreadPoolExecutor)
    synthesis.py             (not yet created — frame construction through
                             the synthesize() call itself, ~800 lines)
    claims/                 ✅ COMPLETE subpackage
        __init__.py
        status.py           ✅ DONE
        validation.py       ✅ DONE
        lifecycle.py        ✅ DONE (claim identity/evidence pool setup +
                             belief update/linker/personality cycle)
        mapping.py          ✅ DONE (de-closured run_claim_evidence_batch)
        retrieval.py        ✅ DONE (Claim Resolution Gate + PASS2 retrieval)
        disagreement.py     ✅ DONE (embedding prefilter + batch NLI + challenge)
    epistemic/               ✅ COMPLETE subpackage
        __init__.py
        existence_contract.py  ✅ DONE
        final_coverage.py      ✅ DONE
        trust_gate.py           ✅ DONE
    runtime/
        __init__.py
        profiling.py         ✅ DONE
        timeout.py            (dropped from target — agent/orch_timeout.py
                               already owns this; confirmed during audit)
        shared_work.py        (not yet created — candidate for
                               generate_local_answer/blind_analysis)
    registry/                (not yet created — nothing extracted into it yet)
        integration.py
    response/                 ✅ COMPLETE subpackage
        __init__.py
        assembly.py          ✅ DONE
```

## MOVED COMPONENTS (in commit order)

| # | Commit | Extracted | From (orig. lines, pre-migration) | To |
|---|---|---|---|---|
| 1 | `e35ddb7` | Existence Query Contract | 5025-5104 | `agent/orchestrator/epistemic/existence_contract.py` |
| 2 | `a613e84` | Final Claim Coverage orchestration | 4453-4562 | `agent/orchestrator/epistemic/final_coverage.py` |
| 3 | `1f1b166` | Pipeline wall-clock `[PROFILE]` report | 5010-5117 | `agent/orchestrator/runtime/profiling.py` |
| 4 | `9174dc9` | `TRUST_STATES`/`_TRUST_ORDER`/`_calculate_delta_factors`/`_apply_trust_cap` + epistemic trust adjustment block | 357-472 (helpers) + 4464-4651 (block) | `agent/orchestrator/epistemic/trust_gate.py` |
| 5 | `1cf3fa7` | `build_self_answer`, `_generate_character_response`, `_generate_apology_response`, `_adapt_answer_to_style`, `_generate_vulgar_response` (dead, moved with siblings) | 618-773 | `agent/orchestrator/response/assembly.py` |
| 6 | `f37be5f` | Claim epistemic status classification (`---- CLAIM EPISTEMIC STATUS ----` block) | 3267-3476 | `agent/orchestrator/claims/status.py` |
| 7 | `0cbf172` | Structural claim validation (`STRUCTURAL CLAIM VALIDATION` block) | 2543-2648 | `agent/orchestrator/claims/validation.py` |
| 8 | `b94ed9f` | Claim & evidence lifecycle setup (CANONICAL EVIDENCE POOL + claim normalization + CLAIM IDENTITY + CLAIM QUERY CONTEXT) | 2409-2542 | `agent/orchestrator/claims/lifecycle.py` |
| 9 | `7926f2e` | `_run_claim_evidence_batch` de-closured (PASS1/PASS2 batch NLI) | 2526-2764 (nested closure) | `agent/orchestrator/claims/mapping.py` |
| 10 | `d84de56` | Claim Resolution Gate + PASS2 retrieval | 2556-2817 | `agent/orchestrator/claims/retrieval.py` |
| 11 | `74138d8` | Belief update + claim<->answer linker + personality cycle | 2668-2809 | `agent/orchestrator/claims/lifecycle.py` (2nd function) |
| 12 | `c406da6` | Claim<->claim disagreement (embedding prefilter + batch NLI + challenge) | 2680-3191 | `agent/orchestrator/claims/disagreement.py` |

Each row was verified against the pre-move source with an exact whitespace-
normalized diff before the call site was rewired (see each commit body for
the specific diff notes — free-variable renames like `_belief_manager` →
`belief_manager`, module-level constant hoisting, or the full free-variable
audit for `_run_claim_evidence_batch`, which captured exactly two names —
`verbose` and `log` — everything else was already a module-level import).

## REMAINING COMPONENTS (still inline in `process()`)

Per the structural audit map, largest remaining undifferentiated regions,
highest risk first:

- **`[10]` Optimistic respond** (~440 lines): background-validation kickoff
  (`nonlocal` closure), cache write, query archive write, V3 memory/
  reflection/dataset write-back (8+ ordered side-effecting calls in one
  try/except), trust banner selection, final `OrchestratorResponse` return.
  HIGH risk — dense side-effect ordering, forward-reads from earlier phases
  (`claims_accepted`/`total_claims` from the still-inline Claim Status Gate).
- **`[8]` Synthesize + frame construction** (~800 lines, now the largest
  remaining undifferentiated region within `[8]`): refutation scan,
  hypothesis graph, local-answer wait, blind analysis, source
  classification, the `synthesize()` call itself. Not yet touched —
  candidate `synthesis.py`.
- **`[9]` Claim Status Gate** (the counting half, not yet split from the
  now-extracted classification loop — status.py owns classification, the
  gate/messaging half that rewrites `synthesis_result.answer` for the 5
  mutually-exclusive cases is still inline): MEDIUM risk, forward-read of
  counts at `[10]`.
- **Pre-pipeline** (~600 lines, 13 short-circuit early-return branches):
  scene/target/intent/self-query/entity/strategy/swear/criticism/boundary/
  song-social-reflection/context/early-gate. MEDIUM risk — each branch is
  simple but ordering between them is behaviorally load-bearing.
- **Discovery fan-out** (`ThreadPoolExecutor(max_workers=4)`, registry/web/
  refutation/local-answer parallel submit): not yet touched.

## IMPORT DEPENDENCY GRAPH

```
agent.orchestrator_v2
    → agent.orchestrator.epistemic.{existence_contract,final_coverage,trust_gate}
    → agent.orchestrator.runtime.profiling
    → agent.orchestrator.response.assembly
    → agent.orchestrator.claims.{status,validation,lifecycle,mapping,retrieval,disagreement}
        (each of the above → existing domain modules only:
         agent.claim_evidence_retriever, agent.claim_evidence_mapper,
         agent.final_claim_coverage, agent.orch_registry_search,
         agent.evidence_pool, agent.claim_relation, agent.source_quality;
         claims.retrieval → claims.mapping (the one intra-package import
         so far — both are claims/*, no cross-subpackage or reverse
         dependency); none import orchestrator_v2)
```

No circular imports found or introduced. Direction holds cleanly:
`agent.orchestrator.* → domain modules` (+ one intra-`claims/*` import),
never the reverse, and `agent.orchestrator_v2 → agent.orchestrator.*`,
never the reverse.

## GLOBAL/SINGLETON HANDLING

No global/singleton ownership changed. V6 singletons (`_belief_manager`,
`_claim_answer_linker`, `_personality_core`, `_disagreement_engine`, `_claim_validator`)
are passed into the extracted `claims/*` functions as explicit parameters
rather than read as module globals inside them — the only structural change
needed to make the extracted code independent of orchestrator_v2.py's module
globals. Does not change how many times any singleton is constructed or when.

## EQUIVALENCE STATUS

**CONFIRMED**, with a caveat on methodology: because this migration moves
code (single source of truth) rather than duplicating it, there is no
separate "old" implementation left to diff against once a block is
extracted — the old inline code is gone, replaced by a call into the new
module. So equivalence is established four ways:

1. **At extraction time**: an exact whitespace-normalized diff between the
   pre-move inline source and the new module's function body, done before
   the call site is rewired (documented per-block in each commit message).
2. **After extraction**: the full 13-suite pre-existing regression baseline,
   run end-to-end through `orchestrator_v2.py` (which now delegates to the
   new modules), green before and after every commit.
3. **Ongoing regression net**: `agent/orchestrator_modularization_regression_test.py`
   (145 checks) pins each extracted unit's behavior going forward, including
   deterministic coverage of network-adjacent code (`claims/mapping.py`'s
   batch NLI, `claims/disagreement.py`'s embedding fail-open path) via
   monkeypatched dependencies — no real network calls in the test suite.
4. **Live sanity checks**: real `--web -v` runs after each extraction,
   confirmed normal end-to-end completion with correct log-marker shapes
   (not offline-mocked) — one per milestone-2/3 extraction so far, each
   targeting the specific newly-moved code path.

## NEXT EXTRACTION TARGET

`synthesis.py`: frame construction (refutation scan, hypothesis graph,
local-answer wait, blind analysis, source classification) through the
`synthesize()` call itself — the largest remaining undifferentiated region
within `[8]`, sitting upstream of the now-complete `claims/*` subpackage.
After that: the pre-pipeline (13 early-return branches) and `[10]`
optimistic-respond (HIGH risk, save for last before `pipeline.py`/`process()`
itself per the brief's explicit P3 ordering).
