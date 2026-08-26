# Fork 2 notes — Claim Identity (Topic A) + Source Independence (Topic B)

Working notes only. CODE > assumptions — every claim below has a file:line citation from a live read, not grep alone.

## TOPIC A — Claim Identity

### A1. Claim ID generation — random UUID, not content-derived

- `agent/orch_synthesizer.py:1016` — original creation site: `claim_id = f"cl_{uuid.uuid4().hex[:8]}"`, inside the per-line claim-extraction loop (~913-1050). Comment at the same site (paraphrased): no evidence links exist yet at this point by design — Mapper determines them later.
- `agent/orchestrator/claims/lifecycle.py:143-144` — fallback re-check: `if not claim.get("claim_id"): claim["claim_id"] = f"cl_{uuid.uuid4().hex[:8]}"`. Same random pattern, no dedup performed here.
- `agent/claim_graph.py:87` — a THIRD, separate site with the same `f"cl_{uuid.uuid4().hex[:8]}"` pattern (see A3 — this is a different, disconnected Claim identity system).
- **Conclusion**: no claim_id in the codebase is a function of claim content. Two runs producing textually identical claims get different, uncorrelated IDs. `claim_confidence` at creation is always a fixed `0.3` (`orch_synthesizer.py` claims.append site), i.e. content-independent too.

### A2. Claim-level dedup: does NOT exist on the live path; a parallel dead system has naive dedup; the real dedup lives one layer up, at Belief

- **Live path (`orch_synthesizer.py` → `claims/lifecycle.py` → `claims/mapping.py` → `claims/status.py`) has NO claim-to-claim similarity/dedup step at all.** Grepped `agent/claim_relation.py` (relation/classification between a *main claim* and a *source claim*, or embeddings for evidence relevance — not claim-to-claim identity) — no dedup function found; it's about evidence-claim relation classification (`classify_relation`, `infer_claim_relation`, `classify_claim_evidence_batch`), not identity merging.
- `agent/claim_graph.py` **is a separate, self-contained Claim dataclass + ClaimGraph class with its own dedup** (`_deduplicate_and_merge`, lines 215-236): key = `claim.text[:50]` (first-50-chars string prefix, exact match only, no embedding, no fuzzy match) — on collision it merges `evidence_for` lists and averages confidence. This is dramatically weaker than belief-level dedup (A2 below) — pure string-prefix equality, so `"Сознание — это X"` vs `"Сознание -- это X"` (different dash char) or any two claims differing after char 50 would NOT merge.
- **`ClaimGraph` is dead on the request path.** `orchestrator_v2.py:151` constructs the singleton (`_claim_graph = get_claim_graph()`) but `.extract_claims()` — the only method that populates `self.claims` and runs dedup — has **zero callers** anywhere except `claim_graph.py`'s own `__main__` test block (`claim_graph.py:404`). Verified via `grep -rn "\.extract_claims("` across `agent/` and top-level `*.py` — only hit is the file's own test. So this whole identity/dedup/graph subsystem is instantiated but inert.
- **Real, live dedup happens at the Belief layer, not Claim**: `agent/belief_manager.py:143-248`. `add_belief()` calls `_find_similar(topic, statement)` (line 154) before creating a new `Belief`. `_find_similar` (184-248): (1) exact-match pass on whitespace-normalized lowercased statement text within the same `topic` string (219-222, no HTTP call); (2) if no exact match, ONE batch embedding call (`embeddinggemma:latest` via local Ollama, 224) over `[new_statement] + candidate_statements`; (3) cosine similarity `>= 0.70` prefilter (242, calibrated per comment: ~0.17 unrelated, ~0.54-0.64 same-topic-different-claim, ~0.81 even opposite statements can score close, ~0.92 near-paraphrase — so threshold is a coarse filter, NOT an equivalence decision); (4) LLM judge (`_llm_judge_relation`, 279+) makes the actual equivalent/not-equivalent call for anything passing the embedding prefilter. Scoping: candidates are filtered to `belief.topic == topic` (exact topic string match, 211) — so this dedup is topic-scoped, not global.
- **Consequence for Topic A**: paraphrase/equivalence detection for the same fact stated differently *does* exist, but only after a claim survives into a Belief (via whatever aggregation path calls `add_belief()` — not traced further in this fork, out of scope) — never at Claim creation or Claim-to-Claim level on the live path.

### A3. `claim_answer_linker.py` — not an identity mechanism

- `agent/claim_answer_linker.py` is live-wired: `orchestrator/claims/lifecycle.py:188,310-312` calls `claim_answer_linker.link_answer_to_claims(...)`. But its job is answer-text ↔ claim-text overlap linking (word-overlap heuristic, `_is_claim_supporting`, ≥40% of first-5-words-of-a-phrase must appear in claim text, lines 75-86), producing `supporting_claim_ids` for trace provenance. It does not compare claims to each other and is irrelevant to identity/dedup.

