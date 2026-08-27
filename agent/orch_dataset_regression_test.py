"""
agent/orch_dataset_regression_test.py — Foundation Repair P1 regression:
orch_dataset.py's trace-to-SFT pipeline schema fix
(YANDI_SELF_LEARNING_RECONCILIATION_AUDIT.md "Dataset readiness").

Root cause covered here: _filter()/_dedup()/_to_chatml()/stats()/review()
previously referenced a trace schema ("task"/"skill"/"model"/"messages"/
"result"/"outcome"-as-string/"quality"/"ts"/"task_type") that the real
producer (agent/orch_tracer.py) never writes, and stats()/review() called
OrchestratorTracer().stats()/.tail() — a no-op stub with neither method,
guaranteed AttributeError. 0/434 real traces ever passed the filter,
silently; stats/review crashed outright.

Uses synthetic in-memory trace dicts shaped like real persisted traces
(verified against a live trace during the fix) and a temp directory for
the file-reading paths — never touches the real registry/dataset/orch_traces/
or registry/dataset/orch_sft/ directories.

Run: /home/iam/venv/bin/python3 -m agent.orch_dataset_regression_test
"""
import json
import tempfile
from pathlib import Path

import agent.orch_dataset as orch_dataset_mod
from agent.orch_dataset import OrchDatasetBuilder

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


def _trace(trust_label="STRONGLY_SUPPORTED", trust_score=0.3, answer_len=200,
           domain="factual", query="test query", trace_id="trace_x"):
    return {
        "trace_id": trace_id,
        "query": query,
        "trust": trust_label,
        "timestamp": 1787830000.0,
        "epistemic": {"domain": domain},
        "outcome": {
            "trust_label": trust_label,
            "trust_score": trust_score,
            "coverage_ratio": 0.5,
            "final_answer": "x" * answer_len,
        },
        "claims_filtered_count": 3,
        "claims_rejected_count": 0,
    }


builder = OrchDatasetBuilder()

# ── _filter(): real-schema field checks ──
strongly = _trace(trust_label="STRONGLY_SUPPORTED")
partially = _trace(trust_label="PARTIALLY_SUPPORTED")
weakly = _trace(trust_label="WEAKLY_SUPPORTED")
unverified = _trace(trust_label="UNVERIFIED")
too_short = _trace(trust_label="STRONGLY_SUPPORTED", answer_len=10)
old_shape = {"outcome": "success", "quality": 0.9, "result": "x" * 200, "task": "old", "skill": "search"}

filtered = builder._filter([strongly, partially, weakly, unverified, too_short, old_shape])
check(
    "STRONGLY_SUPPORTED and PARTIALLY_SUPPORTED traces pass the filter",
    strongly in filtered and partially in filtered,
    f"filtered={filtered}",
)
check(
    "WEAKLY_SUPPORTED and UNVERIFIED traces do NOT pass (not good SFT material)",
    weakly not in filtered and unverified not in filtered,
    "",
)
check(
    "an answer shorter than MIN_RESULT_LEN is rejected even with a good trust label",
    too_short not in filtered,
    "",
)
check(
    "a trace shaped like the OLD (never-real) schema does not pass "
    "(trust missing -> defaults to UNVERIFIED, correctly rejected, not "
    "silently accepted via stale field names)",
    old_shape not in filtered,
    "",
)
check(
    "exactly the 2 good-trust, long-enough traces pass, nothing else",
    len(filtered) == 2,
    f"len={len(filtered)}",
)

# ── _dedup(): keys on the real "query" field, not "task" ──
dup_a = _trace(query="одинаковый по смыслу запрос про планеты солнечной системы")
dup_b = _trace(query="одинаковый по смыслу запрос про планеты солнечной системы")
distinct = _trace(query="совершенно другой вопрос про биологию клетки")
deduped = builder._dedup([dup_a, dup_b, distinct])
check(
    "near-identical queries are deduplicated (Jaccard on the real 'query' "
    "field, not the nonexistent 'task' field)",
    len(deduped) == 2 and dup_a in deduped and distinct in deduped,
    f"deduped_len={len(deduped)}",
)

# ── _to_chatml(): produces the messages/quality shape orch_finetune.py reads ──
row = builder._to_chatml(strongly)
check(
    "_to_chatml produces a 2-turn messages list orch_finetune.py can read "
    "(user query, assistant final_answer)",
    row["messages"] == [
        {"role": "user", "content": "test query"},
        {"role": "assistant", "content": "x" * 200},
    ],
    f"row={row}",
)
check(
    "_to_chatml's quality/outcome/domain/trace_id are sourced from the "
    "real nested fields, not the old flat ones",
    row["quality"] == 0.3 and row["outcome"] == "STRONGLY_SUPPORTED"
    and row["domain"] == "factual" and row["trace_id"] == "trace_x",
    f"row={row}",
)

# ── stats()/review(): must not crash (previously: guaranteed AttributeError
#    via the OrchestratorTracer stub) and must reflect real data ──
with tempfile.TemporaryDirectory() as tmp_dir:
    tmp_traces_dir = Path(tmp_dir) / "orch_traces"
    tmp_traces_dir.mkdir()
    tmp_sft_dir = Path(tmp_dir) / "orch_sft"
    tmp_sft_dir.mkdir()

    with open(tmp_traces_dir / "20260827.jsonl", "w", encoding="utf-8") as f:
        for t in [strongly, partially, weakly]:
            f.write(json.dumps(t) + "\n")

    _orig_traces_dir = orch_dataset_mod.TRACES_DIR
    _orig_sft_dir = orch_dataset_mod.SFT_DIR
    orch_dataset_mod.TRACES_DIR = tmp_traces_dir
    orch_dataset_mod.SFT_DIR = tmp_sft_dir
    try:
        isolated_builder = OrchDatasetBuilder()
        try:
            st = isolated_builder.stats()
            stats_ok = (
                st["traces"]["total"] == 3
                and abs(st["traces"]["success_rate"] - 2 / 3) < 1e-3
                and st["targets"]["orchestrator"]["have"] == 3
            )
            stats_err = ""
        except Exception as e:
            stats_ok = False
            stats_err = repr(e)
        check(
            "stats() runs without crashing and reports real total/success_rate "
            "(previously: unconditional AttributeError via the "
            "OrchestratorTracer stub, regardless of data)",
            stats_ok,
            stats_err or f"st={st if stats_ok else None}",
        )

        try:
            isolated_builder.review(2)
            review_ok = True
            review_err = ""
        except Exception as e:
            review_ok = False
            review_err = repr(e)
        check(
            "review() runs without crashing on real-shaped traces "
            "(previously: unconditional AttributeError)",
            review_ok,
            review_err,
        )
    finally:
        orch_dataset_mod.TRACES_DIR = _orig_traces_dir
        orch_dataset_mod.SFT_DIR = _orig_sft_dir

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
