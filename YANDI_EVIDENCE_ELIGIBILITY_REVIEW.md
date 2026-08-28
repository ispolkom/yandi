# YANDI — Evidence Eligibility / Directness Review (PART B)

Trigger: `YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md` §14 flagged a possible code/doc mismatch —
`directness_strong` appeared to be computed but never written back into `evidence_eligible`,
raising the question of whether the documented P0-F "second independent eligibility path" was
actually wired into production or only into a log line.

**Verdict: NOT A BUG.** The P0 audit fork's finding was correct about the narrow fact it checked
(the raw per-evidence `evidence_eligible` field is never mutated by directness) but incomplete —
it did not trace far enough to find the actual decision point. Directness IS wired into claim
status computation, through a separate, correctly-implemented function
(`_counts_toward_status`), and is demonstrably active and affecting real claim outcomes in both
benchmark runs inspected below. This section documents the full trace and corrects the P0 finding
on the record, rather than silently overwriting it.

---

## 1. Where each signal is computed

| Signal | Computed in | What it is |
|---|---|---|
| `source_class`, `authority`, `primaryness` | `agent/source_quality.py:91`, `_classify_source(host, url)` | Per-*evidence-record* property, from a domain/host classifier. Does not depend on which claim the evidence might be paired with. |
| `traceability`, `quality_score` | `agent/source_quality.py:345`, `evaluate_source_quality(...)` | `quality_score = authority*0.45 + traceability*0.35 + primaryness*0.20` (per prior audit; formula unchanged, re-confirmed by 0.655/0.818/0.945 constants still appearing live in §14 of the P0 report). |
| `evidence_eligible`, `evidence_role` | `agent/source_quality.py:481` (the `quality_score >= 0.70` check, part of `evaluate_source_quality`) | Set **once, per evidence record**, purely from `source_class`/`quality_score`/`authority`/`traceability` — the authority path only. Structurally a source-level property: it does not and cannot see which claim it will later be paired against, because evidence is pooled and mapped to claims only afterward. |
| `directness` | `agent/orchestrator/claims/mapping.py:174`, `evaluate_evidence_directness(claim_text, ev_text)` (function defined `agent/source_quality.py:581`) | Computed **per (claim, evidence) pair**, inside `run_claim_evidence_batch`, at NLI-batch-preparation time — i.e. only once a specific claim is being evaluated against a specific evidence item. Logged into the `[Evidence Eligibility]` line's `directness=` field and the `reason=directness_strong` bit when `>=0.60`. |
| `_counts_toward_status` decision | `agent/orchestrator/claims/status.py:60-84` | The actual gate that decides whether one (claim, evidence) relation counts toward that claim's `verification_status`. Two independent paths, either sufficient: `via="authority"` (`evidence_role=="direct" and evidence_eligible is True`) or `via="directness"` (`source_class not in HARD_BLOCKED_SOURCE_CLASSES and retrieval_origin != "local_registry" and directness >= DIRECTNESS_SUPPORT_THRESHOLD(0.60)`). Called from `classify_claim_epistemic_status()` (`status.py:129`, invoked `orchestrator_v2.py:489`) for every relation on every claim, logged as `[Claim Support Decision] ... via=authority|directness ... counted=True`. |

**Answer to the "where" question**: `evidence_eligible` and `directness` are deliberately different
fields, computed at different times, for different reasons — `evidence_eligible` is a per-evidence
(source-level, class-driven) property; `directness` is a per-pair (claim×evidence) property. They
cannot be the same field. The actual eligibility-for-status decision is `_counts_toward_status`,
which reads both.

---

## 2. Intended contract — recovered from git history

