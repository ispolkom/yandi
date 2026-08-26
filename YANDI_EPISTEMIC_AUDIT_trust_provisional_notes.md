# Trust Engine / Provisional Status / Negative Evidence — working notes

Scope: brief §5 (provisional knowledge status), §8 (trust ≠ truth), §9 (negative evidence).
Other forks cover dataflow/object-model, claim identity/source independence, and
dependency-graph/storage — not duplicated here.

---

## TOPIC 1 — Trust ≠ Truth (§8)

### 1.1 THE BIGGEST FINDING: the gated trust label never reaches the user-facing response

`agent/orchestrator/epistemic/trust_gate.py::apply_epistemic_trust_adjustment()` computes a
carefully-gated `label` — starting from `epistemic_trust_label`, then applying a trust cap
(`_apply_trust_cap`, line 128), an interpretive/non-falsifiable downgrade (132-136), a
final-claim-coverage gate (166-195), an evidence-support-grounding gate (209-237), and a
belief-manager average-confidence gate (239-248). This is real, non-trivial downgrade logic.

But `label` is written **only** to `trace.trust` (trust_gate.py:250 — the internal/persisted
trace object) and returned to the call site
(`orchestrator_v2.py:524 label = apply_epistemic_trust_adjustment(...)`), where **it is never
read again** — confirmed via `grep -n "\blabel\b" agent/orchestrator_v2.py`: line 524 is the
only occurrence. It is not assigned back to `synthesis_result.trust_level`, not passed to
`response/writeback.py`, and never appears in the final `OrchestratorResponse`.

What the user actually sees (`OrchestratorResponse.trust_level`, `response/writeback.py:405,
421-422,449,470,635`) is `synthesis_result.trust_level` — a **separate, earlier** trust
computation done inside `synthesize()` (`agent/orch_synthesizer.py:1183-1206`), from a weighted
`trust_raw` combining `claim_validity_score`, `evidence_score`, `source_agreement`,
`source_quality`, `hypothesis_consistency`, `reflection_success`, `historical_reliability` —
computed **before** claim-evidence NLI relations are even finalized into per-claim
`verification_status`, and before any of trust_gate.py's coverage/grounding gates run.

**Consequence**: `evaluate_claim_status_gate()` (`claims/status.py:229-489`, runs AFTER
`apply_epistemic_trust_adjustment` in `orchestrator_v2.py`'s sequence — confirmed by reading
`process()`'s call order) DOES correctly mutate `synthesis_result.trust_level` directly
in-place for its 5 branches (e.g. forcing `UNVERIFIED` when all claims are
contradicted/rejected, capping at `WEAKLY_SUPPORTED` on disputed claims) — so the *final*
user-facing value is not the raw pre-gate synthesis trust either; it goes through this second,
different gate. But `apply_epistemic_trust_adjustment`'s `label` — the one incorporating
final-claim-coverage and support-grounding scores — is discarded. Two independent trust-capping
mechanisms exist (`trust_gate.py`'s `label` and `claims/status.py`'s direct
`synthesis_result.trust_level` mutation); only the second one's output is visible to the user,
and it does not incorporate the coverage/grounding gates the first one computed. This is a
structural trust≠truth risk: the more rigorous of the two gates is computed and then thrown away.

### 1.2 Trust increase source — not gameable via repeated agreement in `_calculate_delta_factors`, but this function is dead weight for the live epistemic path

`_calculate_delta_factors()` (`trust_gate.py:45-78`) takes `verification_verdict, confidence,
has_sources, consensus_agreement, total_nodes` — this is P2P/multi-node consensus machinery
(`consensus_ratio = consensus_agreement/total_nodes`), not the claim/evidence pipeline. **It has
zero call sites** outside its own file and `orchestrator_modularization_regression_test.py`
(`grep -rn "_calculate_delta_factors" agent/` — only definition + import in
`response/writeback.py:59`, itself unused after import — confirmed by grepping
`_calculate_delta_factors(` with call parens: zero matches). So today's live trust delta isn't
computed by this function at all for claim verification — it's dead/vestigial, apparently built
for a node-reputation-voting model that was never wired to the current claim pipeline. Not a
"gameable via resubmission" risk today because it isn't in the live path; flag as
DEPRECATE-candidate or note as an already-designed-but-unused hook for future P2P consensus
work (see §14 DO-NOT-TOUCH: "P2P узел" is explicitly off-limits, so leave as-is, just document).

### 1.3 `orch_reputation.py` — separate node-reputation system, also not part of claim trust, has a real Sybil/circularity mechanism if ever wired to claims

`agent/orch_reputation.py` tracks per-*node* (LLM inference endpoint) accuracy via SQLite
(`_update_node_local`, lines 149-186: `reputation = correct/total`, exponential-smoothed
`speed_avg`). Update requires an explicit `correct: bool` judgment supplied by the caller —
grep confirms **zero call sites for `update_node(` anywhere in `agent/` outside its own
`__main__` test block** — this module is not wired into the orchestrator at all currently. It
also has a Redis pub/sub distributed-sync path (`_publish_reputation_update`,
`_start_listener_daemon`, lines 43-104) that would let one instance's `correct=True` judgments
propagate to other instances' `reputation` scores — a real Sybil vector (any instance can
publish arbitrary correctness for any node_id) **if this were ever activated**, but it is
inert today. `add_decision_event`/`get_trace`/`get_ledger` (253-268) are explicit stub
functions ("Заглушка для совместимости") — confirms this module was scaffolded for a
Decision-Ledger concept that was never completed.

