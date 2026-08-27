# Epistemic Core v1 — Phase 4 Benchmark: Belief Storage

Per the night-shift plan's explicit instruction for this phase: **benchmark
first, do not assume the audit's proposed hot/cold split is the right
answer, choose the minimal justified change, and if the measured benefit
doesn't justify a change, implement nothing and commit only this report.**

That is exactly what happened here. All measurements below were taken
against a **copy** of `registry/beliefs.json` (`BeliefManager(storage_file=<copy>)`)
— the real registry file was never mutated by this benchmark; verified by
never calling `BeliefManager()` (the default-path constructor) during this
phase, only `BeliefManager(storage_file=scratch_copy)`.

## 1. State distribution (the number that changes the recommendation)

```
total beliefs: 552
by status:     {'active': 552}
revised:       0
superseded:    0
rejected:      0
```

**All 552 beliefs are `status="active"`.** Zero are `revised`, `superseded`,
or `rejected`. This directly undercuts the audit's proposed HOT/COLD split
(hot = actively-mutating beliefs, cold = superseded/rejected/history-like,
in a separate rarely-rewritten file): there is currently **nothing to move
to a cold file**. A hot/cold split by status would leave 100% of the data
in the "hot" file today, saving zero bytes on every `_save()` call. The
split only starts paying off once a meaningful fraction of beliefs
actually reach `revised`/`superseded`/`rejected` — which isn't happening
yet in this registry (worth asking separately why nothing has ever been
superseded, but that's a belief-lifecycle question, not a storage one, and
out of scope for this phase).

History is healthy and small: 4,493 total history entries across 552
beliefs (avg 8.14/belief, max 19) — not a growth driver on its own.

10 distinct topics; `factual` (316) and `biological` (108) dominate.

## 2. Timing

| Operation | Result |
|---|---|
| `_save()` alone, repeated 5x, 1.3MB file | 48.7–52.0ms (avg 49.6ms) |
| `BeliefManager()` construct (`_load` + `_apply_decay`, which itself calls `_save()` once), repeated 5x | 60.4–61.1ms (avg 60.6ms) |
| Simulated 1 request, 6 `add_belief()` calls, same topic | **16.515s total, 2752.5ms/call avg** |
| Simulated 1 request, 11 `add_belief()` calls, same topic | **35.327s total, 3211.5ms/call avg** |

The bare `_save()` cost (~50ms) is real but small. The simulated
per-request cost is **50-60x larger than the file-rewrite cost alone** —
which means the file rewrite is not actually the dominant cost of an
`add_belief()` call in a real request.

## 3. Where the real per-call cost comes from (not what Phase 4 was originally scoped to fix)

`add_belief()` calls `_find_similar(topic, statement)` before deciding to
create-vs-update (`belief_manager.py:154`). `_find_similar()`
(`belief_manager.py:184-248`) filters candidates by `belief.topic == topic`
— and **every claim from the same query shares the same topic**
(`claims/lifecycle.py:275`: `topic=epistemic_result.domain` for every
claim in the request). So from the second `add_belief()` call in a
request onward, `_find_similar()` has real topic-scoped candidates, an
exact-match text pass finds no match (each claim's text differs), and it
falls through to a **live embedding HTTP call** (`_embed_batch()`,
`belief_manager.py:250-277`, POST to `127.0.0.1:11434/api/embed`) over
`[new_statement] + all_topic_candidates` — and for any candidate scoring
≥0.70 cosine similarity, a **second live LLM-judge HTTP call**
(`_llm_judge_relation()`, `belief_manager.py:279-349`, POST to
`.../api/generate`).

This benchmark's synthetic topic started empty and grew to only 5
same-topic candidates by the last call — a *conservative* stand-in for
production, where `topic="factual"` already has 316 existing beliefs from
the first call of every request. The realistic per-call cost in
production is likely **at or above** what was measured here, not below it.

**This is the actual dominant cost of belief storage per request** — not
the JSON file rewrite. It's a different kind of problem (semantic-dedup
network cost, not storage/growth) and explicitly out of this phase's scope
per the plan's stop condition ("не расширять scope самостоятельно",
"требуется изменить... semantics вне текущего этапа" -> STOP, document,
don't fix in this phase). Flagging it here as a discovered finding for a
**separate**, dedicated future phase — it was not on the audit's original
P0 list, and touching `_find_similar()`'s dedup logic is squarely the kind
of "neighboring subsystem" change the plan says to stop at, not push
through under Phase 4's name.

## 4. Decision

**The hot/cold storage split proposed in the audit is NOT implemented this
phase.** Per the plan's explicit escape clause ("Если measured benefit не
оправдывает изменение: НЕ реализовывать. Commit только audit/benchmark
result"):

- The measured benefit of the split (saving ~50ms of file-rewrite time per
  `add_belief()` call, on a file where 100% of records are `active` today
  so nothing would actually move to a cold file yet) is small and, at
  current data volume, close to zero real-world impact.
- The migration risk (backward compatibility, `get_all_active()`/
  `get_by_topic()` semantics, "don't lose a single belief" requirement)
  is real engineering cost for a change that wouldn't fix the thing that
  actually makes belief updates slow in production.
- Implementing it now, under Phase 4's name, while the real cost driver
  (`_find_similar()`'s embedding+LLM-judge network calls) stays unaddressed,
  would be exactly the kind of "фикс, который ничего не чинит" this
  benchmark-first discipline exists to prevent.

**No code was changed in this phase.** `agent/belief_manager.py` is
untouched. `registry/beliefs.json` is untouched (only read for the state
distribution count; all timing/mutation tests ran against a scratch copy
under `/tmp`, never the real file).

## 5. What would need to be true to revisit this

- If `revised`/`superseded`/`rejected` beliefs start accumulating in real
  numbers (currently zero), a hot/cold split by status becomes
  measurably worth it — re-benchmark then, don't implement speculatively
  now.
- If `registry/beliefs.json` grows an order of magnitude (13MB+), `_save()`'s
  ~50ms cost would grow roughly proportionally and start to matter even
  without a status-based split being available — worth tracking as part
  of Phase 15's storage/retention design, not fixing preemptively here.
- The `_find_similar()` per-call network cost is the actual candidate for
  a future dedicated performance phase, separate from this one — it was
  not part of the original audit's P0 list and needs its own benchmark,
  invariant analysis (must not weaken belief-equivalence correctness for
  speed), and live-run verification, same discipline as every other phase.
