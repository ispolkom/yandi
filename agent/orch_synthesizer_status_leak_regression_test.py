"""
agent/orch_synthesizer_status_leak_regression_test.py — regression for a
correctness bug found live (pasted real production log, PRE-PUSH GATE
review): the hypothesis-mode synthesis prompt instructed the LLM to
self-report its own "СТАТУС: Степень поддержки гипотезы (WEAKLY/
PARTIALLY/STRONGLY_SUPPORTED)" as a numbered section INSIDE the answer
body text, generated during synthesize() - before claim extraction,
evidence eligibility, trust_gate, the canonical Trust cutover, or
reflection ever run. This is a second, ungrounded, LLM-invented "Trust"
baked directly into the rendered answer, independent of and able to
diverge from the real canonical Trust shown in the response footer.

Observed live: rendered body said "СТАТУС ... PARTIALLY_SUPPORTED" while
the same response's footer correctly said "Trust: UNVERIFIED" (canonical,
post-reflection). Two different trust-like values in one final answer -
exactly the "second source of truth" pattern this session's Foundation
Repair (P0-2) and PET boundary refactor (Phase 4) both removed elsewhere;
this is the same class of bug in a place neither pass looked at (the
synthesis prompt template itself, not a code path).

Fix: removed the "6. СТАТУС" instruction from _HYPOTHESIS_FIRST_PROMPT
and added an explicit instruction not to self-report a status - nothing
downstream parses that line (confirmed via grep before removing it), so
this is a pure prompt-template edit, no behavior-affecting code change.

Run: /home/iam/venv/bin/python3 -m agent.orch_synthesizer_status_leak_regression_test
"""
from __future__ import annotations

import agent.orch_synthesizer as synth

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


prompt = synth._HYPOTHESIS_FIRST_PROMPT

check(
    "the hypothesis-mode prompt no longer instructs the LLM to write its "
    "own numbered STATUS section",
    "6. СТАТУС" not in prompt,
    f"prompt={prompt!r}",
)
check(
    "the prompt explicitly instructs the model NOT to self-report a "
    "WEAKLY/PARTIALLY/STRONGLY_SUPPORTED-style verdict (a negative "
    "instruction, not the old positive one asking it to produce one)",
    "НЕ пиши" in prompt and "СТАТУС" in prompt
    and "6. СТАТУС" not in prompt,
    f"prompt={prompt!r}",
)
check(
    "the prompt still contains the real structure (observation through "
    "alternatives) - this fix removes only the self-status instruction, "
    "not the rest of the hypothesis-mode structure",
    "НАБЛЮДЕНИЕ" in prompt and "ГИПОТЕЗА" in prompt
    and "ПОДДЕРЖКА" in prompt and "АЛЬТЕРНАТИВЫ" in prompt,
    f"prompt={prompt!r}",
)
check(
    "the prompt now explicitly tells the model the real Trust is computed "
    "separately, after this text, by the pipeline - not left implicit",
    "Trust" in prompt or "вычисляется пайплайном" in prompt,
    f"prompt={prompt!r}",
)

# _get_compose_prompt(answer_mode) must still route hypothesis_first to
# this same (now-fixed) prompt - confirms the fix landed on the actually
# selected prompt, not an unused duplicate.
check(
    "_get_compose_prompt('hypothesis_first') returns the same, fixed "
    "_HYPOTHESIS_FIRST_PROMPT object",
    synth._get_compose_prompt("hypothesis_first") is synth._HYPOTHESIS_FIRST_PROMPT,
    "",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