### 1.4 `agent/trust_model.py` exists but is about interpersonal/character trust, not epistemic claim trust — naming collision risk for the audit itself

Read in full: `TrustModel` (lines 44-291) tracks a 0-100 `level` per `user_id`, driven by
`TrustEvent`s like `apology/insult/honesty/dishonesty/help/consistency` (weights at
144-193) — this is the character/personality subsystem's model of how much YANDI trusts *the
user* in conversation, persisted per-user to `registry/trust_{user_id}.json`. **Zero call
sites**: `grep -rn "get_trust_model\|TrustModel(" agent/*.py *.py` outside `trust_model.py`
itself returns nothing — this module is fully dead code, not wired into the orchestrator or
epistemic pipeline at all. Flagging explicitly because its name is easy to confuse with the
epistemic trust label system (`trust_gate.py`) during the architecture proposal — they are
unrelated concepts that happen to share the word "trust." Recommend the final report note this
explicitly so KEEP/EXTEND/DEPRECATE lists don't conflate them.

### 1.5 Does HIGH trust ever skip verification? — No live evidence found

No call site reads `epistemic_trust_label`, `trace.trust`, or `synthesis_result.trust_level`
*before* the claim/evidence pipeline runs to decide whether to skip retrieval/NLI/validation —
the trust label is computed only after claims_data has already been through structural
validation, evidence mapping, and NLI (`process()`'s sequence, confirmed via
`orchestrator_v2.py`'s call order: claims lifecycle → mapping → status classification →
`apply_epistemic_trust_adjustment` → claim status gate). So today, trust does not bypass
verification for the *current* request. Whether trust from a *prior* request could suppress
re-verification of the *same* claim in a *future* request is a provenance/dependency-graph
question (see other forks — no such cross-request trust-based skip mechanism was found in this
fork's reading either; `belief_manager.py`'s `_find_similar` re-checks similarity every time,
it does not skip re-verification based on stored confidence).

---

## TOPIC 2 — Provisional Knowledge Status (§5)

### 2.1 Full status enumeration (claim-level, `verification_status`)

