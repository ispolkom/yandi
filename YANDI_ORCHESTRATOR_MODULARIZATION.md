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
ORCHESTRATOR MODULARIZATION STATUS: WORKING (substantially complete)
LEGACY orchestrator_v2.py:           FACADE-LEANING PRIMARY — still the
                                      canonical process() entry point and
                                      CLI, but its body is now almost
                                      entirely a sequence of calls into
                                      agent/orchestrator/*; the only logic
                                      left inline is thin connective glue
                                      (trace/decision-event bookkeeping
                                      between phase calls) and one
                                      confirmed-dead closure (_claims_conflict).
BEHAVIORAL EQUIVALENCE:              CONFIRMED (14/14 regression suites
                                      green after every one of 24 commits;
                                      198/198 modularization equivalence
                                      checks green)
READY FOR EPISTEMIC ARCHITECTURE WORK: YES
```

## RESULT

- `orchestrator_v2.py`: **5620 → 698 lines (−4922, −87.6%)**.
- `agent/orchestrator/`: **5882 lines across 20 files**, fully covering
  `claims/*` (7 files: status, validation, lifecycle, mapping, retrieval,
  disagreement, `__init__`), `epistemic/*` (4: existence_contract,
  final_coverage, trust_gate, `__init__`), `response/*` (3: assembly,
  writeback, `__init__`), `runtime/*` (2: profiling, `__init__`), plus
  `pre_pipeline.py`, `pipeline.py`, `synthesis.py`, and the package
  `__init__.py`.
- **24 commits** this migration (21 `refactor:` extractions, 1 `fix:`,
  2 `docs:`/`test:`), every one landing with 14/14 regression suites green
  and a clean working tree.
- `process()` itself is now ~440 lines, almost all of it a straight-line
  sequence of calls into the extracted modules (pre_pipeline → pipeline →
  synthesis → claims/epistemic orchestration → writeback), plus the
  `log`/`_claims_conflict` closures, trace/decision-event setup, and
  early-return handling for each extracted phase's result.

## MOVED COMPONENTS (in commit order)

| # | Commit | Extracted | To |
|---|---|---|---|
| 1 | `e35ddb7` | Existence Query Contract | `epistemic/existence_contract.py` |
| 2 | `a613e84` | Final Claim Coverage orchestration | `epistemic/final_coverage.py` |
| 3 | `1f1b166` | Pipeline wall-clock `[PROFILE]` report | `runtime/profiling.py` |
| 4 | `9174dc9` | Trust helpers + epistemic trust adjustment | `epistemic/trust_gate.py` |
| 5 | `1cf3fa7` | Response assembly helpers | `response/assembly.py` |
| 6 | `f37be5f` | Claim epistemic status classification | `claims/status.py` |
| 7 | `0cbf172` | Structural claim validation | `claims/validation.py` |
| 8 | `b94ed9f` | Claim & evidence lifecycle setup | `claims/lifecycle.py` |
| 9 | `7926f2e` | `_run_claim_evidence_batch` de-closured | `claims/mapping.py` |
| 10 | `d84de56` | Claim Resolution Gate + PASS2 retrieval | `claims/retrieval.py` |
| 11 | `74138d8` | Belief update + linker + personality cycle | `claims/lifecycle.py` (2nd fn) |
| 12 | `c406da6` | Claim<->claim disagreement | `claims/disagreement.py` |
| 13 | `40501dc` | Frame construction + synthesize() | `synthesis.py` |
| 14 | `c4d956f` | Claim Status Gate counting/messaging | `claims/status.py` (2nd fn) |
| 15 | `4338c05` | Pre-pipeline (11 early-return branches) | `pre_pipeline.py` |
| 16 | `5f30c07` | Standard pipeline `[0]`-`[7]` | `pipeline.py` |
| 17 | `a8694d9` | *(fix)* `cache` singleton threading bug | `pipeline.py` |
| 18 | `3fa46c5` | `[10]` Optimistic respond | `response/writeback.py` |
| 19 | `7146796` | Evidence mapping PASS1 + claim trace/grounding | `claims/mapping.py`, `claims/status.py` |

Each row was verified against the pre-move source with an exact diff before
the call site was rewired — sequence-sensitive `diff` where branch/side-
effect *order* was the primary risk (`pre_pipeline.py`, `pipeline.py`,
`response/writeback.py`), whitespace-normalized multiset compare otherwise.

## THE ONE BUG FOUND (and how it was caught)

Commit `a8694d9`: the `[0]`-`[7]` extraction's `state_out` dict omitted the
`cache` singleton (`cache = get_cache()`, constructed in `[0]`, read in
`[10]`) — a `NameError` that no diff or regression suite catches, because
it's in *new* glue code (the state-dict plumbing), not in moved code a
diff can verify against a "before" version. It surfaced on the first live
run this session that combined a fresh session with `enable_validation=True`
— exactly the kind of path the existing 13 regression suites (all
module-level, not full-pipeline) don't exercise. Fixed same-session, live-
verified twice after (including the real `_background_validate` non-daemon
thread completing end-to-end). This is the concrete argument for why live
sanity runs stayed mandatory for every extraction touching `process()`'s
outer shape, even after diff + regression both passed clean.

## REMAINING (deliberately not done)

- **`process()` itself still lives in `orchestrator_v2.py`**, not moved
  into a `pipeline.py`-owned `process()` with `orchestrator_v2.py` reduced
  to a pure CLI facade (`from agent.orchestrator.pipeline import process`).
  What's left in it now is thin connective tissue — `log`/`_claims_conflict`
  closures, `trace`/`decision_id`/`cost` setup, and the sequential early-
  return checks after each extracted phase call — not logic. Moving it is
  low-risk at this point (little content left to get wrong) but also
  low-value (no further complexity reduction), and the brief explicitly
  stages this as the deliberate last step, done separately once the
  package's own shape is proven stable under real traffic.
- `_claims_conflict` (confirmed dead — zero call sites anywhere in the
  file, before or after this migration) stays in `orchestrator_v2.py`,
  flagged not fixed, per the migration's own "no silent fixes" rule.
- `generate_local_answer`/`resolve_entity` already moved into `pipeline.py`
  and `_background_validate`/`_build_tags` into `response/writeback.py` —
  no further top-level helpers remain to extract.
- `PipelineContext`/`RequestContext`: still not introduced anywhere. Every
  extraction uses either explicit named parameters or a plain dict return
  (`pre_pipeline.py`, `pipeline.py`) — never a new class. Whether one is
  warranted is a decision for whoever takes on the `process()`-into-
  `pipeline.py` step, not before.

## EQUIVALENCE STATUS

**CONFIRMED.** Every extraction: exact diff against pre-move source (order-
sensitive where order was the risk) → full 14-suite regression → live
`--web -v` (or scripted fresh-session) run targeting the specific new code
path → commit. `agent/orchestrator_modularization_regression_test.py`
carries 198 deterministic checks (network/LLM dependencies monkeypatched)
as an ongoing regression net for the extracted units going forward.
