"""
llm_gateway/native_client_parity_test.py — доказательство, что РОДНОЙ Rust-шлюз (rustlib/yandi_llm/src/client.rs: resolve_target, complete, complete_with_meta, complete_semantic, embed)
делает то же, что llm_gateway/client.py, включая ГЛАВНЫЙ ИНВАРИАНТ «явный выбор владельца узла сильнее любого отката».

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Схема как в native_remote_parity_test: ОДИН локальный HTTP-сервер получает запросы и от Python, и от Rust; сравнивается (1) ВСЁ, что ушло на сервер (сырой путь, заголовки, тело с порядком ключей),
(2) результат — текст, метаданные вместе с `_llm_gateway_trace` (адаптер, источник, причина выбора, возможности, контракт вывода, класс и текст ошибки) — либо ошибка (класс + текст).
Настройки узла подменяются ОДНОЙ и той же картой «имя → запись» (и Python, и Rust); встроенный движок недоступен, как в песочнице (`Llama is None`).
Цели: явная настройка remote openai/anthropic (с ключом/без, с/без `model`, `protocol` по умолчанию), плохой протокол, неизвестный backend, запись без backend, llamacpp с несуществующим файлом,
запись без обязательного поля (KeyError), исключение хранилища; НЕ настроенное имя → Ollama-фоллбэк; встроенный реестр (включён и файл есть) → провал движка → откат на Ollama ТОЛЬКО для builtin-цели;
явный base_url ≠ по умолчанию → Ollama-совместимая цель. Ответы сервера: норма / 500 / мусор. Параметры: prompt и messages, system строкой и списком, json-режим, strip_think, temperature/max_tokens/stop/extra_options.
Семантика: три контракта (json_schema у Ollama, json_object у OpenAI, «маркерный» у Anthropic) × ответы модели. Эмбеддинги: настроено remote / не настроено (Ollama /api/embed) / битые батчи / отказ Anthropic.
РАСХОЖДЕНИЯ (не считаются ошибкой): деталь ошибки СОЕДИНЕНИЯ (текст исключения Python) — там сравнивается класс и наличие ключевой фразы.

Требует собранного моста yandi_llm — если не установлен, SKIP.

Run: python -m llm_gateway.native_client_parity_test
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import sys
import tempfile
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
        print(f"SKIP: yandi_llm не собран ({e})")
        return 0
    from llm_gateway import client as c
    from llm_gateway import llamacpp_backend as lb
    from llm_gateway.native_remote_parity_test import Server
    from llm_gateway.types import SemanticOutputRequirement

    srv = Server()
    SRV = srv.url
    tmp = Path(tempfile.mkdtemp(prefix="yandi-client-parity-"))
    reg_file = tmp / "registered.gguf"
    reg_file.write_bytes(b"x")

    CONFIG = {
        "m_openai": {"backend": "remote", "protocol": "openai", "base_url": SRV, "model": "real-openai", "api_key_env": "YANDI_TEST_KEY"},
        "m_openai_slash": {"backend": "remote", "protocol": "openai", "base_url": SRV + "//", "model": "real-openai"},
        "m_anth": {"backend": "remote", "protocol": "anthropic", "base_url": SRV, "model": "claude-x", "api_key_env": "YANDI_TEST_KEY"},
        "m_openai_nomodel": {"backend": "remote", "base_url": SRV},
        "m_badproto": {"backend": "remote", "protocol": "grpc", "base_url": SRV},
        "m_badbackend": {"backend": "weird"},
        "m_nobackend": {},
        "m_llama_missing": {"backend": "llamacpp", "path": "/nonexistent/x.gguf", "n_ctx": 4096},
        "m_llama_nopath": {"backend": "llamacpp"},
        "m_remote_nobase": {"backend": "remote"},
        "m_raise": {"__raise__": "ошибка хранилища: база повреждена"},
        "модель-юникод 🌍": {"backend": "remote", "protocol": "openai", "base_url": SRV, "model": "юникод"},
    }
    REGISTRY = {"builtin-m": str(reg_file), "builtin-missing": "/nonexistent/none.gguf"}
    state = {"local_enabled": False}

    def mock_get(model):
        e = CONFIG.get(model)
        if e is None:
            return None
        if "__raise__" in e:
            raise RuntimeError(e["__raise__"])
        return dict(e)

    # подмена настроек и реестра в Python-оригинале
    c.node_config.get_model_entry = mock_get
    c.DEFAULT_BASE_URL = SRV
    lb._MODEL_REGISTRY = {k: lb.ModelSpec(path=v) for k, v in REGISTRY.items()}
    os.environ["YANDI_TEST_KEY"] = "sk-test"
    engine_reason = str(lb._import_error)

    def norm_req(r):
        body = r["body"]
        try:
            body = json.dumps(json.loads(body), ensure_ascii=False)
        except ValueError:
            pass
        return {"path": r["path"], "headers": dict(r["headers"]), "body": body}

    n = 0

    def rs(name, **args):
        args.update({"config": CONFIG, "registry": REGISTRY, "local_enabled": state["local_enabled"], "engine_reason": engine_reason, "default_base_url": SRV})
        return json.loads(yandi_llm.call(name, json.dumps(args, ensure_ascii=False)))

    def py_out(fn, **kw):
        c._LOCAL_ENABLED = state["local_enabled"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                r = fn(**kw)
            if dataclasses.is_dataclass(r) and hasattr(r, "space"):
                r = {"vectors": r.vectors, "space": r.space.to_dict()}          # asdict() не включает fingerprint (это метод)
            elif dataclasses.is_dataclass(r):
                r = dataclasses.asdict(r)
            return {"ok": r}
        except Exception as e:  # noqa: BLE001
            return {"error": {"class": type(e).__name__, "msg": str(e)}}

    def scenario(label, kind, reply, *, loose=False, **kw):
        """kind: complete | meta | semantic | embed"""
        nonlocal n
        status, payload = reply
        srv.reply = (status, payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8"), 0.0)
        base_url = kw.pop("base_url", SRV)
        pk = dict(kw)
        if kind == "embed":
            fn = c.embed
            pyargs = dict(texts=pk.pop("texts"), model=pk.pop("model"), base_url=base_url)
            rsargs = dict(pyargs)
            rname = "gw_embed"
        else:
            pyargs = dict(pk)
            pyargs["base_url"] = base_url
            if "requirement" in pyargs:
                pyargs["requirement"] = SemanticOutputRequirement(**pyargs["requirement"])
            fn = {"meta": c.complete_with_meta, "semantic": c.complete_semantic}.get(kind)
            rsargs = dict(kw)
            rsargs["base_url"] = base_url
            rname = {"complete": "gw_complete", "meta": "gw_complete_meta", "semantic": "gw_complete_semantic"}[kind]
        b = len(srv.records)
        if kind == "complete":
            def fn(**k):
                text, raw = c._do_complete(k.pop("prompt", None), model=k.pop("model"), system=k.pop("system", None), messages=k.pop("messages", None),
                                           temperature=k.pop("temperature", None), max_tokens=k.pop("max_tokens", None), timeout=k.pop("timeout", c.DEFAULT_TIMEOUT),
                                           base_url=k.pop("base_url"), strip_think=k.pop("strip_think", True), extra_options=k.pop("extra_options", None),
                                           response_format=k.pop("response_format", None), stop=k.pop("stop", None))
                return {"text": text, "raw": raw}
        p = py_out(fn, **pyargs)
        py_sent = srv.records[b:]
        b = len(srv.records)
        r = rs(rname, **rsargs)
        rs_sent = srv.records[b:]
        n += 1
        same_req = [norm_req(x) for x in py_sent] == [norm_req(x) for x in rs_sent]
        if loose:
            same_res = ("error" in p) == ("error" in r) and (("error" not in p) or p["error"]["class"] == r["error"]["class"])
        else:
            same_res = json.dumps(p, ensure_ascii=False) == json.dumps(r, ensure_ascii=False)
        check(label, same_req and same_res, f"\n py_sent={[norm_req(x) for x in py_sent]}\n rs_sent={[norm_req(x) for x in rs_sent]}\n py={json.dumps(p, ensure_ascii=False)[:900]}\n rs={json.dumps(r, ensure_ascii=False)[:900]}")

    OPENAI_OK = (200, {"choices": [{"message": {"content": "<think>раздумье</think> ответ  "}, "finish_reason": "length"}], "usage": {"completion_tokens": 11}})
    ANTH_OK = (200, {"content": [{"type": "text", "text": "ответ Клода"}], "stop_reason": "max_tokens", "usage": {"output_tokens": 7}})
    OLLAMA_OK = (200, {"message": {"role": "assistant", "content": "<think>x</think>\nответ Ollama\n"}, "done": True, "done_reason": "length", "eval_count": 5})
    E500 = (500, {"error": "boom"})
    GARBAGE = (200, b"not json")
    PARAMS = [
        {"prompt": "привет"},
        {"prompt": "привет", "system": "ты помощник", "temperature": 0.2, "max_tokens": 50},
        {"prompt": "q", "system": ["a", "b"], "response_format": "json", "stop": ["\n"]},
        {"messages": [{"role": "user", "content": "1"}, {"role": "assistant", "content": "2"}, {"role": "user", "content": "3"}], "system": ["s1", "s2"]},
        {"prompt": "q", "strip_think": False, "extra_options": {"seed": 3, "repeat_penalty": 1.1}, "timeout": 30},
        {"prompt": "", "system": ""},
    ]
    try:
        # ---- 0. вид адреса в трассе (`_location_kind`) — прямое сравнение функции ----
        for loc in (None, "", "http://x", "https://x", "HTTP://x", "https:/x", "/path/file.gguf", "ftp://x", "file:///x", "x", "http:", "http://"):
            n += 1
            check("0 _location_kind", c._location_kind(loc) == rs("location_kind", location=loc), repr(loc))
        # ---- A. complete: явная настройка ----
        for model in ("m_openai", "m_openai_slash", "m_anth", "m_openai_nomodel", "модель-юникод 🌍"):
            for params in PARAMS:
                for reply in (OPENAI_OK if model != "m_anth" else ANTH_OK, E500, GARBAGE):
                    scenario(f"A1 {model} {params.get('prompt', 'messages')!r}", "complete", reply, model=model, **params)
        for model in ("m_badproto", "m_badbackend", "m_nobackend", "m_llama_missing", "m_llama_nopath", "m_remote_nobase", "m_raise"):
            for params in PARAMS[:3]:
                scenario(f"A2 {model} (ошибка настройки/цели)", "complete", OPENAI_OK, model=model, **params)
        # ошибки входа
        scenario("A3 нет prompt/messages", "complete", OPENAI_OK, model="m_openai")
        scenario("A4 и prompt и messages", "complete", OPENAI_OK, model="m_openai", prompt="x", messages=[{"role": "user", "content": "y"}])
        # ---- B. НЕ настроено → Ollama; явный base_url; встроенный реестр ----
        for params in PARAMS:
            for reply in (OLLAMA_OK, E500, GARBAGE):
                scenario("B1 не настроено → Ollama", "complete", reply, model="plain-model", **params)
        for params in PARAMS[:3]:
            scenario("B2 явный base_url ≠ по умолчанию (Ollama-совместимая цель)", "complete", OLLAMA_OK, model="m_openai", base_url=SRV + "/other", **params)
        state["local_enabled"] = True
        for params in PARAMS:
            for reply in (OLLAMA_OK, E500, GARBAGE):
                scenario("B3 встроенный реестр: движок недоступен → откат на Ollama", "complete", reply, model="builtin-m", **params)
        scenario("B4 реестр: файла нет → Ollama сразу", "complete", OLLAMA_OK, model="builtin-missing", prompt="x")
        scenario("B5 явная настройка сильнее включённого реестра", "complete", OPENAI_OK, model="m_openai", prompt="x")
        scenario("B6 явная ошибка НЕ откатывается на Ollama (включённый движок)", "complete", E500, model="m_openai", prompt="x")
        state["local_enabled"] = False
        scenario("B7 реестр выключен → Ollama", "complete", OLLAMA_OK, model="builtin-m", prompt="x")
        # ---- B8. очистка ответа: закрытые think-блоки удаляются, ОДИНОЧНЫЕ теги остаются; strip — питоновский (U+001C..1F, U+0085, U+00A0) ----
        for content in ("x<think>без закрытия", "закрыт</think> висит", "<think>a</think>b<think>c", "\x1c\x1d ответ \x1e\x1f", "\u0085\u00a0ответ\u2028", "  <think>x</think>  ", "<think>\nмного\nстрок\n</think>\nответ", "<THINK>x</THINK>y"):
            for strip in (True, False):
                scenario("B8 очистка ответа (Ollama)", "complete", (200, {"message": {"content": content}, "done": True}), model="plain-model", prompt="x", strip_think=strip)
                scenario("B8 очистка ответа (OpenAI)", "complete", (200, {"choices": [{"message": {"content": content}}]}), model="m_openai", prompt="x", strip_think=strip)
        # ---- C. complete_with_meta ----
        for model, reply in (("m_openai", OPENAI_OK), ("m_anth", ANTH_OK), ("plain-model", OLLAMA_OK), ("m_openai", (200, {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}))):
            for params in PARAMS[:2]:
                scenario(f"C1 complete_with_meta {model}", "meta", reply, model=model, **params)
        # ---- D. семантический результат ----
        REQS = [
            {"kind": "reply_state", "state_schema": {"type": "object", "properties": {"severity": {"type": "number", "minimum": 0, "maximum": 1}, "is_insult": {"type": "boolean"}}, "required": ["severity"]}, "state_required": True},
            {"kind": "reply_state", "state_required": False},
            {"kind": "reply_state", "state_schema": {"required": ["a"]}, "reply_required": False},
            {"kind": "other"},
        ]
        SEM_BODIES = {
            "m_openai": lambda t: (200, {"choices": [{"message": {"content": t}, "finish_reason": "stop"}]}),
            "m_anth": lambda t: (200, {"content": [{"type": "text", "text": t}], "stop_reason": "end_turn"}),
            "plain-model": lambda t: (200, {"message": {"content": t}, "done": True}),
        }
        CONTENTS = ['{"reply": "Привет!", "state": {"severity": 0.5, "is_insult": false}}', '{"reply": "x", "state": null}', '{"reply": "", "state": {}}', "не JSON",
                    'Привет!\n###YANDI_STATE###\n{"severity": 0.3}', 'Только текст без маркера', '<think>x</think>{"reply": "ok", "state": {"a": 1}}', '{"state": {"severity": 1}}']
        for model, mk in SEM_BODIES.items():
            for req in REQS:
                for content in CONTENTS:
                    scenario(f"D1 семантика {model}", "semantic", mk(content), model=model, prompt="привет", requirement=req)
        scenario("D2 семантика: явная ошибка", "semantic", E500, model="m_openai", prompt="x", requirement=REQS[0])
        scenario("D3 семантика: system-список и messages", "semantic", SEM_BODIES["m_openai"](CONTENTS[0]), model="m_openai", messages=[{"role": "user", "content": "q"}], system=["a"], requirement=REQS[0])
        scenario("D4 семантика: нет входа", "semantic", SEM_BODIES["m_openai"](CONTENTS[0]), model="m_openai", requirement=REQS[0])
        state["local_enabled"] = True
        scenario("D5 семантика: реестр → откат на Ollama", "semantic", SEM_BODIES["plain-model"](CONTENTS[0]), model="builtin-m", prompt="x", requirement=REQS[0])
        state["local_enabled"] = False
        # ---- E. эмбеддинги ----
        VEC = [[1.0, 2.0], [3.0, 4.0]]
        E_OLL = (200, {"embeddings": VEC})
        E_OAI = (200, {"data": [{"index": 1, "embedding": [3.0, 4.0]}, {"index": 0, "embedding": [1.0, 2.0]}]})
        for texts in (["a", "б"], ["a"], "строка"):
            scenario("E1 не настроено → Ollama /api/embed", "embed", E_OLL if len(texts) != 1 else (200, {"embeddings": [[1.0, 2.0]]}), model="plain-emb", texts=texts)
        scenario("E2 настроено remote openai", "embed", E_OAI, model="m_openai", texts=["a", "б"])
        scenario("E3 настроено remote anthropic → отказ", "embed", E_OAI, model="m_anth", texts=["a"])
        scenario("E4 плохой backend в настройке", "embed", E_OAI, model="m_badbackend", texts=["a"])
        scenario("E5 llamacpp с несуществующим файлом", "embed", E_OAI, model="m_llama_missing", texts=["a"])
        scenario("E6 исключение хранилища", "embed", E_OAI, model="m_raise", texts=["a"])
        scenario("E7 пустой список", "embed", E_OAI, model="m_openai", texts=[])
        scenario("E8 явный base_url ≠ по умолчанию → без настройки", "embed", E_OLL, model="m_openai", texts=["a", "б"], base_url=SRV + "/other")
        for name, body in (
            ("batch не тот размер", (200, {"embeddings": [[1.0, 2.0]]})), ("пустой батч", (200, {"embeddings": []})), ("вектор пуст", (200, {"embeddings": [[], [1.0]]})),
            ("разной размерности", (200, {"embeddings": [[1.0, 2.0], [1.0]]})), ("не число", (200, {"embeddings": [["a", 1.0], [1.0, 2.0]]})),
            ("bool", (200, {"embeddings": [[True, 1.0], [1.0, 2.0]]})), ("целые допустимы", (200, {"embeddings": [[1, 2], [3, 4]]})), ("не тот ключ", (200, {"vectors": VEC})),
            ("не список", (200, {"embeddings": "x"})), ("мусор", (200, b"not json")), ("HTTP 500", (500, {})), ("вектор не список", (200, {"embeddings": [1, 2]})),
        ):
            scenario(f"E9 Ollama-эмбеддинги: {name}", "embed", body, model="plain-emb", texts=["a", "б"], loose=name in ("мусор",))
        for name, body in (("пустой data", (200, {"data": []})), ("вектор пуст", (200, {"data": [{"index": 0, "embedding": []}]})), ("HTTP 401", (401, {}))):
            scenario(f"E10 remote-эмбеддинги: {name}", "embed", body, model="m_openai", texts=["a"])
        # ---- F. соединение отклонено (деталь ошибки не сравнивается) ----
        for model in ("m_openai", "plain-model"):
            for kind, kw in (("complete", {"prompt": "x"}), ("embed", {"texts": ["a"]})):
                b0 = SRV
                CONFIG["m_dead"] = {"backend": "remote", "protocol": "openai", "base_url": "http://127.0.0.1:1"}
                scenario(f"F1 соединение отклонено {model}/{kind}", kind, OPENAI_OK, model="m_dead" if model == "m_openai" else model, base_url="http://127.0.0.1:1" if model != "m_openai" else SRV, loose=True, **kw)
    finally:
        srv.stop()
        os.environ.pop("YANDI_TEST_KEY", None)
    print(f"\n(сценариев: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
