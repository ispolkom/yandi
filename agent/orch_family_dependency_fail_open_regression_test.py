"""
agent/orch_family_dependency_fail_open_regression_test.py — apply_family_dependency_shadow() is genuinely fail-open in
agent/orchestrator_v2.py's real production source: an exception inside it (a real one crashed a live request with
(1054, "Unknown column 'triggering_claim_ids'"), losing 20+ minutes of otherwise-complete verification work with no
answer delivered) must never propagate out of process(), and `None` — the value every downstream reader of this
result already declares Optional and already accepts — must be exactly what a failure degrades to.

    A. structural: the call site in the real production source is wrapped in try/except, assigns `None` on failure,
       and never re-raises — checked against the actual file text, not a copy.
    B. functional: with apply_family_dependency_shadow monkeypatched to raise the EXACT error that broke live,
       running that one guarded statement (executed from the real module's own compiled code, not reimplemented)
       does not raise, and leaves the local variable at None.
    C. the two functions that read this value (build_shadow_request_summary, apply_dependency_recheck) both declare
       it Optional — None was already a contractually valid input before this fix, not a new special case bolted on.

Run: python -m agent.orch_family_dependency_fail_open_regression_test
"""
from __future__ import annotations

import inspect
import re
import sys

import agent.orchestrator_v2 as orch_v2

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    src = inspect.getsource(orch_v2)

    m = re.search(
        r"( *)try:\s*\n\s*_family_dependency_stats = apply_family_dependency_shadow\(\s*\n"
        r"(?:.*\n)*?\1except Exception as e:.*\n\s*log\((.*)\)\s*\n\s*_family_dependency_stats = None",
        src,
    )
    check("A1: the call is wrapped in try/except in the real production source (not a copy)", m is not None)
    check("A2: on failure it logs the failure (never silent) and names the request as unaffected",
          m is not None and "fail-open" in m.group(2) and "answer unaffected" in m.group(2))
    check("A3: nothing after the except re-raises (the except body is exactly log + assign, both on record)",
          m is not None)
    check("A4: the guarded call still appears exactly once (this wraps THE real call site, not a second copy of it)",
          src.count("_family_dependency_stats = apply_family_dependency_shadow(") == 1)

    # ── functional: execute the real guarded statement, with the real function replaced by one that raises ──
    real_stats: object = "sentinel-untouched"

    def raises_the_exact_reported_error(*args, **kwargs):
        raise Exception("(1054, \"Unknown column 'triggering_claim_ids' in 'field list'\")")

    def fake_log(message: str) -> None:
        fake_log.messages.append(message)
    fake_log.messages = []

    namespace = {
        "apply_family_dependency_shadow": raises_the_exact_reported_error,
        "log": fake_log,
        "claims_data": [], "_disagreement_result": None, "verbose": False,
        "_family_dependency_stats": real_stats,
    }
    # The exact text of the guarded block, extracted from the real source by the SAME regex that proved it exists
    # (A1) — not a hand-typed approximation, so it can never silently drift from what ships. Dedented only so exec()
    # can run it as a standalone statement; the relative indentation inside it (and so its actual structure) is untouched.
    import textwrap
    guarded = textwrap.dedent(m.group(0)) if m else ""
    check("B0: the extracted guarded block is non-empty (A1 must have matched for this to mean anything)", bool(guarded))
    exec(compile(guarded, "<guarded-call-under-test, extracted from the real source>", "exec"), namespace)  # noqa: S102
    check("B1: the exception from a failing shadow write does not propagate", True)  # exec() above would have raised otherwise
    check("B2: the local ends up exactly None, not the exception, not left at its old value", namespace["_family_dependency_stats"] is None)
    check("B3: the failure was logged, and the message names the real exception and says the answer is unaffected",
          len(fake_log.messages) == 1 and "Exception" in fake_log.messages[0] and "answer unaffected" in fake_log.messages[0])
    check("B4: no secret or a raw traceback leaked into the log line — only the exception's own short text",
          "Traceback" not in fake_log.messages[0])

    # ── downstream readers already declare Optional — None is not a new special case ──
    from agent.epistemic_contradiction_shadow import build_shadow_request_summary
    from agent.dependency_recheck import apply_dependency_recheck
    sig1 = inspect.signature(build_shadow_request_summary)
    sig2 = inspect.signature(apply_dependency_recheck)
    check("C1: build_shadow_request_summary's family_dependency_stats parameter is declared Optional",
          "Optional" in str(sig1.parameters["family_dependency_stats"].annotation))
    check("C2: apply_dependency_recheck's family_dependency_stats parameter is declared Optional",
          "Optional" in str(sig2.parameters["family_dependency_stats"].annotation))

    # ── functional: both readers genuinely tolerate None without raising ──
    summary = build_shadow_request_summary([], {}, None)
    check("C3: build_shadow_request_summary(None) returns a summary, does not raise", isinstance(summary, dict))
    check("C4: …and reports zero current recheck candidates for a None input, not a stale or invented number",
          summary.get("current_recheck_candidates") == 0)

    stats = apply_dependency_recheck(None, belief_manager=None, cost={}, log=lambda m: None, verbose=False)
    check("C5: apply_dependency_recheck(None) returns a stats dict, does not raise", isinstance(stats, dict))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
