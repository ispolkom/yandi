"""
agent/answer_delivery_persistence_regression_test.py — P0 (storage audit):
DELIVERED ANSWER == PERSISTED ANSWER.

Storage audit finding: `trace.final_answer` (agent/orchestrator/response/
writeback.py, set from `synthesis_result.answer` before optimistic
banner/badge/source-list wrapping) is NOT the literal text
`OrchestratorResponse.answer` actually returns to the caller — at least
5 divergent copies of "the answer" existed across the pipeline. Fix:
`trace.add_observation("delivered_answer_text", optimistic.text)` now
runs AFTER the banner/bad_state_prefix block and BEFORE `tracer.
save_trace(trace)`, so the persisted trace can finally answer "what did
the user literally see" without reconstructing it from decoration logic.

This suite proves:
    A. delivered_answer_text (in-memory, on `trace`) == OrchestratorResponse.answer
    B. delivered_answer_text, ROUND-TRIPPED through the real JSONL save/read
       path, still == OrchestratorResponse.answer (not just true in memory
       before serialization)
    C. trace.final_answer (the OLD field) genuinely differs from the new
       delivered_answer_text when a banner/source-list is present — proving
       this is an ADDITIVE fix, not a duplicate of an existing field
    D. KNOWN LIMITATION (found during this investigation, explicitly NOT
       fixed in this pass — out of scope for a persistence-correctness
       fix, see the final report): the trust badge baked into the
       delivered text can still show a stale trust label relative to the
       final `OrchestratorResponse.trust_level`, because responder.respond()
       runs before this function's own reflection-mistake downgrade and
       canonical-Trust cutover. Encoded here as a known-gap assertion (same
       pattern this codebase already uses for other documented gaps) so a
       future fix will make this specific test fail and force a conscious
       update, not silently regress.

Run: /home/iam/venv/bin/python3 -m agent.answer_delivery_persistence_regression_test
"""
from __future__ import annotations

import json
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import agent.orch_tracer as ot
import agent.orchestrator.response.writeback as wb
from agent.orch_schemas import SynthesisResult

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def _noop_log(*a, **k):
    pass


