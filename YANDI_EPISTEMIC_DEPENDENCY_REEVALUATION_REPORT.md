# YANDI Epistemic Core v1 — Phases 11–12: Dependency / Re-evaluation

Continuation of `YANDI_EPISTEMIC_ARCHITECTURE_AUDIT.md` and Phases 0–10
(`6ab3b8c`). Scope: move from PROVENANCE/IDENTITY (Phases 0–10: does YANDI
recognize the same knowledge across requests?) to DEPENDENCY/RE-EVALUATION
(Phases 11–12: if that knowledge changes, does YANDI know what else might
need re-checking?). Trust consolidation (Phases 13–14) is explicitly out of
scope and was not started.

Commits produced (all on `main`, not pushed):

```
9debd32 epistemic: detect dependent claims requiring re-evaluation      (Phase 11)
aab4f92 chore: pick up dataset episode entry from Phase 11 live run
0c3a321 epistemic: add bounded dependency re-evaluation                 (Phase 12)
7f48188 chore: pick up dataset episode entries from Phase 12 live runs
```

---

## 1. Audit mapping

Traced the real chain end to end (not assumed) before writing any code:

```
claim occurrence (claim_id, claim_text)
    |
    +-- content_hash            agent/claim_identity.py (Phase 2)
    |                           set in orchestrator/claims/lifecycle.py::
    |                           setup_claim_and_evidence_lifecycle()
    |
    +-- semantic_family_id      agent/claim_family_registry.py (Phase 10)
    |                           set ONLY for claims_data[:3] in
    |                           update_beliefs_link_answer_and_personality_cycle()
    |
    +-- evidence_relations      claim["evidence_relations"], Phase 1,
    |                           persisted on Trace's ClaimRecord unchanged
    |
    +-- claim<->claim NLI       agent/orchestrator/claims/disagreement.py::
    |   (per-request only)      apply_claim_claim_disagreement() ->
    |                           batch_results + pair_claims, ALL claims_data
    |                           pairs (not capped at [:3])
    |
    +-- Phase 8 graph           agent/claim_graph_shadow.py — builds a FRESH,
    |   (per-request, thrown    per-call ClaimGraph keyed by claim_id from the
    |    away after logging)    same NLI results; never persisted, never
    |                           keyed by family
    |
    +-- belief identity         agent/belief_manager.py — SEPARATE identity:
                                keyed by (topic, statement) via its own
                                embedding+LLM-judge _find_similar(), NOT by
                                semantic_family_id or content_hash
```

**The load-bearing finding**: `semantic_family_id` (Phase 10, the whole
point of which was "recognize the same knowledge across requests") and
belief identity (topic+statement equivalence) are two independent,
never-cross-referenced equivalence systems over the same underlying
claims. `Belief.claim_ids` already exists and already records which
claims produced a belief, but nothing before Phase 12 ever read it
against a family's member list. This is why Phase 12 had to build a
read-only bridge (`agent/dependency_recheck.py::_belief_for_family()`)
rather than assuming one existed.

