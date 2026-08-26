# YANDI Runtime Regression Fix Report

Source evidence: `/tmp/yandi_p0p1_integration.log` (live orchestrator run after P0-P3, TOTAL=630.35s), `/home/iam/yandi/YANDI_FULL_PIPELINE_AUDIT.md` (original audit). Note: `/home/iam/yandi/YANDI_P0_P1_IMPLEMENTATION_REPORT.md` referenced in the task instructions does not exist on disk — the P0-P3 implementation summary only existed as chat output from the previous turn; this report treats that chat summary plus this session's own investigation as the implementation record. No new full orchestrator run was performed in this pass, per instructions.

Ollama is unreachable in this sandbox shell (`curl → connection refused` on `127.0.0.1:11434`), and this shell's Python lacks `bs4`/`numpy` (externally-managed system Python, distinct from the project's actual `venv` used for the live run). All logic in this report was therefore verified either by (a) exact-formula validation against real numbers from the live log, or (b) isolated execution of extracted function source (no network/LLM), consistently with prior passes this session.

---

## Backups (this pass, in order created)

| File | Backup |
|---|---|
| `agent/claim_validator.py` | `claim_validator.py.bak_20260825_222042` |
| `agent/claim_evidence_retriever.py` | `claim_evidence_retriever.py.bak_20260825_222727` |
| `agent/orch_synthesizer.py` | `orch_synthesizer.py.bak_20260825_223123` |
| `agent/orchestrator_v2.py` | `orchestrator_v2.py.bak_20260825_223344` |
| `agent/orch_web_query.py` | `orch_web_query.py.bak_20260825_223855` |

(No new backup was needed for `agent/claim_priority_regression_test.py` — new file, nothing to overwrite. All prior backups from earlier sessions/passes remain untouched.)

## Files changed (this pass)

`agent/claim_validator.py`, `agent/claim_evidence_retriever.py`, `agent/orch_synthesizer.py`, `agent/orchestrator_v2.py`, `agent/orch_web_query.py`. New file: `agent/claim_priority_regression_test.py`.

---

## §A — CLAIM VALIDATOR CORE-CLAIM BUG

### Root cause (exact, proven by regex test, not guessed)

`claim_validator.py::META_PATTERNS` contained (before fix):
```python
r'(?i)^\s*по\s+имеющейся\s+информации\b',
r'(?i)^\s*по\s+имеющимся\s+данным\b',
```
Both are **unconditional prefix matches** — any sentence starting with this hedge phrase is rejected as meta, regardless of what follows. Compare the sibling pattern in the same list:
```python
r'(?i)^\s*согласно\s+современным\s+(?:научным\s+)?данным\s*,?\s*(?:ответ|вывод|оценка)\b',
```
which correctly **requires** a trailing reference to the answer/source/conclusion itself before treating it as meta.

Verified directly:
```
'По имеющимся данным разумная жизнь на Юпитере не была обнаружена.'
  по имеющимся данным (unconditional)  match=True   <- REJECTED (wrong)
'Согласно имеющейся информации, разумная жизнь на Юпитере считается крайне маловероятной.'
  (no pattern matches)                 match=False  <- ACCEPTED (survived by lexical accident only)
```

Compounding factor: `generate_local_answer()`'s own prompt (`orchestrator_v2.py:202-230`) explicitly instructs the model to use `"по имеющейся информации"` as a hedge phrase. The validator was rejecting exactly the phrasing the prompt was teaching the model to produce.

### SOURCE/META vs SUBJECT MATTER — how distinguished now

Both patterns now mirror the working sibling: require an explicit trailing reference to `ответ|вывод|оценка|источник...` before classifying as meta:
```python
r'(?i)^\s*по\s+имеющейся\s+информации\s*,?\s*(?:ответ|вывод|оценка|источник[а-я]*)\b',
r'(?i)^\s*по\s+имеющимся\s+данным\s*,?\s*(?:ответ|вывод|оценка|источник[а-я]*)\b',
```
`"По имеющимся данным жизнь не обнаружена"` → no trailing meta-noun → **not meta** (SUBJECT MATTER, accepted). `"По имеющимся данным, ответ является неполным"` → trailing `ответ` → **meta** (SOURCE/META, still rejected).