Defined/assigned exclusively in `agent/orchestrator/claims/status.py`:
- `"candidate"` — default before NLI (`claims/status.py:110`, also set as the get-default at
  `claim.get("verification_status", "candidate")`); real assignment origin is
  `agent/claim_evidence_mapper.py:401` (per dataflow fork's notes).
- `"supported"` / `"contradicted"` / `"disputed"` / `"unverified"` — assigned by
  `classify_claim_epistemic_status()` (`claims/status.py:178-190`), the ONLY function that
  writes these four.
- `"rejected"` — assigned upstream by structural validation (`claims/validation.py`, per
  dataflow fork), read (not written) at `claims/status.py:114`.
- `"verified"` — **referenced but never assigned on the live path.** Confirmed via
  `grep -rn '"verified"' agent/ agent/orchestrator/` — the only claim-level occurrence is
  `claims/status.py:264` (`evaluate_claim_status_gate`'s `claims_verified` count reading
  `verification_status == "verified"`); no function anywhere sets a claim's
  `verification_status` to `"verified"`. `classify_claim_epistemic_status`'s own docstring says
  so explicitly (`status.py:12`: *"verified never assigned here"*).

**Concrete structural consequence**: `evaluate_claim_status_gate`'s branch at
`claims/status.py:416` (`elif claims_verified == 0:`) is **unconditionally true on every live
request** — `claims_verified` is always 0 — so the trust ceiling this gate can produce is
permanently capped at `PARTIALLY_SUPPORTED` (or lower), never `SUPPORTED`/`STRONGLY_SUPPORTED`/
`VERIFIED`, for *any* claim-bearing answer, regardless of evidence quality. This isn't a
provisional-status *design* — it's an accidental permanent ceiling from an enum value
(`"verified"`) that nothing in the live pipeline is wired to produce. Worth flagging in KEEP/
EXTEND: either wire a real verification path to `"verified"`, or remove the dead branch/enum
value — as-is, "verified" is an aspirational status with no producer.

### 2.2 No explicit "provisional, pending revisit" status distinct from "unverified"

`"candidate"` is the closest analog (pre-NLI), but it never survives to the final response —
every claim leaves `classify_claim_epistemic_status()` reassigned to one of
supported/contradicted/disputed/unverified/rejected (function loops over all `claims_data`
unconditionally, `status.py:107-224`, no early-exit that would leave `"candidate"` standing).
So today, "provisional/awaiting revisit" and "checked, found nothing" are the same terminal
value: `"unverified"`. There's no field indicating a claim *should be rechecked later* vs. was
*checked and found genuinely inconclusive right now* — see Topic 3 for the sharper version of
this gap (NOT-FOUND vs FAILED vs never-attempted).

### 2.3 `belief_manager.py`'s `Belief.status` is about supersession lineage, not confidence tier

`Belief.status: str = "active"  # active | revised | rejected | superseded`
(`belief_manager.py:44`), transitions: `_update_existing()` sets `"revised"` (490),
`supersede_belief()` sets `"superseded"` on the old belief (508). This tracks *which record is
current* in a belief's edit history, not *how provisionally-held* a belief is — a belief can be
`status="active"` with `confidence=0.3` (weak but current) or `confidence=0.95` (strong and
current); status doesn't encode confidence tier at all — `confidence: float` (a separate field,
`belief_manager.py` dataclass) is the actual epistemic-strength signal, and it's a continuous
0-1 float with no discrete "provisional" cutoff defined anywhere (no threshold constant found
for what confidence counts as "provisional" vs "established").

---

## TOPIC 3 — Negative Evidence (§9)

### 3.1 NOT-FOUND vs FALSE — correctly kept separate (not a gap)

