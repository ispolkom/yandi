# Epistemic Core v1 — Phase 9B: Semantic Identity Hardening

Per the explicit follow-up instruction after Phase 9: precision=0.800 at
recall=1.000 is not acceptable for an automatic cross-request linking
mechanism, because semantic identity is **fail-dangerous** — a false
negative just creates a duplicate (annoying, recoverable), but a false
positive silently merges two different claims' evidence, history, and
dependencies (corrupting the record, much harder to detect or recover
from). This phase investigates and closes the false-positive classes
found, without building a third embedding/NLI engine, and without
touching `belief_manager.py`'s shared production judge.

## 1. Root cause of Phase 9's false positive

Diagnosed by reading `belief_manager.py::_llm_judge_relation()`'s exact
prompt (`belief_manager.py:291-321`), not guessed:

- The pair (causal "вызывает" vs correlational "статистически связано
  с") correctly cleared the embedding prefilter (similarity ≥ 0.70) —
  **not an embedding/threshold problem**.
- The LLM's raw JSON response parsed cleanly as `{"relation":
  "equivalent"}` — **not a parsing problem**.
- The prompt's `equivalent` criterion says only "различия только в
  формулировке или несущественных деталях" (differences only in wording
  or insignificant details) and never names epistemic-strength language
  (causal vs correlational, certainty vs hedged, universal vs
  existential scope, etc.) as a *significant* detail category.

**Root cause: judge semantics — prompt under-specification.** The model
followed the prompt correctly; the prompt itself doesn't ask about this
dimension.

## 2. Fix strategy: guard layer, not a shared-prompt edit

Per the brief's own preference ("если безопасно достичь без ломки
общего judge не удаётся... предложить архитектуру"), and because editing
`belief_manager.py::_llm_judge_relation()` would require, per the same
brief, a full separate regression baseline against every existing belief
consumer before it could even be considered — this phase deliberately
took the lower-risk path instead: a **deterministic, regex-based
marker-mismatch guard**, layered entirely inside
`agent/claim_semantic_identity_prototype.py` (Phase 9's own module),
never touching `belief_manager.py`.

`agent/claim_semantic_identity_hardening.py::hardening_guard(a, b)`
checks for asymmetric linguistic markers across dimension pairs (one
text matches pattern X, the other matches pattern Y, neither matches
both) covering: causal vs correlational, necessary vs sufficient,
possibility vs certainty, current vs historical, absolute vs qualified,
scope (all vs some), attribution (quoted/reported speech vs bare
assertion), prediction vs observation, absence-of-evidence vs
evidence-of-absence, negation, and numeric mismatch (catches the "95 vs
96" case generically via number-set comparison, not a hardcoded
special-case `if`).

This is **not** a new embedding/NLI engine — every check is a cheap
regex comparison, applied only as a **post-filter on an `equivalent`
verdict** the real embedding+LLM pipeline already produced. It can only
ever downgrade `equivalent → different` (fail toward safety, per the
brief: "Лучше UNKNOWN/NOT_EQUIVALENT и дубликат, чем ложное
объединение"). It never upgrades anything, and never touches
`contradicts` or `different` verdicts.

## 3. Results: 50-pair hard-negative corpus, real (unmocked) Ollama calls

`agent/claim_semantic_identity_corpus_hard.py` — 40 hard negatives (3-4
per named dimension) + 8 true positives (paraphrase/exact-duplicate,
recall check) + 2 sanity-floor negatives.

| | Precision | Recall | False positives |
|---|---|---|---|
| **RAW** (Phase 9 pipeline, no guard) | 0.800 | 1.000 | causal_vs_correlational, **attribution** (new miss found by the larger corpus) |
| **HARDENED** (Phase 9B, guard applied) | **1.000** | **1.000** | **none** |

**Acceptance target met and exceeded**: brief asked for precision ≥
0.95, ideally 1.00 on the hard-negative critical set — achieved 1.000
with **zero recall loss** (all 8 true positives stayed correctly
classified; the guard never fired on a genuine paraphrase).

Most hard negatives (36 of 40) were already correctly rejected by the
raw embedding+LLM pipeline on its own — the LLM judge is generally
competent at these dimensions. Only 2 pairs actually needed the guard to
fire: the known causal/correlational case, and one attribution case
("Эксперт считает, что рынок недвижимости стабилизируется..." vs the
bare "Рынок недвижимости стабилизируется...") that the larger corpus
surfaced and Phase 9's smaller 12-pair corpus hadn't covered — direct
evidence that expanding the corpus was worth doing, not just a formality.

## 4. What this phase does NOT claim or do

- Does not claim 50 hand-built pairs constitute a large or fully
  adversarial corpus — same honest caveat as Phase 5 and Phase 9. A
  determined adversarial search would likely find more marker-free false
  positives the guard can't catch (e.g., a causal/correlational
  distinction phrased without either marker word).
- Does not touch `belief_manager.py`'s `_llm_judge_relation()` prompt —
  the root-cause gap there still exists for `belief_manager.py`'s own
  live belief-deduplication path; this phase's guard only protects the
  Phase 9 claim-identity prototype, not beliefs.
- Does not wire anything into production claim identity — Phase 2's
  `content_hash` is still untouched, and this module is still imported
  by nothing in `agent/orchestrator/*`.

## 5. Verification

- New suite
  `agent/epistemic_claim_semantic_identity_hardening_regression_test.py`
  (19 checks, fully deterministic — the guard itself makes no network
  calls): each of the 8 regex dimension pairs fires on a real
  asymmetric example, attribution/negation one-sided checks, numeric
  mismatch fires generically (proven with a second, unrelated numeric
  pair — not just the moons example), same-numbers-in-both-texts does
  NOT false-trigger, the guard does not fire on 3 genuine paraphrases,
  and integration tests confirm `classify_claim_pair_detailed()` only
  downgrades `equivalent` when the guard actually fires, and never
  touches a `contradicts` verdict even when a marker mismatch is
  present. 19/19 green.
- Full regression sweep: 23/23 green (14 pre-existing + Phase 1-9's
  suites, including Phase 9's own — confirmed unaffected: the guard
  doesn't trigger on Phase 9's generic mocked test strings).
- The 50-pair real-corpus evaluation above (unmocked) is this phase's
  substantive result. A separate live pipeline query confirms zero
  production impact (nothing in `agent/orchestrator/*` imports these
  files) — see the commit message.

## 6. Acceptance decision

**Acceptance criterion met** (precision ≥ 0.95, achieved 1.000 with no
recall loss). Per the brief: proceed autonomously into Phase 10
(cross-request claim linking) rather than stopping to report.
