# Epistemic Core v1 — Phase 10: Cross-Request Claim Linking

Gated on Phase 9B's acceptance criterion (precision ≥ 0.95 on the
hard-negative corpus) being met — it was (1.000), so this phase proceeded
autonomously per the explicit authorization.

## 1. Mechanism

`agent/claim_family_registry.py::ClaimFamilyRegistry` — a minimal,
JSON-backed registry (`registry/claim_families.json`, gitignored like
`registry/beliefs.json` and every other registry data file). One record
per **semantic claim family**:

```
{
  "family_id": "fam_<uuid8>",
  "domain": "...",
  "canonical_text": "<first member's claim_text>",
  "members": [{"claim_id", "claim_text", "linked_at"}, ...],
  "created_at", "updated_at"
}
```

`find_or_link_claim(claim_text, claim_id, domain)` scans existing
families in the same `domain`, calls Phase 9B's hardened
`classify_claim_pair()` (reused **unmodified** — zero new
embedding/NLI logic) against each family's `canonical_text`; on
`exact`/`equivalent` it appends this occurrence to that family's
`members` list (never overwriting or removing an existing member — pure
append), otherwise it creates a new family.

Guarantees satisfied by construction, not by extra code:
- **`claim_id` never destroyed** — each occurrence keeps its own random
  claim_id from Phase 2; the registry only ever adds a parallel
  `semantic_family_id`, it doesn't touch or replace claim_id anywhere.
- **Wording preserved** — each member's original `claim_text` is stored
  verbatim, not normalized away.
- **Evidence provenance preserved** — this registry stores no evidence
  at all; a claim's `evidence_relations` (Phase 1) stay exactly where
  they were, on the trace's `ClaimRecord`. Family linking is purely
  additive metadata alongside that.
- **Temporal variants not collapsed** — transitively guaranteed by reuse:
  Phase 9B's `hardening_guard()` downgrades `current_vs_historical`
  marker-mismatched pairs to "different" inside `classify_claim_pair()`
  itself, so a temporal-variant pair simply never reaches "equivalent"
  in the first place. Nothing temporal-specific was added here.

Wired into `agent/orchestrator/claims/lifecycle.py::update_beliefs_link_answer_and_personality_cycle()`
— the one place `claim_text`/`claim_id`/`domain` (via `epistemic_result.domain`)
are already all available together, right alongside the pre-existing
belief-update loop. Capped to `claims_data[:3]`, reusing that loop's own
established cap (same class of cost concern — bounded per-claim network
calls — not a new number invented for this phase).

`semantic_family_id` persisted through `ClaimRecord`
(`orch_schemas.py`) and `Trace.add_claim_raw()`/`to_dict()`
(`orch_tracer.py`), same minimal pattern as Phases 1-3.

## 2. Live verification (two independent proofs)

### 2.1 Definitive cross-*process* proof

Two genuinely separate Python process invocations (`python3 -c "..."`,
not two calls in one script), real network calls, sharing the real
on-disk `registry/claim_families.json`:

```
Process 1: "Компания Apple была основана в 1976 году в гараже."
           -> new family fam_b0e462a7, member cl_process1_occurrence

Process 2: "В 1976 году Стив Джобс и Стив Возняк основали Apple
            в гараже родителей."
           -> linked into fam_b0e462a7, member cl_process2_occurrence
```

Confirmed in the persisted file: **one family, two distinct claim_ids,
both texts preserved verbatim, materially different wording** (different
sentence structure, second text names both founders and "родителей",
first doesn't). This is the core Phase 10 claim, proven directly.

### 2.2 Full live pipeline proof (end-to-end, both requests through `process()`)

Two separate full pipeline queries, real web search, real LLM claim
extraction, no mocking:

```
Query 1: "Кто написал роман Война и мир?"
  -> 3 new families created (first-time claims), including fam_05ca2a13:
     cl_80f51a1e: "Русский писатель Лев Николаевич Толстой является
                   автором романа «Война и мир»."

Query 2: "Кто автор произведения Война и мир?"
  -> cl_82539c26: "Русский писатель Лев Николаевич Толстой является
                   автором произведения «Война и мир»."
     -> LINKED into fam_05ca2a13 (now 2 members)
  -> 2 other claims from this query created their own new families
     (LLM extraction phrased those differently enough this time — not
     a failure, just non-determinism in which specific claims from a
     9-10-claim set happen to converge in wording; the definitive proof
     is §2.1, this is corroborating evidence on top of it)
```

Confirms the production wiring works end-to-end through the real
orchestrator call chain, not just via direct API calls.

## 3. Design decisions and their rationale

- **Domain-scoped comparison** (not global): mirrors
  `belief_manager.py::_find_similar()`'s own topic-scoped candidate
  filtering — an established pattern, not invented here.
- **`[:3]` cap per request**: reuses the exact cap the pre-existing
  belief-update loop in the same function already applies, for the same
  reason (bounded per-claim network cost in a function that already
  makes several network calls per request).
- **Full-file JSON rewrite on every link** (`_save()`): same pattern as
  `belief_manager.py`'s `_save()` — deliberately not hot/cold split or
  otherwise optimized preemptively, following Phase 4's own conclusion
  that such optimization should be benchmark-driven, not done
  speculatively. `registry/claim_families.json` starts empty; at current
  scale (a handful of families created during this session's testing)
  this is a non-issue.

## 4. Honest open question — NOT addressed this phase

**Comparison-cost scaling is not benchmarked.** `find_or_link_claim()`
does a linear scan over all families in a domain, calling
`classify_claim_pair()` (embedding + possibly LLM judge) against each
until a match is found or the scan is exhausted. At current scale (a
few families per domain from this session) this is fast. What happens
once a busy domain accumulates hundreds of families — whether linking
latency becomes a real per-request cost, and whether a cheaper
first-pass filter (e.g., a bulk embedding prefilter across all
candidates at once, rather than one `classify_claim_pair()` call per
candidate) would be worth adding — is an open question, explicitly
**not resolved here**, consistent with Phase 4's benchmark-first
discipline: don't optimize speculatively, benchmark first if/when this
becomes a real concern. Flagged as a candidate for a future dedicated
phase, not forced now under time pressure.

## 5. Verification

- New suite `agent/epistemic_claim_family_regression_test.py` (15
  checks, `classify_claim_pair()` mocked — proves the REGISTRY's own
  logic, not the classifier, which Phase 9B already covers): new family
  creation, linking into an existing family, claim_id preserved
  distinctly across members, wording preserved verbatim, a
  not-equivalent claim gets its own family (proving the wiring respects
  whatever the classifier says, including temporal variants), idempotent
  re-linking of the same claim_id, domain scoping, persistence/reload
  correctness, empty-input handling, fail-safe corrupt-file loading,
  round trip through `Trace`, backward compatibility. 15/15 green.
- Full regression sweep: 24/24 green (14 pre-existing + Phase 1-9B's
  suites).
- Live verification: both proofs above (§2.1 definitive, §2.2
  corroborating), real network calls throughout, no mocking.
