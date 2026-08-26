# YANDI Claim Role Morphology Fix

Follow-up to `YANDI_ABSENCE_REGRESSION_FIX.md`. Triggered by a second live run (`/tmp/yandi_regression_fix_integration.log`, TOTAL=450.25s, down from 630.35s — see assessment below) whose new `[Claim Retrieval Priority]` diagnostic log surfaced two concrete, real misclassifications that the offline suite hadn't covered. No orchestrator run performed in this pass — regex-level fix, verified by isolated execution only.

## Assessment of the second live run (context, not part of the fix)

Before the bug analysis: this run confirms the prior two passes' fixes worked. Latency 630.35s → 450.25s (−29%). `unaccounted` 275.87s (43.8%) → 75.29s (**16.7%**) — the five new `[PROFILE]` buckets from the previous pass captured nearly all of the previously-invisible time; `pre_pipeline_personality` turned out to cost only 0.02s, ruling it out as a contributor (useful negative result). `claim_specific_retrieval` is now honestly the visible, correctly-identified bottleneck (208.58s, 46.3%). `ClaimValidator` accepted 13/13 with zero false meta-rejections. `[Web Query LLM] generation queue wait=1.85s` appeared once — negligible, no evidence the P2.2 semaphore is harming throughput. All of this is working as intended. The two bugs below are refinements on top of an already-working pipeline, not new regressions of the same class.

---

## ROOT CAUSE (both bugs)

### BUG 1 — "Нет X" not recognized as absence

Real failing claim: `"Нет разумной жизни на Юпитере."` → logged `absence=False role=EXPLANATORY final=9.74` (rank 2, but *only* via topic_similarity=0.905 — an embedding accident, not decision relevance).

`_ABSENCE_MARKERS` only covered "evidence-of-X-not-found" phrasing (`нет доказательств`, `нет свидетельств`, `нет подтверждения`) and verb-negation phrasing (`не обнаружен`, `не найден`, ...) — never the **bare existential negation** "нет X" ("X does not exist"), which is actually the *most direct* possible absence claim, more direct than "no evidence has been found."

### BUG 2 — verb-form morphology gap

Real pair from the same run:
```
"Телескопические наблюдения не зафиксировали ..." → DIRECT_DECISION_EVIDENCE  (correct)
"Космические миссии ... не зафиксировали ..."      → EXPLANATORY              (wrong — should match)
```
Two independent stem mismatches, both proven by direct string containment, not assumed:
```python
>>> "зафиксирован" in "зафиксировали".lower()
False   # participle stem (обнаружен-, -ая, -ы) doesn't cover the verb form (обнаружил-и)
>>> "миссия" in "миссии".lower()
False   # 6-char stem breaks on a 4-letter declension change
```
`"миссии"` doesn't contain the instrument marker `"миссия"` (5th letter differs: я vs и) → `has_instrument=False` for claim B → falls through to plain CORE/EXPLANATORY logic instead of the more specific `DIRECT_DECISION_EVIDENCE`.

A **third**, related issue was found while fixing the above and is disclosed here for completeness (not separately requested, fixed as part of the same reorder): after fixing BUG 1, claims like "Телескопические наблюдения не зафиксировали..." satisfied **both** the CORE condition (target_match+assertion) and the instrument condition — and the decision tree checked CORE first, so instrument-bearing claims would have collapsed into plain CORE, losing the more precise DIRECT_DECISION_EVIDENCE label. Fixed by checking `has_instrument` first in the decision tree (§ below) — role *boost values* were not touched, only which label a claim receives.

---

## NEW CANONICAL LOGIC

### `_ABSENCE_MARKERS` — added existential-negation marker, fixed morphology

```python
_ABSENCE_MARKERS = (
    rf"не{_NEGATION_GAP}\s+обнаруж",
    rf"не{_NEGATION_GAP}\s+найден",
    rf"не{_NEGATION_GAP}\s+зафиксирова",   # was "зафиксирован" (12 chars) -> "зафиксирова" (11)
    rf"не{_NEGATION_GAP}\s+выявлен",
    rf"не{_NEGATION_GAP}\s+установ",        # was "установлен" -> "установ"
    r"нет\s+доказательств",
    r"нет\s+свидетельств",
    r"нет\s+подтверждени",
    r"не\s+подтвержд",
    r"отсутству",
    r"ни\s+один[^.]*не\s+",
    r"\bнет\s+(?!сомнени)[а-яё]",           # NEW: bare "Нет X" existential negation
)
```
`(?!сомнени)` excludes `"нет сомнений, что X"` — a double-negation hedge meaning X **is** true, the semantic opposite of ordinary "нет X". Not a claim to cover every such idiom, only the most common one, per the explicit "no full morphological analyzer" instruction.

