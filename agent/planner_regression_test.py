"""
agent/planner_regression_test.py — P1 regression (plan sub-profiling pass).

Covers the bug found while instrumenting build_plan()/_get_reflection_policies():
`lesson_text` was referenced but never defined — any lesson with
confidence>=0.6 raised NameError, silently swallowed by a broad except,
abandoning the rest of lesson-to-policy conversion for that call. Never
observed in prior live logs only because confidence happened to stay
below the 0.6 threshold first.

Also exercises _get_reflection_policies()/build_plan() end-to-end to
confirm the new [Plan SubProfile] instrumentation doesn't crash and
covers both real internal phases (reflection_policies, experience_memory,
planner_core).

Run: /home/iam/venv/bin/python3 -m agent.planner_regression_test
"""

from unittest.mock import patch, MagicMock

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


import agent.orch_planner as planner

# ── 1. lesson_text bug: a lesson with confidence>=0.6 must not raise,
#      and must correctly produce the mapped policy for each of the
#      three lesson_text branches. ──

high_confidence_lesson_ok = {
    "query": "прошлый запрос",
    "domain": "test",
    "confidence": 0.8,
    "timestamp": "",
    "lessons": ["Стратегия прошли валидацию, повторить"],
}

fake_memory = MagicMock()
fake_memory.get_relevant_lessons.return_value = [high_confidence_lesson_ok]

with patch("agent.experience_memory.get_experience_memory", return_value=fake_memory):
    with patch.object(planner, "REFLECTION_AVAILABLE", False):
        try:
            policies = planner._get_reflection_policies("тестовый запрос")
            raised = False
        except NameError as e:
            policies = []
            raised = True
            _err = e

check(
    "lesson with confidence>=0.6 does not raise NameError on lesson_text",
    not raised,
    f"raised: {_err if raised else ''}",
)

if not raised:
    rule_texts = [p.get("rule", "") for p in policies if p.get("type") == "policy"]
    check(
        "confidence>=0.6 + 'прошли валидацию' lesson -> validation policy added",
        any("Добавить валидацию" in r for r in rule_texts),
        f"policies={policies}",
    )

# ── 2. Confidence >= 0.6 but no matching phrase -> no crash, no spurious policy ──

neutral_lesson = {
    "query": "прошлый запрос 2",
    "domain": "test",
    "confidence": 0.9,
    "timestamp": "",
    "lessons": ["Просто нейтральный текст без ключевых фраз"],
}
fake_memory2 = MagicMock()
fake_memory2.get_relevant_lessons.return_value = [neutral_lesson]

with patch("agent.experience_memory.get_experience_memory", return_value=fake_memory2):
    with patch.object(planner, "REFLECTION_AVAILABLE", False):
        try:
            policies2 = planner._get_reflection_policies("тестовый запрос")
            raised2 = False
        except NameError as e:
            raised2 = True
            _err2 = e

check(
    "confidence>=0.6, no matching phrase -> no crash",
    not raised2,
    f"raised: {_err2 if raised2 else ''}",
)

# ── 3. Confidence < 0.6 still works exactly as before (non-regression) ──

low_confidence_lesson = {
    "query": "прошлый запрос 3",
    "domain": "test",
    "confidence": 0.3,
    "timestamp": "",
    "lessons": ["прошли валидацию"],
}
fake_memory3 = MagicMock()
fake_memory3.get_relevant_lessons.return_value = [low_confidence_lesson]

with patch("agent.experience_memory.get_experience_memory", return_value=fake_memory3):
    with patch.object(planner, "REFLECTION_AVAILABLE", False):
        policies3 = planner._get_reflection_policies("тестовый запрос")

check(
    "confidence<0.6 -> lesson recorded but no derived policy rule (unchanged behavior)",
    any(p.get("type") == "lesson" for p in policies3)
    and not any(p.get("type") == "policy" for p in policies3),
    f"policies={policies3}",
)

# ── 4. build_plan() with use_llm=False runs end-to-end without crashing
#      and the sub-profile instrumentation fires (visible on stderr,
#      not asserted structurally here — this is a smoke test, not a
#      timing assertion). ──

from agent.orch_schemas import RiskResult

fake_memory4 = MagicMock()
fake_memory4.get_relevant_lessons.return_value = []

with patch("agent.experience_memory.get_experience_memory", return_value=fake_memory4):
    with patch.object(planner, "REFLECTION_AVAILABLE", False):
        risk = RiskResult(risk_level="low", nodes_required=1)
        try:
            result = planner.build_plan("Тестовый запрос", risk, use_llm=False)
            check("build_plan(use_llm=False) runs without crashing", result is not None)
        except Exception as e:
            check("build_plan(use_llm=False) runs without crashing", False, f"{type(e).__name__}: {e}")

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
