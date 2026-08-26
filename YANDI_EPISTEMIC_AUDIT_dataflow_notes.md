# Dataflow / Provenance fork — working notes (not the final report)

Scope covered by this fork: core dataflow trace (query→evidence→claim→relation→trace→disk),
provenance chain survivability. Claim-identity/source-independence, trust-engine/provisional-
negative-evidence, and dependency-graph/re-evaluation/storage are covered by three parallel
background forks — cross-check their notes before synthesizing the final report.

## 0. Files read in full this pass
- `agent/orch_schemas.py` (409 lines) — all dataclasses.
- `agent/orch_tracer.py` (479 lines) — `Trace`, `DecisionTracer`, disk format.
- `agent/claim_evidence_mapper.py` (442 lines) — `map_claims_to_evidence`.
- `agent/claim_relation.py:833-959+` — `classify_claim_evidence_batch`.
- `agent/orchestrator/claims/mapping.py:1-140` — `run_claim_evidence_mapping_pass1`,
  `run_claim_evidence_batch` (top).
- `agent/orchestrator/pipeline.py:180-230` — cache-hit trace rehydration path.
Not yet read this pass (leave to parallel forks / next pass if time permits):
`belief_manager.py`, `claim_graph.py`, `claim_evidence_retriever.py`, `evidence_pool.py`,
`claim_answer_linker.py`, `context_registry.py`, `knowledge_graph.py`, `dataset_builder.py`,
`orch_query_archive.py`.

## 1. THE SINGLE BIGGEST FINDING: NLI relation verdict is computed, then thrown away before persistence

**Claim: the persisted trace cannot answer "why do I believe this claim" beyond a bare list of
evidence IDs — the actual supports/contradicts/unrelated verdict per (claim, evidence) pair
never reaches disk.**

Chain, with exact call sites:

1. `agent/claim_relation.py:833-959` `classify_claim_evidence_batch(claim_jobs, batch_size)`
   runs batch NLI per (claim, source) pair and returns
   `{claim_id: {"supports":[...], "contradicts":[...], "unrelated":[...], "uncertain":[...]}}`,
   each entry carrying `relation`, `relation_method`, `source_claim` (the exact extracted
   sentence used as NLI premise) — this is the actual epistemic justification data.

2. `agent/orchestrator/claims/mapping.py:126+` `run_claim_evidence_batch(claims, evidence,
   batch_label, log, verbose)` consumes that grouped dict and — per its own module docstring at
   `mapping.py:20`, *"Mutates each claim in `claims` in place: writes
   claim["evidence_relations"]."* — attaches the full per-evidence relation list (evidence_id,
   relation, method, source_claim, source_class, quality_score, evidence_eligible,
   evidence_role, retrieval_origin, directness — confirmed field set from Phase-1 modularization
   work on this exact function) onto the **in-memory claim dict**, at `claim["evidence_relations"]`.
   This happens twice per query: PASS1 (after initial evidence mapping) and PASS2 (after
   claim-specific re-retrieval) — both called from `orchestrator_v2.py:453` and
   `agent/orchestrator/claims/retrieval.py:183`.

3. This richer dict is what downstream status/validation/synthesis code actually reasons over
   during `process()` — but it is never handed to the tracer as-is. Two ingestion paths into
   `Trace`, both lossy the same way:
   - `agent/orchestrator/claims/status.py:521` → `trace.add_claim_raw(claim)` (passes the same
     mutated dict that has `evidence_relations` on it).
   - `agent/orchestrator/pipeline.py:216` → `trace.add_claim_raw(claim_data)` (cache-hit
     rehydration path — same shape).
   - `orch_tracer.py:292-313` `Trace.add_claim_raw()` builds a `ClaimRecord` reading **only**
     `claim_id`, `claim_text`, `derived_from_evidence_ids`, `claim_type`, `claim_confidence`,
     `verification_status` off the incoming dict. `evidence_relations` is not one of the keys it
     reads — it is silently dropped. Confirmed by cross-reading `orch_schemas.py`'s `ClaimRecord`
     dataclass (`claim_id, claim_text, derived_from_evidence_ids, claim_subject, claim_type,
     claim_confidence, supports_query_aspect, conflicts_with_claim_ids, verification_status`):
     there is **no field anywhere in `ClaimRecord` capable of holding a per-evidence relation
     type**. The only surviving link is the bare ID list in `derived_from_evidence_ids`, with no
     record of whether each linked evidence *supported* or *contradicted* the claim.

