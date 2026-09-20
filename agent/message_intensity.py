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

# Live-observed failure modes (A/B/C stability test, 2026-09-16, both
# on heretic:q8 and later on a completely different model family,
# Rocinante-X-12B): the model sometimes emits the marker malformed —
# missing the leading "###", or (second model, first observed run #18
# of the cross-family control test) "YANDI STATE" with a SPACE instead
# of the underscore — followed by PROSE or a JSON object with its OWN
# invented key spelling instead of our exact schema. A plain rfind() for
# the exact literal STATE_MARKER string then finds nothing, the "no
# marker at all" branch fires, and the garbled tag leaks into what the
# user is shown verbatim. An LLM's adherence to an exact output format
# is never guaranteed — not even the choice of underscore vs space vs
# period as separator in its own copy of a token it was shown once — so
# detection is done with a fuzzy pattern (the rare, distinctive
# "YANDI"+"STATE" pair, tolerant of underscore/space/hyphen/period
# between them, with optional surrounding hashes). This still can't
# false-positive on ordinary conversation text, but catches near-miss
# tag shapes a strict literal match misses. Third live-observed variant
# (example-ablation test, 2026-09-16): "###YANDI.State###".
_MARKER_RE = re.compile(r"#{0,3}\s*YANDI[_\s.-]STATE\s*#{0,3}", re.IGNORECASE)


def _strip_all_markers(text: str) -> str:
    """Removes every marker-shaped substring (well-formed or garbled),
    plus a trailing stray colon the model sometimes appends. Used only
    as a last-resort fallback when a marker was detected but nothing
    parseable followed it — the ONE invariant that always holds is that
    this internal tag itself must never reach the user, even when we
    can't make structural sense of what came after it."""
    return re.sub(_MARKER_RE.pattern + r"\s*:?\s*", " ", text, flags=re.IGNORECASE).strip()


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
    # Optional commitment events (strict booleans; absent -> False) and the
    # verbatim evidence quote that pet/chat_local.py validated for them.
    is_promise: bool = False
    claims_fulfilled: bool = False
    evidence: str = ""
    # (event type, start, end) of the code-owned evidence span of every event
    # that was accepted this turn; audit data for the causal-event ledger.
    spans: tuple = ()


def _neutral(error: str) -> IntensityResult:
    return IntensityResult(ok=False, error=error, **_NEUTRAL_RESULT)


def intensity_from_state(state: object, *, error: str = "") -> IntensityResult:
    """Convert already-normalized semantic state into PET domain state.

    This is deliberately not a transport parser: llm_gateway is
    responsible for separating visible reply from internal state before
    PET calls this function.
    """
    if not isinstance(state, dict):
        return _neutral("semantic state missing or not an object")
    try:
        return IntensityResult(
            ok=True,
            is_insult=bool(state["is_insult"]),
            is_apology=bool(state["is_apology"]),
            severity=max(0.0, min(1.0, float(state["severity"]))),
            sincerity=max(0.0, min(1.0, float(state["sincerity"]))),
            error=error,
            is_promise=state.get("is_promise") is True,
            claims_fulfilled=state.get("claims_fulfilled") is True,
        )
    except (KeyError, TypeError, ValueError) as e:
        return _neutral(f"semantic state missing/invalid fields: {e}")


def _parse_structured(data: dict) -> Tuple[str, IntensityResult] | None:
    """STRUCTURED CONTRACT (mandate "structured self-report", live-
    tested 2026-09-16 — see pet/chat_local.py's _STATE_SCHEMA docstring
    for the full A/B/C/factorial/ablation series that motivated this):
    when the backend honors llm_gateway's response_format=<schema>, the
    ENTIRE raw generation is one JSON object {"reply": str, "state":
    {...}} — no marker, no free-text-then-tag transition to regex out,
    no natural-language worked example for the model to anchor on.
    Returns None (not a result) when `data` doesn't have this shape, so
    the caller falls through to the legacy STATE_MARKER path untouched —
    this is a fallback-preserving ADD, never a replacement of it, since
    not every backend honors response_format as a real schema (see
    llm_gateway.remote_backend/llamacpp_backend: an unrecognized
    response_format value is silently ignored there, not an error)."""
    if "reply" not in data or "state" not in data:
        return None
    reply, state = data.get("reply"), data.get("state")
    if not isinstance(reply, str) or not isinstance(state, dict):
        return None
    result = intensity_from_state(state)
    if not result.ok:
        # Fail-open on the STRUCTURED DATA only (exactly like the legacy
        # path) — the shape was right, the field values weren't; the
        # reply the model actually wrote is still real and still shown.
        return reply.strip(), _neutral(result.error.replace("semantic", "structured", 1))
    if not reply.strip():
        # Live-observed failure mode, same as the legacy tag-only case:
        # a technically valid structured object with an empty reply is
        # still "no visible reply" to the user, never fabricated text.
        return "", IntensityResult(**{**result.__dict__, "error": "model produced no visible reply, tag only"})
    return reply.strip(), result


def parse_self_report(raw: str) -> Tuple[str, IntensityResult]:
    """Splits `raw` (the model's full, untouched generation) into
    (visible_reply, IntensityResult). `visible_reply` never contains the
    marker or the JSON that follows it — this is what pet/chat_local.py
    is allowed to show the user.

    Tries the STRUCTURED contract first (see _parse_structured's own
    docstring); falls through to the original free-text + trailing
    "###YANDI_STATE###"-family tag parsing, UNCHANGED, for anything that
    doesn't parse as that structured shape."""
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            structured = _parse_structured(data)
            if structured is not None:
                return structured

    matches = list(_MARKER_RE.finditer(raw))
    if not matches:
        return raw.strip(), _neutral("no state marker found in model output")

    last = matches[-1]
    visible = raw[:last.start()].strip()
    tail = raw[last.end():]

    match = re.search(r"\{.*\}", tail, re.DOTALL)
    if not match:
        return (visible or _strip_all_markers(raw)), _neutral(f"marker present but no JSON object after it: {tail[:200]!r}")

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        return (visible or _strip_all_markers(raw)), _neutral(f"malformed JSON after marker: {e}")

    try:
        result = IntensityResult(
            ok=True,
            is_insult=bool(data["is_insult"]),
            is_apology=bool(data["is_apology"]),
            severity=max(0.0, min(1.0, float(data["severity"]))),
            sincerity=max(0.0, min(1.0, float(data["sincerity"]))),
        )
    except (KeyError, TypeError, ValueError) as e:
        return (visible or _strip_all_markers(raw)), _neutral(f"state JSON missing/invalid expected fields: {e}")

    if not visible:
        # Live-observed failure mode: the model emitted ONLY the tag,
        # no actual reply — a real self-report but nothing to show the
        # user. Treat the report as valid (still worth recording) but
        # flag it distinctly so the caller can decide how to handle an
        # empty visible reply (never silently show blank text).
        return "", IntensityResult(**{**result.__dict__, "error": "model produced no visible reply, tag only"})

    return visible, result