### Regression tests run (offline, real + adversarial cases)

```
OK accept=True  expect=True   | CORE claim (был REJECT'нут в реальном прогоне) — теперь принят, reason=fact
OK accept=True  expect=True   | regression — claim, что уже проходил, продолжает проходить
OK accept=False expect=False  | настоящий meta wrapper "...ответ на вопрос является неполным" — по-прежнему отклоняется
OK accept=False expect=False  | настоящий meta wrapper "...источник не указывает точных значений" — по-прежнему отклоняется
OK accept=True  expect=True   | другой домен (вода на Марсе), тот же паттерн — работает НЕ Jupiter-специфично
ALL REGRESSION CASES PASS: True
```

---

## §B — DECISION RELEVANCE vs SEMANTIC SIMILARITY

### Why P0.1 (pure embedding relevance) was conceptually insufficient

Confirmed by the live run: 7 of 8 retrieved claims in the new ranking were still habitability/background reasoning. The reason, proven by design: embedding cosine similarity measures **topical closeness to the whole query** ("Есть ли жизнь на Юпитере" is topically about habitability, temperature, pressure, water — all genuinely related subjects), not **whether the claim's truth-value changes the answer**. `"На Юпитере отсутствует жидкая вода"` is topically close to the life question (habitability) but does not itself assert or deny life's presence — its truth value doesn't flip the yes/no answer the way `"жизнь считается маловероятной"` does.

### New mechanism: `_classify_claim_role()` (`claim_evidence_retriever.py`)

Fully deterministic, no LLM per claim, no hardcoded subject (Jupiter/life). Built from three independent, reusable lexical signals:

1. **`_is_existence_question(query)`** — regex on the ORIGINAL query for `"есть ли X"` / `"существует ли X"` / `"обнаружена ли X"` / `"найдена ли X"` / `"зафиксирована ли X"`. Role logic activates **only** for this question type — per instruction, `"Почему..."` / `"Расскажи..."` / `"Какие условия..."` queries get `role=None` and fall back to the old relevance+specificity behavior, preserving breadth exactly where breadth is appropriate.
2. **`_extract_existence_target(query)`** — pulls the noun phrase between "ли" and the next location/purpose preposition (generic heuristic, works for any domain: tested against "жизнь на Марсе", "жизнь на Юпитере" style queries, not hardcoded).
3. **`_EXISTENCE_ASSERTION_MARKERS`** — reuses the already-built `_ABSENCE_MARKERS` (negative detection) plus positive-detection and probability-hedge markers (`обнаружен`, `найден`, `маловероятн`, `считается маловероятн`, ...).
4. **`_EVIDENCE_INSTRUMENT_MARKERS`** — `телескоп`, `зонд`, `аппарат`, `сигнал`, `сигнатур`, `спектр`, `наблюдени`, `миссия`, `радар`, `датчик` — for claims about detection *methods*, a distinct role tier.

Decision tree: `target_match AND assertion → CORE`; `instrument AND (target_match OR assertion) → DIRECT_DECISION_EVIDENCE`; `target_match only → EXPLANATORY`; otherwise `BACKGROUND`.

`_CLAIM_ROLE_BOOST = {CORE: 6.0, DIRECT_DECISION_EVIDENCE: 4.0, EXPLANATORY: 0.0, BACKGROUND: 0.0}` **replaces** the old blanket P0.2 `ABSENCE_CLAIM_BOOST` (which rewarded *any* negation regardless of target — exactly the bug: `"отсутствует жидкая вода"` and `"жизнь не обнаружена"` got the same +2.0 before).

### Old vs new ranking — real 16-claim dataset (+1 claim rescued by §A)

