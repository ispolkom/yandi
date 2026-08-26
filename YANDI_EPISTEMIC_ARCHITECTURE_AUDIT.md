# YANDI Epistemic Core v1 — Architecture Audit

Investigation-only deliverable. No code was modified to produce this report. All claims below
are grounded in direct reads of the current code (via four parallel research passes covering
disjoint scopes) — file:line citations throughout, not assumptions. Working notes this report
synthesizes: `YANDI_EPISTEMIC_AUDIT_dataflow_notes.md`,
`YANDI_EPISTEMIC_AUDIT_identity_independence_notes.md`,
`YANDI_EPISTEMIC_AUDIT_trust_provisional_notes.md`,
`YANDI_EPISTEMIC_AUDIT_dependency_storage_notes.md`.

Core axiom under test: *"Не доверяй — проверяй. Проверка создаёт доверие. Накопленное доверие
влияет на приоритет, но никогда не отменяет необходимость проверки."* Question: can YANDI move
from "verify one answer" to "accumulate, revise, and link verified knowledge" — and if so, how,
starting from what already exists?

---

## 1. Current epistemic dataflow

```
query → intent → claim extraction → evidence retrieval → claim↔evidence NLI
  → structural validation → epistemic status classification → synthesis
  → trust adjustment ×2 → belief update → trace persistence
```

Exact chain, with file:line:

1. **Claim extraction**: `agent/orch_synthesizer.py:1016` — `claim_id = f"cl_{uuid.uuid4().hex[:8]}"`
   inside the extraction loop (~913-1050). `claim_confidence` starts fixed at `0.3`.
2. **Evidence candidate generation**: `agent/claim_evidence_mapper.py::map_claims_to_evidence()` —
   embedding-similarity only (`PRIMARY_CANDIDATE_THRESHOLD=0.35`, `SECONDARY=0.45`, lines
   281-282), sets `verification_status="candidate"` (line 401). Sole writer of
   `derived_from_evidence_ids` (`orchestrator/claims/mapping.py:39-40` docstring).
3. **NLI verdict**: `agent/claim_relation.py:833-959` `classify_claim_evidence_batch()` — the
   actual supports/contradicts/unrelated/uncertain judgment per (claim, evidence) pair, with
   `relation_method` and `source_claim` (the compared sentence).
4. **Attachment**: `agent/orchestrator/claims/mapping.py:126` `run_claim_evidence_batch()`
   writes the full relation dict to `claim["evidence_relations"]` (in-memory only). Runs twice
   per query — PASS1 (`orchestrator_v2.py:453`) and PASS2 after claim-specific re-retrieval
   (`agent/orchestrator/claims/retrieval.py:183`).
5. **Structural validation**: `agent/orchestrator/claims/validation.py` — filters malformed
   claims, sets `verification_status="rejected"`.
6. **Epistemic status**: `agent/orchestrator/claims/status.py:87-226`
   `classify_claim_epistemic_status()` — the sole place `supported/contradicted/disputed/
   unverified` get assigned, from `_counts_toward_status()`'s two paths (authority: line 71-74;
   directness: 76-82).
7. **Synthesis + first trust computation**: `agent/orch_synthesizer.py:1183-1270` — `trust_raw`
   from 7 weighted factors → `trust` string → `SynthesisResult.trust_level`.
8. **Second, gated trust computation**: `agent/orchestrator/epistemic/trust_gate.py:90-307`
   `apply_epistemic_trust_adjustment()` — computes `label` with coverage/grounding/belief gates,
   writes only to `trace.trust` (line 250), **never flows back to `synthesis_result.trust_level`**
   (§6.1 below — this is the single most consequential finding of the audit).
9. **Third trust mutation**: `agent/orchestrator/claims/status.py:229-489`
   `evaluate_claim_status_gate()` — directly mutates `synthesis_result.trust_level`/`.confidence`/
   `.answer` for 5 cases; this is what the user actually receives.
10. **Trace persistence**: `agent/orch_tracer.py::Trace.add_claim_raw()` (292-313) — lossy
    ingestion, see §3.
11. **Belief update**: `agent/belief_manager.py::add_belief()` (143-248) — separate object,
    separate identity, separate persistence (`registry/beliefs.json`).

---

## 2. Existing object model

