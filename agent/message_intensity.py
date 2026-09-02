"""
agent/message_intensity.py — parses YANDI's OWN self-reported reading of
a conversation turn out of her own generated reply.

Owner mandate, established after live-testing against the real local
model on this machine (heretic:q8 via Ollama): накал/tone must NOT be
judged by a SEPARATE classifier call that then dictates a reaction to a
second generation — that is "как нам хочется" (we decide how she
feels), not "как хочется ей". Instead pet/chat_local.py makes ONE model
call; the system prompt asks her to recognize how she is being
addressed AS PART of generating her own natural-language reply, then
append a small, strictly-formatted self-report line at the very end
(after a blank line) describing what SHE just perceived. This module's
only job is splitting that trailing line off her visible reply and
parsing it — no network call, no judgment of its own.

Live-tested prompt shapes that made heretic:q8 reliably comply (worth
preserving if this prompt is ever revised): (1) an explicit instruction
that the natural reply must come FIRST and must not be empty, and (2) a
concrete worked EXAMPLE of the full expected structure — an instruction
alone ("add a state line") without an example was live-observed to make
the model skip the actual reply entirely and emit ONLY the tag.

FAIL-OPEN BY DESIGN: a missing or malformed tag means "nothing to
record" (neutral IntensityResult, ok=False) and the ENTIRE original
text is returned as the visible reply untouched — never truncate or
hide part of a real reply because the tag parsing failed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Tuple

STATE_MARKER = "###YANDI_STATE###"

_NEUTRAL_RESULT = {
    "is_insult": False, "is_apology": False, "severity": 0.0,
    "sincerity": 0.0,
}


@dataclass
class IntensityResult:
    ok: bool
    is_insult: bool
    is_apology: bool
    severity: float
    sincerity: float
    error: str = ""


def _neutral(error: str) -> IntensityResult:
    return IntensityResult(ok=False, error=error, **_NEUTRAL_RESULT)


def parse_self_report(raw: str) -> Tuple[str, IntensityResult]:
    """Splits `raw` (the model's full, untouched generation) into
    (visible_reply, IntensityResult). `visible_reply` never contains the
    marker or the JSON that follows it — this is what pet/chat_local.py
    is allowed to show the user."""
    idx = raw.rfind(STATE_MARKER)
    if idx == -1:
        return raw.strip(), _neutral("no state marker found in model output")

    visible = raw[:idx].strip()
    tail = raw[idx + len(STATE_MARKER):]

    match = re.search(r"\{.*\}", tail, re.DOTALL)
    if not match:
        return (visible or raw.strip()), _neutral(f"marker present but no JSON object after it: {tail[:200]!r}")

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        return (visible or raw.strip()), _neutral(f"malformed JSON after marker: {e}")

    try:
        result = IntensityResult(
            ok=True,
            is_insult=bool(data["is_insult"]),
            is_apology=bool(data["is_apology"]),
            severity=max(0.0, min(1.0, float(data["severity"]))),
            sincerity=max(0.0, min(1.0, float(data["sincerity"]))),
        )
    except (KeyError, TypeError, ValueError) as e:
        return (visible or raw.strip()), _neutral(f"state JSON missing/invalid expected fields: {e}")

    if not visible:
        # Live-observed failure mode: the model emitted ONLY the tag,
        # no actual reply — a real self-report but nothing to show the
        # user. Treat the report as valid (still worth recording) but
        # flag it distinctly so the caller can decide how to handle an
        # empty visible reply (never silently show blank text).
        return "", IntensityResult(**{**result.__dict__, "error": "model produced no visible reply, tag only"})

    return visible, result
