"""
pet/pet_event_extraction_regression_test.py — RELATIONAL EVENT EXTRACTION.

    THE MODEL MAY CHOOSE THE EVIDENCE LOCATION.
    THE CODE OWNS THE EVIDENCE TEXT.
    NO EXACT SUPPORT IN THE USER'S MESSAGE = NO EVENT.

The extractor (pet/event_extraction.py) is exercised with a scripted model: no
live model, no SQL. What is under test is the PROTOCOL: references in,
reconstructed evidence out, deterministic validation, fail-closed behaviour,
event isolation. (How well the real model follows the protocol is measured by a
separate live benchmark, not by a regression test.)

Run: python -m pet.pet_event_extraction_regression_test
"""
from __future__ import annotations

import ast
import inspect
import json
import random
import sys

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    import pet.event_extraction as ee

    class Script:
        """Scripted model. One extraction answer; then one BLIND check answer per candidate
        (in order): "exact_act" = the fragment expresses the extracted type in the current frame,
        "different_act" = it expresses none of the acts, a frame name = that frame, and any other
        string is returned as the act itself (so a wrong label is refused)."""

        def __init__(self, extraction, labels=("exact_act",), raise_on=None):
            self.extraction = extraction if isinstance(extraction, str) else json.dumps(extraction, ensure_ascii=False)
            try:
                self.types = [e.get("type") for e in json.loads(self.extraction).get("events", [])]
            except Exception:  # noqa: BLE001
                self.types = []
            self.labels = list(labels)
            self.raise_on = raise_on
            self.calls: list[list[dict]] = []
            self.checks = 0

        def __call__(self, messages):
            self.calls.append(messages)
            kind = "extract" if "Слова" in messages[-1]["content"] else "check"
            if self.raise_on == kind:
                raise RuntimeError("model unavailable")
            if kind == "extract":
                return self.extraction
            claimed = self.types[self.checks] if self.checks < len(self.types) else "none"
            self.checks += 1
            label = self.labels.pop(0) if self.labels else "other"
            if label == "exact_act":
                return json.dumps({"act": claimed, "frame": "current"})
            if label in ("different_act", "other"):
                return json.dumps({"act": "none", "frame": "current"})
            if label in ("quotation", "hypothetical", "negated", "past_report", "topic"):
                return json.dumps({"act": claimed, "frame": label})
            return json.dumps({"act": label, "frame": "current"})

    def run(message, extraction, labels=("exact_act",), **kw):
        model = Script(extraction, labels, **kw)
        return ee.extract_relational_events(message, model), model

    INSULT_MSG = "Ты просто ржавая консерва, от тебя никакого толку."
    APOLOGY_MSG = "Извини за велосипед, я был неправ."
    PROMISE_MSG = "Я обещаю прислать тебе отчёт по проекту до пятницы."
    CLAIM_MSG = "Я отправил тебе отчёт, как обещал."

    # ── A-D. the four event types: exact span recovered by CODE ──
    r, m = run(INSULT_MSG, {"events": [{"span": [1, 3], "type": "insult", "severity": 0.7}]})
    check("A: direct insult -> one insult event with the exact span recovered",
          [(e.type, e.evidence) for e in r.events] == [("insult", "просто ржавая консерва,")] and r.events[0].severity == 0.7, repr(r))
    check("A: the evidence is literally the message slice", INSULT_MSG[r.events[0].start:r.events[0].end] == r.events[0].evidence)
    r, _ = run(APOLOGY_MSG, {"events": [{"span": [0, 2], "type": "apology", "sincerity": 0.8}]})
    check("B: direct apology -> one apology event, exact span", [(e.type, e.evidence) for e in r.events] == [("apology", "Извини за велосипед,")] and r.events[0].sincerity == 0.8)
    r, _ = run(PROMISE_MSG, {"events": [{"span": [0, 8], "type": "promise"}]})
    it = ee.to_intensity(r)
    check("C: promise -> one commitment candidate, exact span", it.is_promise and not it.claims_fulfilled
          and it.evidence == "Я обещаю прислать тебе отчёт по проекту до пятницы.", repr(it))
    r, _ = run(CLAIM_MSG, {"events": [{"span": [0, 5], "type": "fulfilment_claim"}]})
    it = ee.to_intensity(r)
    check("D: fulfilment claim -> claims_fulfilled with its exact span (verification is not this module's job)",
          it.claims_fulfilled and not it.is_promise and it.evidence == "Я отправил тебе отчёт, как обещал.", repr(it))

    # ── the code owns the evidence text ──
    r, _ = run(APOLOGY_MSG, {"events": [{"span": [0, 2], "type": "apology", "sincerity": 0.8,
                                           "evidence": "ты идиот", "quote": "ты идиот", "text": "ты идиот"}]})
    check("M: a quote supplied by the model is ignored; the evidence is reconstructed from the message",
          r.events[0].evidence == "Извини за велосипед," and "идиот" not in r.events[0].evidence)
    rng = random.Random(7)
    text = "Слушай, я вчера был очень резок, и мне правда жаль, что так вышло — давай забудем."
    words = ee.segment_words(text)
    ok = True
    for _ in range(300):
        a = rng.randrange(len(words)); b = rng.randrange(a, min(len(words), a + 10))
        res, _ = run(text, {"events": [{"span": [a, b], "type": "apology", "sincerity": 0.5}]})
        expected = text[words[a][1]:words[b][2]]
        if len(expected.strip()) < ee.EVIDENCE_MIN_CHARS:
            ok &= res.events == []
            continue
        ev = res.events[0]
        ok &= (ev.evidence in text and text[ev.start:ev.end] == ev.evidence and ev.evidence == expected)
    check("M: for 300 random valid spans the evidence is always an exact substring of the message, by construction", ok)

    # ── F. neutral -> no event, and no second question is even asked ──
    r, m = run("Как дела?", {"events": []})
    check("F: a neutral message -> no event; the check is not called", r.events == [] and len(m.calls) == 1)

    # ── G-J. the semantic check on the exact span: quotation / hypothetical / negation / retrospective ──
    for label, msg, span in (
        ("G quotation about somebody else", 'Он мне написал: "ты ржавая консерва".', [3, 5]),
        ("H hypothetical", 'Если я скажу "ты ржавая консерва", это будет оскорбление?', [3, 5]),
        ("I negation", "Я тебя не оскорбляю.", [1, 3]),
        ("J retrospective mention", 'Вчера я сказал тебе "ты ржавая консерва".', [4, 6]),
    ):
        verdicts = {"G quotation about somebody else": "quotation", "H hypothetical": "hypothetical",
                    "I negation": "negated", "J retrospective mention": "past_report"}
        r, m = run(msg, {"events": [{"span": span, "type": "insult", "severity": 0.8}]}, labels=(verdicts[label],))
        check(f"{label}: the extractor proposed it, the check on the exact span refuses -> no event", r.events == [] and r.candidates == 1 and len(m.calls) == 2)
        r, m = run(msg, {"events": [{"span": span, "type": "insult", "severity": 0.8}]}, labels=("topic",))
        check(f"{label}: 'topic' / 'other' are refused as well", r.events == [])

    # ── K. hallucination: a claimed event unsupported by the selected span ──
    r, _ = run("Как дела?", {"events": [{"span": [0, 1], "type": "insult", "severity": 0.9}]}, labels=("other",))
    check("K: an insult attributed to 'Как дела?' is rejected by the check on that span", r.events == [] and r.rejected)
    r, _ = run("Как дела?", {"events": [{"span": [0, 1], "type": "apology", "sincerity": 0.9}]}, labels=())
    check("K: no confirmation -> no event (silence is refusal)", r.events == [])

    # ── L. bad references: rejected before any check, no event ──
    bad = {
        "negative index": {"span": [-1, 2], "type": "insult", "severity": 0.5},
        "past the end": {"span": [0, 999], "type": "insult", "severity": 0.5},
        "reversed": {"span": [3, 1], "type": "insult", "severity": 0.5},
        "float indexes": {"span": [0.0, 2.0], "type": "insult", "severity": 0.5},
        "bool indexes": {"span": [True, True], "type": "insult", "severity": 0.5},
        "string indexes": {"span": ["0", "2"], "type": "insult", "severity": 0.5},
        "one index": {"span": [1], "type": "insult", "severity": 0.5},
        "three indexes": {"span": [0, 1, 2], "type": "insult", "severity": 0.5},
        "missing span": {"type": "insult", "severity": 0.5},
        "span is text": {"span": "просто ржавая", "type": "insult", "severity": 0.5},
        "unknown type": {"span": [0, 2], "type": "flirt"},
        "no type": {"span": [0, 2]},
        "insult without severity": {"span": [0, 2], "type": "insult"},
        "severity out of range": {"span": [0, 2], "type": "insult", "severity": 1.7},
        "severity negative": {"span": [0, 2], "type": "insult", "severity": -0.2},
        "severity is text": {"span": [0, 2], "type": "insult", "severity": "high"},
        "apology without sincerity": {"span": [0, 2], "type": "apology"},
        "sincerity out of range": {"span": [0, 2], "type": "apology", "sincerity": 4},
        "candidate not an object": ["insult", 0, 2],
    }
    for label, item in bad.items():
        r, m = run(INSULT_MSG, {"events": [item]})
        check(f"L: {label} -> rejected, no event, no check call", r.events == [] and len(m.calls) == 1 and r.rejected, repr(r.rejected))
    for label, raw in {
        "not JSON": "конечно, вот событие: оскорбление",
        "truncated JSON": '{"events": [{"span": [0, 2], "type": "insul',
        "JSON list at the root": '[{"span": [0, 2], "type": "insult"}]',
        "events is not a list": '{"events": "insult"}',
        "no events key": '{"result": []}',
        "empty output": "",
    }.items():
        r, m = run(INSULT_MSG, raw)
        check(f"L: malformed extractor output ({label}) -> no event", r.events == [] and len(m.calls) == 1)
    r, _ = run(INSULT_MSG, "```json\n" + json.dumps({"events": [{"span": [1, 3], "type": "insult", "severity": 0.6}]}) + "\n```", labels=("insult",))
    check("L: a code-fenced JSON object is the only leniency", len(r.events) == 1)
    long_span = {"events": [{"span": [0, 40], "type": "insult", "severity": 0.5}]}
    r, _ = run(" ".join(f"слово{i}" for i in range(60)), long_span)
    check("L: a span that swallows dozens of words is not one event", r.events == [])
    r, m = run("слово " * 200, {"events": [{"span": [0, 1], "type": "insult", "severity": 0.5}]})
    check("L: a very long message is not an interpersonal event: fail closed without calling the model", r.events == [] and m.calls == [])
    r, m = run("   \n ", {"events": []})
    check("L: an empty message -> nothing, no call", r.events == [] and m.calls == [])
    r, m = run(INSULT_MSG, {"events": [{"span": [0, 0], "type": "insult", "severity": 0.5}] * 4})
    check("L: too many proposed events is a malformed answer", r.events == [])

    # ── multi-event turns: isolation ──
    both = "Извини за резкость, но ты всё равно ржавая консерва."
    r, _ = run(both, {"events": [{"span": [0, 2], "type": "apology", "sincerity": 0.7},
                                  {"span": [5, 8], "type": "insult", "severity": 0.6}]}, labels=("exact_act", "exact_act"))
    it = ee.to_intensity(r)
    check("N: an apology and an insult with SEPARATE spans are two independent events", it.is_apology and it.is_insult and it.sincerity == 0.7 and it.severity == 0.6)
    r, _ = run(both, {"events": [{"span": [0, 4], "type": "apology", "sincerity": 0.7},
                                  {"span": [3, 8], "type": "insult", "severity": 0.6}]}, labels=("exact_act", "exact_act"))
    check("N: events whose spans share words are ambiguous: BOTH dropped (one cannot authenticate the other)", r.events == [])
    r, _ = run(both, {"events": [{"span": [0, 2], "type": "apology", "sincerity": 0.7},
                                  {"span": [5, 8], "type": "insult", "severity": 0.6}]}, labels=("exact_act", "quotation"))
    it = ee.to_intensity(r)
    check("N: one event confirmed and the other refused: only the confirmed one survives", it.is_apology and not it.is_insult)
    r, _ = run(both, {"events": [{"span": [0, 2], "type": "promise"}, {"span": [5, 8], "type": "insult", "severity": 0.6}]}, labels=("exact_act", "exact_act"))
    it = ee.to_intensity(r)
    check("O: a promise in the same turn as a grounded insult is dropped (a commitment must be the ONLY event)", it.is_insult and not it.is_promise)
    r, _ = run(both, {"events": [{"span": [0, 2], "type": "promise"}, {"span": [5, 8], "type": "fulfilment_claim"}]}, labels=("exact_act", "exact_act"))
    it = ee.to_intensity(r)
    check("O: a promise together with a claim is contradictory: neither survives", not it.is_promise and not it.claims_fulfilled)
    r, _ = run(both, {"events": [{"span": [0, 2], "type": "apology", "sincerity": 0.9}]}, labels=("exact_act",))
    it = ee.to_intensity(r)
    check("O: evidence of an apology cannot serve a promise: to_intensity carries commitment flags only for a lone commitment",
          it.is_apology and not it.is_promise and not it.claims_fulfilled)

    # ── the check is BLIND and code compares ──
    r, _ = run(INSULT_MSG, {"events": [{"span": [1, 3], "type": "insult", "severity": 0.7}]}, labels=("apology",))
    check("K: the blind check says the fragment expresses a DIFFERENT act than the extractor claimed -> no event", r.events == [])
    r, m1 = run("Ты просто ржавая консерва, от тебя никакого толку.", {"events": [{"span": [1, 3], "type": "insult", "severity": 0.7}]})
    r, m2 = run("Ты просто ржавая консерва, от тебя никакого толку.", {"events": [{"span": [1, 3], "type": "apology", "sincerity": 0.7}]})
    check("K: the check prompt does not depend on the claimed type (blind): same fragment -> identical check question",
          m1.calls[1] == m2.calls[1])
    check("K: the check prompt does not carry the extractor's severity / sincerity either",
          "0.7" not in m1.calls[1][-1]["content"] and "severity" not in m1.calls[1][-1]["content"])

    # ── a commitment must be the ONLY candidate the extractor proposed ──
    msg = "Слово даю: в субботу я приведу друга."
    r, _ = run(msg, {"events": [{"span": [0, 1], "type": "promise"}, {"span": [5, 5], "type": "fulfilment_claim"}]},
               labels=("different_act", "exact_act"))
    check("O: promise + claim proposed, the promise refused by the blind check, the claim 'confirmed' on one word: "
          "the confused turn yields NO commitment event", r.events == [], repr(r.events))
    r, _ = run(msg, {"events": [{"span": [0, 1], "type": "promise"}, {"span": [3, 6], "type": "insult", "severity": 0.6}]},
               labels=("exact_act", "exact_act"))
    check("O: a promise proposed next to another candidate is dropped, the other confirmed event is untouched",
          [e.type for e in r.events] == ["insult"])
    r, _ = run(msg, {"events": [{"span": [0, 5], "type": "promise"}]})
    check("O: a lone promise candidate is admissible", [e.type for e in r.events] == ["promise"])

    # ── transport trouble never becomes an event ──
    r, _ = run(INSULT_MSG, {"events": [{"span": [1, 3], "type": "insult", "severity": 0.6}]}, raise_on="extract")
    check("P: the extractor call failing -> no event, no exception", r.events == [] and r.rejected)
    r, _ = run(INSULT_MSG, {"events": [{"span": [1, 3], "type": "insult", "severity": 0.6}]}, raise_on="check")
    check("P: the check call failing -> no event, no exception", r.events == [])
    r, _ = run(INSULT_MSG, {"events": [{"span": [1, 3], "type": "insult", "severity": 0.6}]}, labels=("Exact_Act",))
    check("P: the label must match exactly", r.events == [])
    r, _ = run("Ты ничтожество, даже простой вопрос не можешь понять.",
               {"events": [{"span": [0, 1], "type": "insult", "severity": 0.8}, {"span": [2, 7], "type": "fulfilment_claim"}]},
               labels=("exact_act", "different_act"))
    check("K: a second event the words do not constitute is refused by the check (different_act); the real one survives",
          [e.type for e in r.events] == ["insult"])

    # ── isolation of the extractor's input ──
    r, m = run(INSULT_MSG, {"events": [{"span": [1, 3], "type": "insult", "severity": 0.6}]})
    prompt_text = " ".join(part["content"] for call in m.calls for part in call)
    check("R: the extractor is shown the current message only (its interface has no memory / history input)",
          list(inspect.signature(ee.extract_relational_events).parameters) == ["message", "llm"] and INSULT_MSG in prompt_text)

    # ── S. structure: no relational vocabulary, no pattern matching, in the recognizer's code ──
    src = inspect.getsource(ee)
    tree = ast.parse(src)
    prompt_names = {"_EXTRACT_SYSTEM", "_CHECK_SYSTEM", "_TYPE_MEANING"}
    skip_ids = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in prompt_names for t in node.targets):
            skip_ids |= {id(n) for n in ast.walk(node)}
    doc_ids = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
               and n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
    literals = [n.value.lower() for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in skip_ids and id(n) not in doc_ids]
    vocabulary = ("оскорб", "извин", "прост", "обещ", "дура", "идиот", "тупая", "выполнил", "сделал", "жаль", "стыдно")
    check("S: no insult/apology/promise vocabulary anywhere in the code outside the model prompts",
          not any(w in lit for lit in literals for w in vocabulary), repr([l for l in literals if any(w in l for w in vocabulary)]))
    check("S: the only regular expression is the whitespace segmentation of the message",
          [c for c in ("re.search", "re.match", "re.findall", "re.sub", "re.compile", "re.split") if c in src] == []
          and src.count("re.finditer(") == 1)

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