| Object | Defined | ID scheme | Persisted? |
|---|---|---|---|
| Claim | `orch_synthesizer.py:1016` (dict, not dataclass on live path) | random `cl_<uuid8>`, content-independent | No (only `ClaimRecord` summary in trace) |
| Evidence | `orch_schemas.py` `EvidenceRecord` dataclass | `evidence_id` (scheme not traced in this pass) | Partial — first 10/trace only (`orch_tracer.py:387`) |
| Relation (NLI verdict) | ephemeral dict in `claim["evidence_relations"]`, no dataclass | none (not addressable) | **No — dropped before persistence** |
| Source | `EvidenceRecord` fields (`source_uri`, `source_class`...) or thin `add_source()` call | none separate from evidence | Same as Evidence, sometimes thinner (§3.3) |
| Belief | `belief_manager.py` `Belief` dataclass (32-51) | topic+statement, no stable id shown in this pass | Yes — `registry/beliefs.json`, full-file rewrite per mutation |
| Trace record | `orch_schemas.py` `ClaimRecord`/`EvidenceRecord`/`Trace` | `trace_id` | Yes — `registry/dataset/orch_traces/*.jsonl`, append-only, but truncated (§3) |
| `Claim` (dead system) | `agent/claim_graph.py:31-50` — separate dataclass, has `supports`/`contradicts`/`depends_on` | same `cl_<uuid8>` pattern, independent generator (`claim_graph.py:87`) | Zero-call dead singleton — never populated |

No object anywhere has a content-derived identity (hash of normalized text). Every ID is a
fresh random UUID per request; two runs producing textually identical claims get uncorrelated
IDs (`YANDI_EPISTEMIC_AUDIT_identity_independence_notes.md` §A1).

---

## 3. Provenance gaps (grounded in dataflow fork's findings)

### 3.1 NLI verdict computed, then dropped before persistence — the core provenance break

`Trace.add_claim_raw()` (`orch_tracer.py:292-313`) reads only `claim_id, claim_text,
derived_from_evidence_ids, claim_type, claim_confidence, verification_status` off the claim
dict — `evidence_relations` (the actual supports/contradicts verdict per evidence item,
produced at `claim_relation.py:833-959`) is not one of the keys read. `ClaimRecord`
(`orch_schemas.py`) has no field that could hold it. The surviving link
(`derived_from_evidence_ids`) says *which* evidence was linked, never whether it supported or
contradicted — and that list is itself truncated to 3 IDs (`orch_tracer.py:394`).

### 3.2 Truncation caps make this worse at scale

`orch_tracer.py:387` — only first 10 evidence records per trace. `orch_tracer.py:399` — only
first 15 claims per trace. A live test this session logged `claims=8, candidate_links=14` —
already exceeding the 3-per-claim evidence cap for a routine multi-claim query.

### 3.3 `EvidenceRecord` has no reverse link to claims

Fields checked (`orch_schemas.py`): no claim-id/claim-id-list field on `EvidenceRecord`. The
claim→evidence link is one-directional. Reconstructing "which claims used source X" from a
persisted trace requires scanning every `ClaimRecord` in every daily `.jsonl` file — no index.

### 3.4 Two divergent evidence-ingestion paths into the trace

`Trace.add_source()` (`orch_tracer.py:256-269`, called from `synthesis.py:170`,
`pipeline.py:916,930`) builds a record leaving `evidence_role/quality_score/source_class/
traceability/primaryness` at dataclass defaults. `Trace.add_evidence()` (271-272, called from
`pipeline.py:220` on cache-hit rehydration) takes a fully-populated record. Same conceptual
object, two different completeness levels depending on which call path built it.

### 3.5 Net effect

Provenance ("why do I believe this claim") is real and complete only at *runtime*, inside
`process()`'s local `claims_data`. It is **structurally incapable of surviving to disk** in
useful form — not a bug where a field was forgotten, but a schema-abstraction mismatch:
`ClaimRecord` was designed one level shallower than the actual runtime reasoning object, and
`add_claim_raw()`'s dict→dataclass conversion is the silent choke point, every time, with no
warning logged.

---

## 4. Identity gaps (claim identity / same-fact-different-wording)

- **No content-derived ID anywhere** (`identity_independence_notes.md` §A1). `claim_confidence`
  at creation is also always the fixed `0.3` — content-independent.
