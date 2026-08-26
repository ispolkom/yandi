# Dependency Graph + Re-evaluation + Storage/Growth — Investigation Notes

## TOPIC A: Knowledge dependency graph / re-evaluation

### A1. `agent/claim_graph.py` — fully-built but structurally dormant
- `Claim` dataclass (`agent/claim_graph.py:31-50`) already has the exact edge
  vocabulary the brief asks for: `supports`, `contradicts`, `depends_on`
  (claim_id lists), plus `evidence_for`/`evidence_against`.
- `ClaimGraph._build_graph()` (`claim_graph.py:239-256`) populates these
  edges via **regex/word-overlap heuristics**, not NLI:
  `_is_contradiction()` (258-274) = negation-marker mismatch;
  `_is_support()` (276-282) = `len(shared_words) > 3`. Both are crude
  compared to the real NLI classifier used elsewhere in the pipeline
  (`orch_web_scraper.py` claim/evidence NLI). This is a real quality gap if
  this module is ever reactivated as-is.
- `get_graph()` (302-327) already returns a nodes/edges JSON structure
  (`{"from","to","type"}`) — i.e. the *shape* of a dependency graph API
  already exists and doesn't need to be invented.
- **Confirmed dormant**: `agent/orchestrator_v2.py:79` imports
  `get_claim_graph`; `orchestrator_v2.py:151` does
  `_claim_graph = get_claim_graph()` inside `_init_v3()`. Grep for
  `_claim_graph\b` across `orchestrator_v2.py` shows only 3 hits total: the
  `global` declaration (113, 123) and this one assignment (151). **No code
  path ever calls `.extract_claims()`, `.get_graph()`, or reads
  `_claim_graph` again.** No other file in `agent/` references
  `claim_graph`/`get_claim_graph` at all. This is a fully-built, zero-call
  dead singleton — confirms the modularization audit's earlier finding
  still holds.

### A2. `agent/belief_manager.py` — flat records, no belief-to-belief edges
- `Belief` dataclass (`belief_manager.py:32-51`) has `claim_ids: List[str]`
  (which claims produced this belief) but **no field referencing other
  beliefs it depends on or was derived from** — beliefs are topic-keyed
  flat records (`add_belief`/`_find_similar` match by `topic` +
  semantic-equivalence of `statement`, `belief_manager.py:143-248`), not
  graph nodes.
- No cascade: `_update_existing()` (351-439) and `challenge_belief()`
  (441-494) only ever touch the ONE belief passed in. Neither method
  searches `self.beliefs` for other beliefs that cite this belief's id or
  topic as a dependency — because no such link exists to search.
- **History is append-only, never deleted.** `belief.history` is a list
  every update appends a `{timestamp, old_confidence, new_confidence,
  reason, change}` record to (168-174, 429-437, 480-487, 511-515) — full
  audit trail preserved. Only `belief.status` transitions
  (`active→revised→superseded`, `supersede_belief()` 496-518) change
  "current" interpretation; raw evidence lists (`evidence_for`/`_against`)
  only grow via `.append()`, never truncate. So the "preserve history
  separately from current status" requirement from the brief is **already
  satisfied by the existing status+history design** — no new mechanism
  needed for that part, only for cross-belief dependency.

### A3. `agent/knowledge_graph.py` — a different graph entirely, not usable as-is
- Docstring (`knowledge_graph.py:1-38`) and node/edge types (`session,
  topic, concept, decision, task, dataset, file` /
  `mentions, derived_from, resolves, contradicts, belongs_to, co_occurs,
  next`) show this models **council/project meta-knowledge** (which
  session discussed which concept, which decision resolved which task) —
  not claim-to-claim epistemic dependency. Storage: SQLite at
  `registry/knowledge/graph.db` + in-memory NetworkX cache (line 23, 51-56).
  Note it also imports `redis` unconditionally (line 52) for pub/sub.
- Call sites (`grep -rl`): `council_questioner.py`, `decision_tracker.py`,
  `finetune.py`, `model_runner.py`, `daemon.py`, `reflector.py` — all
  council/assistant tooling. **Zero references from
  `orchestrator_v2.py` or `agent/orchestrator/*`.** It has a `contradicts`
  edge type that's topically close, but it's wired to a completely
  separate subsystem (council sessions) and shouldn't be repurposed for
  claim dependency without becoming two unrelated concerns in one table.

### A4. No cascade/recheck mechanism anywhere
- `grep -rniE "dependent|prerequisite|cascade|invalidate|recheck|stale"
  agent/*.py` returns zero hits related to claims/beliefs. The only
  matches are unrelated: `orch_cache.py`'s HTTP-fetch-cache
  `invalidate()/invalidate_all()/invalidate_by_entity()` (lines 317, 325,
  335 — a request-scoped fetch cache, not epistemic state), and
  `reflector.py`'s `STALE_DECISION_DAYS` (line 42) which ages **council
  decisions** (a different registry: `registry/decisions`), not claims.
  **Conclusion: there is no re-evaluation/cascade mechanism in the
  codebase today, dormant or active.** This must be built from scratch;
  nothing exists to "reuse" here except the edge vocabulary already
  defined in `claim_graph.py`'s `Claim` dataclass.

