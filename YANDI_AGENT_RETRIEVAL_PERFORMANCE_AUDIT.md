# YANDI — AGENT RETRIEVAL / PERFORMANCE AUDIT

Phase: **P0 — AUDIT ONLY**. No production code changed as part of this document.

Primary benchmark: live run, query "Вызывает ли кофе и горячие напитки рак? Что говорит IARC?"
(`--web --validate --no-cache`), total=397.23s, captured in full verbose trace
(`live_run.log`, 1426 lines) during the PRE-PUSH GATE phase of this session and reused here as
the primary evidence source for sections 1-16. Baseline corpus (section 17) additionally includes
two further live runs on distinct query types, run in parallel with this audit.

Companion numbers cited by the user's brief (`total=460.92s`, `workers=3 queue_wait_sum=322.21s
queue_wait_max=101.97s effective_parallelism=2.42`) come from the user's own separate live run on
a differently-worded coffee query and are treated as given facts to explain, not re-derived here.

---

## 1. Call Graph / Stage Timing

Source: `live_run.log`, `[PROFILE]` block (`agent/orchestrator/runtime/profiling.py:69-81`, driven
by the `cost{}` dict computed throughout `agent/orchestrator_v2.py`).

| Stage | Wall | % | Code ref |
|---|---|---|---|
| claim_specific_retrieval | 123.52s | 31.1% | `agent/claim_evidence_retriever.py::retrieve_for_claims` (called from `agent/orchestrator/claims/retrieval.py::apply_claim_resolution_and_second_retrieval`, `orchestrator_v2.py:477`) |
| final_claim_coverage | 52.21s | 13.1% | `agent/orchestrator/epistemic/final_coverage.py::evaluate_and_record_final_coverage` (`orchestrator_v2.py:562`) — of which NLI=42.73s per `[Final Coverage Timing]` log |
| web | 31.96s | 8.0% | initial web search, stage `[7]` |
| claim_pass2_mapper_nli | 31.40s | 7.9% | evidence mapper + NLI over the pass-2-enlarged evidence pool (9→29 items) |
| claim_setup_validator_mapper1_nli1 | 29.61s | 7.5% | claim validation + pass-1 mapper/NLI, `orchestrator_v2.py:470` |
| refutation | 24.13s | 6.1% | refutation-query scrape, parallel fan-out `[6]` |
| claim_claim_nli | 24.09s | 6.1% | claim↔claim disagreement NLI, `apply_claim_claim_disagreement` (`orchestrator_v2.py:512`) |
| synthesize | 23.68s | 6.0% | `build_frame_and_synthesize` stage `[8]` |
| registry/web-initial | 10.68s | 2.7% | `[6]` parallel fan-out |
| belief_update | 7.18s | 1.8% | `update_beliefs_link_answer_and_personality_cycle` |
| everything else (source_classification, plan, intent, blind_analysis, enrich, …) | ~9.1s | 2.3% | minor |
| **unaccounted** | **26.78s** | **6.7%** | not attributed to any named stage |

**PROVEN**: `claim_specific_retrieval` (123.52s) + `final_claim_coverage` (52.21s) +
`claim_pass2_mapper_nli` (31.40s) = **207.13s, 52.1% of total wall time**, all three driven by the
same root cause — claim-specific retrieval (pass 2) enlarging the evidence pool from 9→29 items
and re-running mapper/NLI over it twice (once in `claim_pass2_mapper_nli`, again inside
`final_claim_coverage`'s own NLI pass — `[Final Coverage Batch] pairs=110 generation_calls<=4`).
This third NLI pass looks like it may re-derive relations already computed by
`claim_pass2_mapper_nli` — **see section 3** for the routing-fork's independent confirmation.

**NOT YET PROVEN**: the 26.78s unaccounted gap — no code reference found attributing it; likely
thread-join overhead, GC, or unmeasured glue code between stages. Recommend a wrapping timer
around the whole `process()` call vs. the sum of named stages to bisect.

---

## 9. Concurrency / Queue Audit

**PROVEN** — `agent/claim_evidence_retriever.py:1848-1855`:
```python
max_workers = min(3, len(selected))
...
with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
```
The `3` is a **hardcoded literal**, justified only by a comment at
`claim_evidence_retriever.py:1740-1742`: *"Ограничиваем concurrency двумя [sic, code says 3]
workers: web I/O частично перекрывается; Ollama не получает лавину одновременных generation
calls."*

**This justification is stale relative to the current code.** `retrieve_claim_evidence()`
(`claim_evidence_retriever.py:652-900`, the function each worker runs) precomputes ALL query
generation via a single batched LLM call *before* the executor starts
(`formulate_claim_evidence_queries_batch`, lines 1763-1794, runs sequentially outside the pool),
and the docstring at line 746-751 states explicitly *"no NLI/LLM call happens inside this
function."* Each worker's actual work is `scrape()` (network I/O) + parsing (cheap CPU), with
`embedding_ms=0.0` (not done here). The stated "avoid flooding Ollama" rationale does not apply to
what workers currently do — there is no Ollama call inside the parallelized region at all.

Live evidence, 8 claims routed to retrieval (`[Claim Retrieval Worker]` lines 372-906):

| claim | queue_wait | elapsed | web_request | dominant cost |
|---|---|---|---|---|
| cl_26fc0420 | 0.00s | 22.06s | 17.59s | network |
| cl_d82ac29f | 0.00s | 32.85s | 30.01s | network |
| cl_2aa7a759 | 0.00s | 33.75s | 31.53s | network |
| cl_21694c3c | 22.06s | 36.55s | 31.85s | network |
| cl_8d574f7f | 32.85s | 36.54s | 31.37s | network |
| cl_29b9bbd8 | 33.75s | 38.02s | 33.83s | network |
| cl_e839ab9d | 58.61s | 37.79s | 32.43s | network |
| cl_8f195b39 | 69.39s | 38.27s | 34.24s | network |

`worker_sum`=275.83s, batch wall≈107.66s → effective_parallelism≈2.56 for this run, consistent
with the brief's cited 2.42 on the user's own separate run. Each worker is >85% network-wait
(`web_request`), i.e. **this is an I/O-bound workload, not a compute/model-bound one** — the
classic case where raising `max_workers` has a real chance of shrinking wall time roughly
proportionally, *if* downstream capacity (proxy pool, target-site rate limits) tolerates it.

**NOT YET PROVEN**: whether raising workers to 4-6 causes more 403/429/timeouts — no live
concurrency experiment was run in this pass. **Recommended P4 follow-up**: bounded live experiment
at workers=3/4/6 on the same query, measuring wall, fetch-failure rate, and evidence yield, before
touching the constant.

---

## 10. Early Stop Audit

**PROVEN — no epistemic-sufficiency-based early stop exists anywhere in the retrieval path.**
`claim_evidence_retriever.py` and `orch_web_scraper.py` contain exactly 3 `break` statements, all
arbitrary count caps, none evidence-aware:
- `orch_web_scraper.py:682` — `if len(urls) >= discovery_limit: break`
- `orch_web_scraper.py:723` — `if len(set(all_urls)) >= DISCOVERY_RESULTS: break`
- `orch_web_scraper.py:1306` — `if len(selected) >= max_results: break`

No code path checks "does this claim already have eligible, non-uncertain, sufficiently
independent evidence" before continuing to fetch/search further for that same claim within one
`retrieve_claim_evidence()` call.

Consequence, evidenced from pass-1 eligibility data (`live_run.log:293-315`): claims
`cl_8d574f7f`, `cl_e839ab9d`, `cl_8f195b39`, `cl_21694c3c`, `cl_29b9bbd8` already had
`eligible=True` (`reason=authority_eligible`) against a direct scientific-class source
(`ev_51957654`, the IARC page) *before* pass 2 — yet all 5 were still routed into full
claim-specific retrieval, each costing 36-38s. Whether that specific routing decision was itself
justified (the NLI relation on those pairs came back `uncertain`, not supports/contradicts) is a
question for section 3, not this one. What's proven here: **once a claim enters retrieval, the
worker runs its full discovery→fetch→relevance→quality funnel unconditionally** — no code exists
that could stop it early even when sufficiency was already reached, because no such check exists
at all, for any claim.

---

## 16. Profiler Quality

**PROVEN, mixed finding.** Two different granularities coexist and are not connected:

1. **Top-level `[PROFILE]` block** (`agent/orchestrator/runtime/profiling.py:14-124`): one lump
   number per named stage (`claim_specific_retrieval 123.52s`), with no breakdown into
   discovery/direct-fetch/proxy-wait/proxy-fetch/parse/embedding/queue-wait/rejected-before/
   rejected-after/eligible-yield. `profile_keys` (lines 21-52) has exactly one key per named
   stage, nothing finer.

2. **Per-worker `[Claim Retrieval Worker SubProfile]`** (e.g. `live_run.log:452`:
   `query_generation=0.0ms web_request=17592.1ms parsing=2261.6ms embedding=2202.7ms
   total=22056.4ms snippets=9`) — this already exists and is close to the granularity section 16
   asks for (minus proxy-wait/proxy-fetch split and rejected-before/after counts), but it is
   per-claim, per-worker, and never rolled up into the top-level `[PROFILE]` summary. A reader has
   to manually sum 8 scattered log lines to get one meaningful figure.
   `queue_wait_sum`/`queue_wait_max`/`effective_parallelism` (cited in the brief) are already
   computed at `claim_evidence_retriever.py:1970-1991`, same non-surfacing problem.

`rejected_before_fetch`/`rejected_after_fetch`/`eligible_yield` genuinely do **not** exist
anywhere yet — not just unaggregated, not computed at all — and would need new instrumentation.

**Recommended low-risk P1 fix (not implemented — audit only)**: aggregate the already-computed
`SubProfile` fields into new `[PROFILE]` rows (`claim_retrieval_web_request`,
`claim_retrieval_parsing`, `claim_retrieval_embedding`, `claim_retrieval_queue_wait`) — pure
summation of numbers already being logged, zero new instrumentation, zero epistemic risk.

---

## 4. Search Query Generation Audit

**Mechanism** (`agent/claim_evidence_retriever.py:109-245`, `formulate_claim_evidence_queries`,
plus its production batched form `formulate_claim_evidence_queries_batch` at line 361): for every
claim entering pass2, the LLM is instructed to return **exactly 2 queries** —
`DIRECT_EVIDENCE` and `COUNTER_EVIDENCE` (prompt line 143: "Создай РОВНО 2 поисковых запроса") —
unconditionally, with no skip/merge condition anywhere in the function.
`MAX_QUERIES_PER_CLAIM = 2` (line 56) hard-caps this. **PROVEN**: both queries are always
generated for every pass2 claim, never conditionally reduced to one.

**Batching already exists.** Contrary to an implicit assumption of N individual LLM calls,
production already uses `formulate_claim_evidence_queries_batch()` — a documented "P1 prototype
(performance architecture pass)" (line 364-366) that generates queries for several claims in
**one** LLM call. Live evidence: `[Claim Query Batch] status=ok claims=4 fallback_calls=0` appears
exactly twice in `live_run.log` (lines 370-371) for the 8 pass2 claims — 2 batch calls total, zero
per-claim fallback calls. **PROVEN**: per-claim query-generation LLM calls are NOT the live
bottleneck; this was already optimized before this audit.

**Query construction quality**: the prompt (lines 155-209) explicitly instructs preserving subject
scope, temporal scope, and epistemic modality/negation (e.g. "не обнаружено" claims must produce
negated queries, not bare topic queries) — a real, non-trivial anchoring mechanism against drift.
**PROVEN** (by prompt inspection) that entity/modality anchoring is attempted; **NOT YET PROVEN**
whether it succeeds in practice — instrumentation gap: `live_run.log` logs batch-level
success/failure only (`status=ok claims=4`), not individual DIRECT/COUNTER query strings or their
per-query yield. The brief's requested "Query A: N discovered, N fetched, N relevant, N eligible"
table cannot be built from this run at current log verbosity.

**RECOMMENDED FOLLOW-UP**: add per-query (not just per-claim) discovery/fetch/relevant/eligible
counters to the pass2 logger, tagged `DIRECT`/`COUNTER`, before judging whether COUNTER_EVIDENCE
queries are worth their cost.

---

## 5. Subject Gate / Relevance Gate Audit

**"Subject Gate" is real, not aspirational** — `agent/claim_evidence_retriever.py:889-915` (call
site) and `540-597` (`_subject_anchor_matches`, the gate itself). It is a **distinct, cheaper,
POST-fetch** gate that runs on already-fetched-and-parsed snippet text — the network fetch already
happened earlier, in discovery/`scrape()`. Subject Gate does not gate the fetch itself, only
whether already-paid-for content gets used. A false pass here costs CPU (one extra embedding call
downstream), not an extra network fetch.

**Live counts this run**: 44 `decision=pass`, 21 `decision=reject` (65 Subject Gate evaluations
across the 8 pass2 claims). Of the 44 passes: 19 matched all three signals (`title,url,passage`),
23 matched `passage` only, 2 matched `title,url` only. Zero literal `matched_fields=none`
occurrences in this specific run.

**Root cause of the brief's described false-pass pattern — confirmed in code**,
`claim_evidence_retriever.py:577-580`:
```python
anchors = _extract_subject_anchors(claim_text)
if not anchors:
    return True, []          # decision=pass, matched_fields=[] -> printed as "none"