- **No claim-to-claim dedup on the live path at all.** The one system with claim-level dedup —
  `agent/claim_graph.py::_deduplicate_and_merge()` (215-236, naive `text[:50]` prefix match,
  no embedding) — is a fully dormant, zero-call singleton
  (`orchestrator_v2.py:151` constructs it; `.extract_claims()` has no callers outside its own
  `__main__` block).
- **The only live paraphrase-equivalence logic is one layer up, at Belief creation**:
  `belief_manager.py:184-248` — exact-match, then batch embedding + 0.70 cosine prefilter + LLM
  equivalence judge, scoped to same-`topic` beliefs. Claims themselves never get this treatment;
  only claims that survive into a Belief do.
- **`claim_answer_linker.py` is not an identity mechanism** — word-overlap heuristic for
  answer-text↔claim-text linking, unrelated to claim-to-claim comparison
  (`identity_independence_notes.md` §A3).

---

## 5. Source-independence gaps

Confirmed, evidenced, not hypothetical (`identity_independence_notes.md` §B1-B4):

- `source_quality.py` classifies source *class* (scientific/forum/social/...) per-URL — no
  syndication/publisher/independence concept anywhere in the file.
- `evidence_pool.py::_dedupe()` (103-193) dedups by exact URL or content-prefix. Two different
  URLs carrying the same wire-service story both survive as independent evidence.
- `claims/status.py:146-156` tallies `support_count` as a flat count of qualifying relations —
  **N syndicated copies of one story each individually count toward N**, which can flip
  `verification_status` from `unverified`/`disputed` to `supported` with zero true independent
  corroboration (`claims/status.py:178-190` is the exact branch this feeds).
- `orch_web_scraper.py`'s dedup is a fetch-efficiency cache keyed on canonicalized URL — not a
  publisher-identity concept.

---

## 6. Trust weaknesses (trust ≠ truth)

### 6.1 THE headline finding: the gated trust label is computed, then discarded

`trust_gate.py::apply_epistemic_trust_adjustment()` computes a real, multi-gate `label`
(trust cap → interpretive downgrade → final-claim-coverage gate → support-grounding gate →
belief-confidence gate, lines 90-307) — assigned to a local at `orchestrator_v2.py:524`, never
read again (confirmed: `grep -n "\blabel\b" orchestrator_v2.py` → one hit). The value the user
actually receives (`OrchestratorResponse.trust_level`) traces back to `synthesis_result.trust_level`
— set once from a *different*, earlier `trust_raw` computation (`orch_synthesizer.py:1183-1206`,
before claim NLI even finishes) and then separately re-mutated by
`claims/status.py::evaluate_claim_status_gate()` (a *third*, independent gating mechanism). The
most rigorous of the three trust computations never reaches the response.

### 6.2 Dead/unwired trust-adjacent modules create naming-collision risk, not live risk

- `_calculate_delta_factors()` (`trust_gate.py:45-78`) — P2P/consensus-voting math
  (`consensus_agreement/total_nodes`), zero call sites with arguments anywhere in `agent/`. Not
  live.
- `agent/orch_reputation.py` — per-node accuracy tracking with a Redis pub/sub cross-instance
  sync path (43-104) that **would** be a real Sybil vector if activated (any instance can
  publish arbitrary correctness for any node_id) — but `update_node()` has zero live call sites.
  Explicit stub functions at 253-268 confirm an abandoned "Decision Ledger" concept.
- `agent/trust_model.py` — interpersonal/character trust (0-100 `level` per user_id, driven by
  apology/insult/honesty events), **zero call sites anywhere**, unrelated to epistemic claim
  trust despite the shared word "trust."

### 6.3 No live evidence of trust suppressing verification within a request

Trust is computed only after claim/evidence/NLI has already run for the current request — no
call site branches on a trust value to skip retrieval or NLI. Whether *prior* trust could
suppress *future* re-verification is a dependency-graph question (§7) — no such cross-request
mechanism exists today either way, because no cross-request claim linkage exists at all.

---

## 7. Dependency / re-evaluation gaps

(`dependency_storage_notes.md` §A1-A4)

