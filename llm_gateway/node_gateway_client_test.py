"""Ядро на Python через шлюз узла (`YANDI_NODE_GATEWAY_URL`) против прямого Python-пути.

«Узел» здесь — маленький HTTP-сервер с тем же маршрутом `/api/gateway/call`, который внутри вызывает родной шлюз (`yandi_llm`, тот же код, что у узла).
Одни и те же вызовы `client.complete_with_meta / complete_semantic / embed` выполняются напрямую и через «узел»: результаты (в т.ч. трасса) и запросы к общему
HTTP-серверу модели обязаны совпасть. Сам маршрут узла проверяет node/tests/native_intelligence_link.rs.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    from llm_gateway import node_gateway
    from llm_gateway.native_remote_parity_test import Server
    from llm_gateway.types import SemanticOutputRequirement

    srv = Server()
    SRV = srv.url
    CONFIG = {
        "m_openai": {"backend": "remote", "protocol": "openai", "base_url": SRV, "model": "real-openai"},
        "m_anth": {"backend": "remote", "protocol": "anthropic", "base_url": SRV, "model": "claude-x"},
        "m_bad": {"backend": "weird"},
    }
    c.node_config.get_model_entry = lambda m: dict(CONFIG[m]) if m in CONFIG else None
    c.DEFAULT_BASE_URL = SRV
    for fn in (c.complete, c.complete_with_meta, c.complete_semantic, c.embed):
        fn.__kwdefaults__["base_url"] = SRV      # значение по умолчанию связывается при определении функции
    lb._MODEL_REGISTRY = {}
    c._LOCAL_ENABLED = False

    class Node(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            args = dict(body["args"], config=CONFIG, registry={}, local_enabled=False, engine_reason="x", default_base_url=SRV)
            try:
                out = yandi_llm.call(body["name"], json.dumps(args, ensure_ascii=False))
                code = 200
            except ValueError as e:
                out, code = json.dumps({"error": {"class": "BadRequest", "msg": str(e)}}), 400
            data = out.encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    node = ThreadingHTTPServer(("127.0.0.1", 0), Node)
    threading.Thread(target=node.serve_forever, daemon=True).start()
    NODE_URL = f"http://127.0.0.1:{node.server_address[1]}"

    def run(fn, via_node):
        if via_node:
            os.environ[node_gateway.ENV_URL] = NODE_URL
        else:
            os.environ.pop(node_gateway.ENV_URL, None)
        b = len(srv.records)
        try:
            r = fn()
            if dataclasses.is_dataclass(r):
                r = dataclasses.asdict(r)
            out = {"ok": r}
        except Exception as e:  # noqa: BLE001
            out = {"error": {"class": type(e).__name__, "msg": str(e)}}
        finally:
            os.environ.pop(node_gateway.ENV_URL, None)
        sent = [(x["path"], json.loads(x["body"])) for x in srv.records[b:]]
        return out, sent

    n = 0

    def both(label, fn, status=200, payload=None):
        nonlocal n
        srv.reply = (status, json.dumps(payload, ensure_ascii=False).encode(), 0.0)
        direct = run(fn, False)
        via = run(fn, True)
        n += 1
        check(label, json.dumps(direct, ensure_ascii=False, sort_keys=True) == json.dumps(via, ensure_ascii=False, sort_keys=True),
              f"\n direct={json.dumps(direct, ensure_ascii=False)[:700]}\n via   ={json.dumps(via, ensure_ascii=False)[:700]}")

    OPENAI = {"choices": [{"message": {"content": "<think>м</think> ответ  "}, "finish_reason": "length"}], "usage": {"completion_tokens": 11}}
    ANTH = {"content": [{"type": "text", "text": "ответ Клода"}], "stop_reason": "max_tokens", "usage": {"output_tokens": 7}}
    M = [{"role": "user", "content": "привет"}]

    for model, reply in (("m_openai", OPENAI), ("m_anth", ANTH)):
        both(f"A1 complete_with_meta {model}", lambda m=model: c.complete_with_meta("вопрос", model=m, temperature=0.3, max_tokens=50, stop=["z"]), payload=reply)
        both(f"A2 complete (messages, system) {model}", lambda m=model: c.complete(messages=M, model=m, system="ты помощник"), payload=reply)
        both(f"A3 complete без очистки размышлений {model}", lambda m=model: c.complete("x", model=m, strip_think=False), payload=reply)
    both("A4 json-режим", lambda: c.complete("x", model="m_openai", response_format="json"), payload=OPENAI)
    both("A5 extra_options", lambda: c.complete("x", model="m_openai", extra_options={"seed": 3}), payload=OPENAI)
    # семантика
    req = SemanticOutputRequirement(kind="reply_with_state", state_schema={"type": "object", "properties": {"mood": {"type": "string"}}}, state_required=True)
    sem = {"choices": [{"message": {"content": json.dumps({"reply": "привет!", "state": {"mood": "ok"}}, ensure_ascii=False)}, "finish_reason": "stop"}], "usage": {"completion_tokens": 5}}
    both("B1 semantic ok", lambda: c.complete_semantic("x", model="m_openai", requirement=req), payload=sem)
    both("B2 semantic мусор", lambda: c.complete_semantic("x", model="m_openai", requirement=req), payload=OPENAI)
    both("B3 semantic messages", lambda: c.complete_semantic(messages=M, model="m_openai", requirement=SemanticOutputRequirement(kind="reply_with_state")), payload=OPENAI)
    # эмбеддинги
    emb = {"data": [{"index": 1, "embedding": [0.5, 1.5]}, {"index": 0, "embedding": [1.0, 2.0]}]}
    both("C1 embed список", lambda: c.embed(["a", "б"], model="m_openai"), payload=emb)
    both("C2 embed строка", lambda: c.embed("a", model="m_openai"), payload={"data": [{"index": 0, "embedding": [1.0]}]})
    both("C3 embed anthropic → отказ", lambda: c.embed(["a"], model="m_anth"), payload={})
    both("C4 embed пусто", lambda: c.embed([], model="m_openai"), payload={})
    # ошибки
    for st in (500, 401):
        both(f"D1 HTTP {st}", lambda: c.complete("x", model="m_openai"), st, {"error": "x"})
    both("D2 плохой backend в настройке", lambda: c.complete("x", model="m_bad"), payload={})
    both("D3 ни prompt, ни messages", lambda: c.complete(model="m_openai"), payload={})
    both("D4 и prompt, и messages", lambda: c.complete("x", messages=M, model="m_openai"), payload={})

    # узел недоступен: честная ошибка, НЕ откат на прямой путь (сервер модели не получает ни одного запроса)
    os.environ[node_gateway.ENV_URL] = "http://127.0.0.1:1"
    b = len(srv.records)
    try:
        c.complete("x", model="m_openai")
        got = None
    except Exception as e:  # noqa: BLE001
        got = e
    finally:
        os.environ.pop(node_gateway.ENV_URL, None)
    n += 1
    check("E1 узел недоступен → ошибка без отката", isinstance(got, node_gateway.NodeGatewayError) and "недоступен" in str(got) and len(srv.records) == b, repr(got))
    # без переменной — узел не трогается
    n += 1
    check("E2 без переменной шлюз узла выключен", not node_gateway.enabled())
    # неверный вызов (400) → NodeGatewayError
    os.environ[node_gateway.ENV_URL] = NODE_URL
    try:
        node_gateway.call("no_such_function", {}, timeout=5)
        got = None
    except node_gateway.NodeGatewayError as e:
        got = e
    finally:
        os.environ.pop(node_gateway.ENV_URL, None)
    n += 1
    check("E3 BadRequest → NodeGatewayError", got is not None and "отклонил" in str(got), repr(got))

    total = _OK + len(FAILURES)
    print(f"\n(сценариев: {n}; успешных проверок: {_OK} из {total})")
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)} проверок")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