Role classification (deterministic, run directly against real claim texts — no mocking needed for this part):

| claim (real, from live log) | old rank (before this pass) | role (new) |
|---|---|---|
| "разумная жизнь считается крайне маловероятной" | #3 | **CORE** |
| "по имеющимся данным ... не была обнаружена" (§A rescue) | *rejected as meta* | **CORE** |
| "отсутствует жидкая вода на поверхности" | #1 | BACKGROUND (target_match=False) |
| "отсутствуют энергетические градиенты" | #2 | BACKGROUND (target_match=False) |
| "температура ... -145°C" | #4 | BACKGROUND |
| "давление варьируется..." | #5 | BACKGROUND |
| "атмосфера ... водорода и гелия" | #7 | BACKGROUND |
| "газовый гигант без чёткой границы" | #8 | BACKGROUND |
| "любые формы жизни требуют..." (×3) | #13,#15,#16 | EXPLANATORY (target_match=True, no assertion) |
| "водород и гелий не подходящая среда для жизни" | #10(cut) | EXPLANATORY |

Full priority recompute (embedding relevance mocked with plausible synthetic values, since Ollama unreachable here — explicitly marked as such):

```
rank 1: score=13.30 CORE   "разумная жизнь ... крайне маловероятна"
rank 2: score= 7.60 CORE   "по имеющимся данным ... не была обнаружена"   [§A rescue]
rank 3: score= 6.80 BACKGROUND "отсутствует жидкая вода"
...
rank 9-17: BACKGROUND/EXPLANATORY, all CUT
CORE/DIRECT_DECISION_EVIDENCE claims in top-8: 2 of 2 total in dataset
```

**Requirement met**: CORE/DIRECT now occupy budget before BACKGROUND — both CORE claims land in ranks 1-2, none cut.

---

## §C — PROMPT / LOCAL ANSWER SCOPE

Static verification (no live LLM call possible here):

- **Single source confirmed**: `grep -n "def generate_local_answer\|generate_local_answer("` → exactly one definition (`orchestrator_v2.py:202`), exactly one call site (`orchestrator_v2.py:1948`, inside the fan-out `ThreadPoolExecutor`). No other prompt template, no planner/frame injection, no evidence context — `context` parameter is literally `query_to_use` (the same string as `query`), unchanged since the P1.1 patch. **The scope-binding instruction applied last round is the only prompt in play; nothing overrides it.**
- **Why it likely did not reduce claim count** (reasoned, not proven without a live call): the instruction says "focus on direct evidence for/against existence." For a question like intelligent life on Jupiter, genuine direct observational evidence is essentially nonexistent — so the model's most natural way to satisfy "focus on direct evidence" was to construct a **habitability checklist** (define what life requires → check each criterion against Jupiter's conditions) as the best available proxy for "evidence." This decomposes into *more*, not fewer, atomic claims under the extractor's correct atomicity rules — consistent with the observed extracted=17 vs previous 11, despite local_answer itself shrinking 2811→1620 chars (-42%).
- **Decision**: did **not** apply a second, unvalidated prompt revision on top of the first. Stacking two unproven prompt changes before the one integration test this pass is preparing for would make it impossible to attribute any observed effect to either change. A refined V2 direction is proposed below for a *future*, separate iteration, explicitly not installed:

  > *Proposed addition (NOT applied):* "Если для вопроса существования нет прямых наблюдательных данных (обнаружение сигналов, следов, спектральных подтверждений) — прямо скажи об этом ('прямых наблюдений не проводилось'), не разворачивая это в пошаговую проверку каждого критерия обитаемости. Объясняющий контекст — максимум 1-2 предложения."

- Question-type-dependent scope (breadth preserved for "Почему.../Расскажи.../Какие условия...") is **not** implemented at the prompt level (would require classifying question type before prompt construction, a larger change) — but §B's role classifier already implements the equivalent gating at the *retrieval priority* level (`role=None` for non-existence questions), which is the layer that actually controls cost/breadth of retrieval, arguably a better place for this than the prompt itself.