`classify_claim_epistemic_status()`'s branch logic (`claims/status.py:178-190`):
```
if supports_count > 0 and contradicts_count > 0: "disputed"
elif supports_count > 0:                          "supported"
elif contradicts_count > 0:                        "contradicted"
else:                                              "unverified"   # no evidence at all
```
Confirmed: a claim with **zero relations found at all** (empty `evidence_relations` list) and a
claim with **relations found but all `uncertain`/`unrelated`** both fall through every branch to
`"unverified"` (line 187-190's own comment: *"uncertain / unrelated / отсутствие evidence не
дают основания считать claim поддержанным"*) — **never** `"contradicted"`. So the core axiom's
first half — NOT-FOUND must never silently become FALSE — **is respected structurally**: there
is no code path from empty/absent evidence to `contradicted`. Good news, not a gap.

### 3.2 Absence-of-counter-evidence never silently becomes PROVEN either — also respected

Same branch table: reaching `"supported"` strictly requires `supports_count > 0` (line 181) —
`contradicts_count == 0` alone is never sufficient; a claim with zero support AND zero
contradiction lands on `"unverified"`, not `"supported"`. Checked the two counting inputs
(`supports_count`/`contradicts_count`, lines 146-156) — both are independent evidence-relation
tallies, no code path treats "nothing contradicts it" as itself a positive signal. This
particular failure mode (asked for explicitly in the brief) **does not exist in the current
code** — confirmed by reading, not assumed.

### 3.3 THE REAL GAP: NOT-FOUND, FOUND-BUT-UNCERTAIN, and NEVER-SEARCHED all collapse into the same `"unverified"` label — no FAILED-search state either

Despite 3.1/3.2 being clean, `"unverified"` is heavily overloaded — it is the landing status
for at least four distinguishable real-world situations that a genuine epistemic system should
tell apart:
1. Evidence search ran, found candidate evidence, NLI ran, all relations came back `uncertain`
   or `unrelated` (searched, inconclusive).
2. Evidence search ran and found **zero** candidate evidence at all (searched, confirmed
   nothing exists to cite — a genuine NOT-FOUND).
3. Evidence search never ran for this claim (e.g. `skip_rag`, budget/timeout cutoff before this
   claim's turn — see `claims/retrieval.py`'s PASS2 gate, not fully traced in this fork but
   flagged for the dependency/storage fork's timeout findings).
4. Evidence search ran but **errored/timed out** (a technical failure, not an epistemic
   finding) — `agent/orch_web_scraper.py`'s fetch-cache mentions failures but this fork did not
   trace whether a scraper exception ever gets surfaced as a distinct claim-level flag vs.
   silently producing an empty evidence set indistinguishable from case 2.

No field anywhere in `claims_data`/`ClaimRecord` (checked `orch_schemas.py`'s `verification_status`
definition, line 383 — plain `str`, no companion `search_attempted`/`search_error` boolean)
distinguishes these. This is the sharper, evidenced form of the brief's negative-evidence
concern: not "NOT-FOUND becomes FALSE" (doesn't happen) but "NOT-FOUND, INCONCLUSIVE, SKIPPED,
and ERRORED are all indistinguishable from each other" once collapsed to `"unverified"` — a
downstream re-evaluation/cascade mechanism (other fork's §6/§7 topic) cannot tell "worth
retrying with a real search" from "already tried, genuinely nothing there" from "we never even
looked."

### 3.4 Existence-question contract (`existence_contract.py`) doesn't add this distinction either

`agent/orchestrator/epistemic/existence_contract.py::apply_existence_query_contract()`
(read in full) only checks whether any claim has `supports_query_aspect[0] == "CORE"`
(line 52) — it catches "all claims are supported but none actually answers the existence
question," which is a different, narrower problem (CORE-claim coverage, not search
success/failure). It performs no distinction between "searched, confirmed doesn't exist" and
"search never produced a CORE claim for other reasons." `_is_existence_question()`
(`agent/claim_evidence_retriever.py:1242`) is a boolean query-shape classifier only — it
doesn't track search outcome either. Confirmed via reading both functions fully; no FAILED vs
NOT-FOUND vs NEVER-ATTEMPTED distinction exists anywhere in the existence-question path.

### Proposed minimal fix direction (NOT implemented — proposal only)

Split `"unverified"` into what it's already implicitly conflating, reusing existing signal
where possible: keep `"unverified"` for case 1 (searched, inconclusive — this is the true
provisional state), and add two cheap boolean/enum companions rather than new top-level
statuses: `evidence_search_attempted: bool` (case 3 vs 1/2) and
`evidence_search_error: Optional[str]` (case 4 vs 1/2) alongside the existing
`verification_status` field on the claim dict — this keeps the existing 6-value status
vocabulary intact (respecting "epistemic statuses" being on the DO-NOT-TOUCH list per brief
§12) while making the NOT-FOUND/FAILED/NEVER-TRIED distinction reconstructable without
redefining what `"unverified"` means.

---

## Report to parent
File written: this file, ~185 lines. No code modified, no commits made.
