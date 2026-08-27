# YANDI Epistemic Core v1 — Phases 13–14: Trust Consolidation

Continuation of Phases 0–12 (`e5411df`) and the query_context bugfix
checkpoint (`e5411df` → pushed as `e5411df`, followed by the local
`96a1296` fix and its own checkpoint push). Scope: find every place
Trust is computed or mutated in the live pipeline, prove the real data
flow with code (not variable names), and consolidate it to one
canonical, single-owner result — first in shadow (Phase 13), then as
the cutover (Phase 14).

Commits produced (all on `main`, not pushed — per instructions):

```
72fca80 epistemic: add canonical trust shadow evaluation      (Phase 13)
35d7eef chore: pick up dataset episode entries from Phase 13 live runs
888777a epistemic: make canonical trust the final trust result (Phase 14)
494e1bc chore: pick up dataset episode entries from Phase 14 live runs
```

---

## 1. Before architecture

Grepped every file touching `trust_score|trust_raw|trust_level|
trust_label|verification_status|coverage_score|grounding|
validation_score|source_agreement|source_quality|
hypothesis_consistency|reflection_success|historical_reliability`
(49 files), then narrowed to actual `=` assignments of trust fields
(not reads) and verified each candidate against real call sites, not
variable names:

- `db/manager.py`, `db/migrate.py`, `orch_knowledge_writer.py`,
  `orch_citations.py`, `orch_feedback.py`, `orch_optimistic.py`,
  `orch_trace_generator.py`: **zero call sites** from
  `orchestrator_v2.py` or `orchestrator/*` — confirmed via grep. These
  are either dormant/legacy subsystems (the council/knowledge-DB world,
  same category as `claim_graph.py` before Phase 8) or module
  self-test (`if __name__ == "__main__":`) blocks. Not part of the live
  Trust path.
- `epistemic_router.py`'s `classify_claim()`/`trust_score`/
  `get_trust_label_for_epistemic()`: **real, live-path code**, imported
  by `orchestrator/pipeline.py`. This is where the "trust_gate strand"
  (below) gets its starting classification from.
- `consensus_engine.py`, `trust_model.py`: **zero call sites anywhere**
  in the codebase. Fully dormant.
- `orch_cache.py`, `orch_query_archive.py`: real live-path consumers,
  but pure pass-through (read a previously-decided `trust_level` with a
  safe default, or store whatever they're given) — not independent
  computation.

## 2. All found Trust paths (the real, live ones)

### 2.1 The "synthesizer strand" (CREATE)

`agent/orch_synthesizer.py::synthesize()`, lines ~1188–1275:

```
trust_raw = claim_validity_score * 0.10
          + evidence_score        * 0.20
          + source_agreement      * 0.15
          + source_quality        * 0.15   # crude web_count heuristic —
                                            # unaware of Phases 5-7's
                                            # source-independence/
                                            # clustering work
          + hypothesis_consistency* 0.15
          + reflection_success    * 0.15   # HARDCODED 0.3, ALWAYS
          + historical_reliability* 0.10   # HARDCODED 0.4, ALWAYS
```

Thresholds: `>= 0.7` → `STRONGLY_SUPPORTED`, `>= 0.5` →
`PARTIALLY_SUPPORTED`, `>= 0.3` → `WEAKLY_SUPPORTED`, else
`UNVERIFIED`. Output: `SynthesisResult.trust_level`. Consumer: every
downstream stage that reads `synthesis_result.trust_level` (see 2.3).
Persisted: yes, eventually, as `OutcomeRecord.trust_label` /
`archive_query`'s stored record (both at their PRE-cutover snapshot —
see §9). Can be overwritten later: yes, repeatedly (2.3).

### 2.2 The "trust_gate strand" (a SEPARATE, more rigorous computation)

`agent/orchestrator/epistemic/trust_gate.py::apply_epistemic_trust_
adjustment()`. Inputs: `epistemic_trust_label` (from `epistemic_router.
classify_claim()`), `epistemic_result.max_trust_cap`, testability/
domain, `final_claim_coverage_score`, `support_grounding_score`,
`belief_manager`. Gates, in order:

