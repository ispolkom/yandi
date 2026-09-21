"""
pet/pet_commitment_verification_regression_test.py — the VERIFICATION protocol
(pet/commitment_verification.py) with a scripted model (no model, no network).

    USER REPORTS FULFILMENT != FULFILMENT VERIFIED.   EXTERNAL SELF-REPORT != DIRECT OBSERVATION.
    THE MODEL MAY POINT TO EVIDENCE; THE CODE OWNS THE EVIDENCE TEXT.
    AMBIGUOUS TARGET != VERIFIED TARGET.   FAIL CLOSED.

Run: python -m pet.pet_commitment_verification_regression_test
"""
from __future__ import annotations

import inspect
import json
import sys

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    import pet.commitment_verification as cv
    from pet.commitment_verification import classify_commitment, verify_direct_fulfilment
    from pet.verification_test_support import scripted_verify_llm
    from agent.relationship_commitments import KIND_EXTERNAL, KIND_IN_CHAT

    P_CODE = {"commitment_id": "c_code", "evidence": "в следующем сообщении дам тебе кодовое слово"}
    P_NUM = {"commitment_id": "c_num", "evidence": "в следующем сообщении дам число от 100 до 999"}

    def run(message, candidates, **kw):
        llm = scripted_verify_llm(**kw)
        return verify_direct_fulfilment(message, llm, candidates), llm

    # ── classification ──
    for answer, want in (("in_chat", KIND_IN_CHAT), ("external", KIND_EXTERNAL), ("unclear", KIND_EXTERNAL), ("", KIND_EXTERNAL), ("IN_CHAT!", KIND_EXTERNAL)):
        check(f"1: a promise classified {answer!r} -> {want}", classify_commitment("Обещаю.", "Обещаю", scripted_verify_llm(classify=answer)) == want)
    def boom(messages):
        raise RuntimeError("transport")
    check("1: a model error classifies as EXTERNAL (nothing gets verified)", classify_commitment("Обещаю.", "Обещаю", boom) == KIND_EXTERNAL)
    check("1: malformed output classifies as EXTERNAL", classify_commitment("Обещаю.", "Обещаю", lambda m: "not json") == KIND_EXTERNAL)
    check("1: no promise words -> EXTERNAL", classify_commitment("Обещаю.", "", scripted_verify_llm()) == KIND_EXTERNAL)

    # ── direct delivery is verified; the evidence is code-reconstructed ──
    msg = "Вот оно: кодовое слово АЛЬФА."
    r, llm = run(msg, [P_CODE], delivery=("АЛЬФА", 0))
    v = r.verified
    check("2: a direct delivery of the promised content is VERIFIED, with the exact evidence sliced from the message by code",
          v is not None and v.commitment_id == "c_code" and v.evidence == "АЛЬФА" and msg[v.start:v.end] == "АЛЬФА")
    check("2: one extraction + one blind judgement per open promise (1 promise = 2 calls)", r.calls == 2 and len(llm.calls) == 2)
    r, _ = run("Держи: 427", [P_NUM], delivery=("427", 0))
    check("2: a number deliverable is verified the same way (no vocabulary of deliverables)", r.verified is not None and r.verified.evidence == "427")
    r, _ = run("Держи: 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", [{"commitment_id": "c_h", "evidence": "в следующем сообщении пришлю SHA256-хэш"}],
               delivery=("9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", 0))
    check("2: a hash deliverable too", r.verified is not None and len(r.verified.evidence) == 64)

    # ── the model may not invent evidence ──
    r, _ = run("Сегодня хорошая погода.", [P_CODE], delivery=("АЛЬФА", 0), delivers=lambda promise, fragment: "АЛЬФА" in fragment)
    check("3: a quote that is not in the message can only point at UNRELATED words of it; a judge that reads the fragment refuses them",
          r.verified is None, repr(r.rejected))
    for text, quote in (("Вот оно: кодовое слово АЛЬФА.", "АЛЬФА"), ("Держи: 427, вот", "427"), ("Сегодня хорошая погода.", "АЛЬФА")):
        rr, _ = run(text, [P_CODE], delivery=(quote, 0))     # the worst-case checker accepts everything
        check(f"3: EVIDENCE IS ALWAYS THE MESSAGE'S OWN BYTES — even against a checker that accepts everything ({text!r})",
              rr.verified is None or text[rr.verified.start:rr.verified.end] == rr.verified.evidence)
    for label, raw in (("out of range", {"delivery": {"span": [0, 99], "target": 0}}), ("not integers", {"delivery": {"span": ["a", "b"], "target": 0}}),
                       ("bad target", {"delivery": {"span": [0, 0], "target": 7}}), ("too long", {"delivery": {"span": [0, 40], "target": 0}})):
        r, _ = run("слово " * 50, [P_CODE], raw=json.dumps(raw)) if label == "too long" else run("Кодовое слово АЛЬФА", [P_CODE], raw=json.dumps(raw))
        check(f"3: {label} -> no verification", r.verified is None and r.rejected)
    r, _ = run("Кодовое слово АЛЬФА", [P_CODE], raw="not json")
    check("3: malformed extractor output -> no verification", r.verified is None)
    r, _ = run("Кодовое слово АЛЬФА", [P_CODE], raw=json.dumps({"delivery": None}))
    check("3: 'nothing delivered' -> no verification and no blind check is even made", r.verified is None and r.calls == 1)

    # ── the blind judgement refuses everything that is not the person's own direct delivery now ──
    reports = {
        "report": ("Я сделал это.", "Я сделал это"),
        "report2": ("Я выполнил обещание.", "Я выполнил обещание"),
        "believe": ("Поверь мне, всё готово.", "Поверь мне, всё готово"),
        "paid": ("Я точно оплатил.", "Я точно оплатил"),
        "hypothetical": ("Представь, что я дал кодовое слово АЛЬФА.", "кодовое слово АЛЬФА"),
        "if_i_had": ("Если бы я выполнил обещание, то сказал бы АЛЬФА.", "сказал бы АЛЬФА"),
        "other_person": ("Мой брат сказал кодовое слово АЛЬФА.", "кодовое слово АЛЬФА"),
        "quotation": ("Ты написала: «кодовое слово АЛЬФА».", "кодовое слово АЛЬФА"),
    }
    for frame, (text, quote) in reports.items():
        bad_frame = {"report": "report", "report2": "report", "believe": "not_delivery", "paid": "report", "hypothetical": "hypothetical",
                     "if_i_had": "hypothetical", "other_person": "other_person", "quotation": "quotation"}[frame]
        r, _ = run(text, [P_CODE], delivery=(quote, 0), delivers=lambda p, f, b=bad_frame: {"delivers": False, "frame": b})
        check(f"4: {text!r} judged {bad_frame} by the blind check -> NOT verified", r.verified is None, repr(r.rejected))
    r, _ = run("Мой брат вот что сказал: кодовое слово АЛЬФА.", [P_CODE], delivery=("кодовое слово АЛЬФА", 0), delivers=lambda p, f: {"delivers": True, "frame": "quotation"})
    check("4: even 'delivers: true' with a frame that is not the person's own delivery NOW is refused", r.verified is None)
    r, _ = run("Кодовое слово АЛЬФА", [P_CODE], delivery=("АЛЬФА", 0), delivers=lambda p, f: {"frame": "made-up"})
    check("4: an unknown frame is refused (fail closed)", r.verified is None)
    r, _ = run("Кодовое слово АЛЬФА", [P_CODE], delivery=("АЛЬФА", 0), delivers=lambda p, f: {"delivers": "yes"})
    check("4: a non-boolean 'delivers' is refused (fail closed)", r.verified is None)

    # ── targets: ambiguity and wrong targets ──
    both = "Вот обещанное: АЛЬФА."
    r, _ = run(both, [P_NUM, P_CODE], delivery=("АЛЬФА", 1))
    check("5: AMBIGUOUS — the fragment passes for two open promises: NO verification (no arbitrary target)", r.verified is None and any("ambiguous" in x for x in r.rejected), repr(r.rejected))
    only_code = lambda p, f: "кодовое слово" in p          # the blind judge accepts the fragment only for the code-word promise
    r, _ = run("Кодовое слово АЛЬФА", [P_NUM, P_CODE], delivery=("АЛЬФА", 1), delivers=only_code)
    check("5: two open promises, the deliverable fits exactly one -> that one is verified", r.verified is not None and r.verified.commitment_id == "c_code")
    r, _ = run("Кодовое слово АЛЬФА", [P_NUM, P_CODE], delivery=("АЛЬФА", 0), delivers=only_code)
    check("5: the extractor names the WRONG promise -> the blind judgements disagree with it -> NO verification (no wrong-target match)", r.verified is None)
    r, _ = run("Кодовое слово АЛЬФА", [P_NUM, P_CODE], delivery=("АЛЬФА", 1), delivers=lambda p, f: False)
    check("5: no open promise accepts the fragment -> no verification", r.verified is None)
    check("5: ONE evidence span verifies at most ONE promise (the result carries a single commitment id)",
          not hasattr(cv.VerifiedDelivery, "__iter__") and set(cv.VerifiedDelivery.__dataclass_fields__) == {"commitment_id", "evidence", "start", "end"})

    # ── fail closed ──
    r, _ = run("Кодовое слово АЛЬФА", [], delivery=("АЛЬФА", 0))
    check("6: no open promises -> nothing to verify, no model call", r.verified is None and r.calls == 0)
    calls = [0]

    def fails_on_blind(messages):
        calls[0] += 1
        if messages[0]["content"] == cv._DELIVERS_SYSTEM:
            raise RuntimeError("timeout")
        return scripted_verify_llm(delivery=("АЛЬФА", 0))(messages)
    r = verify_direct_fulfilment("Кодовое слово АЛЬФА", fails_on_blind, [P_CODE])
    check("6: a blind check that cannot be made -> NOT verified", r.verified is None)
    check("6: a message over the word limit is not verified", verify_direct_fulfilment("слово " * 200, scripted_verify_llm(delivery=("слово", 0)), [P_CODE]).verified is None)
    check("6: an extractor transport error -> not verified, never a raise", verify_direct_fulfilment("Кодовое слово АЛЬФА", boom, [P_CODE]).verified is None)

    # ── the judges are shown the minimum, and no vocabulary of deliverables lives in the module ──
    r, llm = run("Кодовое слово АЛЬФА", [P_CODE], delivery=("АЛЬФА", 0))
    shown = " ".join(m["content"] for call in llm.calls for m in call).lower()
    check("7: the judges are shown no trust, no relationship, no wish to confirm (only the promise words, the exact fragment and the message)",
          not any(w in shown for w in ("доверие", "trust", "обид", "хотим подтвердить", "подтверди")))
    src = inspect.getsource(cv)
    import ast
    tree = ast.parse(src)
    regexes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("compile", "search", "match", "findall")]
    check("7: the module holds no regex or word list for deliverables (segmentation and offset validation only)", not regexes)

    # ── quotations and reported events are refused by CODE, whatever the checker says ──
    for text, quote in (("Он написал: «кодовое слово АЛЬФА».", "АЛЬФА"), ("В книге было написано «кодовое слово СИГМА».", "СИГМА"),
                        ('Она сказала "кодовое слово ГАММА" и ушла.', "ГАММА"), ("Так: „кодовое слово ДЕЛЬТА“.", "ДЕЛЬТА"), ("Незакрытая «кодовое слово ЭТА", "ЭТА")):
        rr, _ = run(text, [P_CODE], delivery=(quote, 0))       # the worst-case checker accepts everything
        check(f"8: {text!r}: evidence inside a quotation is never a delivery (structure, not vocabulary)", rr.verified is None and any("quotation" in x for x in rr.rejected), repr(rr.rejected))
    rr, _ = run("Кодовое слово АЛЬФА, а он сказал «привет».", [P_CODE], delivery=("АЛЬФА", 0))
    check("8: a delivery OUTSIDE the quotation in the same message is still verified", rr.verified is not None and rr.verified.evidence == "АЛЬФА")
    check("8: inside_quotation on plain text is False, on «...» True", not cv.inside_quotation("просто текст", 5) and cv.inside_quotation("он: «текст", 7))
    vres = cv.VerificationResult(verified=cv.VerifiedDelivery("c", "АЛЬФА", 10, 15))
    cv.drop_if_reported(vres, [("fulfilment_claim", 0, 12)])
    check("8: a verified fragment overlapping a confirmed 'fulfilment_claim' span is dropped", vres.verified is None)
    vres = cv.VerificationResult(verified=cv.VerifiedDelivery("c", "АЛЬФА", 10, 15))
    cv.drop_if_reported(vres, [("fulfilment_claim", 0, 9), ("insult", 8, 20), ("apology", 8, 20)])
    check("8: a claim span that does not touch it, or spans of other event types, do not drop it", vres.verified is not None)
    vres = cv.VerificationResult(verified=cv.VerifiedDelivery("c", "АЛЬФА", 10, 15))
    cv.drop_if_reported(vres, [("promise", 14, 30)])
    check("8: a new promise overlapping it drops it too", vres.verified is None)

    # ── MUTANTS of the verification module itself ──
    import types
    src = inspect.getsource(cv)

    def mutated(old_text, new_text):
        assert src.count(old_text) == 1, old_text
        mod = types.ModuleType("pet.commitment_verification__mutant")
        mod.__file__ = cv.__file__
        sys.modules[mod.__name__] = mod
        exec(compile(src.replace(old_text, new_text), cv.__file__, "exec"), mod.__dict__)
        return mod

    # M2: the verifier is allowed to invent evidence: the evidence is the model's own quote, not the message's bytes
    m2 = mutated("    evidence = message[start:end]\n", "    evidence = str(delivery.get('quote') or message[start:end])\n")
    r = m2.verify_direct_fulfilment("Сегодня хорошая погода.", scripted_verify_llm(raw=json.dumps({"delivery": {"span": [0, 0], "target": 0, "quote": "АЛЬФА"}})), [P_CODE])
    check("M2: MUTANT 'the verifier may invent evidence' is CAUGHT (evidence not in the message)",
          r.verified is not None and "Сегодня хорошая погода.".find(r.verified.evidence) < 0)
    r = cv.verify_direct_fulfilment("Сегодня хорошая погода.", scripted_verify_llm(raw=json.dumps({"delivery": {"span": [0, 0], "target": 0, "quote": "АЛЬФА"}})), [P_CODE])
    check("M2: the real module ignores an invented quote (its evidence is the message's own bytes)",
          r.verified is None or "Сегодня хорошая погода.".find(r.verified.evidence) >= 0)
    # M3: an ambiguous target is resolved arbitrarily (the extractor's choice wins even when several promises accept the fragment)
    m3 = mutated("    if passing != [target]:\n", "    if target not in passing:\n")
    r3 = m3.verify_direct_fulfilment("Вот обещанное: АЛЬФА.", scripted_verify_llm(delivery=("АЛЬФА", 1)), [P_NUM, P_CODE])
    check("M3: MUTANT 'an ambiguous target is chosen arbitrarily' is CAUGHT", r3.verified is not None)
    check("M3: the real module refuses the same case", verify_direct_fulfilment("Вот обещанное: АЛЬФА.", scripted_verify_llm(delivery=("АЛЬФА", 1)), [P_NUM, P_CODE]).verified is None)
    # M9': a self-report accepted because the blind check is skipped
    m9 = mutated("        if verdict[\"delivers\"] is True and verdict[\"frame\"] == \"current\":\n", "        if verdict[\"delivers\"] is True:\n")
    rr = m9.verify_direct_fulfilment("Ты написала: «кодовое слово АЛЬФА».", scripted_verify_llm(delivery=("кодовое слово АЛЬФА", 0), delivers=lambda p, f: {"delivers": True, "frame": "quotation"}), [P_CODE])
    check("M9: MUTANT 'the frame (quotation / hypothetical / other person) is ignored' is CAUGHT", rr.verified is not None)

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
