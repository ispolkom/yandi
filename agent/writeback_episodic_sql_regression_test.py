#!/usr/bin/env python3
"""
Regression: production writeback must persist YANDI's lived query
episodes through SQL-backed EpisodicMemory, not agent/dataset/*.jsonl.

Run:
  python -m agent.writeback_episodic_sql_regression_test
"""

from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
SRC = BASE / "agent" / "orchestrator" / "response" / "writeback.py"


def main() -> int:
    src = SRC.read_text(encoding="utf-8")
    checks = [
        ("imports SQL-backed episodic memory", "from agent.memory_episodic import get_memory as get_episodic_memory" in src),
        ("does not import dataset builder", "from agent.dataset_builder import get_dataset_builder" not in src),
        ("writes episode via EpisodicMemory.add", "episodic_memory.add(" in src),
        ("does not append via dataset_builder.record_episode", "dataset_builder.record_episode(" not in src),
        ("preserves trace id in SQL episode details", '"trace_id": trace_id' in src),
        ("stores canonical trust in SQL episode details", '"trust": _canonical_trust_for_learning' in src),
    ]
    failed = 0
    for label, ok in checks:
        print(("OK   " if ok else "FAIL ") + label)
        failed += 0 if ok else 1
    if failed:
        raise SystemExit(1)
    print("все проверки пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
