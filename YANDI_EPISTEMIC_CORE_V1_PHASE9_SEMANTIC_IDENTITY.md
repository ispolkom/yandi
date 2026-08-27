# Epistemic Core v1 — Phase 9: Claim Semantic Identity (Offline Research)

Per the plan: **offline research, reuse belief_manager.py's existing
exact→embedding→LLM-judge pattern verbatim, do not write a third
equivalence engine, do not change production claim identity this
phase.** Phase 2's `content_hash` (`agent/claim_identity.py`) is
untouched — this is a separate, harder question (paraphrase equivalence,
not exact/near-exact text identity) evaluated independently.

## 1. What was (and wasn't) built

`agent/claim_semantic_identity_prototype.py::classify_claim_pair()`
calls `BeliefManager._embed_batch()` (static) and
`get_belief_manager()._llm_judge_relation()` (the production singleton's
own method) directly — zero reimplementation of embedding, cosine
similarity, or the LLM judge prompt. The threshold (0.70) is the exact
same one `belief_manager.py` already uses and calibrated
(`belief_manager.py:234-243`), not re-tuned.

Unlike Phase 5's deliberately network-free shingle fingerprinting, this
prototype makes real Ollama calls — that's inherent to the pattern being
reused, not an oversight. "Offline" here means *not wired into
production claim identity*, not *no network calls*.

## 2. Results against the 12-pair labeled corpus (real embedding + real LLM judge, no mocking)

```
TP=4 FP=1 FN=0 TN=7
precision=0.800  recall=1.000
```

| Category | Expected | Outcome | Correct |
|---|---|---|---|
| exact_duplicate | equivalent | `exact` | ✓ |
| paraphrase | equivalent | `equivalent` | ✓ |
| **near_paraphrase_changed_number** (the plan's named critical risk) | NOT equivalent | `contradicts` | **✓** |
| near_paraphrase_changed_date | NOT equivalent | `contradicts` | ✓ |
| near_paraphrase_changed_entity | NOT equivalent | `contradicts` | ✓ |
| negation | NOT equivalent | `contradicts` | ✓ |
| scope_change | NOT equivalent | `different` | ✓ |
| temporal_change | NOT equivalent | `different` | ✓ |
| causal_vs_correlational | NOT equivalent | `equivalent` | **✗ — the one miss** |
| multilingual_paraphrase (ru/en) | equivalent | `equivalent` | ✓ |
| unrelated_sanity_floor | NOT equivalent | `different` | ✓ |
| paraphrase | equivalent | `equivalent` | ✓ |

### 2.1 The critical risk, tested directly and passed

"У Юпитера подтверждено 95 спутников." vs "...96 спутников." — the
plan's own named worst-case (near-identical wording, embedding
similarity likely very high, epistemically different claims) was
correctly judged `contradicts`, not `equivalent`. Same result for a
changed date (1976 vs 1975) and a changed entity (Tolstoy vs
Dostoevsky). **The reused belief_manager.py pattern is robust against
exactly the failure mode the plan was most worried about** — this is
the headline finding.

### 2.2 The one honest miss: causal vs. correlational

"Курение вызывает рак лёгких" (causal: X causes Y) vs "Курение
статистически связано с повышенным риском рака лёгких" (correlational:
X is associated with Y) were judged `equivalent`. These are NOT the same
epistemic claim — a causal claim asserts a mechanism, a correlational
claim only asserts an observed association, and conflating them is a
real, known category of overclaiming. This is a genuine limitation of
the reused LLM judge prompt (`belief_manager.py:291-321`), which asks
"do these express the same thought" without explicitly instructing the
model to treat causal-strength language as load-bearing.

**Not fixed here.** The judge prompt is shared, live production code
(used by `belief_manager.py::add_belief()`'s real dedup path today) —
editing it is out of this phase's explicit "don't change production
identity" boundary, and doing so to fix ONE test case without a broader
evaluation of the prompt's behavior elsewhere would be exactly the kind
of unproven threshold/prompt tuning the plan warns against
("Не менять thresholds без отдельного доказательства" applies in spirit
here too, even though this is a prompt not a threshold). Flagged as a
real, documented limitation for whoever takes on prompt-level work on
`_llm_judge_relation` in the future — this phase's job was to find this,
not fix it.

### 2.3 Multilingual paraphrase worked

The Russian/English tachyon pair was correctly judged `equivalent` —
`embeddinggemma` is a multilingual model and the LLM judge prompt is
language-agnostic, so this worked without any special handling. Not
guaranteed to generalize to all language pairs from a single test case,
but a positive, honest data point.

## 3. What this phase does NOT claim or do

- Does not claim precision=0.800 is production-ready — 12 hand-built
  pairs is not a large corpus, same caveat as Phase 5.
- Does not touch `agent/claim_identity.py`'s `content_hash` or any
  production claim-identity code path.
- Does not modify `belief_manager.py`'s `_llm_judge_relation()` prompt,
  despite finding a real gap in it — that's explicitly out of scope
  (shared production code, would need its own evaluation).
- Does not propose wiring `classify_claim_pair()` into the live claim
  pipeline yet — Phase 10 (cross-request claim linking) is the separate,
  later, deliberate step this plan gates behind "only if Phase 9 is
  successful," and this report's honest 0.800 precision (not a clean
  1.000) is exactly the kind of input that decision should weigh.

## 4. Verification

- New suite `agent/epistemic_claim_semantic_identity_regression_test.py`
  (9 checks, network calls mocked per this project's established
  convention): exact-match shortcut never touches the network,
  below-threshold similarity skips the LLM judge entirely,
  above-threshold similarity correctly routes to and passes through all
  three LLM judge verdicts (equivalent/contradicts/different), an
  unrecognized LLM response fails safe to "different" (never a
  fabricated equivalence), an embedding failure fails safe the same way,
  empty text doesn't crash, and the threshold constant matches
  `belief_manager.py`'s own calibrated value rather than being
  re-derived. 9/9 green.
- Full regression sweep: 22/22 green (14 pre-existing + Phase 1-9's
  suites — Phase 4 added none).
- The real (unmocked) 12-pair corpus evaluation above IS this phase's
  live sanity check of the actual reused pattern — not re-run inside the
  regression suite (which would make every future regression sweep
  depend on Ollama being reachable, a dependency this project's suites
  deliberately avoid). A separate zero-code-impact live pipeline query
  was also run to confirm this phase's new files (imported by nothing in
  production) cause zero behavior change — see the commit message.