- **`agent/claim_graph.py`'s `Claim` dataclass already has the exact edge vocabulary needed** —
  `supports`, `contradicts`, `depends_on` (claim_id lists) — but is a zero-call dead singleton,
  and its edge-detection is regex/word-overlap (`_is_contradiction`/`_is_support`, 258-282), far
  cruder than the pipeline's real NLI classifier.
- **`belief_manager.py` has no belief-to-belief edges** — `Belief.claim_ids` records which
  claims produced a belief, nothing records which *other beliefs* it depends on.
  `_update_existing()`/`challenge_belief()` only ever touch the one belief passed in — no
  cascade search.
- **History preservation is already solved**: `belief.history` is append-only
  (`belief_manager.py:168-174,429-437,480-487,511-515`); only `.status` (active→revised→
  superseded) changes current interpretation. The brief's "preserve history separately from
  current status" requirement needs no new mechanism for this part.
- **`agent/knowledge_graph.py` is a different domain entirely** — council/project meta-knowledge
  (session/topic/concept/decision nodes), SQLite + NetworkX, wired only to council tooling
  (`council_questioner.py`, `decision_tracker.py`, etc.), zero references from the orchestrator.
  Has a superficially-similar `contradicts` edge type but shouldn't be repurposed — would mix
  two unrelated concerns in one table.
