"""
agent/orch_reputation_relationship_regression_test.py

Proves the LOVE/HATE relationship state machine added to
orch_reputation.py (owner mandate, 2026-09-13: relationship memory
needs asymmetric inertia — betrayal is remembered longer than it took
to cause, and redemption costs more than the betrayal did).

Runs against a temporary DB/log file (monkeypatches orch_reputation's
module-level DB_FILE/LOG_FILE) — orch_reputation.py itself has no env
var override for its storage location (a real, separate gap, out of
scope here), so this test must redirect those globals itself rather
than touching the real registry/nodes/reputation.db.

Запуск: python3 -m agent.orch_reputation_relationship_regression_test
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        import agent.orch_reputation as rep

        rep.DB_FILE = Path(tmp) / "reputation.db"
        rep.LOG_FILE = Path(tmp) / "reputation_log.jsonl"

        # ── Pure transition-table checks (no I/O) ──
        check(
            "neutral stays neutral below both thresholds",
            rep._evaluate_transition(rep.REL_NEUTRAL, 1.0, 1.0) == rep.REL_NEUTRAL,
        )
        check(
            "neutral -> love once love threshold is crossed",
            rep._evaluate_transition(rep.REL_NEUTRAL, rep.LOVE_ENTER_THRESHOLD, 0.0) == rep.REL_LOVE,
        )
        check(
            "neutral -> hate once hate threshold is crossed",
            rep._evaluate_transition(rep.REL_NEUTRAL, 0.0, rep.HATE_ENTER_THRESHOLD) == rep.REL_HATE,
        )
        check(
            "hate threshold is strictly lower than love threshold (quick to distrust, slow to trust)",
            rep.HATE_ENTER_THRESHOLD < rep.LOVE_ENTER_THRESHOLD,
        )
        check(
            "love -> hate needs LESS hate evidence than neutral -> hate (betrayal from someone loved cuts deeper)",
            rep._evaluate_transition(rep.REL_LOVE, 0.0, rep.HATE_ENTER_THRESHOLD * rep.LOVE_BETRAYAL_MULTIPLIER) == rep.REL_HATE
            and rep.LOVE_BETRAYAL_MULTIPLIER < 1.0,
        )
        check(
            "love stays love below the (lowered) betrayal threshold",
            rep._evaluate_transition(rep.REL_LOVE, 0.0, rep.HATE_ENTER_THRESHOLD * rep.LOVE_BETRAYAL_MULTIPLIER * 0.9) == rep.REL_LOVE,
        )
        check(
            "hate never softens straight to love, only to neutral",
            rep._evaluate_transition(rep.REL_HATE, rep.HATE_EXIT_THRESHOLD, 0.0) == rep.REL_NEUTRAL,
        )
        check(
            "hate exit threshold is strictly harder than hate entry threshold (redemption costs more than betrayal did)",
            rep.HATE_EXIT_THRESHOLD > rep.HATE_ENTER_THRESHOLD,
        )
        check(
            "hate does not release early, below its exit threshold",
            rep._evaluate_transition(rep.REL_HATE, rep.HATE_EXIT_THRESHOLD * 0.5, 0.0) == rep.REL_HATE,
        )

        # ── Full integration: a real run of severe violations reaches HATE ──
        node = "node-betrayer"
        for _ in range(3):
            result = rep.record_severe_violation(node, reason="proven deception in a validated claim")
        check("three severe violations (4.0 each) are enough to reach HATE", result["state"] == rep.REL_HATE, f"got {result}")

        # A single more positive interaction must NOT instantly forgive HATE.
        after_one_good = rep.record_relationship_signal(node, positive=True, severity=1.0)
        check("a single good interaction does not undo HATE", after_one_good["state"] == rep.REL_HATE, f"got {after_one_good}")

        # Enough accumulated good behavior eventually earns release back to NEUTRAL — never straight to LOVE.
        result = None
        for _ in range(40):
            result = rep.record_relationship_signal(node, positive=True, severity=1.0)
            if result["state"] != rep.REL_HATE:
                break
        check("sustained good behavior eventually releases HATE back to NEUTRAL (not LOVE)", result["state"] == rep.REL_NEUTRAL, f"got {result}")

        # ── Full integration: ordinary good interactions can earn LOVE, slowly ──
        good_node = "node-reliable"
        result = None
        for _ in range(60):
            result = rep.update_node(good_node, correct=True, latency=1.0, domain="general")
            r = rep.get_relationship(good_node)
            if r["state"] == rep.REL_LOVE:
                result = r
                break
        check("sustained ordinary correct answers can eventually earn LOVE", result is not None and result.get("state") == rep.REL_LOVE, f"got {result}")

        # ── A node that's never interacted has a clean, neutral default ──
        fresh = rep.get_relationship("never-seen-node")
        check("an unknown node defaults to NEUTRAL with zero scores", fresh == {"node_id": "never-seen-node", "state": rep.REL_NEUTRAL, "love_score": 0.0, "hate_score": 0.0, "entered_at": None}, f"got {fresh}")

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
    else:
        print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