### Proposed minimal identity model (NOT implemented — proposal only, per "пока НЕ менять")
- Add a `content_hash` (e.g. sha256 of normalized claim_text) alongside the existing random `claim_id`, computed at the single live creation site (`orch_synthesizer.py:1016`). Would enable O(1) exact-duplicate detection across requests/sessions without touching the belief-level embedding path, and would give downstream consumers (dependency graph, re-evaluation) a stable join key that survives a claim being re-extracted verbatim in a later request. Paraphrase-level identity would still require the existing embedding+LLM-judge pattern from `belief_manager.py`, reused rather than reinvented.

---

## TOPIC B — Source Independence

### B1. `source_quality.py` — no independence/syndication concept at all

- `agent/source_quality.py`: scanned all top-level defs and domain-related logic (`_hostname`, `_matches_domain`, `_classify_source`, `_refine_source_class`, `evaluate_source_quality`, `evaluate_evidence_directness`). Classifies a source into a *class* (scientific/reference/forum/social/etc, weighted quality/directness score) purely per-URL. No field, function, or concept anywhere for "these N sources are the same underlying publisher/wire copy" — no `independent`, `syndicat`, or `cluster` term appears anywhere in the file (checked via grep).

### B2. `evidence_pool.py::_dedupe()` — identity is URL-exact or content-prefix, not publisher-identity

- `agent/evidence_pool.py:103-193`. Two dedup regimes, both documented in the docstring (109-136):
  - **Global/shared evidence** (no claim ownership): key = `("url", url)` lowercased-stripped exact URL (167-168), or `("content", source_type, title[:100], excerpt[:200])` if no URL (170-175).
  - **Claim-owned evidence** (`retrieval_origin == "claim_specific"` with a `retrieval_claim_id`): key additionally includes the owning claim_id (156-165) — explicitly documented (126-132) that claim-owned evidence with the *same URL* as global/other-claim evidence is deliberately NOT deduped, to preserve per-claim provenance.
  - **Neither regime has any concept of same-publisher-different-URL.** Two different URLs — e.g. a wire-service story republished at `reuters.com/...` and `yahoo.com/news/reuters-...` — hash to different keys and both survive as independent evidence entries.

### B3. `claims/status.py` support/contradiction tally — no source-clustering awareness

- `agent/orchestrator/claims/status.py:146-156`: `supports_count`/`contradicts_count` are computed as `sum(1 for rel in direct_relations if rel.get("relation") == "supports"/"contradicts")` — a flat count over evidence *relations* gated only by `_counts_toward_status()` (60-84: authority-eligible OR directness-threshold-and-not-hard-blocked-and-not-local-registry). No dedup or clustering by source domain/publisher anywhere in this function or its gate. **N syndicated copies of one wire story, each individually passing the eligibility/directness gate, contribute N toward `support_count`.**
- This directly feeds `verification_status` (178-190: `supports_count>0 and contradicts_count>0` → disputed; `supports_count>0` → supported; etc.) — so syndication inflation can flip a claim from `unverified`/`disputed` to `supported` purely by URL-copy count, without independent corroboration.

### B4. `orch_web_scraper.py` — dedup is at URL-fetch-cache level only

- `agent/orch_web_scraper.py:84-113`: `canonicalize(url)` (conservative scheme/host lowercasing) feeds a fetch cache keyed `f"{transport}:{canonicalize(url)}"` (113) — this prevents re-fetching the *same* URL twice within a run/cache instance (line 271 `"duplicate": "дубликат уже полученного URL"`, line 703 comment). It is a network-efficiency cache, not a source-identity/syndication concept — no cross-domain publisher matching, no wire-service/syndication detection anywhere in the file (grepped for `syndicat`, `wire`, `AP`, `Reuters`, `canonical`, `same_origin`: only the URL-canonicalization hit).

### Conclusion for B — confirmed gap
Nothing in the current pipeline (source_quality.py, evidence_pool.py, claims/status.py, orch_web_scraper.py) prevents syndicated/republished copies of one underlying story from each counting as independent support toward a claim's `support_count` and thus its `verification_status`. This is a real, evidenced gap, not a hypothetical.

### Proposed minimal model (NOT implemented — proposal only, per "НЕ реализовывать без отдельного решения")
- A `source_cluster_id` field (e.g. derived from registered wire-service/syndication domain lists, or a lightweight content-fingerprint match between evidence excerpts from different domains within the same claim's evidence set) attached to each evidence item. `claims/status.py`'s counting would then count **distinct `source_cluster_id`s**, not raw relation count, when tallying `support_count`. Left as a design question (not decided here) whether clustering is domain-list-based (cheap, misses novel syndication) or content-similarity-based (reuses the embedding infra already in `belief_manager.py`, costs an extra embed call per evidence pair).

---

## Report to parent
File written: `YANDI_EPISTEMIC_AUDIT_identity_independence_notes.md`, ~85 lines.
