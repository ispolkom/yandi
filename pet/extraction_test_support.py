"""
pet/extraction_test_support.py — a scripted stand-in for the model behind
pet/event_extraction.py, for regression tests only (no model, no network).

`scripted_llm([(quote, type, extras), ...], labels=...)` answers the extraction
prompt with word references for where `quote` sits in the message that prompt
carries (the way a well-behaved model would) and answers every blind check on a
fragment as a working checker would: the act it actually expresses (the type of
the event whose quote lies in that fragment) in the "current" frame. `labels`
overrides per check call: "different_act" (the fragment expresses none of the
acts), or a frame name (quotation / hypothetical / negated / past_report /
topic). A `quote` that is not in the message points at word 0..0, i.e. an
unrelated span, which is what a hallucinating model would do.
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pet import event_extraction as ee


def scripted_llm(
    events: Iterable[Tuple[str, str, Optional[dict]]] = (),
    labels: Optional[Sequence[str]] = None,
):
    events = list(events)
    remaining = list(labels) if labels is not None else []
    calls: List[List[Dict[str, str]]] = []

    def llm(messages: List[Dict[str, str]]) -> str:
        calls.append(messages)
        user = messages[-1]["content"]
        if "Слова" not in user:  # the blind check on one fragment
            fragment = user.split("Фрагмент:\n«", 1)[1].rsplit("»", 1)[0]
            act = next((kind for quote, kind, _ in events if quote in fragment or fragment in quote), "none")
            override = remaining.pop(0) if remaining else None
            frame = "current"
            if override in ("different_act", "other"):
                act = "none"
            elif override and override not in ("exact_act", "current_act"):
                frame = override
            return json.dumps({"act": act, "frame": frame})
        message = user.split("Сообщение:\n", 1)[1].split("\n\nСлова (", 1)[0]
        words = ee.segment_words(message)
        proposed = []
        for quote, kind, extras in events:
            at = message.find(quote)
            if at < 0:
                first = last = 0
            else:
                end = at + len(quote)
                first = next(i for i, (_, s, e) in enumerate(words) if e > at)
                last = max(i for i, (_, s, e) in enumerate(words) if s < end)
            proposed.append({"span": [first, last], "type": kind, **(extras or {})})
        return json.dumps({"events": proposed}, ensure_ascii=False)

    llm.calls = calls  # type: ignore[attr-defined]
    return llm


def llm_from_state(text: str, state: Optional[dict]):
    """Translate a legacy-style state description ({is_insult, severity, is_apology,
    sincerity, is_promise, claims_fulfilled, evidence}) into a scripted extractor for
    `text`: the described events are proposed at the words of `evidence`. If that
    quote is missing or not part of `text` (a contaminated / hallucinated event) the blind
    check finds no such act there, as a working checker would."""
    state = state or {}
    quote = state.get("evidence") or text
    events = []
    if state.get("is_insult"):
        events.append((quote, "insult", {"severity": state.get("severity", 0.0)}))
    if state.get("is_apology"):
        events.append((quote, "apology", {"sincerity": state.get("sincerity", 0.0)}))
    if state.get("is_promise"):
        events.append((quote, "promise", None))
    if state.get("claims_fulfilled"):
        events.append((quote, "fulfilment_claim", None))
    grounded = bool(state.get("evidence")) and quote in text  # an assertion with no quotable support is ungrounded
    return scripted_llm(events, labels=None if grounded else ["different_act"] * len(events))
