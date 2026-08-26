# YANDI Epistemic Core v1 — Phase 0 Baseline

Baseline captured before any Phase 1+ implementation change, per the night-shift
implementation plan's mandatory rule: every phase compares against ONE fixed
baseline, not against whatever the previous phase happened to produce. No
semantic changes were made to produce this document — it is pure observation.

Machine-readable copy of the same numbers: `agent/epistemic_core_v1_baseline_fixture.json`.

Git state at capture time: `HEAD=416b0e1` (the audit commit), working tree clean.

---

## 1. Regression suite baseline

All 14 existing suites, run via `/home/iam/venv/bin/python3 -m agent.<suite>` from
the repo root:

```
belief_manager_regression_test              PASS
candidate_routing_regression_test            PASS
claim_lifecycle_regression_test              PASS
claim_priority_regression_test                PASS
claim_query_batch_regression_test             PASS
claim_relation_regression_test                PASS
evidence_eligibility_regression_test          PASS
final_claim_extraction_regression_test        PASS
final_epistemic_regression_test               PASS
orchestrator_modularization_regression_test   PASS
planner_regression_test                       PASS
refutation_performance_regression_test        PASS
shared_fetch_regression_test                  PASS
timeout_regression_test                       PASS
```

**14/14 green.** Every subsequent phase must keep this green before its own
live run, and must keep it green after.

## 2. Registry / storage baseline

| | Before | After 4 live queries | Δ |
|---|---|---|---|
| `registry/beliefs.json` size | 1,301,423 bytes | 1,310,451 bytes | +9,028 bytes |
| `registry/beliefs.json` lines | 43,735 | 44,059 | +324 |

Confirms the audit's P0 finding is still live: `belief_manager.py::_save()`
rewrites the entire file on every mutation; 4 queries (a handful of belief
mutations each) grew the file by ~0.7%. This number is the reference point
for Phase 4's benchmark, not a target to fix in Phase 0.

## 3. Live query baseline (4 representative queries)

Run with fresh `session_id` per query (avoids cross-run character/trust state
bleed, per the modularization migration's known gotcha), `enable_cache=False`
(forces a real pipeline run rather than a cache hit), from repo root with
`PYTHONPATH=/home/iam/yandi`.

| Query | web/validate | latency | trust_level | claims (final/rejected) | status counts (S/D/C/U/R) | grounding (sem/epi/sup) | coverage |
|---|---|---|---|---|---|---|---|
| "В каком году была основана компания Apple?" | web,no-val | 126.5s | UNVERIFIED | 3/0 | **1/0/2/0/0** | 1.00/1.00/0.33 | 0.20 (1/5) |
| "Безопасен ли подсластитель аспартам для здоровья?" | web,no-val | 310.0s | UNVERIFIED | 11/1 | 0/0/0/11/0 | 0.82/0.00/0.00 | 0.73 (8/11) |
| "Существует ли частица тахион?" | web,no-val | 192.7s | WEAKLY_SUPPORTED | 8/0 | 1/0/0/7/0 | 1.00/0.12/0.12 | 1.00 (5/5) |
| "Кто написал роман Война и мир?" | web,**validate** | 295.9s | UNVERIFIED | 6/0 | 3/0/0/3/0 | 1.00/0.50/0.50 | 0.56 (5/9) |

(S/D/C/U/R = supported/disputed/contradicted/unverified/rejected, from
`classify_claim_epistemic_status`'s own `[Claim Status]` summary line.)

### 3.1 The required mixed-evidence case

The plan explicitly asked to preserve "a query where SUPPORTS and CONTRADICTS
are simultaneously present." The **Apple** query produced this naturally:

```
[Claim Status] claim=cl_609013f1 candidate->contradicted supports=0 contradicts=1
[Claim Status] claim=cl_2d48f834 candidate->supported   supports=1 contradicts=0
[Claim Status] claim=cl_cf38956a candidate->contradicted supports=0 contradicts=1
[Claim Status] supported=1 disputed=0 contradicted=2 unverified=0 rejected=0
```

This is the canonical regression case for Phase 1 (relation persistence) and
Phase 7 (independent support counting) — both must reproduce this exact
supported=1/contradicted=2 split from the same underlying evidence after their
changes land.

### 3.2 Observed invariant-relevant behavior (already correct, not a gap)

- **Aspartame query**: 11/11 claims landed on `unverified` despite evidence
  being found and linked (`candidate_links` existed, `secondary`/`context`
  relations present) — because the *direct* relations were `uncertain`, and
  `uncertain` does not count toward `supported`. Confirms invariant "UNVERIFIED
  != CONTRADICTED" and "uncertain != positive signal" hold today, matching the
  audit's §3.1/3.2 finding.
- **Tachyon (existence) query**: correctly resolved to `WEAKLY_SUPPORTED`
  with `existence_contract` status `OK` (3 CORE claims out of 8 total) rather
  than a false negative from the search producing mostly `unverified` claims.

## 4. Trace persistence baseline

All 4 `tracer.save_trace()` calls completed without a caught exception (no
`[tracer] Ошибка сохранения` lines). Two of the four traces landed in
`registry/dataset/orch_traces/20260826.jsonl` and two in `.../20260827.jsonl`
— **this is a date-rollover artifact of the baseline run itself spanning
midnight** (`TRACES_DIR` files are named `{YYYYMMDD}.jsonl` off wall-clock
day), not a persistence bug. Confirmed by grepping both day-files for the
missing trace_ids — both present, just in the pre-midnight file.

Persisted trace sizes (serialized JSON line length, bytes):

| trace_id | query | persisted claims | persisted evidence | rejected_claims |
|---|---|---|---|---|
| `trace_..._411d2d8b` | Apple | 3 | 3 | 0 |
| `trace_..._546544ca` | Aspartame | 11 | 3 | 1 |
| `trace_..._1dfe271d` | Tachyon | 8 | 8 | 0 |
| `trace_..._fad1a4b0` | Война и мир | 6 | 3 | 0 |

Confirms the audit's §3 finding directly: none of these 4 persisted trace
records contain a `relation` (supports/contradicts/uncertain) field per
evidence link — only `derived_from_evidence_ids` (capped at 3). Phase 1's
job is to make this table also show a `relation` column that survives.

## 5. What Phase 1+ must reproduce exactly

For the Apple query specifically (the mixed-evidence case), after Phase 1
lands, re-running the identical query must still produce:
- Final claim count 3, rejected 0
- Claim status counts `supported=1 disputed=0 contradicted=2 unverified=0 rejected=0`
- Grounding `semantic=1.00 epistemic=1.00 support=0.33`
- Coverage `factual=5 covered=1 uncovered=4 coverage=0.20`

Any deviation in these specific numbers on an unchanged query is a regression,
not "expected new behavior" — Phase 1 only adds persistence, it does not
change classification.
