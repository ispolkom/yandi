"""
llm_gateway/native_parity_test.py — доказательство, что РОДНОЙ Rust-шлюз (rustlib/yandi_llm) даёт те же ответы, что Python `llm_gateway`
на чистой (без сети) части: сборка сообщений, контракты вывода, инструкции, разбор «реплика + внутреннее состояние», отпечаток векторного пространства.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Тридцать девятый шаг переноса (2026-09-24) и ПЕРВЫЙ шаг родного шлюза (`rustlib/yandi_llm`, без Python внутри; здесь используется только мост
«JSON внутрь → JSON наружу» `yandi_llm.call`). Сравнение — по значению И по порядку ключей (`json.dumps` без сортировки). Проверяется:
  * `_build_messages` / `_append_system_instruction` (system строкой/списком/пустым, пустые элементы, messages против prompt);
  * `_contract_from_response_format`, `_strip_think_blocks` (закрытые/незакрытые/многострочные/вложенные теги), `_looks_like_internal_state_fragment`;
  * `_state_schema_prompt_hint` на случайных схемах (типы, minimum/maximum как int/float/str, required, description любого типа, не-dict spec) —
    включая Python-точное `str()` чисел (`1e-07`, `1e+16`, `0.1`);
  * `_semantic_result_schema`, `_semantic_contract_from_target` по всем комбинациям возможностей бэкенда;
  * `_normalize_semantic_completion`: структурный и «маркерный» режимы, все ветки ошибок с ТОЧНЫМИ текстами `json.loads`, state обязателен/нет,
    схема с required, think-блоки, мусор; полный результат вместе с metadata;
  * `VectorSpaceId.fingerprint/to_dict` и `compatible`.
ИЗВЕСТНЫЕ РАСХОЖДЕНИЯ (родной Rust, не Python-объекты; в тесте не проверяются): JSON с NaN/Infinity или целыми вне i64 в поле `state` (значения строятся
с потерей точности), одинокие суррогаты. Валидность JSON и тексты ошибок при этом точные.

Требует собранного моста (`cd rustlib/yandi_llm && maturin develop --release`) — если не установлен, SKIP.

Run: python -m llm_gateway.native_parity_test
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import random
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
_OK = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _OK
    if condition:
        _OK += 1
    else:
        if len(FAILURES) < 25:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    try:
        import yandi_llm
    except ImportError as e:
        print(f"SKIP: yandi_llm не собран ({e}) — cd rustlib/yandi_llm && maturin develop --release")
        return 0

    from llm_gateway import client as c
    from llm_gateway import vector_space as vs
    from llm_gateway.types import BackendCapabilities, OutputContract, SemanticOutputRequirement

    rnd = random.Random(20260924)

    def rs(name, **args):
        return json.loads(yandi_llm.call(name, json.dumps(args, ensure_ascii=False)))

    def same(a, b) -> bool:
        return json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)

    n = 0
    # ---- A. сборка сообщений -------------------------------------------------------------------------------------------------
    SYS = [None, "", "x", "система", [], ["a"], ["a", "", "b"], ["", ""], ["один", "два", "три"], "  "]
    PROMPT = [None, "", "привет", "line\nbreak"]
    MSGS = [None, [], [{"role": "user", "content": "x"}], [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a", "extra": 1}]]
    for s, p, m in itertools.product(SYS, PROMPT, MSGS):
        args = {"system": s, "prompt": p, "messages": m}
        want = c._build_messages(p, s, m)
        got = rs("build_messages", **args)
        n += 1
        check("A1 _build_messages", same(want, got), f"{args!r}: {want} vs {got}")
    for s in SYS:
        for ins in ("инструкция", "", "x"):
            want = c._append_system_instruction(s, ins)
            got = rs("append_system_instruction", system=s, instruction=ins)
            n += 1
            check("A2 _append_system_instruction", same(want, got), f"{s!r} {ins!r}")

    # ---- B. контракты, think, «голое состояние» ------------------------------------------------------------------------------------
    for rf in (None, "json", "text", "JSON", "", "json ", {"type": "object"}, 5, ["json"]):
        w = dataclasses.asdict(c._contract_from_response_format(rf))
        g = rs("contract_from_response_format", response_format=rf)
        n += 1
        check("B1 _contract_from_response_format", same(w, g), f"{rf!r}: {w} vs {g}")
    THINK = ["", "текст", "<think>x</think>ответ", "<think>a</think><think>b</think>ок", "<think>без закрытия", "без открытия</think> текст", "<THINK>x</THINK>y",
             "<think>\nмного\nстрок\n</think>\nответ", "<think><think>вложенный</think></think>х", "до <think>среднее</think> после <think>ещё</think> конец",
             "<think></think>", "<think>x</think", "</think><think>", " <think>x</think> ", "a<think>b</think>c</think>d<think>e"]
    for t in THINK:
        n += 1
        check("B2 _strip_think_blocks", c._strip_think_blocks(t) == rs("strip_think_blocks", text=t), repr(t))
    FRAG = ["", " ", "привет", '{"is_insult": false, "severity": 0.5}', '{"is_insult": false}', 'is_insult severity', 'severity', ' {"is_apology": true, "sincerity": 1} ',
            '{"is_insult": 1, "x": 2, "severity": 3}', '[{"is_insult": 1, "severity": 2}]', '{"is_insult":', '{"a": NaN, "is_insult": 1, "severity": 2}', '\x1c{"severity":1,"sincerity":2}\x1d',
            ' {"severity":1,"sincerity":2}', '{"severity": 1, "severity": 2}', 'текст is_apology и sincerity внутри', '{"nested": {"is_insult": 1, "severity": 2}}',
            '{"is_insult": 1, "severity": 2} tail', '{}', '{"is_insult": null, "is_apology": null}', 'x{"is_insult": 1, "severity": 2}']
    for t in FRAG:
        n += 1
        check("B3 _looks_like_internal_state_fragment", c._looks_like_internal_state_fragment(t) == rs("looks_like_internal_state_fragment", text=t), repr(t))

    # ---- C. подсказка по схеме состояния ----------------------------------------------------------------------------------------
    TYPES = ["number", "boolean", "string", "integer", "object", None, 5, ["a", "b"], True, 1.5, "any"]
    NUMS = [0, 1, -1, 0.5, 1.0, 100, 1e-7, 1e16, 1.5e-5, 123456.789, 0.1, 2.5, "0", "abc", None, True, 1e22, 5e-324, 10 ** 15, 10 ** 16, 1e15, 9.5e15, 123456789012345.6, 1e14, 0.0001, 0.00012, 0.00001234, 12345678901234567.0, -1e-7, -2.5e16]
    DESCR = [None, "", "описание", 0, 5, 0.0, 1.5, [], ["x"], {"a": 1}, {}, "it's", 'a "q"', True, False]
    for _ in range(4000):
        props = {}
        for i in range(rnd.randrange(0, 5)):
            spec = {}
            if rnd.random() < 0.85:
                spec["type"] = rnd.choice(TYPES)
            if rnd.random() < 0.6:
                spec["minimum"] = rnd.choice(NUMS)
            if rnd.random() < 0.6:
                spec["maximum"] = rnd.choice(NUMS)
            if rnd.random() < 0.4:
                spec["description"] = rnd.choice(DESCR)
            if rnd.random() < 0.08:
                spec = rnd.choice([None, 5, "x", [], ["a"]])
            props[rnd.choice(["is_insult", "severity", "is_apology", "sincerity", "mood", "имя", "a", "b", "c"]) + str(i)] = spec
        schema = {"type": "object"}
        if rnd.random() < 0.9:
            schema["properties"] = props
        if rnd.random() < 0.5:
            schema["required"] = rnd.choice([list(props)[:2], [], list(props), None, ["x"], "abc", "", {"a0": 1, "b1": 2}, [1, "a"], False, 0])
        if rnd.random() < 0.05:
            schema = rnd.choice([None, {}, [], "x", 5, {"properties": []}, {"properties": {}}, {"properties": None}])
        n += 1
        w = c._state_schema_prompt_hint(schema)
        g = rs("state_schema_prompt_hint", state_schema=schema)
        check("C1 _state_schema_prompt_hint", w == g, f"{schema!r}:\n {w!r}\n {g!r}")

    # ---- D. схема результата и контракт по возможностям ------------------------------------------------------------------------------
    SCHEMAS = [None, {}, {"type": "object"}, {"type": "object", "properties": {"severity": {"type": "number", "minimum": 0, "maximum": 1}}, "required": ["severity"]},
               {"required": ["a", "b"]}, {"required": "abc"}, {"required": [1, "a"]}, {"properties": {"x": {"type": "string"}}}]
    for kind, sch, rr, sr in itertools.product(["reply_state", "other", ""], SCHEMAS, [True, False], [True, False]):
        req = {"kind": kind, "state_schema": sch, "reply_required": rr, "state_required": sr}
        preq = SemanticOutputRequirement(kind=kind, state_schema=sch, reply_required=rr, state_required=sr)
        n += 1
        check("D1 _semantic_result_schema", same(c._semantic_result_schema(preq), rs("semantic_result_schema", requirement=req)), f"{req}")
        for js, jo in itertools.product([False, True], [False, True]):
            caps = {"plain_text": True, "json_object": jo, "json_schema": js, "streaming": False}
            tgt = types.SimpleNamespace(capabilities=BackendCapabilities(**caps))
            try:
                wc, wi = c._semantic_contract_from_target(preq, tgt)
                w = {"contract": dataclasses.asdict(wc), "instruction": wi}
            except c.LLMError as e:
                w = {"error": str(e)}
            g = rs("semantic_contract_from_target", requirement=req, capabilities=caps)
            n += 1
            check("D2 _semantic_contract_from_target", same(w, g), f"{req} {caps}:\n {w}\n {g}")

    # ---- E. разбор ответа модели ---------------------------------------------------------------------------------------------------
    REPLY = ["Привет!", "  Привет  ", "", "   ", None, 5, ["x"], "🌍", "a\nb"]
    STATE = [None, {}, {"severity": 0.5}, {"severity": 0.5, "is_insult": False}, {"is_insult": True, "severity": 1, "extra": [1, 2]}, [], "x", 5, False, {"a": {"b": None}}]
    CONTENT_EXTRA = ["", "```json\n{}\n```", "не JSON", "{", "[]", "null", "5", '"str"', "{}", '{"reply": "x"}', '{"state": {}}', '{"reply": "x", "state": null}', '{"reply":"x","reply":"y"}']
    reqs = [SemanticOutputRequirement(kind="reply_state", state_schema=sch, reply_required=rr, state_required=sr)
            for sch in SCHEMAS[:5] for rr in (True, False) for sr in (True, False)]
    contracts = ["semantic_json_schema", "semantic_json_object", "semantic_legacy_marker", "plain_text"]
    made = 0
    for _ in range(9000):
        rq = rnd.choice(reqs)
        cn = rnd.choice(contracts)
        r_ = rnd.choice(REPLY)
        s_ = rnd.choice(STATE)
        kind = rnd.random()
        if kind < 0.35:
            d = {}
            if rnd.random() < 0.95:
                d["reply"] = r_
            if rnd.random() < 0.9:
                d["state"] = s_
            content = json.dumps(d, ensure_ascii=False)
        elif kind < 0.6:
            content = (r_ if isinstance(r_, str) else "") + rnd.choice(["\n", " ", "\n\n", ""]) + "###YANDI_STATE###" + rnd.choice(["", " ", "\n"]) + json.dumps(s_, ensure_ascii=False) + rnd.choice(["", " tail", "\n"])
        elif kind < 0.7:
            content = rnd.choice(["", "  "]) + (r_ if isinstance(r_, str) else "x") + "###YANDI_STATE###" + rnd.choice(["", "мусор", "{", "}{", '{"a": }', "{'a': 1}", "x {\"a\": 1} y {\"b\": 2} z", '{"severity": 0.5}'])
        elif kind < 0.8:
            content = rnd.choice(THINK) + rnd.choice(CONTENT_EXTRA)
        elif kind < 0.9:
            body = json.dumps({"reply": r_, "state": s_}, ensure_ascii=False)
            content = "<think>рассуждение</think>" + rnd.choice(["", "\n", "  "]) + body
        else:
            content = rnd.choice(CONTENT_EXTRA + FRAG + ["текст ответа", "ответ ###YANDI_STATE### {\"a\":1} ###YANDI_STATE### {\"b\":2}"])
        meta = rnd.choice([{}, {"done_reason": "stop", "eval_count": 5}, {"_llm_gateway_semantic": {"old": 1}, "z": 1}, {"a": 1, "b": [1, 2]}])
        contract = OutputContract(name=cn, response_format=None)
        w = c._normalize_semantic_completion(content, requirement=rq, contract=contract, metadata=dict(meta))
        g = rs("normalize_semantic_completion", content=content, requirement=dataclasses.asdict(rq), contract=dataclasses.asdict(contract), metadata=meta)
        n += 1
        made += 1
        check("E1 _normalize_semantic_completion", same(dataclasses.asdict(w), g), f"{content!r} {rq} {cn}:\n {dataclasses.asdict(w)}\n {g}")
    # точные тексты ошибок json.loads
    for bad in ["", "{", "{'a': 1}", '{"a" 1}', '{"a": 1,}', "[1,2", "nul", '"abc', '{"a": 1} x', "﻿{}", "{\"a\": tru}", '{"a":\n\n 1 2}', "  \n"]:
        for cn in ("semantic_json_object", "semantic_legacy_marker"):
            rq = SemanticOutputRequirement(kind="reply_state", state_required=False)
            content = bad if cn == "semantic_json_object" else "ответ###YANDI_STATE###" + bad
            w = c._normalize_semantic_completion(content, requirement=rq, contract=OutputContract(name=cn, response_format=None), metadata={})
            g = rs("normalize_semantic_completion", content=content, requirement=dataclasses.asdict(rq), contract={"name": cn, "response_format": None}, metadata={})
            n += 1
            check("E2 тексты ошибок json.loads", same(dataclasses.asdict(w), g), f"{content!r}:\n {w.error!r}\n {g.get('error')!r}")
    # состояние обязательно / валидно
    for st in STATE + [{"a": 1, "b": 2}, {"a": 1}]:
        for sch in SCHEMAS:
            for sr in (True, False):
                rq = SemanticOutputRequirement(kind="reply_state", state_schema=sch, state_required=sr)
                w = c._state_valid_for_requirement(st, rq)
                g = rs("state_valid_for_requirement", state=st, requirement=dataclasses.asdict(rq))
                n += 1
                check("E3 _state_valid_for_requirement", w == g, f"{st!r} {sch!r} {sr}")

    # ---- F. векторное пространство --------------------------------------------------------------------------------------------------
    for _ in range(1500):
        d = {"backend": rnd.choice(["llamacpp", "remote", "ollama", "имя"]), "protocol": rnd.choice(["p1", "p|2", ""]), "model": rnd.choice(["m", "m|x", "модель", ""]),
             "dimension": rnd.choice([1, 3, 768, 4096, 0, -1]), "normalized": rnd.choice([True, False]), "schema_version": rnd.choice([1, 2, 0])}
        pid = vs.VectorSpaceId(**d)
        n += 1
        check("F1 fingerprint/to_dict", same(pid.to_dict(), rs("vector_fingerprint", id=d)), f"{d}")
    ids = [vs.VectorSpaceId("a", "p", "m", 3, False), vs.VectorSpaceId("a", "p", "m", 4, False), vs.VectorSpaceId("a", "p", "m", 3, True)]
    sides = [None, vs.UNKNOWN_VECTOR_SPACE, *ids, *(i.fingerprint() for i in ids), "abc"]
    for a, b in itertools.product(sides, sides):
        def enc(x):
            return dataclasses.asdict(x) if isinstance(x, vs.VectorSpaceId) else x
        n += 1
        check("F2 compatible", vs.compatible(a, b) == rs("vector_compatible", a=enc(a), b=enc(b)), f"{a!r} {b!r}")

    print(f"\n(сравнений: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