`git show 27f1d9a~1:YANDI_EVIDENCE_ELIGIBILITY_AND_REGISTRY_AUDIT.md` (deleted from the working
tree in commit `27f1d9a`, "docs: remove superseded phase audit reports, consolidated into
ROADMAP_v7.md", but recoverable — content quoted, not reconstructed from memory):

> "Мэппер (`claim_evidence_mapper.py`) считает cosine similarity между claim и passage
> (`all_scores`, `[Mapper Score]`), но это число использовалось только для выбора топ-2
> кандидатов на маппинг — **оно нигде не участвовало в eligibility/role decision**. Это и есть
> корневая причина `supported=0` при корректном ranking."
>
> "Новый путь строго ỳже старого: добавляет **ровно один** дополнительный случай
> (unknown/не-blocked/не-registry + directness ≥ 0.60), не ослабляет ни одно существующее условие."
>
> Truth table (P0-C), reproduced: `context/False/unknown/web/directness>=0.60/supports` →
> **"ДА (НОВОЕ)" via directness**; `context/False/unknown/web/directness<0.60` → "НЕТ";
> `context/False/unknown/local_registry/directness even 0.95` → **"НЕТ (явное исключение реестра)"**.

This is an exact match, field-for-field, for what `_counts_toward_status` (§1 above) implements
today. **Answer to task item 2 (intended contract)**: directness was designed from the start as an
*addition to claim-status counting*, explicitly *not* as a mutation of the raw `evidence_eligible`
flag — because `evidence_eligible` is source-level and directness is pair-level, merging them into
one field was never the design; `_counts_toward_status` was built specifically to combine both
without conflating them.

**Answer to task item 4 (A/B/C/D)**: **B is false, D is the real answer.** This is not a
"logging-only diagnostic" (B) — it demonstrably changes `verification_status` and hence
`supported`/`disputed` counts and Trust (§3 below, live evidence). It is not "legacy from an old
eligibility formula" (C) — the recovered audit doc shows it was added deliberately, is the CURRENT
formula, and the historical "old" formula (authority-only) is explicitly kept as `_raw_relations`
counters for A/B transparency (`status.py:212-224`), not as the active path. It is not "forgotten
wiring" (B/close cousin) — it IS wired, just into a different, correctly-separated function than
the one the P0 fork traced. The correct characterization is **(D) intentionally does not touch
`evidence_eligible`** — because doing so would conflate a source-level signal with a pair-level
one, and the actual eligibility-for-status decision was deliberately built as a separate function
for exactly that reason.

---

## 3. Decision table, with live counts

| source_class | authority (`evidence_eligible`) | directness | directness≥0.60 | counted via `_counts_toward_status` (current) | counted (intended, per P0-F doc) |
|---|---|---|---|---|---|
| primary / scientific / reference | True | any | any | YES — `authority` | YES |
| unknown, not blocked, not registry | False | <0.60 | No | NO | NO |
| unknown, not blocked, not registry | False | ≥0.60 | Yes | **YES — `directness`** | YES |
| unknown, `retrieval_origin=="local_registry"` | False | ≥0.60 | Yes | NO (explicit registry exclusion) | NO |
| blocked class (forum/social/blog_opinion/speculative/news/popular_article/generated_pipeline) | False | ≥0.60 | Yes | NO (hard block) | NO |

**Current == intended in every row.** No gap found.

### Live counts, coffee benchmark (`live_run.log`, 397.23s run)

- 46 claim×evidence pairs evaluated (`[Evidence Eligibility]` lines).
- 18 counted `eligible=True` (authority path, source-level — matches
  `YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md` §6's figure exactly).
- 7 pairs hit `reason=directness_strong` (directness≥0.60), all `source_class=unknown`.
- Of those 7, **6 were actually counted via `via=directness` in `[Claim Support Decision]`** — the
  7th (`cl_26fc0420`/`ev_f2ee32ad`, directness=0.614 at PASS1) is fully explained, not a gap: by
  the time `_counts_toward_status` ran (post-PASS2 mapper re-link), the mapper had re-assigned
  `ev_f2ee32ad` to a *different* claim (`cl_d82ac29f`), against whose text its directness dropped
  to 0.525 (below threshold) — meanwhile `cl_26fc0420`'s own top-linked evidence became a
  *different*, higher-directness item (`ev_91daec1e`, 0.701) that *did* count. Directness is
  computed per (claim, evidence) pair, so a re-link changes which pairing is scored — this is
  consistent behavior, not lost wiring. (This re-link instability is itself downstream of the
  PASS2-scope bug fixed in PART A of this same task — with that fix landed, claims already
  resolved at PASS1 no longer get re-mapped at all, which should make this specific kind of
  pairing churn rarer for resolved claims; it does not apply to `cl_26fc0420`, which was never
  resolved at PASS1 in this run, so this particular case is unaffected by the PART A fix.)
- **Effect on claim status**: `cl_8f195b39` (claim #6, "limited evidence — esophageal cancer") is
  counted `supports` via directness (two independent evidence items, `ev_d3f9021e` 0.672 and
  `ev_f510567b` 0.680) — this is one of the **3 claims** in the final `[Claim Status Gate]
  supported=3` count. Without the directness path, `supported` would be lower (a stricter/more
  UNVERIFIED-leaning outcome) — i.e. directness is currently making Trust *slightly less
  pessimistic* than authority-only would, exactly as the P0-F design intended (rescuing
  genuinely-relevant unknown-class evidence from an authority threshold that is mathematically
  unreachable for that class, per the recovered audit's own math).
- **Effect on canonical Trust**: none beyond the above — canonical Trust for this run was
  UNVERIFIED regardless (driven by `unverified=8/12` and the mixed-certainty gate from the
  PRE-PUSH GATE session's Blocker-3 fix), and removing the directness path would not have changed
  the final UNVERIFIED label, only the internal supported/unverified split.

### Live counts, "leaves" benchmark (`live_run_leaves.log`, 382.81s run — the diagnostic probe)

- 20 claim×evidence pairs evaluated (`[Evidence Eligibility]` lines) — far fewer than coffee's 46.
- 2 counted `eligible=True` (authority path; `reason=authority_eligible`).
- 2 pairs hit `reason=directness_strong`.
- `[Claim Support Decision]`: 1 relation counted via `authority` (`relation=supports`, this is the
  run's one `supported` claim) and 1 counted via `directness` — but that relation's NLI verdict was
  `relation=uncertain`, so per `_distinct_cluster_count` (only `supports`/`contradicts` relations
  move the counter) it does not add to `supported`. Net effect of directness on this run: **zero**
  — mechanically active, but the specific relation it rescued from the authority gate was itself
  `uncertain`, not `supports`.

**Answer to task item 8** ("Почему листья → UNVERIFIED — связано ли с eligibility gap?"): **No.**
The directness/eligibility mechanism behaved identically in both runs (same code, same thresholds,
same via=authority/via=directness split logic) — the difference is that the leaves query's web
search surfaced a much thinner, lower-quality candidate pool (20 pairs vs. 46; only 2 non-unknown
sources found at all, vs. coffee's much stronger IARC/PMC/NCBI direct sources) and the ONE
directness-rescued relation happened to have an `uncertain` NLI verdict rather than `supports`. The
UNVERIFIED outcome for "leaves" traces to retrieval/evidence *volume and quality* for that specific
query (a `YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md` §17 concern — search coverage for
uncontroversial topics — not an eligibility-wiring defect). Per the task's explicit instruction,
"leaves" itself was not separately fixed — this is a diagnostic probe result, not a patch target.

---

## 4. Recommendation

**CLOSED — NOT A BUG.** No production semantics were changed as part of this review (per the
task's explicit instruction). The apparent gap identified in the P0 performance audit (§14) is
resolved: it was an incomplete trace of the call graph (stopping at `mapping.py`'s
`candidate_sources` construction, before reaching `status.py`'s separate `_counts_toward_status`),
not a real code/doc mismatch. The actual mechanism matches its own design documentation exactly,
field for field, and is demonstrably active in production, with a real, currently-live effect on
claim status (the `cl_8f195b39` supported-claim case above).

One genuine, minor, non-blocking observation for a future pass (not this one): `_claim_has_effective_evidence()`
(`agent/orchestrator/claims/retrieval.py:36-48`, the PASS1→PASS2 routing gate) checks only the
authority path (`evidence_role=="direct" and evidence_eligible is True`), not the directness path —
so a claim that would ultimately be counted `supported`/`contradicted` via directness is still
routed into claim-specific retrieval as if unresolved. This is conservative, not unsafe (it causes
some extra retrieval work, never a false resolution), and is explicitly out of scope for this
review (§9 of the performance audit's "НЕ ТРОГАТЬ ПОКА" list covers routing/priority mechanics)
— noted here only so it isn't lost, not acted on.