---

## §D — supports_query_aspect

**Decision: revived, not deprecated.** It was architecturally exactly the right field for claim role — a `List[str]` tag on each claim describing its relationship to the query, declared in two schema dataclasses, populated with a dead `["general"]` literal and never read. Now:

- `orch_synthesizer.py` imports `_classify_claim_role` from `claim_evidence_retriever.py` (one-directional; verified no circular import — `claim_evidence_retriever.py` does not import `orch_synthesizer.py`).
- At claim construction (`orch_synthesizer.py`, the claim-building loop), `supports_query_aspect = [role or "general"]`, computed once against `enriched.original` (the real query).
- `claim_evidence_retriever.py::_claim_retrieval_priority()` now **reuses** `claim.get("supports_query_aspect")` if it already holds a valid role, instead of recomputing — single source of truth, computed once, consumed downstream. Falls back to local computation for any claim that didn't pass through `orch_synthesizer` construction.

Verified offline (proves the reuse path is real, not just present in the code):
```
score WITH supports_query_aspect=['CORE'] (reused, ignores bogus query_context): 8.5
score WITHOUT supports_query_aspect (recomputed from bogus query_context):        2.5
reuse path applied CORE boost despite irrelevant query_context: True
OK: reuse path confirmed working
```

---

## §E — is_negative_claim DATA FLOW

**Confirmed exactly as the task suspected: `epistemic_router.is_negative_claim` is query-level and was never wired to claim ranking.** Exhaustive grep across `agent/`:
```
epistemic_router.py:76:   is_negative_claim: bool = False
epistemic_router.py:234:  (docstring)
epistemic_router.py:418:  is_negative_claim=is_negative,
```
Zero other hits — `orchestrator_v2.py` never reads `epistemic_result.is_negative_claim` (`grep -n "is_negative_claim" orchestrator_v2.py` → empty, out of 170 other `epistemic_result.` usages). It was computed correctly (fixed from always-`False` to real in the prior pass) but had **no consumer at all** — not even in trace/logs.

**The per-claim signal that actually reaches ranking is a different, separate mechanism**: `_ABSENCE_MARKERS`/`_is_absence_claim()` inside `claim_evidence_retriever.py`, now folded into `_EXISTENCE_ASSERTION_MARKERS` and consumed via `_classify_claim_role()`'s `has_assertion` check (§B) → `role_boost`. This was an intentional design separation from the start (documented in the original P0.2 code comments), not an accidental omission — query-level "is this query framed negatively" and claim-level "does this specific claim assert absence of the target" are different questions, and conflating them risks narrowing breadth for `"Почему X не..."` questions where breadth should be preserved (explicit task constraint).

**Fix applied**: `epistemic_result.is_negative_claim` is now surfaced in trace (`orchestrator_v2.py`, next to the existing `epistemic_modality` observation) so it's visible for future diagnosis — but deliberately **not** wired into any retrieval/ranking decision, to avoid the breadth-narrowing risk on why-questions.

---

## §F — PRIORITY DIAGNOSTICS

New per-claim log line (`claim_evidence_retriever.py::_claim_retrieval_priority`, one print per claim scored):
```
[Claim Retrieval Priority] claim_id=... role=CORE topic_similarity=0.600 decision_relevance=6.0 specificity=1.00 absence=True target_match=True final=13.30 reason=role=CORE boost dominant
```
Covers exactly the requested fields: claim id, role, topic similarity, decision relevance (boost value), specificity, absence, plus `target_match` and a compact `reason` string identifying the dominant contributor. No full claim text dump. This is why the earlier run showed no such line — it did not exist yet before this pass; the existing `[Claim Retrieval Select]` line only ever showed the final aggregate number, not the breakdown.

---

## §G — 275.87s UNACCOUNTED (43.8% of total)