4. That surviving `derived_from_evidence_ids` link is *itself* truncated at serialization time:
   `orch_tracer.py:394` in `Trace.to_dict()` — `"derived_from_evidence_ids":
   c.derived_from_evidence_ids[:3]` — keeps only the first 3 evidence IDs per claim. Evidence
   list is separately truncated at `orch_tracer.py:387` — `for e in self.evidence[:10]` — only
   the first 10 evidence records of the whole trace survive to disk at all. Claims themselves
   truncated at `orch_tracer.py:399` (`self.claims[:15]`).

**Net effect**: `DecisionTracer.save_trace()` (`orch_tracer.py:455-466`) appends one JSON line per
query to `registry/dataset/orch_traces/YYYYMMDD.jsonl` (confirmed on disk —
`/home/iam/yandi/registry/dataset/orch_traces/20260826.jsonl`, 465KB today). That persisted
JSON, for any query with >15 claims, >10 evidence records, or >3 evidence-links per claim
(routine for a multi-claim query — the earlier live sanity run this session logged
`claims=8, linked_claims=8, candidate_links=14`, i.e. already exceeding several claims' 3-link
cap), **cannot reconstruct why a given claim was believed**: it has evidence IDs but not the
relation type (supports/contradicts/unrelated/uncertain), not the NLI method, and not the
`source_claim` sentence that was actually compared. Provenance is real and detailed at
*runtime* (in the `claim["evidence_relations"]` dict, held only in the `process()` call's local
`claims_data` list) and is **structurally incapable of surviving to disk** in its useful form —
this is not a bug where someone forgot a field, it's that `ClaimRecord`'s schema was designed
one abstraction level higher than the actual runtime reasoning object, and the tracer's dict→
dataclass conversion at `add_claim_raw()` is the choke point where the richer structure is
discarded every single time, with no `except`/logging noting the loss.

## 2. Two independent, non-unified mapping pathways (naming caution for parent's report)

- `agent/claim_evidence_mapper.py::map_claims_to_evidence()` — embedding-similarity **candidate
  generator only** (thresholds `PRIMARY_CANDIDATE_THRESHOLD=0.35`,
  `SECONDARY_CANDIDATE_THRESHOLD=0.45` at lines 281-282). Explicitly documented in its own
  module docstring and inline comments (`mapper.py:83-84,278-280`) as NOT support/truth —
  "candidate link != support. Финальное отношение определяет только NLI." Sets
  `verification_status="candidate"` (`claim_evidence_mapper.py:401`) — never anything else. This
  is called from `orchestrator/claims/mapping.py::run_claim_evidence_mapping_pass1()` and is,
  per that function's own comment (`mapping.py:39-40`), *"единственный компонент, который имеет
  право назначать derived_from_evidence_ids"* — i.e. it is the sole writer of that field, and it
  writes it based on embedding similarity, before NLI ever runs.
- `agent/claim_relation.py::classify_claim_evidence_batch()` — the actual NLI verdict engine
  (supports/contradicts/unrelated/uncertain), consumed only via
  `orchestrator/claims/mapping.py::run_claim_evidence_batch()`.
- These are sequenced PASS1 (mapper → candidates → `derived_from_evidence_ids`) then NLI
  (`run_claim_evidence_batch` → `evidence_relations`) — not duplicate implementations of the
  same thing, so **do not flag as a REFACTOR/DEPRECATE candidate** in the final report; they are
  legitimately different pipeline stages (retrieval-candidate vs. verdict). The gap is purely
  that stage 2's output doesn't survive into the object that gets traced.

