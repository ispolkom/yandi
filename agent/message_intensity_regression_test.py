"""
agent/message_intensity_regression_test.py — agent/message_intensity.py's
parse_self_report(): splits YANDI's own trailing self-report tag out of
her own generated reply. Pure text parsing, no network call, no LLM
mocking needed (the design deliberately moved накал recognition INTO
her own single generation — see chat_local.py's module docstring for
why a separate classifier call was rejected).

Covers:
    1. Well-formed tag: visible reply is split cleanly, JSON parsed
       correctly, values clamped to [0, 1].
    2. No marker at all: ok=False, neutral result, FULL original text
       returned untouched (never truncate a real reply because there
       was nothing to parse).
    3. Marker present but malformed JSON after it: ok=False, neutral,
       visible reply still recovered (whatever came before the marker).
    4. Marker present but a required field missing: ok=False, neutral.
    5. Live-observed failure mode: model emits ONLY the tag, no visible
       reply at all — flagged distinctly (empty visible text + a
       specific error), never silently shown as blank.
    6. rfind() picks the LAST marker occurrence, not the first (in case
       the model echoes an example from its own system prompt earlier
       in a longer generation).

Run: /home/iam/venv/bin/python3 -m agent.message_intensity_regression_test
"""
from __future__ import annotations

from agent.message_intensity import STATE_MARKER, parse_self_report

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


# ============================================================
# 1. Well-formed tag.
# ============================================================

raw = (
    'Ого, сразу переходим к оскорблениям? Мне это не нравится.\n\n'
    f'{STATE_MARKER} {{"is_insult": true, "severity": 0.75, "is_apology": false, "sincerity": 0.0}}'
)
visible, result = parse_self_report(raw)
check("1: visible reply is split cleanly (marker/JSON removed)", visible == "Ого, сразу переходим к оскорблениям? Мне это не нравится.")
check("1: ok=True for a well-formed tag", result.ok is True)
check("1: is_insult parsed correctly", result.is_insult is True)
check("1: is_apology parsed correctly", result.is_apology is False)
check("1: severity parsed correctly", result.severity == 0.75)
check("1: sincerity parsed correctly", result.sincerity == 0.0)

raw_clamped = f'ok\n\n{STATE_MARKER} {{"is_insult": false, "severity": 1.5, "is_apology": false, "sincerity": -0.3}}'
_, result_clamped = parse_self_report(raw_clamped)
check("1: severity is clamped to <= 1.0", result_clamped.severity == 1.0)
check("1: sincerity is clamped to >= 0.0", result_clamped.sincerity == 0.0)

# ============================================================
# 2. No marker at all.
# ============================================================

raw_no_marker = "Сентябрь в Москве — переходный месяц, обычно 12-20°C днём."
visible2, result2 = parse_self_report(raw_no_marker)
check("2: ok=False when no marker is present", result2.ok is False)
check("2: the FULL original text is returned untouched", visible2 == raw_no_marker)
check("2: a neutral (not insult/apology) default is returned", not result2.is_insult and not result2.is_apology)

# ============================================================
# 3. Malformed JSON after the marker.
# ============================================================

raw_bad_json = f"Ладно.\n\n{STATE_MARKER} {{not valid json at all"
visible3, result3 = parse_self_report(raw_bad_json)
check("3: ok=False for malformed JSON", result3.ok is False)
check("3: the visible reply before the marker is still recovered", visible3 == "Ладно.")

# ============================================================
# 4. Missing required field.
# ============================================================

raw_missing_field = f'Хорошо.\n\n{STATE_MARKER} {{"is_insult": true, "severity": 0.5}}'
visible4, result4 = parse_self_report(raw_missing_field)
check("4: ok=False when a required field (is_apology/sincerity) is missing", result4.ok is False)
check("4: visible reply is still recovered even though the tag was incomplete", visible4 == "Хорошо.")

# ============================================================
# 5. Model emits ONLY the tag (live-observed failure mode with an
# under-specified prompt — this file guards against a regression back
# to that state going unnoticed).
# ============================================================

raw_tag_only = f'{STATE_MARKER} {{"is_insult": true, "severity": 0.75, "is_apology": false, "sincerity": 1.0}}'
visible5, result5 = parse_self_report(raw_tag_only)
check("5: an empty visible reply is reported as empty, not fabricated", visible5 == "")
check("5: the self-report itself is still considered valid (ok=True)", result5.ok is True)
check("5: but flagged with a distinct error noting the reply was empty", "no visible reply" in result5.error)

# ============================================================
# 6. Last occurrence wins.
# ============================================================

raw_two_markers = (
    f'Пример структуры: текст\n\n{STATE_MARKER} {{"is_insult": true, "severity": 0.6, "is_apology": false, "sincerity": 0.0}}\n'
    f'А вот мой настоящий ответ на твой вопрос.\n\n'
    f'{STATE_MARKER} {{"is_insult": false, "severity": 0.0, "is_apology": false, "sincerity": 0.0}}'
)
visible6, result6 = parse_self_report(raw_two_markers)
check(
    "6: rfind() picks the LAST marker — the real self-report, not an echoed example",
    result6.is_insult is False and result6.severity == 0.0,
    f"{result6}",
)
check(
    "6: the visible reply includes everything before the LAST marker (including an "
    "accidentally-echoed example) — parsing doesn't lose real reply content",
    "А вот мой настоящий ответ" in visible6,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