- **No cascade/recheck mechanism exists anywhere** —
  `grep -rniE "dependent|prerequisite|cascade|invalidate|recheck|stale" agent/*.py` returns zero
  claim/belief-related hits. Must be built from scratch; only the edge *vocabulary* can be
  reused (from `claim_graph.py`'s dataclass).

---

## 8. Existing components to reuse (not reinvent)

| Need (per brief) | Already exists as | Where |
|---|---|---|
| Dependency-graph edge types | `Claim.supports/contradicts/depends_on` | `claim_graph.py:44-46` |
| Graph JSON export shape | `ClaimGraph.get_graph()` — nodes/edges | `claim_graph.py:302-327` |
| History preservation (append-only) | `Belief.history` | `belief_manager.py:43,168-174` |
| Paraphrase/equivalence detection | embedding+cosine+LLM-judge pipeline | `belief_manager.py:184-248` |
| NLI verdict engine (for graph edges too) | `classify_claim_evidence_batch()` | `claim_relation.py:833-959` |
| Provisional-status handling pattern | existing 6-value status vocabulary + gates | `claims/status.py` |
| Storage tier "HOT" | `registry/beliefs.json` (needs restructuring, not replacing) | `belief_manager.py:74-80` |
| Storage tier "WARM" (append-only chronological) | `orch_traces/*.jsonl`, `query_archive/*.jsonl` | `orch_tracer.py`, `orch_query_archive.py` |
| Backup/archival naming convention | `registry/backups/` (ad hoc today, extend don't replace) | filesystem, confirmed present |

---

## 9. Components that should NOT be duplicated

- **A second NLI/relation classifier** — `claim_relation.py::classify_claim_evidence_batch()`
  is the single source of truth; `claim_graph.py`'s regex-based `_is_support`/`_is_contradiction`
  should be *replaced by a call into* the real classifier, not kept as a second implementation.
- **A second claim-graph module** — reactivate `claim_graph.py`, don't build a parallel
  `dependency_graph.py`; it already has the right shape.
- **A second belief-equivalence mechanism** — reuse `belief_manager.py:184-248`'s
  embedding+LLM-judge pattern for any new claim-level content-hash/paraphrase work; don't
  reimplement embedding+threshold logic a third time.
- **A second trust-computation path** — §6.1 shows there are already *three*; the fix is
  consolidation (make one gate authoritative and wire its output to the response), not adding a
  fourth.
- **A new storage subsystem** — §8's HOT/WARM mapping shows the tiers already exist in
  embryonic form; the gap is retention/compaction logic on top of what's there, not a new DB
  (also explicitly forbidden by brief §12).

---

## 10. Proposed minimal architecture (proposal only — nothing below is implemented)

1. **Content identity**: add `content_hash` (sha256 of normalized `claim_text`) alongside the
   existing `claim_id`, computed at the single live creation site (`orch_synthesizer.py:1016`).
   Enables cross-request exact-duplicate detection without touching belief-level embedding path.
2. **Relation persistence**: extend `ClaimRecord` (or add a sibling `RelationRecord`) so
   `evidence_relations`' verdict (not just the bare ID list) survives `add_claim_raw()`. Fixes
   §3.1 without redesigning the runtime object.
3. **Source clustering**: add `source_cluster_id` to `EvidenceRecord`, computed via a
   domain-list or content-fingerprint match; `claims/status.py` counts distinct cluster IDs, not
   raw relation count, when tallying `support_count`. Fixes §5.
4. **Trust consolidation**: make `trust_gate.py`'s gated `label` the one value that flows to
   `synthesis_result.trust_level`, replacing (not adding to) the currently-separate direct
   mutation in `evaluate_claim_status_gate()`. Fixes §6.1 — this is a *behavior* change and
   needs its own deliberate, separately-scoped follow-up (not part of this audit).
5. **Negative-evidence disambiguation**: add `evidence_search_attempted: bool` and
   `evidence_search_error: Optional[str]` alongside `verification_status`, without altering the
   existing 6-value status vocabulary (respects brief §12's "existing epistemic statuses" DO NOT
   TOUCH). Fixes the NOT-FOUND/INCONCLUSIVE/NEVER-TRIED/ERRORED conflation (§3 of trust notes).
6. **Dependency graph reactivation**: wire `claim_graph.py`'s edge detection to the real NLI
   classifier (replacing `_is_support`/`_is_contradiction`'s regex heuristics), and call
   `.extract_claims()`/populate the graph from the live `claims_data` pipeline. Then wire
   `belief_manager.add_belief()`/`_update_existing()` to consult `depends_on` edges and mark
   dependents `RECHECK_REQUIRED` on a contradicting update.
7. **Storage**: split `beliefs.json` into active (hot, small, frequently rewritten) vs
   superseded/rejected (cold, append-only) files rather than one growing file rewritten whole on
   every mutation (§11 below, this is P0).

---

## 11. Migration sequence

**P0 (already-live problems, fix regardless of the rest of this roadmap):**
- `belief_manager.py::_save()` full-file `json.dump()` rewrite on every single mutation against
  a 1.3MB/43,735-line `registry/beliefs.json` (confirmed live size). Split hot/cold as in §10.7.
- The trust-label discard (§6.1) is arguably P0-adjacent — it means the system's most rigorous
  trust computation is entirely invisible to users today, which undercuts the "verification
  creates trust" axiom's visibility even without any new epistemic feature.

**P1 (provenance + identity, unlocks everything downstream):**
- `content_hash` on claims (§10.1).
- Relation persistence into the trace (§10.2) — without this, no downstream re-evaluation
  mechanism can explain *why* something needs rechecking.
- Negative-evidence disambiguation fields (§10.5).

**P2 (source independence + dependency graph):**
- `source_cluster_id` (§10.3).
- `claim_graph.py` reactivation with real NLI wiring (§10.6).

**P3 (trust consolidation + storage tiering):**
- Trust-path consolidation (§10.4) — deliberately last because it changes user-visible trust
  labels and needs its own regression baseline, same discipline as the orchestrator
  modularization.
- Formal HOT/WARM/SNAPSHOT/ARCHIVE tiering built on top of the already-existing `beliefs.json`
  split and `orch_traces/*.jsonl` pattern (§8's mapping), plus reconciling the `orch_traces/`
  vs. `registry/traces/` split location noted in dependency-storage notes.

---

## 12. Risks

- **Trust consolidation (P3) is the highest-blast-radius change** — three independent trust
  computations exist today; unifying them will change what trust label real users see for real
  queries. Needs the same diff+regression+live-test discipline as the modularization, with its
  own dedicated regression baseline first.
- **`claim_graph.py` reactivation touches a currently-dead code path** — "dead" here means
  untested-in-anger; wiring it live could surface performance or correctness issues that don't
  show up in its own `__main__` test block. Should ship behind a feature check, not unconditionally.
- **Source-clustering (§5 fix) needs a real decision, not just implementation** — domain-list
  clustering is cheap but misses novel syndication; content-similarity clustering reuses
  existing embedding infra but costs an extra embed call per evidence pair. This is a genuine
  design fork the brief did not resolve and this audit should not resolve unilaterally either.
- **Storage split for `beliefs.json` must preserve exact current read semantics** —
  `get_all_active()`/`get_by_topic()` etc. iterate `self.beliefs` as one list; splitting the
  backing file must not change what these return, only how it's persisted.

---

## 13. KEEP / EXTEND / REFACTOR / DEPRECATE / DO NOT TOUCH

**KEEP as-is:**
- `claim_relation.py::classify_claim_evidence_batch()` — the real NLI engine, single source of
  truth, correctly separated from candidate generation.
- `belief_manager.py`'s append-only history + status lineage.
- `claims/status.py`'s NOT-FOUND-never-becomes-FALSE / absence-never-becomes-PROVEN branch logic
  (§3.1/3.2 of trust notes) — already correct, do not touch while fixing the overload of
  `"unverified"`.
- The existing 6-value `verification_status` vocabulary (per brief §12's explicit instruction).

**EXTEND:**
- `ClaimRecord`/`Trace.add_claim_raw()` — add relation-verdict persistence (§10.2).
- `EvidenceRecord` — add `source_cluster_id` (§10.3).
- Claim dict — add `content_hash`, `evidence_search_attempted`, `evidence_search_error`
  (§10.1, §10.5).
- `claim_graph.py` — replace its heuristic edge detection with the real NLI classifier; wire it
  live (§10.6).

**REFACTOR:**
- Trust computation — consolidate three independent paths
  (`orch_synthesizer.py`'s `trust_raw`, `trust_gate.py`'s `label`,
  `claims/status.py`'s direct mutation) into one authoritative flow (§10.4, §6.1). This is a
  behavior change and explicitly out of scope for this audit itself — flagged for a future,
  separately-scoped pass.
- `belief_manager.py::_save()` — hot/cold file split (§10.7, §11 P0).

**DEPRECATE (candidates, not decided — flag for user/maintainer decision):**
- `_calculate_delta_factors()` in `trust_gate.py` — dead P2P-consensus code with zero live call
  sites; either wire it to something real or remove it.
- `agent/trust_model.py` — fully dead, zero call sites, name-collides with epistemic trust.
- `agent/orch_reputation.py` — fully dead on the claim path (its Redis Sybil-vector risk is
  moot while unwired, but worth resolving one way or the other rather than leaving dormant).

**DO NOT TOUCH (per brief §12, reconfirmed applicable after this audit):**
- Trust formula internals beyond the consolidation-of-paths question above (i.e. don't
  reweight `_calculate_delta_factors`/`trust_raw`'s coefficients).
- Eligibility/quality thresholds (`DIRECTNESS_SUPPORT_THRESHOLD=0.60`, `0.70`/`0.35`/`0.45`
  embedding thresholds, etc.).
- NLI semantics (`classify_claim_evidence_batch`'s relation categories).
- CORE claim selection / `_classify_claim_role`.
- The existing epistemic status vocabulary (extend via companion fields, not by renaming/adding
  new top-level statuses).
- `agent/knowledge_graph.py` — different domain (council/project meta-knowledge), leave wired to
  its current consumers only.
- Registry deletion, new DB, blockchain, tokens, P2P node activation, governance, bulletin
  boards — none of the above proposals require any of these.

---

## What YANDI already has vs. what would be mistakenly reinvented

If this roadmap were approached without this audit, the most likely mistakes would be:
1. **Building a new dependency-graph module** — `claim_graph.py` already has the right
   dataclass shape and JSON export; the actual gap is edge-detection quality and zero
   integration, not absence of a graph structure.
2. **Building a new belief-equivalence/dedup system for claims** — the embedding+LLM-judge
   pattern already exists at the belief layer (`belief_manager.py:184-248`) and should be reused
   for claim-level content-hash work, not reimplemented.
3. **Building new storage tiers from scratch** — HOT (beliefs.json) and WARM (`.jsonl` append
   logs) already exist in substance; the actual gap is retention/compaction logic and a
   HOT-internal hot/cold split, not new infrastructure.
4. **Adding a fourth trust computation** to "fix" trust ≠ truth — there are already three; the
   fix is consolidation, and doing anything else compounds the exact problem being solved.
5. **Treating `trust_model.py` or `orch_reputation.py` as reusable building blocks for epistemic
   trust** — both are dead code for unrelated concerns (interpersonal trust, P2P node
   reputation) and would need to be either activated for their *original* purpose or removed,
   not repurposed as epistemic-claim trust infrastructure.