### Newly instrumented buckets (all additive, wrap existing call sites, `time.time()` before/after — same pattern as the already-working `claim_retrieval_ms`)

| new `[PROFILE]` label | cost key | wraps |
|---|---|---|
| `pre_pipeline_personality` | `pre_pipeline_ms` | Character/Scene/Target/Entity/Strategy/Criticism/Boundary/Early-Gate block (`orchestrator_v2.py:936`→`Early Gate` pass) — previously **zero** timing coverage at all |
| `claim_setup_validator_mapper1_nli1` | `claim_setup_ms` | ClaimValidator + Mapper PASS1 + NLI PASS1 combined |
| `claim_pass2_mapper_nli` | `claim_pass2_mapping_nli_ms` | Mapper PASS2 + NLI PASS2 (inside the `added_count > 0` branch) |
| `claim_claim_nli` | `claim_claim_nli_ms` | Claim↔Claim NLI block — captured from its own already-computed `disagreement_elapsed`, not a new timer |
| `final_claim_coverage` | `final_coverage_ms` | `evaluate_final_claim_coverage()` call |

Cross-referencing against the **previous** live run's own named-but-unlisted print lines (best available estimate, not a live measurement of the current code):
```
claim_setup_ms            ≈ 2.30s (Mapper) + 11.39s (NLI PASS1)         ≈ 13.7s
claim_pass2_mapping_nli_ms ≈ 4.20s (Mapper) + 14.47s (NLI PASS2)         ≈ 18.7s
claim_claim_nli_ms         ≈ 21-40s (more claim pairs now: C(16,2)=120 vs C(11,2)=55 before)
final_coverage_ms          ≈ 1.1s
pre_pipeline_ms            UNKNOWN — never measured before this pass, plausibly the largest remaining piece given character_engine/scene_builder/target_router/entity_resolver/strategy_router/criticism_detector/personal_boundary all run here with zero prior visibility
```

**Honest limitation**: summing the known pieces (~55-75s) does not fully explain 275.87s. `pre_pipeline_ms` is the best remaining suspect but its actual magnitude is **unmeasured until the next live run** — this report does not claim the gap is closed, only that every named phase identifiable from the code and log has now been instrumented. If `pre_pipeline_ms` turns out small in the next run, remaining unaccounted time would point to thread-pool/GIL contention across the many concurrent `ThreadPoolExecutor` regions, which cannot be measured by simple wall-clock wraps and would need separate investigation.

---

## §H — P2.2 SEMAPHORE

No direct runtime evidence was captured for `orch_web_query.py`'s new `GENERATION_SEMAPHORE` usage in the last run (no `[Web Query LLM] generation queue wait=` line existed yet — it wasn't implemented until now). Added, mirroring `orch_synthesizer.py`'s existing pattern exactly:
```python
_wait_started = time.time()
with GENERATION_SEMAPHORE:
    _waited = time.time() - _wait_started
    if _waited > 0.05:
        print(f"[Web Query LLM] generation queue wait={_waited:.2f}s")
    ...
```
(Caught and fixed a missing `import time` in `orch_web_query.py` during this — `py_compile` would not have caught it, since a missing module-level import used inside a function is a runtime `NameError`, not a syntax error; would have crashed on first real call.)