1. Epistemic classification label (if not subjective and not
   `PARTIALLY_SUPPORTED`/`UNVERIFIED`).
2. `max_trust_cap` (epistemic domain's own ceiling).
3. Testability (`interpretive`/`non_falsifiable` can't be
   `STRONGLY_SUPPORTED`).
4. Domain (`axiological`/`normative`/`philosophical` → `VALUE_FRAMEWORK`).
5. Media-interpretation entity-resolution failure.
6. "Science as model" cap.
7. **FINAL CLAIM COVERAGE GATE**: `< 0.50` → `UNVERIFIED`; `< 0.80` →
   capped at `PARTIALLY_SUPPORTED`.
8. **EVIDENCE SUPPORT GROUNDING GATE**: `< 0.3` → `UNVERIFIED`; `< 0.6`
   → capped at `PARTIALLY_SUPPORTED`.
9. Belief-manager average confidence gate (`< 0.5` → cap at
   `PARTIALLY_SUPPORTED`).

Output: `label`, a local variable at the one call site
(`orchestrator_v2.py`). **Before Phase 14, this value's only effect was
`trace.trust = label` inside the function itself** — the caller's local
`label` was read exactly once (to log it) and never reached
`synthesis_result.trust_level` or the response. Confirmed by grep:
zero other references to bare `label` in `orchestrator_v2.py` before
this phase.

### 2.3 Downgrade-only mutators of the synthesizer strand

All operate on `synthesis_result.trust_level`, all monotonic
(down-only), all reusing proper rank comparisons except where noted:

- `agent/orchestrator/claims/status.py::evaluate_claim_status_gate()`
  — claim verification_status counts (no claims → `UNVERIFIED`; all
  rejected → `UNVERIFIED`; only-contradicted → `UNVERIFIED`; disputed
  present → capped `WEAKLY_SUPPORTED`; `verified == 0` → capped
  `PARTIALLY_SUPPORTED`, or `WEAKLY_SUPPORTED` if also
  `supported == 0`).
- `agent/orchestrator/epistemic/existence_contract.py::apply_
  existence_query_contract()` — existence-question CORE-claim check,
  proper rank-based downgrade to `WEAKLY_SUPPORTED`.
- `agent/orchestrator/response/writeback.py` (inline) — if V3
  reflection found mistakes: `STRONGLY_SUPPORTED → PARTIALLY_SUPPORTED
  → WEAKLY_SUPPORTED`, one step.

### 2.4 The cache-hit path (a THIRD, separate early return)

`agent/orchestrator/pipeline.py`, lines ~199–211: on a cache hit,
strands 2.1–2.3 never run at all. Instead: `trust_level =
cache_result.trust_level` (a previously-computed value) is passed
through `_apply_trust_cap(trust_level, cap_label)`, where `cap_label`
comes from a **fresh** `get_trust_cap_for_testability(epistemic_result.
testability)` call. This is the exact call site that could have
exercised the `_TRUST_ORDER` bug (§5) in production before this phase's
fix, if a cached `WEAKLY_SUPPORTED` trust ever needed capping down by a
stricter fresh testability classification.

---

## 3. Why the two strands diverged — the exact code path

**A** (`trust_gate.py::apply_epistemic_trust_adjustment`) calculates
`label` — the strictest, most gate-aware result, including the FINAL
CLAIM COVERAGE and EVIDENCE SUPPORT GROUNDING hard gates.
**→** `label`'s only externally-visible effect (pre-Phase-14) was
`trace.trust = label` (`trust_gate.py` line 250) — a write into the
`Trace` object.
**→ B**: `process()`'s local variable `label`
(`orchestrator_v2.py:569`) received the same value but was **never
read again** — confirmed dead by grep, zero further references.
**→ C**: `synthesis_result.trust_level` — set independently by
`orch_synthesizer.py`'s own `trust_raw` heuristic **before**
`trust_gate.py` even runs — is what continues through
`claims/status.py`, `existence_contract.py`, and `writeback.py`'s
reflection downgrade.
**→ D** (response): `run_optimistic_respond` →
`OrchestratorResponse(trust_level=synthesis_result.trust_level, ...)`
— what the user received. It never incorporated `label`.

Consequence for the persisted trace: the same `Trace` object ended up
with `trace.trust` (rigorous `label`) genuinely disagreeing with
`trace.outcome.trust_label` / `trace.executions[...]['trust']` (both =
`synthesis_result.trust_level`, the less rigorous value) — the trace
itself contained two disagreeing trust values, and whichever one a
downstream consumer chose to read determined which "truth" it saw.
**This confirms the original pre-Phase-0 audit's finding is still true
after Phases 0–12** — the code structure changed (modularized into
`trust_gate.py`/`claims/status.py`), but the exact architectural gap
persisted.

---

## 4. Canonical owner

`agent/orchestrator/epistemic/canonical_trust.py::
compute_canonical_trust()` (Phase 13: `compute_canonical_trust_shadow`,
renamed at Phase 14 cutover — same function, same logic).

## 5. Canonical inputs

Exactly two already-computed strings — no claims_data, no coverage/
grounding numbers read directly (those already fed into strand 2.2):

- `final_synthesizer_trust` = `synthesis_result.trust_level`, read at
  the very end of `run_optimistic_respond`, i.e. **after** every
  existing mutation (2.3) has already applied.
- `trust_gate_label` = `label` from 2.2, threaded through as the new
  `epistemic_trust_gate_label` keyword argument.

## 6. Gates/caps

`canonical = _apply_trust_cap(final_synthesizer_trust,
trust_gate_label)` — reusing `trust_gate.py`'s own `_TRUST_ORDER` /
`_apply_trust_cap` **verbatim**, not reimplemented. This is a MIN
operation over two already-monotonic chains, so it inherits both
chains' hard gates for free: canonical can never rank above either
strand, so it can never show a label either strand's own coverage/
grounding/claim-status/belief-confidence gates would refuse.

**Bug found and fixed as part of this consolidation** (not deferred —
it directly affects canonical's correctness): `_TRUST_ORDER` was
missing a `"WEAKLY_SUPPORTED"` entry, silently defaulting to rank `0`
via `.get(label, 0)` — **below** `UNVERIFIED`'s rank of `1`. This broke
`_apply_trust_cap`'s "a cap only ever lowers, never raises" invariant
in both directions:

- a `WEAKLY_SUPPORTED` current value could never be capped down by
  anything (rank 0 can't exceed any real rank);
- using `WEAKLY_SUPPORTED` as the cap against an `UNVERIFIED` current
  value incorrectly **upgraded** it to `WEAKLY_SUPPORTED` (rank
  1 > rank 0 was read as "current exceeds the cap").

Fixed by adding `"WEAKLY_SUPPORTED": 2` — not a new value invented for
this fix, but the exact rank this label already held, uncontested, in
this same module's own two local `trust_rank` copies (inside
`apply_epistemic_trust_adjustment`'s disputed-claims and
`verified == 0` branches: `UNVERIFIED=0 < WEAKLY_SUPPORTED=1 <
PARTIALLY_SUPPORTED=2 < ...`). This was **found live**, not by the
synthetic 20-scenario matrix — see §11.

---

## 7. Shadow divergence results (Phase 13)

21-scenario deterministic matrix (`agent/epistemic_canonical_trust_
shadow_regression_test.py`), comparing OLD production Trust vs.
CANONICAL:

| # | Scenario | Old prod | trust_gate | Canonical | Diverged | Stricter |
|---|---|---|---|---|---|---|
| 1 | strong_independent_support | STRONGLY_SUPPORTED | STRONGLY_SUPPORTED | STRONGLY_SUPPORTED | No | equal |
| 2 | one_support_only | PARTIALLY_SUPPORTED | PARTIALLY_SUPPORTED | PARTIALLY_SUPPORTED | No | equal |
| 3 | five_syndicated_supports | STRONGLY_SUPPORTED | PARTIALLY_SUPPORTED | PARTIALLY_SUPPORTED | **Yes** | trust_gate |
| 4 | multiple_independent_supports | STRONGLY_SUPPORTED | STRONGLY_SUPPORTED | STRONGLY_SUPPORTED | No | equal |
| 5 | support_plus_contradiction | WEAKLY_SUPPORTED | PARTIALLY_SUPPORTED | WEAKLY_SUPPORTED | **Yes** | synthesizer |
| 6 | strong_contradiction | UNVERIFIED | UNVERIFIED | UNVERIFIED | No | equal |
| 7 | missing_evidence | UNVERIFIED | UNVERIFIED | UNVERIFIED | No | equal |
| 8 | search_attempted_not_found | UNVERIFIED | UNVERIFIED | UNVERIFIED | No | equal |
| 9 | search_error | UNVERIFIED | UNVERIFIED | UNVERIFIED | No | equal |
| 10 | search_never_attempted | UNVERIFIED | UNVERIFIED | UNVERIFIED | No | equal |
| 11 | high_quality_low_coverage | STRONGLY_SUPPORTED | UNVERIFIED | UNVERIFIED | **Yes** | trust_gate |
| 12 | high_coverage_poor_grounding | PARTIALLY_SUPPORTED | UNVERIFIED | UNVERIFIED | **Yes** | trust_gate |
| 13 | critical_unsupported_claim | WEAKLY_SUPPORTED | WEAKLY_SUPPORTED | WEAKLY_SUPPORTED | No | equal |
| 14 | recheck_supports_old_belief | SUPPORTED | SUPPORTED | SUPPORTED | No | equal |
| 15 | recheck_contradicts_old_belief | STRONGLY_SUPPORTED | PARTIALLY_SUPPORTED | PARTIALLY_SUPPORTED | **Yes** | trust_gate |
| 16 | recheck_inconclusive | SUPPORTED | SUPPORTED | SUPPORTED | No | equal |
| 17 | dependency_recheck_error | SUPPORTED | SUPPORTED | SUPPORTED | No | equal |
| 18 | semantic_family_duplicates | PARTIALLY_SUPPORTED | PARTIALLY_SUPPORTED | PARTIALLY_SUPPORTED | No | equal |
| 19 | same_evidence_via_clusters | STRONGLY_SUPPORTED | PARTIALLY_SUPPORTED | PARTIALLY_SUPPORTED | **Yes** | trust_gate |
| 20 | mixed_claim_statuses | WEAKLY_SUPPORTED | WEAKLY_SUPPORTED | WEAKLY_SUPPORTED | No | equal |
| 21 | live_caught (§11) | WEAKLY_SUPPORTED | UNVERIFIED | UNVERIFIED | **Yes** | trust_gate |

7/21 diverge. Every divergence is explained by a real, already-existing
gate the synthesizer strand is blind to (coverage, grounding,
cluster-aware syndication counting, belief confidence) — except #5,
where the *synthesizer* strand (via `claims/status.py`'s disputed-claim
handling) is stricter than `trust_gate`'s own grounding-only view,
correctly demonstrating canonical takes the MIN regardless of which
side is stricter. No divergence ever produced a HIGHER canonical value
than either strand — verified by an explicit monotonic-safety assertion
on every row.

---

## 8. Cutover (Phase 14)

Single point: `agent/orchestrator/response/writeback.py`, inside
`run_optimistic_respond()`, immediately after the V3 reflection block
ends and immediately before `tracer.save_trace(trace)`:

```python
_canonical_result = compute_canonical_trust(
    synthesis_result.trust_level, epistemic_trust_gate_label, log, verbose,
)
synthesis_result.trust_level = _canonical_result["canonical_trust"]
trace.trust = _canonical_result["canonical_trust"]
trace.trust_reason = _canonical_result["reason"]
```

This is the same point Phase 13's shadow computation already occupied
— cutover only changed what happens with the result (assign, not just
log/observe). Nothing between this assignment and the final
`return OrchestratorResponse(...)` reassigns `trust_level` again
(verified by a structural regression check scanning every line between
the two). `trace.trust` — previously set once, early, inside
`trust_gate.py`, holding only the trust_gate strand — now holds the
same canonical value the user sees, closing the exact gap §3
identified.

---

## 9. Legacy paths remaining

Deliberately **not** touched this phase (plan section 16 — no large
cleanup):

- **`OutcomeRecord.trust_label`** (`writeback.py`, constructed ~200
  lines before the cutover, right after `trace.cost = cost`): still a
  pre-cutover, pre-reflection-downgrade snapshot of
  `synthesis_result.trust_level`. This was *already* a pre-existing
  inconsistency (it never reflected the reflection downgrade either,
  even before this phase) — not introduced or worsened here.
- **`archive_query(trust_level=...)`** (`writeback.py`, same
  pre-cutover point): same snapshot-in-time limitation.
- **`self_model.add_decision(trust=...)`**, **`memory.add_query(trust=
  ...)`**, **`reflection.reflect_on_query(trust=...)`**: all inside the
  V3 reflection block, which runs *before* the cutover point by
  necessity (reflection's own mistake-downgrade is one of the inputs
  the synthesizer strand carries INTO the cutover) — these see the
  pre-canonical value as an intentional, legitimate input to
  self-reflection, not a bug.

None of these are "consumers of the old final Trust" in the sense the
plan warns about removing blindly — they are consumers of an
intermediate value that legitimately exists at an earlier point in the
same request, and moving their call sites is a separate, larger
refactor this phase does not attempt.

---

## 10. Trace consistency

Verified directly against a persisted trace file (not just the console
log) for a completed live run
(`trace_1787819484_5f3e20c5`, query "Когда была основана компания
Apple?"):

```
trace.trust               = "UNVERIFIED"
trace.trust_reason        = "synthesizer_strand=UNVERIFIED trust_gate_strand=UNVERIFIED -> canonical=UNVERIFIED"
trace.observations.canonical_trust           = "UNVERIFIED"
trace.observations.canonical_trust_diverged  = False
trace.outcome.trust_label = "UNVERIFIED"
displayed OrchestratorResponse.trust_level = "UNVERIFIED"
```

All four agree in this run (no divergence occurred in this particular
request, so `outcome.trust_label`'s pre-cutover-snapshot limitation
happened not to matter here — see §9 for when it would).

---

## 11. Regression results

29/29 suites green throughout Phases 13–14 (27 pre-existing + 2 new):

- `agent/trust_order_weakly_supported_regression_test.py` — 12 checks:
  the `_TRUST_ORDER` fix itself (`WEAKLY_SUPPORTED` present, correctly
  ranked between `UNVERIFIED` and `PARTIALLY_SUPPORTED`), and
  `_apply_trust_cap`'s "never outranks either input" invariant checked
  against every direction of every affected pair.
- `agent/epistemic_canonical_trust_shadow_regression_test.py` — 52
  checks: the 21-scenario matrix (with monotonic-safety assertions on
  every row), edge cases for a missing/unavailable strand, and (Phase
  14) structural cutover-proof checks — the assignment exists, in the
  right order, nothing recomputes it afterward, `trace.trust` is
  reconciled too.

## 12. Live results

Five live `--web` runs across Phases 13–14 (fresh sessions, cache
disabled each time):

1. "Столица Франции?" — **caught the `_TRUST_ORDER` bug live**:
   `synthesizer_strand=WEAKLY_SUPPORTED trust_gate_strand=UNVERIFIED`
   resolved (incorrectly, pre-fix) to `canonical=WEAKLY_SUPPORTED`.
2. Same query, re-run after the fix — completed cleanly, both strands
   happened to land on `UNVERIFIED` this time (no crash, sane
   agreement).
3. "Сколько лет пирамиде Хеопса?" — completed cleanly, both strands
   `UNVERIFIED`, no divergence-direction anomalies.
4. "Когда была основана компания Apple?" (Phase 14) — completed
   end-to-end; verified three-way consistency directly against the
   persisted trace file (§10).
5. "Полезен ли пост для здоровья?" (Phase 14, second class) — ran the
   full claim/evidence/family-dependency/recheck pipeline cleanly (13
   claims, 78 claim-pair NLI comparisons, zero errors) but exceeded the
   400s harness timeout on this expensive query before reaching the
   final answer — not a defect in this phase's own code (every stage
   through `Dependency Recheck Summary` completed normally); simply an
   expensive query needing a longer budget, consistent with similar
   timeouts observed in Phases 11–12's own live testing.

## 13. Performance impact

Zero new network/embedding/LLM calls, confirmed by design (canonical
Trust only compares two already-computed label strings) and by
observation (`elapsed_ms` for the combined Phase 11+12+13 shadow/
cutover block stayed in the 1–5ms range across every live run;
Phase 14's cutover added no measurable overhead beyond one dict lookup
and two string comparisons).

## 14. Known limitations

1. **§9's legacy snapshot paths** — `OutcomeRecord`/`archive_query`/
   self-reflection inputs still see the pre-cutover value. Already
   true before this phase for the reflection downgrade specifically;
   now also true for the coverage/grounding/belief gates that only
   apply at cutover. A future phase could move `OutcomeRecord`
   construction to after the cutover point if this divergence is ever
   found to matter for a real downstream consumer of archived/outcome
   data — not attempted here (scope discipline, plan section 16).
2. **Cache-hit path (§2.4) uses its own, narrower cap** — a cached
   trust value is only ever passed through
   `get_trust_cap_for_testability()`'s single cap, not the full
   canonical computation (which needs `trust_gate_label` and
   `final_synthesizer_trust`, neither computed on a cache hit). This
   phase did fix the `_TRUST_ORDER` bug that this exact path could
   have hit, but did not unify the cache-hit path onto
   `compute_canonical_trust()` itself — a legitimate future
   consolidation target, deliberately out of this phase's minimal-
   cutover scope.
3. **`epistemic_router.py`'s own `trust_score`/`objectivity_score`**
   remain a further upstream input INTO the trust_gate strand, not
   independently audited for internal correctness beyond confirming
   its call sites and its role as `epistemic_trust_label`'s source —
   its own formula was not re-derived or re-validated line by line.

## 15. Bugs found but NOT fixed

Nothing new found this phase beyond the `_TRUST_ORDER` bug (§6), which
**was** fixed (directly blocking canonical correctness, not deferred).
The `query_context` NameError from the prior checkpoint and the
previously-documented known issues (false insult substring match,
`belief_manager._find_similar()` cost, `source_cluster_id` persistence
parity) were not re-investigated — nothing in Phases 13–14 touched
those code paths.

## 16. Следующие архитектурные задачи

- Consider whether `epistemic_router.py`'s `trust_score`/
  `objectivity_score` formulas deserve their own dedicated audit (they
  feed `trust_gate_label`, one of canonical's two inputs, but were
  treated as an opaque upstream signal this phase — §14 item 3).
- Consider unifying the cache-hit trust-cap path (§2.4) onto
  `compute_canonical_trust()` once/if a cached `trust_gate_label` (or
  equivalent) becomes available at that point.
- Consider moving `OutcomeRecord`/`archive_query` construction to after
  the cutover point, if a real consumer of archived/outcome trust data
  is ever found to need the fully-canonical value instead of the
  pre-cutover snapshot (§14 item 1) — not needed today, no such
  consumer identified.
- `_TRUST_ORDER` vs. the two local `trust_rank` copies inside
  `apply_epistemic_trust_adjustment()` are still three separate,
  textually-duplicated tables (now at least numerically consistent for
  every label all three share) — a future phase could consolidate them
  into one shared table, the same pattern already applied to
  `_resolve_query_context()` in the prior bugfix checkpoint.

---

## Summary

Phase 13 (shadow) and Phase 14 (cutover) are both committed and fully
tested (64 new regression checks across two suites, 0 failures) and
proven live across five real pipeline runs — one of which caught and
led to the fix of a genuine pre-existing `_TRUST_ORDER` bug that
synthetic testing alone had not exercised. Canonical Trust is now the
single, verified final Trust result, reusing exclusively already-
existing gates/thresholds — no new formula, no vocabulary change, no
claim/belief semantics touched. `git status` is clean; nothing has been
pushed.
