"""
pet/fact_test_support.py — scripted stand-ins for the model behind
pet/fact_extraction.py, for regression tests only (no model, no network).

`scripted_fact_llm(facts, memory_query=..., blind=..., support=...)` answers the
three prompts of the fact protocol the way a well-behaved model would: it points at
the word range where a fact's `quote` sits in the message the prompt carries (a
quote that is not in the message points at word 0..0, which is what a
hallucinating model would do), and answers the blind and support checks about the
fragment it is shown. `blind` / `support` / `link` override individual check answers (a list
consumed per call, or a callable), to model a checker that disagrees.

`scripted_router(event_llm, fact_llm)` is the single `llm` callable the chat path
hands to both extractors: it routes by which protocol the system prompt belongs to,
and records everything each side was shown (`.event_inputs`, `.fact_inputs`).
"""
from __future__ import annotations

import json
from typing import Callable, Dict, Iterable, List, Optional

from pet import event_extraction as ee
from pet import fact_extraction as fe


def scripted_fact_llm(
    facts: Iterable[dict] = (),
    memory_query: str = "none",
    blind: Optional[object] = None,
    support: Optional[object] = None,
    raw: Optional[str] = None,
    link: Optional[object] = None,
):
    """facts: dicts with `quote`, `cls`, `statement` and optionally polarity, time,
    stability, relation, target."""
    facts = list(facts)
    blind_queue = list(blind) if isinstance(blind, (list, tuple)) else None
    support_queue = list(support) if isinstance(support, (list, tuple)) else None
    link_queue = list(link) if isinstance(link, (list, tuple)) else None
    calls: List[List[Dict[str, str]]] = []

    def find(fragment: str) -> Optional[dict]:
        return next((f for f in facts if f["quote"] in fragment or fragment in f["quote"]), None)

    def llm(messages: List[Dict[str, str]]) -> str:
        calls.append(messages)
        system, user = messages[0]["content"], messages[-1]["content"]
        if system == fe._CHECK_SYSTEM:
            fragment = user.split("Фрагмент:\n«", 1)[1].rsplit("»", 1)[0]
            f = find(fragment)
            answer = ({"frame": f.get("time", "current"), "polarity": f.get("polarity", "affirmed"),
                       "stability": f.get("stability", "stable"), "secret": False}
                      if f else {"frame": "not_personal", "polarity": "affirmed", "stability": "ephemeral", "secret": False})
            override = blind_queue.pop(0) if blind_queue else (blind(fragment) if callable(blind) else None)
            if override:
                answer = {**answer, **override}
            return json.dumps(answer)
        if system == fe._SUPPORT_SYSTEM:
            answer = {"supported": True, "adds": False}
            override = support_queue.pop(0) if support_queue else (support(user) if callable(support) else None)
            if override:
                answer = {**answer, **override}
            return json.dumps(answer)
        if system in (fe._LINK_SYSTEM, fe._CONFLICT_SYSTEM):
            key = "revises" if system == fe._LINK_SYSTEM else "conflict"
            answer = {key: True}
            override = link_queue.pop(0) if link_queue else (link(user) if callable(link) else None)
            if override:
                answer = {**answer, **override}
            return json.dumps(answer)
        # the extraction prompt
        if raw is not None:
            return raw
        message = user.split("Сообщение:\n", 1)[1].split("\n\nСлова (", 1)[0]
        words = ee.segment_words(message)
        proposed = []
        for f in facts:
            at = message.find(f["quote"])
            if at < 0:
                first = last = 0
            else:
                end = at + len(f["quote"])
                first = next(i for i, (_, s, e) in enumerate(words) if e > at)
                last = max(i for i, (_, s, e) in enumerate(words) if s < end)
            item = {"span": [first, last], "class": f["cls"], "statement": f["statement"],
                    "polarity": f.get("polarity", "affirmed"), "time": f.get("time", "current"),
                    "stability": f.get("stability", "stable"), "relation": f.get("relation", "none")}
            if f.get("target") is not None:
                item["target"] = f["target"]
            proposed.append(item)
        return json.dumps({"memory_query": memory_query, "facts": proposed}, ensure_ascii=False)

    llm.calls = calls  # type: ignore[attr-defined]
    return llm


def scripted_router(event_llm: Callable, fact_llm: Callable, verify_llm: Optional[Callable] = None):
    """One callable for both extractors; remembers what each was shown."""
    fact_prompts = {fe._EXTRACT_SYSTEM, fe._CHECK_SYSTEM, fe._SUPPORT_SYSTEM, fe._LINK_SYSTEM, fe._CONFLICT_SYSTEM}
    event_inputs: List[List[Dict[str, str]]] = []
    fact_inputs: List[List[Dict[str, str]]] = []
    verify_inputs: List[List[Dict[str, str]]] = []
    from pet import commitment_verification as cv
    verify_prompts = {cv._CLASSIFY_SYSTEM, cv._VERIFY_SYSTEM, cv._DELIVERS_SYSTEM}

    def llm(messages: List[Dict[str, str]]) -> str:
        if messages[0]["content"] in verify_prompts:
            verify_inputs.append(messages)
            if verify_llm is None:
                raise RuntimeError("no verification model scripted for this test")
            return verify_llm(messages)
        if messages[0]["content"] in fact_prompts:
            fact_inputs.append(messages)
            return fact_llm(messages)
        event_inputs.append(messages)
        return event_llm(messages)

    llm.event_inputs = event_inputs  # type: ignore[attr-defined]
    llm.fact_inputs = fact_inputs    # type: ignore[attr-defined]
    llm.verify_inputs = verify_inputs  # type: ignore[attr-defined]
    return llm
