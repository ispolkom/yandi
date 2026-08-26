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
ORCHESTRATOR MODULARIZATION STATUS: PARTIAL (substantial — see below)
LEGACY orchestrator_v2.py:           PRIMARY (still owns process()'s outer
                                      pipeline shape, phases [0]-[7]/[9]/[10]
                                      glue, and the CLI)
BEHAVIORAL EQUIVALENCE:              CONFIRMED (14/14 regression suites green
                                      after every commit; 198/198 modularization
                                      equivalence checks green)
READY FOR EPISTEMIC ARCHITECTURE WORK: YES for claims/*/epistemic/* — this was
                                      the actual goal of the migration and is
                                      done; the remaining inline pipeline glue
                                      does not block epistemic/provenance work.
```

## MILESTONES 1-3 — DONE

Package skeleton, all of `claims/*` (identity/validation/mapping/retrieval/
status/disagreement), all of `epistemic/*` (existence_contract/final_coverage/
trust_gate), all of `response/*` (assembly), `runtime/profiling`. See git log
for the 13 commits covering this (`e35ddb7`..`c4d956f`).

## MILESTONE 4 (partial) — synthesis.py + pre_pipeline.py

**Result: DONE for these two; [0]-[7] pipeline glue and [10] deliberately
NOT extracted this session — see "Why stop here" below.**

- `synthesis.py`: frame construction (refutation scan, hypothesis graph,
  local-answer wait, blind analysis, source classification) through the
  `synthesize()` call itself. `blind_analysis` moved with it (single call site).
- `pre_pipeline.py`: the highest-risk extraction of the whole migration —
  ~600 lines, 11 short-circuit early-return branches (self-query,
  provocation, insult×2, apology, personal-question, song/social/self-
  reflection analyzers, no-context, early-gate break/know-but-not-tell),
  preserved in exact sequential order. Return protocol is a plain
  `(response_or_None, dict)` pair — not a positional tuple (too easy to
  transpose 29 values) and not a new PipelineContext class (out of scope
  per the brief) — the caller unpacks the dict key-by-key so a typo is an
  immediate `KeyError`, not a silent value swap. `is_self_query`,
  `load_yandi_manifest`, `extract_urls`, `clean_query_from_urls` moved
  with it (each had exactly one call site, all inside this block).

- `orchestrator_v2.py`: 5620 → **2248 lines (−3372, −60.0%)**.
- `agent/orchestrator/`: **4049 lines across 17 files**.
- 18 extraction commits total this migration, all with the same discipline:
  one extraction → exact-diff verify against pre-move source → full 14-suite
  regression → deterministic equivalence checks added → live sanity run(s)
  → commit. Never batched.
- `agent/orchestrator_modularization_regression_test.py`: 198 deterministic
  checks.
- For `pre_pipeline.py` specifically: a dedicated audit fork independently
  re-derived the full free-variable cross-reference (which of the ~50
  variables assigned in the block are actually read downstream) before any
  code was written, catching several grep false-positives (e.g. a `target=`
  keyword argument colliding with the `target` variable name). Verified with
  a **sequence-sensitive** `diff` (not the whitespace-normalized multiset
  compare used for earlier, order-insensitive extractions), since branch
  *order* is exactly what's at risk in an 11-early-return block. Live-tested
  6 of the 11 early-return branches for real (not mocked): self_query,
  insult_handled, apology, swear-triggered insult (confirming the swear
  check still gates the Criticism Detector), gate_break (hit unplanned —
  accumulated character-state irritation from prior test runs), and the
  full continuation path with a fresh session.

## WHY STOP HERE (this session)

Two things surfaced while scoping the next extraction ([6] discovery
fan-out) that changed the risk/benefit calculus:

1. **Cross-phase timer coupling found**: `cost["registry_ms"]` in `[6]` is
   computed from a `t0` that was actually set in the *previous* phase `[5]
   Query enrich`, not in `[6]` itself. This is existing (if slightly odd)
   behavior, not a bug to fix — but it means `[0]`-`[7]` are more tightly
   timer-coupled to each other than `claims/*`/`synthesis`/`pre_pipeline`
   were, and extracting them piecemeal risks either silently changing what
   a cost bucket measures, or requiring awkward timestamp-passing between
   every adjacent pair of extracted phases.
2. **`[0]`-`[7]` and `[10]` are exactly the "pipeline glue" the brief
   designates P3/last** (§9, §20-23 of the original brief): once `claims/*`
   and `epistemic/*` are done (they are), the remaining phases are better
   absorbed directly into `pipeline.py` as one deliberate, separately-
   reviewed step — matching how `pre_pipeline.py` needed its own dedicated
   audit fork rather than a quick mechanical move. Doing `[0]`-`[7]`
   piecemeal now would likely need re-doing once `pipeline.py` exists,
   for comparatively little interim clarity benefit (they're already
   short, single-purpose, and individually cost-timed).

This is a deliberate stop at a clean, fully-green checkpoint — not a
blocked or failed state. See NEXT STEPS below for the concrete follow-on
plan.

## CURRENT LEGACY STRUCTURE

```
agent/orchestrator_v2.py (2248 lines)
    lines 1-~340   top-level helpers (mostly pure; a few dead, documented, untouched)
    process()      ~1550 lines: phases [0] Cache, [1] Risk, [2] Plan,
                   [3] Intent, [3.5] Epistemic classification,
                   [4] Clarification, [5] Enrich, [6] Discovery fan-out,
                   [7] Web search decision, then delegated calls into
                   pre_pipeline/synthesis/claims/*/epistemic/*, [9] gate
                   call, [10] Optimistic respond (still ~410 lines inline)
    interactive() + CLI entrypoint (untouched, P3 per the migration brief)
```

## TARGET STRUCTURE

```
agent/orchestrator/
    __init__.py
    pipeline.py            (not yet created — P3/last: [0]-[7] pipeline glue
                             + [10] optimistic-respond move here together,
                             then process() becomes a thin call into it)
    context.py              (not yet created — deferred: don't introduce a
                             PipelineContext until pipeline.py's own shape
                             is known)
    discovery.py            (not created as a separate file — [6] fan-out
                             turned out to be timer-coupled to [5]; folds
                             into pipeline.py instead, see "why stop here")
    synthesis.py            ✅ DONE
    pre_pipeline.py         ✅ DONE
    claims/                 ✅ COMPLETE subpackage (6 modules)
    epistemic/               ✅ COMPLETE subpackage (3 modules)
    runtime/
        __init__.py
        profiling.py         ✅ DONE
        timeout.py            (dropped from target — agent/orch_timeout.py
                               already owns this)
        shared_work.py        (not yet created — generate_local_answer is
                               still in orchestrator_v2.py, submitted from
                               [6]; candidate once pipeline.py exists)
    registry/                (not yet created — nothing extracted into it yet)
        integration.py
    response/                 ✅ COMPLETE subpackage (assembly)
```

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

Each row was verified against the pre-move source with an exact diff before
the call site was rewired (whitespace-normalized multiset compare for most;
sequence-sensitive `diff` for `pre_pipeline.py` specifically, where branch
order was the primary risk).

## REMAINING COMPONENTS (still inline in `process()`)

- **`[0]`-`[7]` pipeline glue** (~700 lines): cache check (its own early-
  return on cache hit), risk assess, plan, intent analyze (LLM), epistemic
  classification (~187 lines, the largest sub-block here), clarification,
  query enrich, discovery fan-out (`ThreadPoolExecutor`, registry/web/
  refutation/local-answer), web search decision. Individually simple,
  collectively timer-coupled to each other (see "why stop here"). Target:
  fold directly into `pipeline.py` as one step, not piecemeal.
- **`[10]` Optimistic respond** (~410 lines): background-validation kickoff
  (`nonlocal` closure), cache write, query archive write, V3 memory/
  reflection/dataset write-back (8+ ordered side-effecting calls in one
  try/except, reads `claims_accepted`/`total_claims` via `'in locals()'`
  checks — the same kind of scope-sensitive gotcha `pre_pipeline.py` and
  `synthesis.py` each had one of), trust banner selection, final
  `OrchestratorResponse` return. HIGH risk, explicitly flagged to do last
  in the original brief, right before `pipeline.py` itself.
- `generate_local_answer` (submitted from `[6]`, not yet moved — candidate
  for `runtime/shared_work.py` once `discovery`/`[6]` is addressed).

## IMPORT DEPENDENCY GRAPH

```
agent.orchestrator_v2
    → agent.orchestrator.epistemic.{existence_contract,final_coverage,trust_gate}
    → agent.orchestrator.runtime.profiling
    → agent.orchestrator.response.assembly
    → agent.orchestrator.claims.{status,validation,lifecycle,mapping,retrieval,disagreement}
    → agent.orchestrator.synthesis
    → agent.orchestrator.pre_pipeline
        (claims.retrieval → claims.mapping is the one intra-package import;
         everything else → existing domain modules only; none import
         orchestrator_v2; orchestrator_v2 never imports anything that
         imports it back)
```

No circular imports found or introduced anywhere in the migration.

## GLOBAL/SINGLETON HANDLING

No global/singleton ownership changed anywhere in the migration. Every
V3/V6 singleton and the module-level `_tracer` (`DecisionTracer()` —
confirmed stateful, accumulates an in-memory trace list, so it is always
passed in as a parameter, never re-constructed in an extracted module) is
passed explicitly into the extracted functions. `pre_pipeline.py`'s audit
additionally confirmed every one of the 12 `get_*()` singleton getters it
calls is called exactly once in the whole file (no double-construction risk).

## EQUIVALENCE STATUS

**CONFIRMED.** Methodology (see earlier sections of this doc's history in
git log for the full write-up): exact diff at extraction time (sequence-
sensitive where branch order is the risk, e.g. `pre_pipeline.py`;
whitespace-normalized multiset compare otherwise) + full 14-suite
regression after every commit + `agent/orchestrator_modularization_regression_test.py`
(198 deterministic checks, network/LLM dependencies monkeypatched for
determinism) + live `--web -v` sanity runs targeting each newly-moved code
path specifically, including — for `pre_pipeline.py` — deliberately
constructed inputs to hit 6 of its 11 early-return branches for real.

## NEXT STEPS

1. `pipeline.py`: fold `[0]`-`[7]` pipeline glue into it as one deliberate
   step (not piecemeal — see "why stop here"), resolving the `t0`
   cross-phase timer coupling explicitly rather than threading timestamps
   between separate small extracted functions.
2. `[10]` Optimistic respond → also into `pipeline.py` or a dedicated
   `response/writeback.py` — needs the same kind of dedicated free-variable
   audit `pre_pipeline.py` got, given the `'claims_accepted' in locals()`
   pattern and the `nonlocal` closure.
3. Once both are done, `process()` itself becomes a thin function that
   calls `pipeline.process(...)` — at that point `orchestrator_v2.py`
   is a true CLI + compatibility facade (Milestone 4 complete per the brief).
4. Only after that: consider whether a `PipelineContext`/`RequestContext`
   is warranted (separate commit, separate regression, per the brief —
   still not needed for anything done so far).
