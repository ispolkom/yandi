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