## 3. `Trace.add_source()` vs `Trace.add_evidence()` — divergent field completeness

`orch_tracer.py:256-269` `add_source(url, domain, domain_score, freshness, authority, used,
rejected_reason)` builds an `EvidenceRecord` leaving `evidence_role`, `quality_score`,
`source_class`, `traceability`, `primaryness` at dataclass defaults — only
`relevance_to_query` gets a real value (from `domain_score`). Called from
`orchestrator/synthesis.py:170` and `orchestrator/pipeline.py:916,930`. Meanwhile
`add_evidence(evidence_record)` (`orch_tracer.py:271-272`) takes a fully-populated
`EvidenceRecord` built by the caller — used at `pipeline.py:220` for the cache-hit rehydration
path. Net: evidence quality/authority/traceability metadata is present in the trace only for
whichever code path happened to build the full record; the `add_source()` shorthand path loses
it structurally, not from a bug — the function signature just doesn't accept those fields.
Parent should check with the trust-engine fork whether these silently-defaulted fields
(`quality_score=0.0`, `evidence_eligible=False` default) ever get read back out of the trace for
anything downstream, or if the trace is genuinely write-only/diagnostic.

## 4. `EvidenceRecord` has no reverse link to claims (schema-level, confirmed)

`orch_schemas.py`'s `EvidenceRecord` dataclass fields: `evidence_id, source_type, source_uri,
source_title, retrieval_query, retrieval_rank, content_excerpt, subject_entities,
fact_candidates, relevance_to_query, supports_query_aspect, quality_score, source_class,
evidence_eligible, evidence_role, authority, traceability, primaryness,
is_meta_pipeline_output, is_subject_matter_evidence, rejection_reason` — none of these is a
claim ID or claim-ID list. The claim→evidence link is one-directional
(`ClaimRecord.derived_from_evidence_ids`); reconstructing "which claims used this evidence" from
a persisted trace requires scanning every `ClaimRecord` in that trace and string-matching IDs —
fine within one trace's 15-claim/10-evidence cap, but there is no index anywhere
(`registry/dataset/orch_traces/*.jsonl` is append-only, ungrouped by evidence_id or source_uri)
letting you ask "every claim across all history that used source X" without scanning every
line of every daily file.

## 5. Disk reality check

`registry/dataset/orch_traces/` — daily `.jsonl` append logs (`20260822.jsonl` … `20260826.jsonl`,
78KB–675KB/day), plus older one-off `chain_YYYYMMDD_HHMMSS_<hex>.json` files (pre-dates the
`.jsonl` convention, only 5 present, all from 20260715 — looks like an earlier/abandoned trace
format, worth flagging to parent as a possible dead artifact, not confirmed). `DecisionTracer`
itself (`orch_tracer.py:451-466`) is **instantiated fresh per call** — `get_tracer()` at
line 472-473 returns `DecisionTracer()` with no singleton/cache, so `self._traces` (in-memory
list) never accumulates across calls; only the appended `.jsonl` line is durable. No DB, no
index, no compaction — this is already exactly the kind of unbounded flat-file growth the
brief's §11 (storage tiering) is asking about; defer sizing recommendation to whichever fork
covers storage.

## 6. Open threads for parent / other forks to reconcile
- Does `belief_manager.py::add_belief()` receive `evidence_relations` or only the trace's
  post-truncation `ClaimRecord`? If it reads the richer runtime `claims_data` (not the trace),
  provenance may survive into beliefs even though it's lost from the trace file — check before
  writing "provenance lost" as an absolute claim in the final report. This fork did not reach
  `belief_manager.py`.
- `claim_answer_linker.py`, `context_registry.py`, `knowledge_graph.py`, `orch_query_archive.py`
  not yet read by this fork — check the identity/source-independence and dependency-graph forks'
  notes for whether they touched these.
