"""
agent/dataset_builder_regression_test.py — Foundation Repair regression:
episode<->trace identity (YANDI_SELF_LEARNING_RECONCILIATION_AUDIT.md
section "Episode <-> Trace identity").

Covers agent/dataset_builder.py::DatasetBuilder.record_episode(): before
this fix, episodes had no id embedded in the persisted record (the
generated "ep_..." id was computed AFTER the record was written and
returned but never stored), and no reference to the trace that produced
them (join only possible via unreliable timestamp proximity, proven ~309s
drift on a real pair in the audit). Writes to a temp file, never touches
the real agent/dataset/episodes_*.jsonl.

Run: /home/iam/venv/bin/python3 -m agent.dataset_builder_regression_test
"""
import json
import tempfile
from pathlib import Path

from agent.dataset_builder import DatasetBuilder

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


def _isolated_builder(tmp_path: Path) -> DatasetBuilder:
    db = DatasetBuilder()
    db.file_path = tmp_path  # redirect away from the real dataset dir
    return db


def _read_records(tmp_path: Path):
    with open(tmp_path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


with tempfile.TemporaryDirectory() as tmp_dir:
    tmp_file = Path(tmp_dir) / "episodes_test.jsonl"

    db = _isolated_builder(tmp_file)
    returned_id = db.record_episode({"trace_id": "trace_1234_abcd", "query": "q1"})
    records = _read_records(tmp_file)

    check(
        "record_episode returns a non-empty episode id",
        bool(returned_id),
        f"returned={returned_id!r}",
    )
    check(
        "the persisted record embeds the SAME episode_id that was returned "
        "(previously: computed after the write, never stored)",
        records and records[-1].get("episode_id") == returned_id,
        f"records={records}",
    )
    check(
        "the caller-supplied trace_id passes through unchanged into the "
        "persisted record (reuses existing Trace identity, no parallel "
        "identity system)",
        records and records[-1].get("trace_id") == "trace_1234_abcd",
        f"records={records}",
    )

    db2 = _isolated_builder(tmp_file)
    id_a = db2.record_episode({"trace_id": "trace_A", "query": "qa"})
    id_b = db2.record_episode({"trace_id": "trace_B", "query": "qb"})
    check(
        "two episodes recorded in sequence get distinct episode_ids",
        id_a != id_b,
        f"a={id_a} b={id_b}",
    )
    records2 = _read_records(tmp_file)
    check(
        "both records are persisted (append, not overwrite) with their "
        "own trace_id preserved",
        len(records2) == 3
        and records2[1].get("trace_id") == "trace_A"
        and records2[2].get("trace_id") == "trace_B",
        f"records={records2}",
    )

    db3 = _isolated_builder(tmp_file)
    id_noref = db3.record_episode({"query": "no trace ref"})
    records3 = _read_records(tmp_file)
    check(
        "record_episode still works with no trace_id supplied (optional, "
        "not a required/enforced parameter — callers that legitimately "
        "have no trace are not broken)",
        records3[-1].get("episode_id") == id_noref
        and "trace_id" not in records3[-1],
        f"last={records3[-1]}",
    )

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