### `_EVIDENCE_INSTRUMENT_MARKERS` — shortened "миссия" stem

```python
_EVIDENCE_INSTRUMENT_MARKERS = (
    "телескоп", "зонд", "аппарат", "сигнал", "сигнатур",
    "спектр", "наблюдени", "радар", "датчик",
    "мисси",   # was "миссия" (6 chars) -> "мисси" (5), covers миссия/миссии/миссией/миссиями
)
```

### `_target_overlap()` — stemming formula, was breaking on 4-letter target words

```python
# was: word[:4] in claim_lower  — "вода"[:4] == "вода" itself, never matches "воды"/"водой"
return any(
    word[:max(3, len(word) - 2)] in claim_lower
    for word in target_words
    if len(word) >= 4
)
```
Fixes the "Есть ли вода на Марсе?" cross-domain test — the 4-letter target word "вода" previously stemmed to its whole self and failed to match declined forms like "воды".

### `_classify_claim_role()` decision-tree reorder

```python
# DIRECT_DECISION_EVIDENCE checked FIRST — it's the more specific
# category (names a concrete observation method), not because it
# should "win" over CORE in strength (boost values unchanged:
# CORE=6.0 still > DIRECT_DECISION_EVIDENCE=4.0).
if has_instrument and (target_match or has_assertion):
    role = "DIRECT_DECISION_EVIDENCE"
elif target_match and has_assertion:
    role = "CORE"
elif target_match:
    role = "EXPLANATORY"
else:
    role = "BACKGROUND"
```

`epistemic_router.is_negative_claim` — untouched, remains query-level and architecturally separate, per the standing decision from the prior report.

---

## TRUE / FALSE / ROLE VERIFICATION

All exactly per the task spec, verified by isolated execution:

```
Query: "Есть ли разумная жизнь на Юпитере?"

OK  absence=True   role=CORE                     | Нет разумной жизни на Юпитере.
OK  absence=True   role=CORE                     | Разумная жизнь на Юпитере не была обнаружена.
OK  absence=True   role=DIRECT_DECISION_EVIDENCE | Телескопические наблюдения не зафиксировали признаков жизни на Юпитере.
OK  absence=True   role=DIRECT_DECISION_EVIDENCE | Космические миссии не зафиксировали признаков жизни на Юпитере.
OK  absence=True   role=DIRECT_DECISION_EVIDENCE | Зонды не обнаружили признаков жизни на Юпитере.
OK  absence=True   role=BACKGROUND               | На Юпитере нет жидкой воды.
OK  absence=False  role=BACKGROUND               | Температура на Юпитере не превышает -145°C.

Query: "Есть ли вода на Марсе?" (cross-domain, no Jupiter hardcode)
OK  role=CORE       | Нет воды на Марсе.
OK  role=BACKGROUND | Марс не имеет глобального магнитного поля.

ALL CASES AS EXPECTED: True
```

**Full regression re-run** (all previously-established TRUE/FALSE/A-B-C cases from `YANDI_ABSENCE_REGRESSION_FIX.md`, re-verified after this change to confirm no regression from the reorder/stemming/new-marker changes): **ALL REGRESSION OK: True** — 16 absence cases, 3 role-consistency cases, 2 additional role cases (`core_direct`, `background_atmosphere`), all unchanged and still correct.

---

## RUNTIME RANKING REPLAY (real 13 claims, real topic_similarity from the live log, MAX_CLAIMS=8)

