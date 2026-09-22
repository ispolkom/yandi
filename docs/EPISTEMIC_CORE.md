# Epistemic core

```text
TRUST != TRUTH
```

A source, a model, or a previous conclusion is not true because it is trusted or because the
system produced it. The orchestrator therefore turns an answer into checkable **claims** and asks
what supports each one.

## Pipeline (orchestrator)

```text
question → claims → evidence retrieval → claim/evidence validation → synthesis → trust label → write-back
```

| Stage | Where |
|---|---|
| Claim extraction, identity and families | `agent/claim_*.py`, `agent/orchestrator/claims/` |
| Evidence retrieval and mapping | `agent/claim_evidence_*.py`, `agent/orchestrator/claims/retrieval.py` |
| Validation and contradiction checks | `agent/claim_validator.py`, `agent/contrarian_check.py`, `agent/epistemic_contradiction_shadow.py` |
| Synthesis and trust | `agent/orch_synthesizer.py`, `agent/orchestrator/epistemic/` |
| Beliefs and their history | `agent/belief_manager.py` |
| Reflection on mistakes | `agent/reflection_loop.py` |

## Evidence and source independence

Support is counted from **independent** sources, not from the number of pages repeating one origin;
source clustering and independence are part of the pipeline. "No evidence found" is recorded as
such and is not equal to "supported" (`UNKNOWN != SUPPORTED`).

## Trust labels

Answers carry a label such as `UNVERIFIED`, `WEAKLY_SUPPORTED`, `PARTIALLY_SUPPORTED`,
`STRONGLY_SUPPORTED`. Gates lower a label when claim coverage or evidence grounding is
insufficient, or when a core claim of an existence question is unverified. Labels are downgraded by
gates; they are not raised by confidence of tone.

**Current state:** two trust computations exist in the live path. The value the user sees comes from
the synthesizer strand with downgrade-only gates; a stricter "trust gate" strand exists alongside it.
`agent/orchestrator/epistemic/canonical_trust.py` defines a canonical trust in **shadow mode only**
and records it (for example in episodes) without changing what the user sees. Unifying them is open
work.

## Beliefs

`belief_manager` stores confidence with evidence for and against, contradiction score, history and
decay. A belief can change when new evidence arrives, and the history of why it changed is kept in
SQL (`belief`, `belief_assessment_history`).

## Reflection

The reflection loop reviews the agent's own decisions, derives lessons and policies from mistakes,
and stores active policies in SQL (`reflection_policy`) for the planner to apply. How much of this
reaches the personal chat path is limited today.

## What is deliberately not claimed

- The system does not claim its claims are true, only how well they are supported and by what.
- Shadow-mode components do not change user-visible output.

## Reusing evidence by meaning, not only by near-identical wording (2026-09)

Before this pass, `agent/verification_memory.py`'s LOAD path (one of five evidence channels — local memory
alongside web/nodes/AI-chats/local-model) only reused a prior verification's evidence when the new claim's text
matched a past one **exactly** (after normalization). A rephrased question about the same thing ("сколько стоит
сруб" vs "какая цена у сруба") was treated as entirely new.

**Now**: when the exact match finds nothing and a `domain` is given, the LOAD path also tries the **semantic
family** the current claim belongs to — the same embedding-prefilter-then-LLM-judge matching
(`agent.claim_family_registry.ClaimFamilyRegistry.find_or_link_claim`) the codebase already uses elsewhere to
decide whether two differently-worded claims mean the same thing, not a new comparator.

What is unchanged, on purpose:

* **Never a blind copy.** Reused evidence — found by either path — is fed into the exact same Mapper → NLI every
  fresh claim goes through; the historical verdict (`relation`) is never carried over. A claim reused by meaning
  is re-verified exactly as strictly as one reused by exact text, or one with no history at all.
* **Append-only.** A claim occurrence is a row that is never edited; reuse creates a new occurrence, never rewrites
  an old one. History accumulates; nothing is overwritten.
* **Fails open, not silently.** A failure inside the family lookup (the classifier itself erroring) is caught and
  logged — it degrades to "no memory hit", never to breaking verification.

Separately, and already live before this pass: `agent/claim_history_note.py` appends a short, deterministic (never
LLM-authored) note to the delivered answer — "🕰️ Память о прошлых проверках" — when this request's own fresh
verification of a claim agrees or disagrees with the most recent prior verification of the same semantic family.
That mechanism only ever runs on THIS request's own real, freshly-checked claims (never a cached/short-circuited
answer) — see its own module docstring for the exact structural guarantee.

Cost, honestly stated: the family classifier is not free (one embedding + LLM-judge call). It is only tried when
the cheap exact match already failed, and `find_or_link_claim` is idempotent (linking the same claim twice is a
safe no-op), so the modest duplicate cost with the later, separate `assign_claim_family_identity()` call is a
known, accepted trade — not a bug.