Second finding: `claim_graph.py` (audited as "fully-built but structurally
dormant" in the original audit) was in fact already reactivated in Phase 8
via `claim_graph_shadow.py` — but strictly **per-request**: a fresh
`ClaimGraph()` per call, never persisted, keyed by `claim_id`. Nothing
before Phase 11 accumulated a graph *across* requests, because
cross-request claim identity (`semantic_family_id`) did not exist until
Phase 10. Phase 11 is the natural continuation once that identity existed
— not a new "dependency_graph.py from scratch" (which the original audit
correctly warned against), but a new persisted, family-keyed sibling to
the existing per-request, claim-keyed shadow graph.

Also confirmed live (not assumed): `claim<->claim` NLI pairs cover **all**
of a request's claims, but `semantic_family_id` is only assigned to the
first three (`claims_data[:3]`, an existing Phase 10 cost bound). This
mismatch means most real contradiction pairs involve at least one claim
without a family — see §5/§13 for the measured live impact
(`skipped_no_family` was 7–15 per request across all four live runs).

---

## 2. Dependency semantics (the one substantive design decision)

The plan explicitly warns: *"A supports B does NOT automatically mean B
depends_on A."* The pre-existing Phase 8 shadow graph's own bookkeeping
convention did exactly that (`if A supports B: B.depends_on.append(A)`),
but only as disposable per-request diagnostic — never a problem while it
was thrown away after logging. Persisting it across requests as the basis
for real re-verification would have been exactly the mistake the plan
warns against: an NLI "supports" verdict between two independently
extracted world claims is a lexical/entailment observation, not proof
that one claim's justification structurally routes through the other.

Decision made and implemented (`agent/family_dependency_graph.py`):

| Relation | Persisted edge | Creates `depends_on`? |
|---|---|---|
| `contradicts` | symmetric `contradicts` edge, both directions | **Yes**, symmetric, both directions |
| `supports` | single `supports` edge (NLI pair's own direction) | **No** — diagnostic only |
| `unrelated` / `uncertain` | none | No |

Rationale: two claims found to contradict each other cannot both remain
true. If new evidence later revises one, the other's current status is
directly implicated **by the same fact pattern that created the edge in
the first place** — that is the only relation type this system treats as
epistemic license to flag "requires recheck." Lexical similarity,
co-occurrence, and "supports" are all explicitly excluded as triggers.

---

## 3. Trigger point (Phase 11.2)

A family's "state" is its raw `verification_status` string — no new
vocabulary, reused verbatim from `agent/orchestrator/claims/status.py`.
Each request compares the status of the claim(s) it just linked into a
family against that family's previously persisted status
(`FamilyDependencyGraph.observe_family_status()`). A family's **first-ever
observation is never a change** — there is nothing for the new state to
have diverged from. Only a genuine transition on a subsequent request
triggers traversal. This is a real lifecycle point already computed by
the existing pipeline (claim status classification runs before this
point), not an artificially invented hook.

---

## 4. Shadow results (Phase 11)

`agent/family_dependency_graph.py::apply_family_dependency_shadow()` is
structurally inert with respect to the triggering request: it only reads
`claims_data` (never assigns into a claim dict — checked by a static
source-inspection regression test, not just by convention) and takes no
`synthesis_result`/`trust`/`evidence_data` parameter. It cannot reach any
of those subsystems even if a caller wanted it to.

Live proof (two genuinely separate process invocations, real web search,
real LLM claim extraction and NLI, real `registry/claim_family_graph.json`
persistence):

- Run 1: 3 families created (`fam_009d02b6`/`fam_885f0569`/`fam_a820e536`),
  one claim (`cl_c68431b5`, linked into `fam_009d02b6`) observed with
  status `contradicted`. No baseline existed yet -> `families_changed=0`,
  correctly not flagged as a "change."
- Run 2 (separate process, same query): `fam_009d02b6` reappeared (the
  claim-family linker matched near-identical wording into the same
  family — Phase 10 machinery, unmodified), this time with status
  `supported`. Genuine transition detected:
  `families_changed=1 recheck_candidates=2 cycles=1
  duplicates_suppressed=1 max_depth_reached=2`, with two real
  `RECHECK_CANDIDATE` log lines at depth 1 and depth 2 naming real
  dependent families recorded from run 1's contradiction edges. The
  request's own answer, Trust (`WEAKLY_SUPPORTED`), and coverage were
  unaffected — confirmed by reading the full log to completion.

---

## 5. Strange edges found

None. `strange_edges` (a claim family simultaneously supporting and
contradicting the same other family) was 0 in every live run. The one
genuinely "strange" thing found was **not an edge but a scope mismatch**,
already documented in §1: `skipped_no_family` (pairs where at least one
side's claim never got a `semantic_family_id` because of the pre-existing
`[:3]` cap) was 7, 8, 15, and 5 across the four live runs respectively —
meaning the persisted graph today only ever grows from a minority of the
real contradictions the pipeline actually detects. This is a real,
measured limitation, not a hypothetical one — documented in §13, not
fixed (fixing it means changing the `[:3]` cap's scope, which is a
cost/behavior tradeoff for a future phase to make deliberately, not an
incidental change here).

---

## 6. Cycle behavior

`depends_on` is symmetric by construction (every `contradicts` pair
creates both directions at once), so even a single contradicting pair is
already a 2-cycle. `FamilyDependencyGraph.find_recheck_candidates()`
handles this with: a visited set seeded with the origin (changed) family
— so the family that changed can never be flagged as its own dependent —
plus `MAX_TRAVERSAL_DEPTH=5` and `MAX_RECHECK_CANDIDATES=50` hard caps.
Two diagnostic counters distinguish cycle types: `cycles` (an edge leading
directly back to the origin — a closed loop) vs `duplicates_suppressed`
(an edge leading to some other family already visited via a different
path — a diamond/convergent graph, not strictly a cycle back to start).
All ten required test shapes (A->B, A->B->C, A->B->A, A->A self-loop,
multiple dependents, diamond convergence, etc.) are covered by dedicated
regression checks and pass. The live run in §4 organically exercised the
real 2-cycle case and reported it correctly (`cycles=1`).

---

## 7. Recheck queue implementation (Phase 12)

`agent/dependency_recheck.py::apply_dependency_recheck()` reads Phase
11's `recheck_candidate_details` (now included in its returned stats —
an additive field; nothing about Phase 11's own inertness depends on who
reads its return value, only on what the function itself does) and, for
each depth-1 candidate (see §8 for why depth 1 only):

1. Resolves `dependent_family -> canonical_text` via a direct
   `family_id` lookup against `ClaimFamilyRegistry.families` (no schema
   change to Phase 10's registry).
2. Resolves `family -> belief` via the new read-only bridge in §1
   (`Belief.claim_ids` intersected with the family's member `claim_id`s).
3. If no belief is found: logged, counted (`skipped_no_belief`), no
   fabrication — this system does not invent beliefs during a recheck.
4. If found: runs **real** retrieval
   (`agent.claim_evidence_retriever.retrieve_for_claims` — the same
   primitive the live request path's own second retrieval pass already
   uses) and **real** single-pair NLI
   (`agent.claim_relation.classify_relation`) against the canonical text.
5. Classifies the outcome (`supported` / `contradicted` / `disputed` /
   `inconclusive` / `no_evidence` / `error`) and, **only** for the first
   three (a real supports/contradicts signal was found), calls
   `belief_manager.add_belief(topic=belief.topic,
   statement=belief.statement, ...)` — an exact-match lookup that routes
   to the belief manager's own existing `_update_existing()` (Bayesian
   confidence update + append-only history). No new belief mutation
   logic was written; the entire "update CURRENT state, preserve
   HISTORY" requirement is satisfied by reusing that existing path
   unchanged.

---

## 8. Bounds

| Bound | Value | Purpose |
|---|---|---|
| `MAX_RECHECKS_PER_CALL` | 3 | Global cap on real retrieval calls per invocation, regardless of how many families changed simultaneously — worst case is bounded no matter what Phase 11 found. |
| Cascade depth | 1 (hard-filtered) | Only immediate dependents are chased synchronously. A depth-1 recheck's own resulting status change becomes a **future** request's own trigger — multi-hop propagation happens one hop per request cycle, never all at once in a single request. This is the direct answer to the plan's "one new claim -> hundreds of recursive searches -> network storm" concern. |
| `RECHECK_COOLDOWN_SECONDS` | 3600 | Per-family retry bound / self-trigger protection — a family sitting at the intersection of several simultaneously-changed dependencies is not re-fetched repeatedly in a burst. |
| Phase 11's own `MAX_TRAVERSAL_DEPTH` / `MAX_RECHECK_CANDIDATES` | 5 / 50 | Bounds the *diagnostic* traversal Phase 12 reads from — independent, already-tested bound from Phase 11. |

`retrieve_for_claims` itself additionally carries its own pre-existing
`MAX_CLAIMS=8` selection cap and per-worker timeout — Phase 12 rides on
an already-bounded, already-production-proven primitive rather than
calling a raw network client directly.

---

## 9. History preservation

Verified three ways: (a) regression tests assert `belief.history` grows
by exactly one entry per real recheck and the original `"initial"` entry
is never removed or altered; (b) the controlled live integration run
(§11) showed a real `"bayesian_update"` entry appended after a real
Wikipedia retrieval, with the original entry still present; (c) the
inconclusive/error paths are tested and proven to leave `history`
**completely unchanged** (not even a no-op entry) — a deliberate choice
documented in the module's own docstring: calling `add_belief()` with two
empty evidence lists would only add history noise with no epistemic
signal, so this module skips the call entirely in that case rather than
mirroring `lifecycle.py`'s unconditional per-request call pattern.

---

## 10. Performance cost

Measured live, not estimated:

- Zero-candidate requests (the common case — 3 of 4 live runs): Phase 11
  + Phase 12 combined overhead was **0.15–3.0 ms**, entirely in-memory
  JSON-graph bookkeeping, zero network calls.
- The one live run with a real transition: Phase 11's own traversal
  added `elapsed_ms=0.42`; Phase 12 was not yet wired in at that point in
  development (this run predates Phase 12's commit) so no recheck fired,
  but the mechanism was proven separately.
- Controlled live integration run (real recheck, real network): one
  `retrieve_for_claims` call took ~35s wall time (dominated by
  `web_request=33.5s` — real scraping of ~19 URLs, Cloudflare-challenged
  proxy retries, etc. — this is `retrieve_for_claims`'s own pre-existing
  cost profile, unchanged by this phase) plus one NLI classification.
  With `MAX_RECHECKS_PER_CALL=3`, worst-case added latency to a request
  that happens to trigger the maximum is bounded at roughly
  3 × (that primitive's own cost), not unbounded.

---

## 11. Live examples

Four full production `process()` runs (fresh `session_id` each time to
avoid an unrelated pre-existing personality/irritation early-gate — see
§14 item 5) plus one controlled integration run:

1. **Phase 11 proof run** (`session_id=phase11_live_*`, cache disabled):
   real transition detected, `cycles=1`, `recheck_candidates=2`, answer/
   Trust/coverage unaffected. Full detail in §4.
2–4. **Phase 12 sanity runs** (`session_id=phase12_live_*`, cache
   disabled): all reached the new call sites cleanly, all logged
   `[Family Dependency Shadow]` and `[Dependency Recheck Summary]` with
   zero candidates (no state change that run), zero crashes, `~1–3ms`
   combined overhead each time.
5. **Controlled integration run** (`agent/dependency_recheck.py` called
   directly with a hand-constructed candidate, real unmocked
   `retrieve_for_claims`/`classify_relation`): real Wikipedia evidence
   retrieved for "Компания Apple была основана 1 апреля 1976 года.",
   classified `supports`, belief updated
   (`evidence_for=['ev_46a9ba31']`, history `1 -> 2` entries,
   `recheck_log` outcome `"supported"`). This is the definitive proof of
   real end-to-end causality the plan asks for when a natural internet
   trigger is not reliably reproducible on demand.

---

## 12. Regression results

26/26 suites green throughout (24 pre-existing + 2 new):

- `agent/epistemic_family_dependency_shadow_regression_test.py` — 20
  checks (Phase 11): simple/multi-hop/cyclic/self-cycle dependency,
  multiple dependents, unrelated/uncertain create no edge, same-family
  multiple occurrences, no duplicate candidates, non-LLM method never
  creates a false edge, corrupt-file fail-open, persistence round-trip,
  structural inertness (no mutation, no forbidden parameters).
- `agent/epistemic_dependency_recheck_regression_test.py` — 12 checks
  (Phase 12), against the **real** `BeliefManager` (only the network/NLI
  boundary is mocked): supports/contradicts/inconclusive/error outcomes,
  cooldown-based duplicate suppression, depth-1 cascade bound,
  `MAX_RECHECKS_PER_CALL` hard cap, no-belief-found handling, empty-input
  handling.

No pre-existing suite needed behavioral changes; one pre-existing check
(Phase 11's "return value never captured") was rewritten to test the
stronger, still-true invariant once Phase 12 legitimately started reading
that return value — see the commit message for the full reasoning.

---

## 13. Known limitations (real, measured, not fixed here)

1. **`[:3]` family-linking cap vs. uncapped NLI pairing** (§1/§5): most
   real contradiction pairs involve at least one claim outside the first
   three, so they never reach the persisted graph. Fixing this means
   deliberately changing an existing Phase 10 cost bound — a scope
   decision for a future phase, not an incidental fix here.
2. **Belief-per-family cardinality**: `_belief_for_family()`'s
   most-recently-updated tie-break is a considered but real design
   choice, not a proof that the other candidate beliefs (if a family
   ever spans claims from requests that each independently created their
   own belief before being linked into one family) are wrong to ignore.
3. **`registry/claim_family_graph.json` grows unboundedly**, same
   append-only-JSON-file pattern already accepted (not fixed) for
   `registry/beliefs.json` (Phase 4) and `registry/claim_families.json`
   (Phase 10) — consistent with this project's existing, deliberate
   "measure first, don't optimize speculatively" stance, not something
   this phase should have solved differently from its siblings.
4. **Cross-hop propagation is real but slow-by-design**: a change three
   hops away in the dependency graph will take three separate triggering
   requests to fully propagate (§8's depth=1 bound). This is the
   intended tradeoff against network-storm risk, not an oversight.

---

## 14. Bugs found but NOT fixed (out of scope, flagged per the plan's own instruction)

1. **Pre-existing NameError in `agent/claim_evidence_retriever.py`**
   (`retrieve_claim_evidence()`, line ~711): `elif query_context:`
   references a bare name that is never assigned in that function's own
   scope (it only exists as a local inside a different, nested helper,
   `_build_contextual_claim_text`). Triggers whenever
   `_extract_subject_anchors(claim_text)` returns no anchors — e.g. any
   claim whose only capitalized/entity-like token is its very first word
   (explicitly excluded from anchor extraction) and isn't one of the five
   hardcoded planetary aliases. Caught by an existing per-worker
   try/except, so it fails soft (0 evidence records, not a crash) — but
   this means retrieval silently returns nothing for such claims **in
   the existing production pipeline**, not just in this phase's testing.
   Discovered while building this phase's own controlled integration
   test (a claim text starting with "Земля..." hit it immediately); the
   controlled test was adjusted to use claim text with a discoverable
   mid-sentence anchor instead of touching the unrelated retriever
   module — fixing it would be exactly the kind of "заодно" (incidental)
   change across an unrelated subsystem the plan's STOP conditions warn
   against. Left as-is, flagged here for a dedicated future fix.
2. **The known issues already listed in the brief** (false insult
   substring match on "туп" inside "преступление"; `belief_manager.
   _find_similar()`'s ~2.7–3.2s realistic cost; `source_cluster_id`
   persistence path parity) were not re-investigated this phase — nothing
   in Phases 11–12 touched their code paths.

---

## 15. Readiness for Trust consolidation (Phases 13–14)

Not assessed as "ready" or "not ready" — genuinely out of scope for this
report, per the plan's explicit instruction not to start Phases 13–14
without new direction. One relevant observation for whoever picks that up
next: this phase's own audit (§1) found a second, independent instance of
the same pattern the original audit flagged for Trust — multiple
uncoordinated identity/state systems (`semantic_family_id`, belief
identity, and now this phase's own family-dependency graph) that overlap
in subject matter without a single source of truth. Trust consolidation
should probably account for whether belief confidence (which Phase 12 now
actively mutates, cross-request) is meant to feed Trust's eventual single
computation path, or remain a parallel signal — that is a real design
question the next phase will need to answer, not something this report
is deciding.

---

## Summary

Phase 11 (shadow) and Phase 12 (bounded, real re-evaluation) are both
committed, fully tested (32 new regression checks across two suites, 0
failures), and proven live — both through genuine production pipeline
runs and a controlled deterministic integration run exercising real
network retrieval, real NLI, and a real history-preserving belief update.
No Trust, verification_status vocabulary, or storage-subsystem changes
were made. `git status` is clean; nothing has been pushed.