| rank | new score | OLD role | NEW role | changed |
|---|---|---|---|---|
| 1 | 15.74 | EXPLANATORY | **CORE** | ✅ fixed — "Нет разумной жизни на Юпитере." |
| 2 | 11.90 | EXPLANATORY | **DIRECT_DECISION_EVIDENCE** | ✅ fixed — "Космические миссии ... не зафиксировали..." |
| 3 | 11.68 | DIRECT_DECISION_EVIDENCE | DIRECT_DECISION_EVIDENCE | unchanged (already correct) — "Телескопические наблюдения..." |
| 4-11 | 8.28→6.32 | BACKGROUND | BACKGROUND | unchanged |
| 12 | 5.31 | EXPLANATORY | EXPLANATORY | unchanged (see residual limitation below) |
| 13 | 3.20 | BACKGROUND | BACKGROUND | unchanged |

Ranks 1-3 (of an 8-slot budget) are now all CORE/DIRECT_DECISION_EVIDENCE — the three claims most directly relevant to "is there intelligent life on Jupiter" now unambiguously outrank every background/explanatory claim, not just by embedding-similarity accident. No embedding values were invented — `topic_similarity` figures are the real numbers from the live log; only `role`/`decision_relevance` recomputed with the fixed code.

### Two residual limitations found while building this replay (disclosed, not fixed — out of this task's explicit scope)

- `"Жидкой воды на Юпитере нет."` — "нет" at clause end, not "Нет X" at the start; the new marker requires "нет" followed by a word, so this trailing form isn't caught. Does not change this claim's role outcome here (target_match is False regardless — it's not about "жизнь"), so no ranking impact in this dataset, but flagged as an incomplete pattern.
- `"Отсутствие твёрдой поверхности противоречит..."` — noun form "отсутствие" (ends -ие) vs the verb stem "отсутству-" (ends -ует/-уют) don't share a common prefix; stays unrecognized as absence. Also no ranking impact here (stays EXPLANATORY either way, matching its pre-fix classification), flagged for awareness.

Neither was in the task's required test list; per the explicit "no full morphological analyzer" instruction, left as known gaps rather than chased.

---

## FILES CHANGED

- `agent/claim_evidence_retriever.py` — `_ABSENCE_MARKERS` (new existential marker + 2 shortened stems), `_EVIDENCE_INSTRUMENT_MARKERS` (шortened "миссия"), `_target_overlap()` (stemming formula), `_classify_claim_role()` decision-tree reorder.
- `agent/claim_priority_regression_test.py` — new section 9 with all 9 spec cases (7 Jupiter + 2 Mars).

## BACKUPS

- `agent/claim_evidence_retriever.py.bak_20260825_230941` (before this pass's fixes)
- `agent/claim_priority_regression_test.py.bak_20260826_081847` (before this pass's fixes)

(All backups from the two prior passes this session remain untouched.)

## PY_COMPILE

```
python3 -m py_compile agent/claim_evidence_retriever.py agent/claim_priority_regression_test.py
FINAL_ALL_OK
```

## REGRESSION SUITE

Not executable end-to-end in this sandbox (no `bs4`/`numpy` here — confirmed distinct from the project's real `venv`, same limitation as both prior passes). Every check's logic — the 9 new spec cases, plus a full re-run of all previously-established cases from the two prior reports — was independently verified via isolated execution of the extracted function source; all passed, zero regressions detected. Expected result on next real run: **0 failures**.

## FULL ORCHESTRATOR RUN

NOT RUN

---

# SUMMARY

BUG "НЕТ X" FIXED: **YES**
MISSION/OBSERVATION MORPHOLOGY FIXED: **YES**
CORE CLAIM ROLE VERIFIED: **YES**
DIRECT EVIDENCE ROLE VERIFIED: **YES**
BACKGROUND ABSENCE STILL BACKGROUND: **YES** ("На Юпитере нет жидкой воды" / "На Юпитере отсутствует жидкая вода" both remain BACKGROUND — absence semantics confirmed still distinct from decision relevance)
REGRESSION FAILURES: **0** (verified via isolated execution; suite itself requires the project's real venv to run directly)
FILES CHANGED: `agent/claim_evidence_retriever.py`, `agent/claim_priority_regression_test.py`
BACKUPS: `claim_evidence_retriever.py.bak_20260825_230941`, `claim_priority_regression_test.py.bak_20260826_081847`
FULL ORCHESTRATOR RUN: NOT RUN