### Proposal (not implemented)
Minimal edge types: `supports`, `contradicts`, `depends_on` — these
already exist verbatim as `Claim` fields in `claim_graph.py:44-46`; no new
vocabulary needed. Where the graph should live: **not** a new module.
Argue from what exists: `claim_graph.py`'s `Claim`/`ClaimGraph` classes
already have the right shape (dataclass + adjacency lists + `to_dict()`/
`get_graph()` JSON export) but the wrong edge-detection method (regex
heuristics instead of the pipeline's real NLI classifier) and zero
integration. Reactivating this module — replacing `_is_contradiction`/
`_is_support` with calls into the existing NLI relation classifier already
used for evidence mapping, and actually wiring `get_claim_graph()`'s
result into the belief-update path (`belief_manager.add_belief`/
`_update_existing`) so that a low-confidence/contradicted claim marks
claims that `depends_on` it as `RECHECK_REQUIRED` — is smaller-surface
than inventing a new graph module, and reuses an already-tested dataclass
shape. `knowledge_graph.py` should be left alone; it's a different domain
(council/project meta-knowledge) already in active use elsewhere.

---

## TOPIC B: Storage / growth

### B1. `registry/beliefs.json` is already the largest single-file growth risk
- **1.3MB, 43,735 lines, single JSON file** (`ls -la` /
  `wc -l registry/beliefs.json`, checked live). `belief_manager.py:74-80`
  `_save()` does a full `json.dump()` of the **entire beliefs list** on
  *every* `add_belief`/`_update_existing`/`challenge_belief`/
  `supersede_belief` call — i.e. every single-belief mutation rewrites the
  whole multi-MB file from scratch. No sharding, no pagination, no
  size/age-based rotation of old/superseded beliefs out of the hot file.
  **This is the clearest already-live unbounded-growth concern** — it's
  not hypothetical, the file is already >1MB with tens of thousands of
  belief history entries embedded in it (`Belief.history` list per record,
  `belief_manager.py:43`), and every write pays the cost of the full
  current size. Flag as **P0** for the migration plan: this is a
  correctness-adjacent perf/growth issue today, not a someday concern.

### B2. Everything else persists per-day or per-query, with no retention code found
- `agent/orch_tracer.py:45-46,460`: `TRACES_DIR =
  registry/dataset/orch_traces`, one file per day
  (`{YYYYMMDD}.jsonl`, append-only). On disk today: `registry/dataset/orch_traces`
  = 4.8MB. Separately `registry/traces/` (13 files, 1.5MB, one JSON per
  decision: `trace_<epoch>_<hash>.json`) is a **different, second trace
  location** — worth flagging as a naming/location split to reconcile, not
  just a growth question.
- `grep` across `dataset_builder.py`/`dataset_pipeline.py`/
  `dataset_versioning.py` for `TTL|max_size|rotat|archiv|prune|compact|
  retention` returns **zero matches** — no compaction/pruning/retention
  logic exists anywhere in the dataset persistence layer today.
- `agent/orch_query_archive.py` persists to `registry/query_archive/`
  (120KB total: `bootstrap_state.json`, `tag_tree.json`,
  `unanswered.jsonl` — small today).
- `agent/context_registry.py:82` and `agent/secret_archive.py:21` both
  persist **one JSON file per user_id** (`REGISTRY_DIR/{user_id}.json`,
  `ARCHIVE_DIR/{user_id}.json`) — bounded by user count, not query count;
  lower growth risk than beliefs.json or traces.

### B3. Per-query write footprint today
A single pipeline query that reaches claim/evidence processing writes to:
(1) one line appended to the day's `orch_traces/{date}.jsonl`, (2) a
possible new `registry/traces/trace_*.json` decision file, (3) a full
rewrite of `beliefs.json` per belief touched (can be several per query),
(4) possibly a query_archive entry. Of these, **only `beliefs.json`'s
full-file-rewrite-per-mutation pattern is actively expensive today**; the
`.jsonl` append patterns (traces, query_archive) are cheap and scale fine
without changes.

### Proposal (not implemented) — map onto existing tiers, don't invent new ones
- **HOT** = already exists: `registry/beliefs.json` (active/revised
  beliefs currently being reasoned over) — but needs to stop being "one
  file, full rewrite per mutation." Minimal fix direction: split
  active-status beliefs (hot, small, rewritten often) from
  superseded/rejected beliefs (append to a separate cold file instead of
  keeping them in the same JSON that gets rewritten on every mutation).
- **WARM** = already exists as the `.jsonl` append-only logs:
  `orch_traces/*.jsonl`, `query_archive/unanswered.jsonl` — these are
  already write-cheap and naturally chronological; "warm aggregation"
  would mean periodically summarizing old daily files rather than
  changing their write path.
- **SNAPSHOT/ARCHIVE** = no current analog; `registry/backups/` exists but
  is ad hoc (timestamped one-off dirs from specific past operations, e.g.
  `claim_evidence_batch_20260825_154543`), not a scheduled tiering
  mechanism. This tier would need to be newly built — but should reuse the
  `registry/backups/` naming convention already established rather than
  inventing a new top-level directory.
