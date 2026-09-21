"""
pet/verification_test_support.py — a scripted stand-in for the model behind
pet/commitment_verification.py, for regression tests only (no model, no network).

`scripted_verify_llm(delivery=(quote, target), classify=..., delivers=...)` answers the three prompts of
the protocol the way a model would: `classify` decides in_chat / external for a promise fragment (a string or a
callable on the fragment); the extractor points at the word range where `quote` sits in the message the prompt
carries (a quote that is not in the message points at word 0..0, like a hallucinating model) and names `target`;
`delivers(promise_text, fragment)` is the blind judge (a bool, or a dict merged over the default answer;
default: it accepts everything, i.e. the worst-case checker). Everything it was shown is in `.calls`.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional, Tuple, Union

from pet import commitment_verification as cv
from pet import event_extraction as ee


def scripted_verify_llm(
    delivery: Optional[Tuple[str, int]] = None,
    classify: Union[str, Callable[[str], str]] = "in_chat",
    delivers: Optional[Callable[[str, str], Union[bool, dict]]] = None,
    raw: Optional[str] = None,
):
    calls: List[List[Dict[str, str]]] = []

    def llm(messages: List[Dict[str, str]]) -> str:
        calls.append(messages)
        system, user = messages[0]["content"], messages[-1]["content"]
        if system == cv._CLASSIFY_SYSTEM:
            fragment = user.split("Фрагмент с обещанием:\n«", 1)[1].rsplit("»", 1)[0]
            return json.dumps({"deliverable": classify(fragment) if callable(classify) else classify})
        if system == cv._DELIVERS_SYSTEM:
            promise = user.split("Обещание пользователя:\n«", 1)[1].split("»\n\nСообщение пользователя:", 1)[0]
            fragment = user.split("Фрагмент:\n«", 1)[1].rsplit("»", 1)[0]
            answer = {"delivers": True, "frame": "current"}
            verdict = delivers(promise, fragment) if delivers else True
            if isinstance(verdict, dict):
                answer.update(verdict)
            elif verdict is False:
                answer = {"delivers": False, "frame": "not_delivery"}
            return json.dumps(answer)
        # the extraction prompt
        if raw is not None:
            return raw
        if delivery is None:
            return json.dumps({"delivery": None})
        quote, target = delivery
        message = user.split("Сообщение:\n", 1)[1].split("\n\nСлова (", 1)[0]
        words = ee.segment_words(message)
        at = message.find(quote)
        if at < 0:
            first = last = 0
        else:
            end = at + len(quote)
            first = next(i for i, (_, s, e) in enumerate(words) if e > at)
            last = max(i for i, (_, s, e) in enumerate(words) if s < end)
        return json.dumps({"delivery": {"span": [first, last], "target": target}})

    llm.calls = calls  # type: ignore[attr-defined]
    return llm