def _make_epistemic_result(**overrides):
    defaults = dict(
        domain="factual",
        testability="testable",
        answer_mode="direct",
        is_science_as_model=False,
        should_use_web=True,
        reason="",
        objectivity_score=0.5,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _run(
    *,
    trace_id: str,
    traces_dir: Path,
    answer_text: str,
    sources,
    trust_level: str,
    epistemic_trust_gate_label: str,
    epistemic_kwargs=None,
    bad_state_prefix: str = "",
):
    synthesis_result = SynthesisResult(
        answer=answer_text, confidence=0.6, sources=sources, trust_level=trust_level,
    )
    epistemic_result = _make_epistemic_result(**(epistemic_kwargs or {}))
    trace = ot.Trace(trace_id=trace_id, timestamp=0.0, query="q")
    tracer = ot.DecisionTracer()

    with patch.object(ot, "TRACES_DIR", traces_dir), \
         patch.object(wb, "archive_query", lambda *a, **k: None), \
         patch.object(wb, "mon_record", lambda *a, **k: None):
        resp = wb.run_optimistic_respond(
            request=types.SimpleNamespace(session_id="s1"),
            verbose=False,
            enable_validation=False,
            enable_cache=False,
            t_start=0.0,
            query_frame={},
            log=_noop_log,
            trace=trace,
            trace_id=trace_id,
            decision_id="d1",
            cost={"total_ms": 0.0},
            cache=None,
            request_fetch_cache=None,
            query_to_use="Сколько спутников известно у Юпитера?",
            skip_rag=False,
            is_subjective_answer=False,
            epistemic_result=epistemic_result,
            synthesis_result=synthesis_result,
            risk_result=None,
            intent_result=types.SimpleNamespace(intent="science"),
            search_result=None,
            web_used=True,
            claims_data=[],
            evidence_data=[],
            self_model=None,
            memory=None,
            reflection=None,
            motivation=None,
            core_loop=None,
            reasoning_info={},
            intent_type="science",
            intent_confidence=0.8,
            bad_state_prefix=bad_state_prefix,
            entity=None,
            enrich_result=None,
            tracer=tracer,
            epistemic_trust_gate_label=epistemic_trust_gate_label,
        )

    return resp, trace


# ============================================================
# A/B/C: delivered_answer_text == OrchestratorResponse.answer,
# both in-memory and round-tripped through the real JSONL save.
# ============================================================

traces_dir = Path(tempfile.mkdtemp(prefix="p0_wb_"))
resp, trace = _run(
    trace_id="t_wb_main",
    traces_dir=traces_dir,
    answer_text="У Юпитера известно 95 подтверждённых спутников.",
    sources=["https://science.nasa.gov/jupiter/jupiter-moons/"],
    trust_level="VERIFIED",
    epistemic_trust_gate_label="VERIFIED",
)

check(
    "A: delivered_answer_text (in-memory observation) == OrchestratorResponse.answer",
    trace._observations.get("delivered_answer_text") == resp.answer,
    f"observation={trace._observations.get('delivered_answer_text')!r} resp={resp.answer!r}",
)

saved_line_path = next(traces_dir.glob("*.jsonl"))
with saved_line_path.open("r", encoding="utf-8") as f:
    saved = json.loads(f.readline())

check(
    "B: delivered_answer_text ROUND-TRIPPED through real save_trace()/JSONL == OrchestratorResponse.answer",
    saved.get("observations", {}).get("delivered_answer_text") == resp.answer,
    f"persisted={saved.get('observations', {}).get('delivered_answer_text')!r} resp={resp.answer!r}",
)

check(
    "C: trace.final_answer (OLD field, pre-wrapping) genuinely differs from delivered_answer_text "
    "(proves this is additive, not a duplicate field) — delivered text carries the banner+source list",
    saved.get("final_answer") != resp.answer
    and "Источники:" in resp.answer
    and "Источники:" not in saved.get("final_answer", ""),
    f"final_answer={saved.get('final_answer')!r} delivered={resp.answer!r}",
)

check(
    "sanity: delivered text actually contains a banner line (proves the reordered banner-wrap ran "
    "before the observation was captured, not after)",
    resp.answer.startswith("[") and "\n\n" in resp.answer,
    f"{resp.answer!r}",
)

# ============================================================
# D: KNOWN LIMITATION — trust badge staleness. NOT fixed in this pass
# (out of scope: fixing it safely requires reordering responder.respond()
# relative to the reflection-mistake downgrade / canonical-Trust cutover,
# which risks changing background-validation kickoff timing — a genuine
# ambiguity flagged for a separate, deliberate decision, not silently
# resolved here). This assertion documents the CURRENT behavior so a
# future fix breaks this test on purpose, not by accident.
# ============================================================

traces_dir_d = Path(tempfile.mkdtemp(prefix="p0_wb_badge_"))
resp_d, trace_d = _run(
    trace_id="t_wb_badge",
    traces_dir=traces_dir_d,
    answer_text="Компания Apple была основана в 1976 году.",
    sources=[],
    trust_level="VERIFIED",                 # strand 1: badge baked in as "✅ Проверено"
    epistemic_trust_gate_label="UNVERIFIED",  # strand 2: forces canonical MIN-reconciliation down
)

check(
    "D (KNOWN LIMITATION, not fixed): OrchestratorResponse.trust_level reflects the FINAL canonical "
    "value (downgraded to UNVERIFIED)...",
    resp_d.trust_level == "UNVERIFIED",
    f"{resp_d.trust_level}",
)
check(
    "D (KNOWN LIMITATION, not fixed): ...but the trust badge baked into the delivered TEXT still shows "
    "the STALE pre-cutover label ('✅ Проверено' for VERIFIED) — a real, documented, separate bug from "
    "the persistence-equivalence fix this suite otherwise proves",
    "✅ Проверено" in resp_d.answer,
    f"{resp_d.answer!r}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
