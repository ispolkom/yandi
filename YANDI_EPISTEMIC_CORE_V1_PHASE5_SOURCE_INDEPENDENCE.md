# Epistemic Core v1 — Phase 5: Source Independence Clustering (Offline Prototype)

Per the plan: **offline model first, not connected to production status
calculation this phase.** Nothing in this phase touches
`claims/status.py`'s `support_count` tally, `evidence_pool.py`'s dedup, or
any other production code path. Prototype code lives entirely in
`agent/source_independence_prototype.py` (clustering logic) and
`agent/source_independence_corpus.py` (labeled evaluation data, synthetic
— `example.*`/`example.org`/`example.net` domains throughout, never real
scraped content, so it can never be mistaken for real citations).

## 1. Problem being evaluated

Per the architecture audit's §5 finding: `evidence_pool.py::_dedupe()`
keys on exact URL (or exact content-prefix) only. N syndicated copies of
one wire story, at N different URLs, each individually pass
`claims/status.py`'s eligibility gate and each count toward
`support_count` — inflating a claim's `verification_status` toward
`supported` without genuine independent corroboration.

Two explicit constraints from the plan, both empirically tested below,
not just stated:
- same domain != necessarily same origin
- different domains != necessarily independent

## 2. Signals evaluated

All four signals reuse existing project code/patterns rather than
reinventing them:

| Signal | Reused from |
|---|---|
| Canonical URL | `agent.orch_web_scraper.SharedFetchCache.canonicalize()` |
| Domain/hostname | `agent.source_quality._hostname()` |
| Title similarity | `difflib.SequenceMatcher` over text normalized via `agent.claim_identity.canonicalize_claim_text()` (Phase 2's canonicalization, reused rather than a second normalizer) |
| Content fingerprint | 5-word shingle Jaccard similarity, same normalization |

Content fingerprint uses word-shingle overlap rather than a live
embedding call deliberately, for this offline-evaluation phase: it's
deterministic and reproducible without depending on Ollama being up, and
it's sufficient to answer this phase's actual question (does *any*
cross-domain content signal beat "same domain" as a heuristic). A real
embedding-based signal is a reasonable future upgrade, not required here
— noted in §6.

## 3. Clustering variants compared

- **`url_exact`** — today's production baseline (mirrors
  `evidence_pool.py::_dedupe()`'s exact-URL key).
- **`domain_only`** — the naive assumption the plan explicitly warns
  against, included specifically to measure how badly it over-merges.
- **`combined`** — the candidate model: title similarity ≥ 0.55 OR
  content-fingerprint Jaccard ≥ 0.25, evaluated regardless of whether the
  domain matches (cross-domain aware, and does not treat same-domain as a
  free pass either).

## 4. Labeled corpus (9 pairs, synthetic, all 7 required categories)

A/A exact duplicate · same publisher different article · wire story
syndicated across domains (×2, one plain, one with a byline/intro
variation) · independent reporting of the same fact · partial copy ·
citation of the original source · genuinely independent source · an
extra unrelated-topic sanity floor.

## 5. Results

| Variant | Precision | Recall | False merges | Missed merges |
|---|---|---|---|---|
| `url_exact` (today's baseline) | **1.000** | 0.200 | 0 | wire×2, partial_copy, citation |
| `domain_only` (naive) | 0.500 | 0.200 | **1** (`same_publisher_different_article`) | wire×2, partial_copy, citation |
| `combined` (candidate) | **1.000** | **0.800** | **0** | citation (only) |

**`domain_only` genuinely produces a false merge** on this corpus — two
unrelated articles from the same publisher domain get wrongly treated as
the same origin. This is not a hypothetical; it's the measured behavior,
confirming the plan's warning empirically rather than just accepting it
as an assumption.

**`combined` strictly dominates both baselines**: equal-or-better
precision (never merges two truly independent sources on this corpus —
the critical safety property, since a false merge silently destroys real
corroboration) and 4x the recall of `url_exact` (catches both
cross-domain wire-syndication cases and the partial-copy case that exact-
URL matching structurally cannot).

### 5.1 The one honest miss: `citation_of_original_source`

`combined` does NOT catch the case where an article quotes a short
verbatim span from an original report inside otherwise-original
commentary (title_sim=0.383, content_sim=0.034 — both well below
threshold, diluted by the surrounding non-quoted text).

**Deliberately not tuned away.** Lowering the title threshold to ~0.38 to
catch this would pull in `same_publisher_different_article`
(title_sim=0.400, a true negative just 0.017 away) — a false merge, the
worse failure mode. Lowering the content threshold to ~0.034 would make
the fingerprint signal fire on a single shared 5-word phrase, which is
too fragile to trust outside this specific 9-pair corpus (likely to
false-merge in production on any two articles that happen to quote a
common short official statement). **Catching a short quoted span reliably
needs a different kind of signal (substring/span matching, not
whole-document similarity) — flagged as a real limitation for a future
iteration, not solved here.**

## 6. What this phase does NOT claim or do

- Does not claim `combined`'s exact thresholds (0.55 / 0.25) are
  production-ready or optimal — they're a documented starting point
  evaluated against 9 hand-built pairs, not a large corpus.
- Does not touch `claims/status.py`'s `support_count` computation,
  `evidence_pool.py`'s dedup, `EvidenceRecord`'s schema, or any other
  production file.
- Does not add a `source_cluster_id` field anywhere (that's the audit's
  §10.3/Phase 6 proposal — a *separate*, later, deliberate step, only
  once this offline evaluation is judged convincing enough to act on).
- Does not replace shingle-based fingerprinting with a live embedding
  call — a real embedding signal (reusing the same Ollama
  `embeddinggemma` infra `belief_manager.py` already calls) is a
  reasonable next iteration if this direction is pursued further, but
  wasn't necessary to answer this phase's question.

## 7. Verification

- New suite `agent/epistemic_source_independence_regression_test.py` (15
  checks): signal-level sanity, an explicit demonstration that
  `domain_only` wrongly merges two unrelated same-domain articles while
  `combined` correctly doesn't, an explicit demonstration that
  `url_exact` misses cross-domain syndication while `combined` catches
  it, and the pinned precision/recall/false-merge numbers from §5 (so a
  future edit to the prototype's thresholds or similarity functions
  fails this suite loudly instead of silently drifting). 15/15 green.
- Full regression sweep: 18/18 green (14 pre-existing + Phase 1-4's
  suites; Phase 4 added none, so this is +1 suite over Phase 3's 17).
- Live pipeline run: a real query was run end-to-end with this phase's
  code present in the repo, to prove the offline prototype's mere
  existence causes zero behavior change (nothing imports it) — see the
  commit message for the specific run and outcome.
