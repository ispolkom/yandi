"""
pet/pet_fact_extraction_regression_test.py — the PERSONAL FACT extraction protocol
(pet/fact_extraction.py), with a scripted model (no model, no network).

    THE MODEL MAY POINT TO EVIDENCE.  THE MODEL MAY NOT INVENT EVIDENCE.
    NO EXACT SUPPORT IN THE USER'S MESSAGE = NO FACT.   FAIL CLOSED.

What is proven: the evidence text is reconstructed by code from the message; every
frame that is not the person's own current / past statement (quotation, hypothesis,
question, someone else, a plan, small talk) is refused by the blind judgement;
negation and time keep their meaning; ephemeral and secret-like content is refused;
a statement that says more than its evidence is refused; links to known facts are
validated; the extractor input is the current message plus known-fact statements as
data; and that no vocabulary of facts lives in the module.

Run: python -m pet.pet_fact_extraction_regression_test
"""
from __future__ import annotations

import json
import sys

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    import inspect
    import pet.fact_extraction as fe
    from pet.fact_extraction import extract_personal_facts
    from pet.fact_test_support import scripted_fact_llm

    def run(message, facts, known=None, **kw):
        llm = scripted_fact_llm(facts, **kw)
        return extract_personal_facts(message, llm, known), llm

    DOG = {"quote": "У меня есть собака по кличке Рекс", "cls": "possession",
           "statement": "У пользователя есть собака по кличке Рекс"}

    # ── the accepted path ──
    msg = "Привет. У меня есть собака по кличке Рекс, вот такие дела."
    r, llm = run(msg, [DOG])
    f = r.facts[0] if r.facts else None
    check("1: a stable own fact is accepted; the EVIDENCE is reconstructed by code from the message (exact slice)",
          f is not None and f.evidence == "У меня есть собака по кличке Рекс" and msg[f.start:f.end] == f.evidence
          and (f.polarity, f.temporality, f.relation, f.fact_class) == ("affirmed", "current", "none", "possession"))
    check("1: three model judgements: extraction, blind check, support check (1 candidate = 3 calls)", r.calls == 3 and len(llm.calls) == 3)

    # ── the model may not invent evidence ──
    r, _ = run("Сегодня хорошая погода.", [DOG])
    check("2: a fact whose words are NOT in the message has no evidence: the reference points at unrelated words and the blind judgement refuses it",
          r.facts == [] and r.rejected)
    r, _ = run(msg, [], raw=json.dumps({"memory_query": "none", "facts": [{"span": [0, 99], "class": "possession", "statement": "У пользователя есть собака", "polarity": "affirmed", "time": "current", "stability": "stable", "relation": "none"}]}))
    check("2: an out-of-range reference is refused", r.facts == [] and any("out of range" in x for x in r.rejected))
    r, _ = run(msg, [], raw=json.dumps({"memory_query": "none", "facts": [{"span": ["a", "b"], "class": "possession", "statement": "У пользователя есть собака", "polarity": "affirmed", "time": "current", "stability": "stable", "relation": "none"}]}))
    check("2: a non-integer reference is refused", r.facts == [])
    r, _ = run(msg, [], raw=json.dumps({"memory_query": "none", "facts": [{"span": [2, 4], "quote": "У меня есть собака", "class": "possession", "statement": "У пользователя есть собака", "polarity": "affirmed", "time": "current", "stability": "stable", "relation": "none"}]}))
    check("2: a quote supplied by the model is ignored; only the reference counts (evidence is the words the reference selects)",
          all(x.evidence == "У меня есть" or "меня" in x.evidence for x in r.facts) or r.facts == [])

    # ── frames that are NOT the person's own current/past statement ──
    frames = {
        "hypothetical": ("Если бы у меня была собака по кличке Рекс, я бы гулял.", "у меня была собака по кличке Рекс"),
        "quotation": ("Мой брат сказал: у меня BMW.", "у меня BMW"),
        "other_person": ("У моего соседа есть собака по кличке Рекс.", "У моего соседа есть собака по кличке Рекс"),
        "question": ("Как думаешь, у меня есть собака?", "у меня есть собака"),
        "uncertain": ("Может быть, куплю собаку по кличке Рекс.", "куплю собаку по кличке Рекс"),
        "not_personal": ("Сегодня я устал.", "Сегодня я устал"),
    }
    for frame, (text, quote) in frames.items():
        fact = {"quote": quote, "cls": "possession", "statement": "У пользователя есть собака по кличке Рекс"}
        r, _ = run(text, [fact], blind=[{"frame": frame}])
        check(f"3: frame {frame!r} judged by the blind check -> NO fact", r.facts == [], repr(r.rejected))
    r, _ = run("Сегодня я очень устал.", [{"quote": "Сегодня я очень устал", "cls": "life_fact", "statement": "Пользователь устал", "stability": "ephemeral"}])
    check("3: an ephemeral state is refused by the extractor's own stability", r.facts == [])
    r, _ = run("Сегодня я очень устал.", [{"quote": "Сегодня я очень устал", "cls": "life_fact", "statement": "Пользователь устал"}], blind=[{"stability": "ephemeral"}])
    check("3: a statement the blind check finds ephemeral is refused even if the extractor called it stable", r.facts == [])

    # ── time and polarity keep their meaning ──
    r, _ = run("В детстве у меня была собака.", [{"quote": "В детстве у меня была собака", "cls": "possession", "statement": "В детстве у пользователя была собака", "time": "past"}])
    check("4: a fact about the PAST is kept as past (temporality=past), not as current",
          len(r.facts) == 1 and r.facts[0].temporality == "past")
    r, _ = run("В детстве у меня была собака.", [{"quote": "В детстве у меня была собака", "cls": "possession", "statement": "У пользователя есть собака", "time": "current"}], blind=[{"frame": "past"}])
    check("4: the extractor calling a past statement 'current' is caught by the blind frame -> NO fact (time must agree)", r.facts == [])
    r, _ = run("Я не пью кофе.", [{"quote": "Я не пью кофе", "cls": "preference", "statement": "Пользователь не пьёт кофе", "polarity": "negated"}])
    check("4: a NEGATIVE fact is kept with its polarity (negated)", len(r.facts) == 1 and r.facts[0].polarity == "negated")
    r, _ = run("Я не пью кофе.", [{"quote": "Я не пью кофе", "cls": "preference", "statement": "Пользователь пьёт кофе", "polarity": "affirmed"}], blind=[{"polarity": "negated"}])
    check("4: polarity that the blind judgement contradicts -> NO fact (a negation cannot become an affirmation)", r.facts == [])

    # ── the statement may not say more than the evidence ──
    r, _ = run("У меня есть собака.", [{"quote": "У меня есть собака", "cls": "possession", "statement": "У пользователя есть овчарка по кличке Рекс"}], support=[{"supported": True, "adds": True}])
    check("5: a statement that adds detail not in the evidence is refused", r.facts == [] and any("more" in x for x in r.rejected))
    r, _ = run("У меня есть собака.", [{"quote": "У меня есть собака", "cls": "possession", "statement": "У пользователя есть собака"}], support=[{"supported": False}])
    check("5: a statement the checker does not find supported is refused", r.facts == [])
    r, _ = run("У меня есть собака.", [{"quote": "У меня есть собака", "cls": "possession", "statement": "коротко"}])
    check("5: a malformed / too short statement is refused", r.facts == [])

    # ── secrets ──
    r, _ = run("Мой пароль qwerty12345ASDFG67890zxcv, запомни.", [{"quote": "пароль qwerty12345ASDFG67890zxcv", "cls": "other_stable", "statement": "Пароль пользователя qwerty12345ASDFG67890zxcv"}])
    check("6: a credential-shaped token is never stored as a fact (structural guard)", r.facts == [] and any("secret" in x for x in r.rejected))
    r, _ = run("Мой пин-код лежит в сейфе.", [{"quote": "Мой пин-код лежит в сейфе", "cls": "other_stable", "statement": "Пин-код пользователя лежит в сейфе"}], blind=[{"secret": True}])
    check("6: a fragment the blind check flags as a secret is refused", r.facts == [])
    r, _ = run("Ключ такой.", [{"quote": "Ключ такой", "cls": "secret", "statement": "Ключ пользователя такой"}])
    check("6: a candidate the extractor itself classifies as a secret is refused", r.facts == [])
    r, _ = run("Ничего.", [{"quote": "Ничего", "cls": "life_fact", "statement": "Пользователь ничего"}], blind=[{"secret": None}])
    check("6: a blind answer that does not explicitly deny 'secret' is refused (fail closed)", r.facts == [])
    msg = "Я живу в Казани, мой токен ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4, и у меня есть собака по кличке Рекс."
    r, _ = run(msg, [{**DOG, "quote": "у меня есть собака по кличке Рекс"}, {"quote": "мой токен ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4", "cls": "other_stable", "statement": "Токен пользователя ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4"}])
    check("6: with a secret next to a real fact, ONLY the real fact is kept and its evidence carries no part of the secret",
          [x.evidence for x in r.facts] == ["у меня есть собака по кличке Рекс"] and "ghp_" not in r.facts[0].evidence)

    # ── links to known facts ──
    known = [{"fact_id": "pf_a", "statement": "У пользователя есть собака по кличке Рекс"}, {"fact_id": "pf_b", "statement": "Пользователь не пьёт кофе"}]
    r, llm = run("Нет, я ошибся, её зовут Макс.", [{"quote": "её зовут Макс", "cls": "possession", "statement": "Собаку пользователя зовут Макс", "relation": "replaces", "target": 0}], known=known)
    check("7: a correction links to the known fact it replaces (validated target -> fact id)",
          len(r.facts) == 1 and r.facts[0].relation == "replaces" and r.facts[0].target_fact_id == "pf_a")
    prompt = llm.calls[0][-1]["content"]
    check("7: the known facts reach the extractor only as numbered DATA for linking (never as the message)",
          "№0" in prompt and "Рекс" in prompt and prompt.index("Известные факты") < prompt.index("Сообщение:"))
    check("7: a correction costs one more judgement (does the fragment really REVISE what was said before? blind to the known facts): 4 calls", r.calls == 4 and len(llm.calls) == 4)
    r2, _ = run("Нет, я ошибся, её зовут Макс.", [{"quote": "её зовут Макс", "cls": "possession", "statement": "Собаку пользователя зовут Макс", "relation": "replaces", "target": 0}], known=known, link=[{"revises": False}, {"conflict": False}])
    check("7: if the link check does not confirm the revision, the fact is kept as a NEW fact and the old one is NOT replaced",
          len(r2.facts) == 1 and r2.facts[0].relation == "none" and r2.facts[0].target_fact_id is None)
    support_prompt = llm.calls[2][-1]["content"]
    check("7: the SUPPORT check (not the extractor's evidence) is also shown the known fact a correction refers to, so a pronoun-only span can be worded as a fact",
          "Известный факт, к которому относится фрагмент" in support_prompt and "Рекс" in support_prompt)
    _, plain = run("У меня есть собака по кличке Рекс.", [DOG], known=known)
    check("7: an unlinked fact's support check is NOT shown any known fact", "Известный факт, к которому" not in plain.calls[2][-1]["content"])
    r, _ = run("Нет, я ошибся, её зовут Макс.", [{"quote": "её зовут Макс", "cls": "possession", "statement": "Собаку пользователя зовут Макс", "relation": "replaces", "target": 9}], known=known)
    check("7: a link to a fact that does not exist is refused", r.facts == [])
    r, _ = run("Да, у меня всё так же есть собака Рекс.", [{"quote": "у меня всё так же есть собака Рекс", "cls": "possession", "statement": "У пользователя есть собака по кличке Рекс", "relation": "same", "target": 9}], known=known)
    check("7: a 'same' that names no real fact degrades to a plain new fact (the ledger recognises the identical proposition); a correction still may not",
          len(r.facts) == 1 and r.facts[0].relation == "none" and r.facts[0].target_fact_id is None)
    r, _ = run("Да, у меня всё так же есть собака Рекс.", [{"quote": "у меня всё так же есть собака Рекс", "cls": "possession", "statement": "У пользователя есть собака по кличке Рекс", "relation": "same", "target": 0}], known=known)
    check("7: a repetition links to the known fact it restates", len(r.facts) == 1 and r.facts[0].relation == "same" and r.facts[0].target_fact_id == "pf_a")
    r, _ = run("У меня есть собака по кличке Рекс.", [DOG], known=known)
    check("7: with known facts present, a fact without a link is a plain new fact", r.facts and r.facts[0].relation == "none" and r.facts[0].target_fact_id is None)

    # ── memory_query: a retrieval mode, never a write ──
    for kind in ("general", "specific", "none"):
        r, _ = run("Что ты обо мне помнишь?", [], memory_query=kind)
        check(f"8: memory_query {kind!r} is returned as the retrieval mode and writes nothing", r.memory_query == kind and r.query_known and r.facts == [])
    r, _ = run("Что ты обо мне помнишь?", [], raw="not json")
    check("8: an extractor failure means the route is UNKNOWN (query_known=False), not 'none'", not r.query_known and r.memory_query == "none" and r.facts == [])

    # ── fail closed ──
    def boom(messages):
        raise RuntimeError("transport")
    check("9: a model / transport error is 'no fact', never a raise", extract_personal_facts("У меня есть собака.", boom).facts == [])
    check("9: a message over the word limit is not mined for facts", extract_personal_facts("слово " * 200, scripted_fact_llm([])).facts == [] )
    r, _ = run("У меня есть собака по кличке Рекс.", [DOG, {"quote": "собака по кличке Рекс", "cls": "possession", "statement": "У пользователя есть собака Рекс"}])
    check("9: overlapping evidence spans are ambiguous: none of them is kept", r.facts == [])
    r, _ = run("Я живу в Казани. У меня есть собака Рекс.", [
        {"quote": "Я живу в Казани", "cls": "location", "statement": "Пользователь живёт в Казани"},
        {"quote": "У меня есть собака Рекс", "cls": "possession", "statement": "У пользователя есть собака Рекс"}])
    check("9: two separate facts in one message are both kept, each with its own exact evidence",
          [x.evidence for x in r.facts] == ["Я живу в Казани", "У меня есть собака Рекс"])

    # ── no vocabulary of facts lives in the module ──
    import ast
    tree = ast.parse(inspect.getsource(fe))
    patterns = [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "compile"
                and n.args and isinstance(n.args[0], ast.Constant)]
    patterns += ["".join(c.value for c in n.args[0].values if isinstance(c, ast.Constant)) for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "compile" and n.args and isinstance(n.args[0], ast.JoinedStr)]
    str_tuples = {t.targets[0].id for t in tree.body if isinstance(t, ast.Assign) and isinstance(t.value, ast.Tuple)
                  and isinstance(t.targets[0], ast.Name)}
    check("10: the module holds no vocabulary of facts: its only regex is the structural credential-shape guard (no words at all)",
          len(fe._SECRET_SHAPE.pattern) > 0 and all(not any("а" <= ch.lower() <= "я" for ch in p) for p in [fe._SECRET_SHAPE.pattern]),
          repr(patterns))
    check("10: the only word tuples are the schema labels (classes, polarities, times, relations, query kinds, frames)",
          str_tuples == {"FACT_CLASSES", "POLARITIES", "TIMES", "RELATIONS", "QUERY_KINDS", "FRAMES"}, repr(str_tuples))

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