**Verdict: KEEP, status UNKNOWN pending the next run's new wait-time visibility.** Reasoning: the semaphore's downside is bounded (near-zero overhead when uncontended — a single `threading.Semaphore.acquire()` on an unlocked semaphore is cheap), while its potential upside (preventing unmanaged concurrent Ollama generation calls from `orch_web_query` during PASS2's 3-way retrieval fan-out) was never actually observable before. This pass makes it observable without removing it — reverting blind, with zero evidence either way, would just re-create the same "unknown effect" state this report is trying to eliminate.

---

## §I — OFFLINE REGRESSION SUITE

New file: `agent/claim_priority_regression_test.py`. Run via `python3 -m agent.claim_priority_regression_test` (needs the project's real venv — `bs4`/`numpy`/`requests`, all already proven present there by the live run; this sandbox lacks them and cannot run it directly). Covers exactly the 8 requested checks, built from **real claim texts from the last live run**, not invented examples:

1. Core factual negative claim not rejected as meta (+ genuine meta-wrapper still rejected, adversarial control).
2. Existence query recognized (+ negative control: open question not misclassified).
3. Direct/core claims role above background (including the specific §B regression: absence-without-target-match must NOT be CORE).
4. Numeric/atmospheric claims don't out-rank core claims on specificity alone (relevance forced to 0.0 to isolate the effect).
5. Absence/negative feature reaches role classification (`has_assertion` + `target_match` both true for the core claim).
6. Embedding fallback doesn't crash (returns float, not exception).
7. `supports_query_aspect` wiring — import identity check + reuse-path behavioral check.
8. Profile formatting doesn't crash on the new cost keys.

Every check's underlying logic was independently re-verified in this sandbox via isolated execution (extracting function source, no network) since the full script itself cannot run here — both the isolated checks and the full script's logic agree.

---

## §J — SINGLE NEXT INTEGRATION COMMAND

```bash
cd /home/iam/yandi
python3 -m agent.claim_priority_regression_test && \
python3 agent/orchestrator_v2.py \
  "Есть ли разумная жизнь на Юпитере?" \
  --web --no-cache 2>&1 | \
tee /tmp/yandi_regression_fix_integration.log | \
grep -E \
'Synthesizer Claims]|Synthesizer] Извлечено claims|Claim Validator]|Claim Retrieval Priority]|Claim Retrieval Select]|Claim Retrieval Timing|Claim Retrieval Pass 2|Evidence Mapper|Claim Evidence Batch PASS1|Claim Evidence Batch PASS2|Claim Status]|Claim Status Gate|Web Query LLM]|YANDI PIPELINE WALL-CLOCK PROFILE|\[PROFILE\]|PROFILE BOTTLENECK|Готово за|Latency:'
```
The regression suite runs first and fails fast (exit code 1) if any of the 8 checks regress, before spending 10 minutes on a live run. `[Web Query LLM]` added to the grep to surface §H's new semaphore wait-time visibility.

---

# SUMMARY

CORE CLAIM META BUG FIXED: **YES**
DECISION RELEVANCE IMPLEMENTED: **YES** (deterministic claim-role classifier, separate from topic-similarity embedding; both CORE claims in the real 16/17-claim dataset now rank #1-2, none cut)
BACKGROUND DOMINATES TOP-8: **NO** in offline ranking (was YES before this pass — 7/8; now CORE/DIRECT claims occupy the top slots when present)
P0.2 DATA FLOW VERIFIED: **YES** — proven query-level (`epistemic_router.is_negative_claim`, never consumed, now only trace-visible) vs claim-level (`_is_absence_claim`, feeds role classification, does reach ranking) are correctly separate by design, not a broken pipe
SCOPE PROMPT VERIFIED: **PARTIAL** — confirmed sole source, no override; behavioral effect on claim count not fully as hypothesized (fewer chars, more claims); no live A/B run (Ollama unreachable here); a refined V2 direction is proposed but intentionally not stacked on top of an unvalidated V1 before the next test
PROFILE UNACCOUNTED EXPECTED TO DROP: **YES**, magnitude unconfirmed — 5 new named buckets added (pre-pipeline personality block, claim setup, PASS2 mapping+NLI, claim-claim NLI, final coverage); known-phase estimate covers roughly 55-75s of the 275.87s gap from cross-referencing the prior run's own print lines, `pre_pipeline_ms` is unmeasured and the most likely remaining large piece
P2.2 SEMAPHORE STATUS: **KEEP** (wait-time visibility added, no prior evidence existed either way; bounded downside, unverified upside, next run will show queue wait directly)
FULL ORCHESTRATOR RUN: **NOT RUN** (per instructions — offline/static verification only this pass)