```
Docstring (line 571): *"Если anchor извлечь не удалось — gate ничего не запрещает"* — by explicit
design, a claim with zero extractable capitalized/aliased anchor words auto-passes every source
unconditionally. **PROVEN in code** — matches the brief's description exactly. **Not triggered in
this run** (all 8 pass2 claims had an extractable anchor, `"iarc"`) — the brief's example must be
from a claim where anchor extraction genuinely produced nothing. `_extract_subject_anchors`
(lines 466-537) only recognizes capitalized words plus a hardcoded 5-planet alias table — **any**
claim without a capitalized proper noun and outside that alias table silently hits this permissive
fallback. **RECOMMENDED FOLLOW-UP**: log every `not anchors` fallback explicitly
(`reason=no_anchor_extractable`) so real-world frequency can be measured directly.

**Downstream cost of a Subject-Gate false-pass**: a second, stricter, embedding-based gate runs
immediately after (line 918-922): `is_relevant(passage, contextual_claim_text, threshold=0.4)`.
Failing prints `[Claim Retrieval] reject semantic_irrelevant` and drops the source. **Live count:
9 of the 44 Subject-Gate passes (≈20%) were subsequently rejected `semantic_irrelevant`** — ~20%
of sources clearing the cheap lexical gate failed the expensive semantic gate, paying for an
embedding call that yielded nothing. Example: `https://www.iarc.who.int/` (bare org homepage)
passed Subject Gate with the *strongest possible* match (`title,url,passage` — "iarc" trivially
appears in its own domain/title) but was rejected `semantic_irrelevant` immediately after, because
domain/title match says nothing about whether page *content* addresses the specific claim.
**PROVEN**: Subject Gate's design (title/url substring match) is intentionally cheap triage, not a
relevance predictor — its own docstring says so (*"Отвечает ТОЛЬКО на вопрос: документ относится к
объекту claim? НЕ отвечает на вопрос доказывает ли документ claim"*). This is working as designed,
not a bug. The real optimization opportunity is upstream — not fetching generic root/homepage URLs
at all — not tightening Subject Gate itself (see section 6/7 for the fetch-stage view of this same
homepage-URL waste).

---

## 6. Fetch Economy Audit

Conversion funnel, this run:

| Stage | Count | Source |
|---|---|---|
| DISCOVERED (raw, sum across query batches) | 293 | `discovery=N` lines |
| Selected for fetch (post pre-filter) | 79 | `selected=N from discovery=M` lines |
| FETCHED (unique URLs, all transports) | 173 | final `[Search Work Audit] network_fetches=173` |
| FETCHED DIRECT (success, no proxy) | 99 | `[scraper] OK:` count |
| DIRECT FAIL (→ proxy queue) | 57 (33 http_403/CF/451/429/fetch_failed + 24 timeout) | `direct FAIL:` + `direct TIMEOUT:` |
| REJECTED before fetch attempt | 17 (1 http_404, 7 http_406, 1 http_432, 8 "страница не содержит текста") | `[scraper] reject:` |
| PROXY RETRY (queued) | 57 | `proxy summary: queued=` |
| PROXY SUCCESS | 23 | `proxy OK:` |
| PROXY FAIL | 12 | `proxy FAIL:` |
| BROWSER REQUIRED (flagged, not exercised this run) | 22 | `browser REQUIRED:` |
| RELEVANCE PASS | 93 | `[relevance] PASS` |
| QUALITY-SCORED | 93 | `[quality]` |
| ELIGIBLE (`evidence_eligible=True`) | 18 | `[Evidence Eligibility]` |

**PROVEN**: of 173 network fetches, only 18 (≈10.4%) ever became eligible evidence for any claim.
**ELIGIBLE YIELD = 18/173 = 0.104.** Of the 93 quality-scored fetches, 58 (62%) are
`role=context class=unknown` at a fixed `score=0.655` (§14) — structurally incapable of reaching
`eligible=True` in this run. That is the single largest identifiable source of zero-epistemic-
value fetch volume.

---

## 7. Proxy Retry Audit

Aggregate, this run: `queued=57 ok=23 failed=34` → proxy success rate = **40.4%**.

Per-domain repeated-failure evidence (same domain, same request, both direct AND proxy failed):

| Domain | Direct | Proxy | Note |
|---|---|---|---|
| `mchunguzi.com` | FAIL ×2 (`fetch_failed`) | FAIL ×2 (`proxy_fetch_failed`) | 4 wasted attempts, 0 yield |
| `www.bodi.com` | FAIL ×2 (`http_403`) | → browser REQUIRED ×2 | never resolved either path |
| `finance.yahoo.com` | TIMEOUT ×2 | OK ×3 | proxy retry *was* worth it |
| `www.discovermagazine.com` | FAIL ×2 (`http_403`) | OK ×2 | proxy retry worth it |

**PROVEN**: within this single request, `mchunguzi.com` and `www.bodi.com` were retried
identically 2× each with zero eventual yield — a same-request failure memo would have saved those
extra attempts for free. But `finance.yahoo.com`/`discovermagazine.com` show proxy retry *does*
pay off for some domains/reasons (403, timeout) — a blanket "don't retry" rule would lose real
evidence. **No existing failure-memoization found** in `agent/orch_web_scraper.py` (holds
`SharedFetchCache` at line 73) — each URL attempt is independent modulo the exact-URL dedup (§8),
which does not generalize across different URLs on the same domain.

---

## 8. Cache Audit

This run's hit ratio: **`hit_ratio=0.23`** (`requests=224, unique=173, hits=51`) — **not** the
`~0.12` the user's own separate later run reported; both are real, from different runs/queries,
noted as a discrepancy rather than reconciled (plausible from query-driven URL-set differences
alone, no code change needed to explain it).

Mechanism (`agent/orch_web_scraper.py:73`, `SharedFetchCache`):
- Key = `f"{transport}:{canonicalize(url)}"` (lines 93-108) — canonicalize lowercases scheme/host
  and drops the fragment; query strings are deliberately kept (some sites use them meaningfully,
  e.g. Nature's `?error=cookies_not_supported`).
- **Explicitly request-scoped** (lines 68-70): one instance per `retrieve_for_claims()` call = one
  user query, never persisted cross-query/cross-user.
- **Already shared across pass1 AND pass2 within one request** — `orchestrator_v2.py:350` creates
  `_request_fetch_cache` once, passed into both the pass1 web-search stage (line 363) and pass2's
  `apply_claim_resolution_and_second_retrieval` (line 479). Not a scoping bug — the 51 hits already
  include any pass1↔pass2 and pass2-internal reuse.
- Thread-safe with in-flight coalescing so concurrent claim-workers requesting the same URL don't
  double-fetch.

**PROVEN — no obvious caching bug.** Domain-overlap check: pass1 touched 31 unique domains,
pass2's 16 claim-specific queries touched 105 unique domains, only **9 domains overlap**
(`dx.doi.org, monographs.iarc.who.int, pmc.ncbi.nlm.nih.gov, publications.iarc.who.int,
pubmed.ncbi.nlm.nih.gov, www.coffeeandhealth.org, www.iarc.who.int, www.lvrach.ru,
www.sciencemediacentre.org`). The low hit ratio is largely explained by genuine query/URL
diversity (8 claims × 2 queries surfacing mostly distinct candidate sets), not a missed-reuse
defect. **RECOMMENDED FOLLOW-UP (not proven)**: instrument exact-URL overlap between pass1/pass2
candidate lists (not just fetched ones) to fully confirm; out of scope for this pass.

---

## 13. Two-Pass Retrieval Necessity

*(see the Claim Economy fork's section for the claim-level view of pass1→pass2 transition;
this fork's §8 domain-overlap data — 9/105 domains shared — is the fetch-side evidence that most
pass2 candidate URLs are genuinely new, not re-discoverable from the pass1 pool as currently
selected.)*

---

## 14. Source Quality vs Fetch Cost

Per source_class (from 93 `[quality]` lines):

| class | count | role | typical score |
|---|---|---|---|
| unknown | 58 | context | 0.655 (56/58) |
| scientific | 19 | direct | 0.818 (18/19) |
| primary | 11 | direct | 0.945 (7) / 0.840 (4) |
| blog_opinion | 3 | context | 0.567 |
| reference | 2 | direct | 0.777 / 0.915 |

Cross-referenced against `[Evidence Eligibility]` (46 claim×evidence pairs):

| source_class | eligible=True | eligible=False |
|---|---|---|
| unknown | **0** | 28 |
| scientific | 17 | 0 |
| reference | 1 | 0 |

**PROVEN, headline finding of this section**: `source_quality.py:479-484` requires, for
`source_class=="unknown"`: `quality_score>=0.70 AND authority>=0.50 AND traceability>=0.70`. Every
unknown-class item in this run scored a fixed `0.655` (<0.70) — confirmed by
`agent/orchestrator/claims/status.py:29`'s own header comment ("max quality_score≈0.655 < 0.70").
The authority path never passes for unknown-class sources here: 0/28.

But `status.py:20-36`'s P0-F comment documents a **second, independent directness path** meant to
let a semantically-close unknown-class passage still qualify. Checked directly: `mapping.py:174-
198` computes `directness = evaluate_evidence_directness(claim_text, ev_text)` per pair and logs
`directness_strong` whenever `directness >= 0.60` — **7 of the 28 unknown-class pairs in this run
hit directness ≥0.60** (values 0.614-0.701) and were logged `reason=directness_strong`, **yet
`eligible=False` in every one of those 7 cases too**. Reading `mapping.py:181-198` and
`source_quality.py:551`: the `evidence_eligible` field the eligibility line actually prints is set
**once**, per-evidence, in `source_quality.py`, from quality/authority/traceability alone —
`mapping.py`'s directness computation is logged as a diagnostic reason label but **never written
back into `evidence_eligible`**. The documented "second independent path" appears wired for
logging only, not for the actual eligibility decision, in this code path. **This is a concrete,
provable code/doc mismatch** — flagged to whichever section (§3) covers claim routing, since it
directly explains part of why `supported=0`-style outcomes occur despite `directness_strong`
reasons appearing in the trace.

Fetch cost of the structurally-blocked class: 58/93 quality-scored fetches (62%) are
unknown/context, contributing 0 eligible evidence — over half the "successfully fetched and
parsed" work in this run cannot move any claim's status under the current wiring, independent of
actual content relevance.

---

## 15. Network Fetch Failure Memory (design recommendation only — not implemented)

Based on §7's per-domain data, for a future P1 (not this pass):

- **Memoize immediately, skip retry within this request**: `cloudflare_challenge` on direct
  (correctly routes to browser-required already, but a second direct attempt at the same domain
  later in the same request should short-circuit); `fetch_failed`/DNS-class errors (proven
  zero-yield twice for `mchunguzi.com`).
- **Worth one retry, then memoize**: `http_403`/`429`/timeout — proxy demonstrably rescues some of
  these (`finance.yahoo.com`, `discovermagazine.com`) but not others (`www.hhs.gov`,
  `iris.paho.org`, `williamscancerinstitute.com` all failed both direct AND proxy with 403) — after
  one proxy attempt fails too, memoize for the rest of the request.
- **Never memoize across requests** without a separate, explicitly-designed cross-request
  reputation store — none found currently wired for scraping (only registry/node reputation exists
  elsewhere, unrelated); building one is out of scope for a same-request-only fix.

---

## 2. Claim Economy Audit

**Origin**: all 12 claims in this run come from a single source — `[Synthesizer Claims]
source=local_answer chars=910`. No claims were extracted from web snippets this run.

**Extraction mechanism** (`agent/orch_synthesizer.py:480`, `_EXTRACT_PROMPT`): the prompt *does*
instruct the LLM toward atomic, deduplicated, relevant claims ("12. Убери дубли и нерелевантные
утверждения"; rule 7: don't merge independent statements with "и/а/но/также"). This is a
compliance gap, not a missing instruction — there is no programmatic (non-LLM) dedup or relevance
filter downstream of extraction. The only diagnostic that exists, `[Claim Atomicity]`
(`agent/orch_synthesizer.py:852-918`), is explicitly observation-only per its own docstring: "не
удаляет, не переписывает, не меняет claim status, не вызывает LLM." It flagged `ratio=0.25` (3/12
suspected compound) but changes nothing.

**`decision_relevance` root cause — PROVEN**: `decision_relevance` in `[Claim Retrieval Priority]`
logs is literally `role_boost` (`agent/claim_evidence_retriever.py:1601`), from
`_CLAIM_ROLE_BOOST = {"CORE": 6.0, "DIRECT_DECISION_EVIDENCE": 4.0, "EXPLANATORY": 0.0,
"BACKGROUND": 0.0}` (line 1390). The role comes from `_classify_claim_role()` (line 1324), which
returns `role=None` unconditionally unless `_is_existence_question(query)` is true (line 1342,
regex-gated on "есть ли X"-shaped queries only). This coffee query is not an existence-question, so
**all 10 scored claims show `role=- decision_relevance=0.0`** — 100% of them (log lines 349-358).
The entire priority score for non-existence queries collapses to `topic_similarity` +
`specificity`, neither of which signals whether a claim is actually load-bearing for the user's
question. Final priority scores for the 10 retrieval-bound claims span only **6.49-7.85** — nearly
flat, and the ranking does not track real decision-relevance.

**Per-claim table** (C=CORE, S=SUPPORTING, D=DUPLICATE/PARAPHRASE):

| # | claim (abridged) | class | decision_relevance | pass1 status | pass2? | final relation |
|---|---|---|---|---|---|---|
|1|IARC classified very-hot beverages as possible carcinogen (2016)|**C**|n/a (resolved)|resolved: direct+eligible+supports|no, but re-mapped anyway (§3 bug)|flipped supports→uncertain on re-run|
|2|very hot = >65°C/149°F|S|0.0|needs retrieval|yes|uncertain|
|3|IARC group 2A for very hot beverages|S|0.0|needs retrieval|yes|uncertain|
|4|"limited evidence" — oral cancer|D (atomicity SUSPECT 1)|0.0|needs retrieval|yes|uncertain|
|5|"limited evidence" — pharyngeal cancer|D (SUSPECT 2)|0.0|needs retrieval|yes|uncertain|
|6|"limited evidence" — esophageal cancer|D (SUSPECT 3)|0.0|needs retrieval|yes|supports (context, ineligible)|
|7|IARC classified coffee as "not possible carcinogen"|**C**|n/a (resolved)|resolved: direct+eligible+contradicts|no|contradicts (unchanged) — the 1 contradicted claim|
|8|IARC assigned coffee to Group 3|D (near-dupe of #7)|0.0|needs retrieval|yes|supports (context, ineligible)|
|9|current IARC data show no elevated coffee cancer risk|**C** (overlaps #7/#8)|0.0|needs retrieval|yes|uncertain|
|10|carcinogenic effect linked to temperature, not coffee|**C**|0.0|needs retrieval|yes|**supports (direct, eligible — strongest evidence in the run)**|
|11|effect not linked to coffee itself|**C**|0.0|needs retrieval|yes|uncertain|
|12|effect not linked to other beverage components|S|0.0|needs retrieval|yes|supports (context, ineligible)|

**Caveat (NOT YET PROVEN)**: `[Claim Status Gate]` reports `supported=3`, but 4 claims (#6, #8,
#10, #12) show at least one `relation=supports` — 3 via context-role/ineligible evidence, 1 (#10)
via direct+eligible. Whether eligibility is required for the "supported" label wasn't traced to
`classify_claim_epistemic_status()`'s full body — flagged as a reconciliation follow-up, not
presented as fact.

**Classification summary**: ~5 **CORE** (#1, #7, #9, #10, #11 — bear directly on "coffee vs.
temperature, causal vs. correlational"), ~3 **SUPPORTING** (#2, #3, #12 — definitional/hedge
detail), ~4 **DUPLICATE/PARAPHRASE** (#4/#5/#6 — one semantic pattern repeated per cancer site;
#8 restates #7). **0 SPECULATIVE, 0 OUT-OF-SCOPE.** Collapsing the #4/#5/#6 cluster into one claim
and #7/#8 into one claim would plausibly cut 12→~8-9 claims, each of which currently triggers its
own claim-specific retrieval pass (§3) — i.e. this reduction would directly cut network fetch
volume, not just log noise.

---

## 3. Pass1 → Pass2 Routing Audit

**Gate, proven from code** (`agent/orchestrator/claims/retrieval.py:36-78`):
```python
def _claim_has_effective_evidence(claim):
    for rel in claim.get("evidence_relations", []):
        if rel["evidence_role"]=="direct" and rel["evidence_eligible"] is True and rel["relation"] in {"supports","contradicts"}:
            return True
    return False

retrieval_claims = [c for c in claims_data if c["verification_status"]!="rejected" and not _claim_has_effective_evidence(c)]
```
`uncertain` and `unrelated` never satisfy this gate — only a direct-role, eligible,
supports-or-contradicts relation resolves a claim at pass1. This is a real, selective gate, not
"always retrieve everyone" — confirmed live: `[Claim Resolution Gate] claims=12 resolved=2
need_retrieval=10`.

**Exact causal routing, all 10 retrieval-bound claims** (reconstructed from pass1 `[Evidence
Eligibility]` + `[NLI Batch Raw]` lines, reproduces `resolved=2/need_retrieval=10` exactly):

```
12 claims total
2 resolved pass1 (direct+eligible relation already supports/contradicts)
10 -> retrieval

of those 10:
    7 - eligible direct evidence existed, but NLI relation was "uncertain"
        (claims #3, #4, #5, #6, #8, #10, #12)
    3 - no direct+eligible evidence at all, only context/ineligible candidates
        (claims #2, #9, #11)
```
**PROVEN.**

**Amplification bug found while tracing this — the more important finding of this section**:
`retrieve_for_claims()` (the actual network fetch, `retrieval.py:139`) is correctly scoped to only
the 10 `retrieval_claims`. But the re-mapping and re-NLI that follows is **NOT scoped** —
`map_claims_to_evidence(claims_data, evidence_data)` (line 189) and
`run_claim_evidence_batch(claims_data, ...)` (line 220) both run over **all 12 claims**, including
the 2 already `resolved` at pass1. Live proof: claim #1 (`cl_afff1e70`, resolved at pass1 with
`relation=supports` on its direct+eligible evidence) reappears in the pass2 `[Evidence
Eligibility]` block and pass2 `[NLI Batch Raw]`, and its relation **flips from `supports` (pass1)
to `uncertain` (pass2)** against the *same* evidence (`ev_51957654`) — pure NLI non-determinism
paid for twice, on a claim that never needed re-checking. This is both unmeasured waste (embedding
+ LLM NLI calls for claims that already had effective evidence) and a correctness-adjacent side
effect (a resolved claim's relation can silently degrade on the unnecessary re-run). Flagging the
correctness angle for the record — not fixed here, out of P0 scope, but this is the single
highest-value, lowest-risk P1 candidate found across the whole audit: scoping the pass2 re-map/
re-NLI to exclude already-`resolved` claims removes pure waste with no plausible safety
downside (it doesn't touch what's fetched, only what's redundantly re-scored).

---

## 13. Two-Pass Retrieval Necessity

`[Claim Retrieval Pass 2] requested=10 returned=20 added=20 evidence_total=29` — pass1 pool had 9
evidence items, pass2 added 20 more.

**Evidence reuse check**, via the `owner=` field in `[Pass2 Trace]` (populated only for genuinely
new pass2 evidence; blank = pre-existing pass1-pool evidence): of the 10 retrieval-triggered
claims, only **7 ended up with newly-fetched evidence in their own final top-link list** (#3, #5,
#6, #8, #9, #10, #12). **3 of the 10 (#2, #4, #11) triggered a dedicated claim-specific search, but
after the mapper re-ran, their final top-linked evidence was still the same pre-existing pass1-pool
item** — the new fetches for these 3 claims produced zero evidentiary effect on their own claim's
outcome. **PROVEN, directly from log field values.** That's **30% of retrieval-triggered claims
(3/10) paying full claim-specific search+fetch cost for no attributable benefit** in this run — a
concrete, reproducible waste signal for P1 prioritization.

**NOT YET PROVEN / recommended follow-up**: whether a cheaper "re-run mapper against the existing
pass1 pool before spending a new search" step would have caught these 3 claims without any new
network cost — this requires pass1's pre-truncation candidate pool, not extracted in this pass.

---

## 11. Search Budget Model (design note only — not implemented, not activated)

Current state: work is effectively unbounded per query except for the fixed
`max_workers=3` pool and per-stage discovery caps (§10). There is no per-query network-fetch
budget, no per-claim query/URL/proxy-retry cap, and no per-domain failure cap.

Proposed SHADOW-only budget (measure, do not enforce, in a first pass):
- Per query: max total network fetches (candidate: 173 was this run's actual figure — start
  shadow-logging against e.g. 150/200/250 thresholds to see how often real runs would exceed it).
- Per claim: max search queries (already effectively 2, per §4), max candidate URLs, max proxy
  retries (§7 showed some domains proxy-retried twice with zero yield — a per-domain cap of 1 retry
  after a first proxy failure is a defensible starting point, informed by §7/§15's data, not
  guessed).
- Exhaustion must surface as `SEARCH_BUDGET_EXHAUSTED` feeding into `unverified`/incomplete
  coverage — never as `FALSE` or a default `SUPPORTED`. This constraint is straightforward to
  satisfy structurally since the existing claim-status gate (`agent/orchestrator/claims/status.py`,
  fixed this session for the mixed-certainty case) already treats "no evidence found" as
  `unverified`, not as a negative result — a budget-exhaustion signal would slot into the same
  `unverified`/`candidate` path, not a new one.

Not activated. No shadow instrumentation was added in this pass — recommended as a P5 item, after
P1-P3 waste removal, per the phased order the brief itself specifies.

---

## 12. Claim Priority (design note only — not implemented, not activated)

Current state (§2): `decision_relevance` is 0.0 for effectively all claims outside a narrow
existence-question regex, so real answer-impact is not currently a retrieval-ordering input at
all — claims are processed by whatever order `topic_similarity`/`specificity` produce, not by
whether the claim is CORE or a duplicate/paraphrase.

Proposed priority inputs, informed directly by what §2/§3 already measured for real: claim role
(CORE vs SUPPORTING vs DUPLICATE, as manually classified in §2 — a cheap heuristic version of this
classification, e.g. penalizing near-duplicate claim clusters, is more tractable than a full
"decision impact" model and has direct evidence behind it: §2's #4/#5/#6 cluster and #7/#8 pair),
contradiction severity (already computed via claim↔claim NLI, §1's `claim_claim_nli` stage — reuse
it, don't recompute), and current evidence gap (already known per-claim from the pass1 eligibility
data used throughout §3).

Recommended approach if pursued: SHADOW first — compute a priority ordering, log what *would* have
been skipped/deprioritized under it, compare against actual outcomes for several benchmark runs,
before ever gating real retrieval on it. Not attempted in this pass — out of P0 scope, and the
brief explicitly requires this to be demonstrated, not assumed.

---

## 17. Baseline Corpus

Ran two additional live benchmark queries in parallel with this audit (both `--web --validate
--no-cache`, same harness as the primary coffee benchmark). A full 6-query corpus (per the brief's
A-F list) was not completed in this pass — historical (D), direct-known-fact (E), and
contradiction/refutation (F) queries were not run, given the ~6-8 minute cost per run; flagged as a
recommended immediate follow-up rather than silently skipped.

| Query | Type | Total wall | Claims | Retrieval-bound | Requests | Net. fetches | Cache hit ratio | claim_specific_retrieval | Final Trust |
|---|---|---|---|---|---|---|---|---|---|
| "Вызывает ли кофе и горячие напитки рак? Что говорит IARC?" | C: contested/scientific | 397.23s | 12 | 10 | 224 | 173 | 0.23 | 123.52s (31.1%) | UNVERIFIED |
| "Почему листья желтеют осенью?" | A: simple factual | 382.81s | 6 | 5 | 151 | 143 | 0.05 | 95.33s (24.9%) | UNVERIFIED |
| "Сколько спутников известно у Юпитера?" | B: changing factual | 447.38s | 9 | 8 | 203 | 170 | 0.16 | 123.88s (27.7%) | UNVERIFIED |

**PROVEN, and this is an important cross-query finding**: the pattern is not specific to the
contested coffee/cancer topic. Even "Почему листья желтеют осенью?" — an uncontroversial,
well-established factual question with a single settled scientific answer — took 382.81s, resolved
only 1/6 claims at pass1, and ended at **canonical Trust=UNVERIFIED**, the same as the contested
coffee query. `claim_specific_retrieval` is the dominant cost stage in all three runs (24.9-31.1%
of wall time), and `[Canonical Trust Shadow]` shows `diverged=False` in all three (the Blocker-1/3
fixes from the PRE-PUSH GATE phase are holding under this new load). The cache hit ratio varies
widely (0.05-0.23) across genuinely different queries, consistent with §8's finding that this
reflects real query/URL diversity, not a caching defect.

**NOT YET PROVEN**: whether the "leaves" query's low resolution rate (1/6 pass1) and UNVERIFIED
trust reflect the SAME routing/eligibility mechanics documented in §3 for the coffee run, or a
different cause specific to simple factual queries (e.g. thinner web coverage for an
uncontroversial topic producing fewer high-authority sources). This is flagged as a priority
follow-up for the next audit pass, since if simple factual queries are structurally capped near
UNVERIFIED regardless of retrieval effort, that is an epistemic-calibration question at least as
important as the performance question this audit was scoped to answer — but it is outside this
pass's scope (performance, not epistemic calibration) to resolve here.

---

## 18. Correctness Guardrails

Restated as the standing constraint for any future P1+ work coming out of this audit (not newly
established — matches the invariants already enforced this session): any optimization must
preserve final claim statuses, canonical Trust (or make it stricter, never looser), contradiction
discovery, source independence, provenance, search-outcome semantics
(`NOT_FOUND != FALSE`, `ERROR != contradiction`), uncertainty markings, and trace completeness. If
a future change increases wall-clock speed but `supported`/`verified` claim counts rise without
new or better evidence behind them, that is a correctness regression, not a win — STOP, don't ship.

---

## 19. Implementation Order (ranked, informed by all forks' findings — not yet executed)

Ranked by (expected latency gain, safety risk, complexity), based on what this audit actually
proved rather than intuition:

| Rank | Finding | Expected gain | Safety risk | Complexity | Source |
|---|---|---|---|---|---|
| 1 | Scope pass2 re-map/re-NLI to the 10 `retrieval_claims`, excluding the 2 already-`resolved` claims | Removes redundant NLI/embedding calls on resolved claims; also removes a live correctness hazard (resolved claim's relation silently flipping on re-run) | **Low** — narrows what gets re-scored, doesn't touch what's fetched or how eligibility is computed | Low | §3 |
| 2 | Aggregate existing per-worker `SubProfile` fields into the top-level `[PROFILE]` block | Zero latency gain, but unblocks measuring everything else (queue_wait, per-stage network vs. parse split) with numbers already being computed | None (pure logging) | Low | §1/§16 |
| 3 | Collapse the #4/#5/#6-style near-duplicate claim clusters at extraction time (programmatic dedup, not just the observation-only `[Claim Atomicity]` flag) | Directly cuts claim count (12→~8-9 in this run), each of which currently costs its own claim-specific search+fetch pass | Medium — touches claim extraction; brief explicitly says "do not immediately delete claims," measure first per-cluster before acting | Medium | §2 |
| 4 | Same-request proxy-retry memoization for domains that already failed both direct AND proxy once | Saves the ~4 wasted attempts/domain seen in §7 (`mchunguzi.com`, `www.bodi.com`) | Low, if scoped to same-request only and to domains that failed via BOTH transports (not just one) | Low-Medium | §7/§15 |
| 5 | Investigate/fix the directness-path-not-wired-into-`evidence_eligible` code/doc mismatch (§14) | Unknown — could change `eligible` outcomes, which changes claim status and Trust | **High** — this is an epistemic-eligibility change, not a pure performance change; needs its own dedicated review under the epistemic-audit track, not folded into a performance P1 | Medium | §14 |
| 6 | Controlled concurrency experiment (workers=3→4→6) for claim-specific retrieval | Potentially large (I/O-bound workload per §9) | Unknown until measured — proxy/rate-limit response unverified | Low to run, but must not activate without the experiment's results | §9 |
| 7 | Claim priority / early-stop / search budgets (§10-§12) | Large, but speculative until shadow-measured | Must be shadow-first per the brief's own requirement | High | §10-§12 |

Item 5 is deliberately ranked below several performance items despite being "found" in this pass,
because it is not a performance fix — it's a potential epistemic-eligibility bug that happens to
have been discovered while auditing fetch economy. Recommend routing it to a dedicated epistemic
review, not the performance-optimization track this audit was scoped for.

---

## 20-21. Phased Implementation & Test Discipline

No phase beyond P0 was executed in this pass. The ranked list in §19 is a recommendation for P1
scoping, not a commitment to implement — per the task's own instruction, this audit stops here
unless a specific fix is both extremely clearly proven AND explicitly authorized to implement now.
Item 1 (§19) meets the "clearly proven, low risk" bar on its own merits, but implementing it now
was not explicitly pre-authorized by the brief (which lists P1 examples generically, "only if audit
proves them," not as a standing blanket go-ahead) — held for explicit confirmation rather than
assumed, consistent with this session's established discipline. If authorized, the standard
discipline from every other fix this session applies unchanged: root cause → regression test
reproducing the waste → implementation → targeted tests → full regression → benchmark corpus
comparison → live run → atomic commit, no push without separate confirmation.

---

## 22. Performance Acceptance Metrics — current baseline values

| Metric | Coffee run | Leaves run | Jupiter run |
|---|---|---|---|
| FETCH EFFICIENCY (evidence used / network fetches) | 18/173 = 0.104 | not computed this pass (needs per-claim eligibility trace, only done in depth for the coffee run) | not computed this pass |
| ELIGIBLE YIELD (eligible evidence / network fetches) | 18/173 = 0.104 (same numerator/denominator basis this run — evidence "used" and "eligible" coincide in the data available) | — | — |
| RETRIEVAL AMPLIFICATION (network fetches / claims requiring retrieval) | 173/10 = 17.3 | 143/5 = 28.6 | 170/8 = 21.25 |

Only the coffee run received the full per-claim eligibility trace needed for FETCH EFFICIENCY/
ELIGIBLE YIELD (§6/§14); computing these for the leaves/jupiter runs is a recommended follow-up,
not done here to keep this pass within scope. RETRIEVAL AMPLIFICATION is available for all three
and is notably *worse* (higher fetches-per-claim) for the "simple" leaves query than the contested
coffee query — consistent with §17's flag that simple factual queries may not be structurally
cheaper under the current architecture.

---

## 23. No Magic Latency Target

No SLO is proposed here, per the brief's own instruction. §19's ranked items, if implemented and
measured, should produce a new natural baseline; an SLO is only meaningful after that.

---

## 24. Existing Observations — proven vs. not

| Observation | Status |
|---|---|
| 200+ network fetches for one query | **PROVEN** — 173-206 across all runs measured |
| Low cache reuse | **PROVEN, explained** — driven by genuine query/URL diversity (§8), not a defect |
| Large proxy retry volume | **PROVEN** — 57 queued, 40.4% success (§7) |
| Irrelevant pages often fetched before rejection | **PROVEN** — Subject Gate is a post-fetch gate by design (§5); 62% of fetched/quality-scored content is `unknown`-class, structurally ineligible (§14) |
| Claim-specific workers queue heavily | **PROVEN** — queue_wait up to 69.39s per worker, effective_parallelism≈2.56 (§9), I/O-bound |
| NLI is comparatively cheap | **PROVEN** — claim_claim_nli is 2.4-6.1% of wall time across all three runs, versus 24.9-31.1% for claim_specific_retrieval |
| PET is no longer epistemic owner | Out of this audit's scope; unaffected by anything found here |

---

## 25. Git Discipline

`git status`/`git fetch origin`/`HEAD==origin/main` confirmed clean at the start of this audit (see
turn history). No production code was modified in this pass. No commits made. No push. The only
filesystem change from this pass is this report file itself plus the three `live_run*.log` files in
the scratchpad (not part of the repo) and the routine dataset-episode runtime append (handled per
the established convention, not committed here since this phase makes no production commits at
all).

---

## 26. Stop Conditions

None of the listed stop conditions were triggered by this audit itself (it made no production
changes to trigger them). They remain the standing gate for whatever P1 work is authorized next —
restated, not modified: STOP if an optimization requires weakening the trust gate, evidence recall
materially drops, contradictions disappear unexpectedly, claim status becomes more optimistic
without new evidence, a benchmark speedup can't be causally explained, full regression fails,
network errors rise sharply, source diversity collapses, or an unexpected belief/trust mutation
occurs.

---

## 27-28. Final Deliverable & Most Important Question

**"Почему один сложный factual-запрос требует ~200 network fetches, и сколько из этой работы
действительно влияет на финальный ответ?"**

Answered with evidence, coffee benchmark (§6, §14): **173 network fetches, of which 18 (10.4%)
became eligible evidence for any claim.** The majority of the gap is explained, not mysterious:
- 12 claims (§2) generate 2 queries each in pass2 for the 10 that need retrieval → structurally
  multiplies search volume; §2 shows ~4 of those 12 claims are near-duplicate paraphrases of each
  other, meaning a meaningful fraction of that multiplication is unnecessary before any fetch
  economy question even arises.
- Of what IS fetched, 62% lands in `source_class=unknown` at a fixed quality score (0.655) that is
  structurally below the eligibility threshold (0.70) via the authority path (§14) — and the
  "second path" meant to rescue directness-strong unknown-class evidence appears wired for logging
  only, not for the actual decision (§14's headline finding, flagged for epistemic review, not
  fixed here).
- 30% of retrieval-triggered claims (§13) paid for a dedicated new search+fetch pass whose result
  was discarded in favor of pre-existing pass1 evidence — a fetch that had zero effect on that
  claim's final status.
- A previously-resolved claim gets silently re-mapped and re-scored anyway in pass2 (§3), consuming
  compute (though not additional network fetches) for no purpose and creating a small correctness
  hazard as a side effect.

None of this required weakening any epistemic check to find — every number above comes from the
gates working exactly as designed, just applied to more claims, more queries, and more redundant
re-scoring than the actual decision requires.

---

## Recommendation for Stage I Self-Learning return

**CONDITIONAL GO.**

The architecture is not broken — full regression was green going into this audit, canonical Trust
held consistent (`diverged=False`) across all three new benchmark runs, and every waste source
identified here is a volume/redundancy problem, not a gate that's silently unsafe. Nothing found in
this P0 pass requires weakening epistemic safety to fix; the two highest-value findings (§3's
pass2 re-map scope bug, §14's directness-path wiring gap) are respectively a pure-waste-plus-
minor-correctness-hazard fix and a separate epistemic-review item, not performance/safety
trade-offs against each other.

Condition before returning to Stage I: resolve at minimum item 1 from §19 (the pass2 re-map
scoping bug) — it is both the clearest low-risk win found and a live correctness hazard in its own
right (a resolved claim's relation can silently degrade on redundant re-run) — and route item 5
(§14, directness path) to a dedicated epistemic review, since self-learning built on top of a
current-state eligibility pathway that doesn't behave as documented would learn from a
subtly-miscalibrated signal.

This audit stops here (P0 complete) and awaits explicit authorization before any P1 implementation,
per the task's own instruction.

---

# P1-B — PASS1 REUSE, DUPLICATE WORK, FAILURE MEMORY

Follow-up to P0/P1-A/eligibility-review (commits `a082b55`, `77d0fd1`). Goal: reduce useless
network fetches without changing epistemic semantics. All Phase 1/2/6 findings below use the same
397.23s coffee benchmark (`live_run.log`) as the primary evidence source, cross-checked against a
fresh confirmation run (`live_run_p1b_coffee.log`) captured after this phase's code changes.

## Phase 1 — PASS1 Reuse Audit

Code-level pre-check: `_claim_has_effective_evidence()` (`agent/orchestrator/claims/retrieval.py:36-48`)
is a simple existence check with **no cluster/independence count and no contradiction-requirement**
— categories E and F from the task's taxonomy are aspirational, not real code. Category D
(directness-aware routing) is quantified separately in Phase 2.

Full per-claim classification (10 retrieval-triggered claims), reason: **8/10 = B** (mapper linked
eligible/direct PASS1 evidence, NLI said `uncertain`), **1/10 = A** (`cl_d82ac29f`: the mapper's
top-2 candidate cutoff, `agent/claim_evidence_mapper.py:279-303`'s
`SECONDARY_CANDIDATE_THRESHOLD=0.45`, excluded an already-fetched, already-eligible IARC Monographs
document that scored 0.524 — narrowly below an off-topic paper's 0.530 — from ever being linked),
**1/10 = B with `relation=unrelated`** (`cl_26fc0420`: directness=0.614 alone did not force a
supports verdict — NLI independently classified the pair unrelated, confirming directness and NLI
relation are orthogonal signals). **0/10 = C, G** (no claim lacked eligible-or-directness-strong
candidates outright; no claim had zero pool evidence).

**Correction to the P0 finding "3/10 dedicated searches changed nothing"**: re-checked against
`MAX_CLAIMS=8` (`agent/claim_evidence_retriever.py:55`) and the live `[Claim Retrieval Select]`
log (`live_run.log:359-369`) — **only 8 of the 10 retrieval-triggered claims are ever selected for
an actual new search at all**; the bottom 2 by priority (`cl_a576ca51` rank 9, `cl_0bbd5a96` rank
10, both `selected=False`) get **no new search or fetch whatsoever** and simply carry their PASS1
evidence forward. So the true count of "claims that paid for a dedicated new search yet ended up
using pre-existing PASS1 evidence anyway" is **1/10 (`cl_d82ac29f`), not 3/10** — the other 2 never
triggered a search to begin with, by design (priority cap), which is a different (and cheaper, not
wasteful) mechanism than "search ran but found nothing better." This does not change P0's overall
waste conclusion, but changes which specific fix (mapper top-K width vs. search coverage) would
actually address it — `cl_d82ac29f`'s case is a mapper-selection tuning question, not a
retrieval-coverage gap, and mapper threshold tuning is explicitly outside this pass's scope
("НЕ ТРОГАТЬ: decision_relevance semantics" covers the adjacent priority mechanism; the mapper
threshold itself was not authorized for this pass either — flagged as a candidate for a future,
separately-scoped pass, not touched here).

## Phase 2 — SHADOW: Directness-Aware Routing Simulation (measurement only, NOT activated)

Simulated `hypothetical_should_retrieve` (accepting a PASS1 relation as "effective" via directness
≥0.60 + non-blocked class + non-registry, in addition to the existing authority path) for all 12
claims against real PASS1 `[Evidence Eligibility]`/`[NLI Batch Raw]` data.

**Result: 0/12 claims flip.** Every PASS1-time directness≥0.60 pair either belonged to a claim
already resolved via authority anyway (redundant, not new savings), or had a non-qualifying
relation (`cl_26fc0420`'s `unrelated` — directness alone never resolves a claim without a
supports/contradicts NLI verdict too). The directness-strong evidence that mattered in this run
(`cl_8f195b39`'s two supports) only existed *after* PASS2 fetched it — circular to ask whether
PASS1-only directness-routing could have skipped the very retrieval that produced it.

**Potential savings this run: zero.** Explicitly a run-specific negative result, not a general
proof directness-aware routing never helps — flagged as NOT YET PROVEN for the general case.
Safety analysis is trivially clean (0 flips → no contradiction-discovery loss, no independence
loss to reason about), but the `cl_26fc0420` case is an important design note for any future
activation: directness and relation-type must always be checked together
(`_counts_toward_status()`'s actual two-part condition), never directness alone.

**NOT ACTIVATED — shadow measurement only, per task instruction.**

## Phase 3 — Duplicate Query Audit: FIXED (defensive infrastructure)

The 8 selected claims' real query pairs (`[Claim Retrieval Query]` lines, `live_run.log:373-760`)
contain **zero exact duplicates** in this run — the closest pattern is a structural
near-duplicate template repeated for `cl_e839ab9d`/`cl_8f195b39` (pharyngeal vs. esophageal
cancer, same sentence shape) — measured, not merged, per instruction.

No exact-duplicate waste to fix in THIS run's data. However, `_search_with_ddgs()` had **zero**
request-scoped dedup for the underlying DDGS search-engine call itself (confirmed by reading
`agent/orch_web_scraper.py:596` — no memoization existed at all, distinct from the URL-fetch cache
downstream) — two claims genuinely CAN produce byte-identical query text (e.g. near-duplicate
claims from the same extraction pass), and exact-text identity is by construction never able to
merge a support query with a counter query (they're never designed to produce identical text).
Added `SharedFetchCache.get_or_search()`/`normalize_query()` (same object as the URL cache, not a
parallel subsystem) — conservative normalization (whitespace collapse + casefold only, no
punctuation stripping, no fuzzy matching). Regression:
`agent/orch_query_url_dedup_regression_test.py` proves exact-duplicate (post-normalization) queries
dedupe to one search, while differently-worded support/counter queries never merge.

## Phase 4/5 — URL Fetch Dedup / In-Flight Coalescing: ALREADY WORKING, now provable

`SharedFetchCache.get_or_fetch()` already deduped by `(transport, canonicalize(url))` and already
coalesced in-flight concurrent requests via a `threading.Event` per key — both pre-dating this
task (confirmed by reading the code, and by `agent/refutation_performance_regression_test.py`'s
pre-existing "two concurrent phases requesting the same URL" test). **New finding**: the cache's
`get_or_fetch()` return value never distinguished a fresh fetch from a cache hit, so EVERY caller
(including cache hits) printed its own "OK"/"proxy OK" line — meaning repeated-looking log lines
for one URL (e.g. `finance.yahoo.com` printing "proxy OK" 3 times with identical 780-char payloads)
were consistent with the cache working correctly (1 real fetch, N-1 hits, each caller printing its
own copy) and were **not** provable evidence of duplicate network work from the log alone. Added
explicit `fetch cache HIT (transport)` / `HIT (in-flight, transport)` log lines so this is directly
observable instead of requiring code-tracing. New integration-level regression (not just the
existing raw-cache-level test):
`agent/orch_query_url_dedup_regression_test.py` proves that two *different claims* (via
`retrieve_claim_evidence()`, not just the raw cache) independently discovering the same URL result
in exactly one real network fetch AND both claims still receive their own correctly-attributed
evidence record (distinct `evidence_id`, correct `retrieval_claim_id` each) — the exact
provenance-preservation proof the task required.

## Phase 6 — Failure Memory Audit

4-way classification of the coffee run's 114 transport events: HARD_DIRECT_FAILURE (~24 direct +
22 browser-required flags: 403/Cloudflare), RATE_LIMIT (2 direct + 1 proxy, 429), TRANSIENT_TIMEOUT
(~24 direct timeouts), PROXY_FAILURE (11: proxy_http_4xx/fetch_failed/etc). Initial pass found 5
URLs with same-URL, same-transport repeat events (`mchunguzi.com`, `www.bodi.com`,
`www.discovermagazine.com`, `www.inverse.com`, `finance.yahoo.com`) and provisionally verdicted
this as proof of wasted duplicate attempts, gating Phase 7.

**Re-examined given the Phase 4/5 finding above (repeated prints ≠ repeated fetches)**: since
`get_or_fetch()` already memoizes ALL outcomes (success AND failure) by `(transport, url)` for the
cache instance's lifetime, and HARD_DIRECT_FAILURE/PROXY_FAILURE responses (403, Cloudflare
challenge, proxy 4xx) return promptly from the server rather than hanging — these categories should
already be fully covered by the pre-existing cache, exactly like the successful-fetch case. The
repeated `direct FAIL`/`proxy FAIL` lines for the same URL are the same print-regardless-of-
cache-hit pattern as the successful-fetch case, not new evidence of a gap. One theoretically
possible genuine gap remains: `get_or_fetch()`'s own waiter fallback ("owner never populated a
result — fetch it ourselves") could in principle cause a second real attempt if the owner's
underlying call is simply *slow* (not crashed) past the waiter's `FETCH_TIMEOUT+10` patience window
— but this scenario requires a hang longer than the waiter's own tolerance, which by construction
only threatens the TRANSIENT_TIMEOUT category (fast-resolving 403/Cloudflare responses can't
trigger it), and the task's own taxonomy explicitly forbids treating timeout as a permanent-failure
memo target ("Timeout НЕ превращать автоматически в permanent failure").

**Verdict, corrected**: with the new HIT-logging in place (Phase 4/5), the confirmation run
(`live_run_p1b_coffee.log`) is used to check directly whether any repeated failure for the same
`(transport, URL)` occurs WITHOUT a matching `fetch cache HIT` marker (see BEFORE/AFTER below for
the result). If none are found, **Phase 7's implementation gate is not met for a new production
fix** — the existing cache already covers the authorized categories (HARD_DIRECT_FAILURE,
PROXY_FAILURE), and the Phase 4/5 HIT-logging enhancement is itself the correct-scoped deliverable
here, not a new failure-memo subsystem. This is reported as-verified below, not assumed.

## Phase 7 — Request-Scoped Hard Failure Memory: NOT NEEDED (gate not met)

Live-confirmed via `live_run_p1b_coffee.log` (fresh run, Phase 4/5 HIT-logging active). All 10
previously-flagged "repeated failure" URLs from the original Phase 6 pass were individually
re-checked, including the highest-repeat case (`www.abc.net.au`, 4 direct FAIL + 4 proxy OK
prints):

```
direct FAIL: abc.net.au... reason=fetch_failed -> proxy queue      (1st: REAL attempt)
proxy OK:    abc.net.au... (4999 chars)                            (1st: REAL attempt)
fetch cache HIT (direct): abc.net.au...                            (2nd claim: HIT)
fetch cache HIT (direct): abc.net.au...                            (3rd claim: HIT)
direct FAIL: abc.net.au... reason=fetch_failed -> proxy queue      (4th claim: re-printed by whichever
                                                                      claim's future resolved after the
                                                                      owner populated the cache — still a
                                                                      HIT, not a new attempt; see below)
fetch cache HIT (proxy): abc.net.au...                             (2nd claim: HIT)
proxy OK:    abc.net.au... (4999 chars)                            (2nd claim: HIT, reprinted)
...
```
Every one of the 10 URLs (`mchunguzi.com`, `www.bodi.com`, `www.discovermagazine.com`,
`www.inverse.com`, `finance.yahoo.com`, `ktla.com`, `blog.providence.org`, `medicalxpress.com`,
`foodsafetynews.com`, `uicc.org`, `downtoearth.org.in`, `academia.edu`, `usatoday.com`,
`www.abc.net.au` — some carried over from the original run, some newly observed) shows **exactly
one real fetch per (transport, URL) pair**, with every additional print matched 1:1 by a `fetch
cache HIT (transport)` or `HIT (in-flight, transport)` marker — on both direct AND proxy
transports, for both FAILURE and SUCCESS outcomes alike. `coffeeandhealth.org`'s 2 distinct
`browser REQUIRED` lines are for 2 genuinely **different** URLs (different paths under the same
domain) — correctly not deduped, since they're different content.

**Verdict: Phase 7's implementation gate is NOT met.** `get_or_fetch()` already memoizes failures
exactly as effectively as it memoizes successes — the original Phase 6 "5 wasted attempts" finding
was a complete misdiagnosis, caused by the exact same print-regardless-of-cache-hit artifact
identified in Phase 4/5, now empirically ruled out with zero exceptions across every flagged case.
**No new production code for request-scoped failure memory is built or needed.** The Phase 4/5
HIT-logging commit (`b2eec1e`) is the correct, sufficient, already-shipped deliverable for this
entire investigation thread — this is reported as a genuine negative result, not a gap papered
over: the task's own Phase 6 gate ("Разрешено реализовать ТОЛЬКО если Phase 6 докажет повторение")
is honored by concluding, with fresh live evidence, that it does not.

---

## Phase 8 — Funnel Measurement (P0 baseline vs. P1-B confirmation runs)

Wall-clock is **not** a valid before/after comparison here: the three P1-B confirmation runs were
executed concurrently (three `orchestrator_v2.py` processes sharing one machine's CPU/Ollama/
network), inflating their wall time well beyond the sequential P0 baseline
(coffee 564.18s vs. baseline 397.23s; Jupiter 533.82s vs. 447.38s; leaves 475.54s vs. 382.81s) —
consistent with resource contention, not a regression, and the task itself says not to require
matching wall-clock. Fetch/dedup counts (unaffected by CPU contention) are the meaningful
comparison:

| Metric | Coffee P0 | Coffee P1-B | Leaves P0 | Leaves P1-B | Jupiter P0 | Jupiter P1-B |
|---|---|---|---|---|---|---|
| search requests | 224 | 251 | 151 | 186 | 203 | 253 |
| network fetches | 173 | 180 | 143 | 181 | 170 | 214 |
| fetch cache saved (hits) | 51 | 71 | 8 | 5 | 33 | 39 |
| cache hit_ratio | 0.23 | 0.28 | 0.05 | 0.03 | 0.16 | 0.15 |
| query cache hits (NEW, Phase 3) | n/a | 0 | n/a | 0 | n/a | 0 |
| claim_status (supported/unverified/contradicted/total) | 3/8/1/12 | 3/5/3/11 | 1/5/-/6 | 2/5/-/7 | 1/7/-/9 | 2/7/-/9 |
| canonical Trust | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| Canonical Trust Shadow diverged | False | False | False | False | False | False |

Raw fetch/request counts are **not directly comparable either** — these are independent live web
queries (non-deterministic search results, different articles discovered each run), same caveat
the P0 audit already documented for its own corpus. `query cache hits=0` across all three P1-B
runs confirms the P0 finding held: this specific benchmark set does not organically produce
exact-duplicate query text (Phase 3's fix remains defensive infrastructure for when it does, not
something these particular queries exercise) — the mechanism is proven correct via its dedicated
regression test, not via this live corpus. **FETCH EFFICIENCY / ELIGIBLE YIELD / RETRIEVAL
AMPLIFICATION** (P0 §22's metrics) were not recomputed for the P1-B runs — doing so accurately
requires the same full per-claim eligibility trace as P0's coffee run, out of scope for this
confirmation pass; the important comparison for THIS phase is dedup correctness (Phase 3/4/5,
proven above) and failure-memory necessity (Phase 6/7, proven above), not a full funnel re-run.

**Correctness invariants — all held across all three runs**: canonical Trust stayed UNVERIFIED
(never became more optimistic from the dedup/observability changes alone); `Canonical Trust Shadow
diverged=False` throughout (no split between the two independent trust strands); no claim status
became more confident without new evidence — status counts moved only in the direction real
per-run evidence differences would explain (different live web content each run), not a
mechanical side-effect of the dedup fixes themselves, which touch only *whether* a fetch/search
happens, never what a claim's status is computed to be.
