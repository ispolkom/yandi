"""Родной «мост интеллекта» (rustlib/yandi_llm/src/intelligence.rs) против llm_gateway/intelligence_bridge.py.

Одни и те же тела запросов подаются Python-обработчику (`_handle_infer`) и родному; сравниваются статус, тело ответа (с порядком ключей) и
запрос, дошедший до общего HTTP-сервера. Настройки узла подменены одной картой для обеих сторон. Пиру не должны утекать детали ошибки backend'а,
а поля model/base_url в теле — игнорироваться.
"""
from __future__ import annotations

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
    from llm_gateway import intelligence_bridge as ib
    from llm_gateway import llamacpp_backend as lb
    from llm_gateway.native_remote_parity_test import Server

    srv = Server()
    SRV = srv.url
    CONFIG: dict = {"yandi:peer-default": {"backend": "remote", "protocol": "openai", "base_url": SRV, "model": "peer-model"}}

    def mock_get(model):
        e = CONFIG.get(model)
        return None if e is None else dict(e)

    c.node_config.get_model_entry = mock_get
    c.DEFAULT_BASE_URL = SRV
    c.complete_with_meta.__kwdefaults__["base_url"] = SRV   # значение по умолчанию связывается при определении функции
    lb._MODEL_REGISTRY = {}
    c._LOCAL_ENABLED = False

    def rs(body):
        args = {"config": CONFIG, "registry": {}, "local_enabled": False, "engine_reason": "x", "default_base_url": SRV, "body": body}
        return json.loads(yandi_llm.call("intel_infer", json.dumps(args, ensure_ascii=False)))

    def py(body):
        if not isinstance(body, dict):
            return {"status": 400, "body": {"success": False, "error": "malformed_request"}}
        try:
            r = ib._handle_infer(body)
        except ib._BridgeError as e:
            return {"status": 400, "body": {"success": False, "error": str(e)}}
        return {"status": 200 if r["success"] else 502, "body": r}

    def norm(r):
        try:
            d = json.loads(r["body"])
            # известные безвредные отличия формы: температура 0/1 у Python уходит целым, у Rust — 0.0/1.0; bool в max_tokens у Rust — целое 1
            if isinstance(d.get("temperature"), (int, float)) and not isinstance(d.get("temperature"), bool) and float(d["temperature"]).is_integer():
                d["temperature"] = int(d["temperature"])
            if isinstance(d.get("temperature"), bool):
                d["temperature"] = int(d["temperature"])
            if isinstance(d.get("max_tokens"), bool):
                d["max_tokens"] = int(d["max_tokens"])
            b = json.dumps(d, ensure_ascii=False)
        except ValueError:
            b = r["body"]
        return {"path": r["path"], "body": b}

    n = 0

    def case(label, body, status=200, payload=None):
        nonlocal n
        srv.reply = (status, json.dumps(payload if payload is not None else OK, ensure_ascii=False).encode(), 0.0)
        b = len(srv.records)
        p = py(body)
        ps = [norm(x) for x in srv.records[b:]]
        b = len(srv.records)
        r = rs(body)
        rr = [norm(x) for x in srv.records[b:]]
        n += 1
        same = json.dumps(p, ensure_ascii=False) == json.dumps(r, ensure_ascii=False) and ps == rr
        check(label, same, f"\n body={json.dumps(body, ensure_ascii=False)[:300]}\n py={json.dumps(p, ensure_ascii=False)[:500]} sent={ps}\n rs={json.dumps(r, ensure_ascii=False)[:500]} sent={rr}")

    OK = {"choices": [{"message": {"content": "<think>x</think> ответ пиру "}, "finish_reason": "length"}], "usage": {"completion_tokens": 9}}
    M = [{"role": "user", "content": "привет"}]

    # --- успешные запросы ---
    for extra in ({}, {"max_tokens": 50}, {"temperature": 0.3}, {"temperature": 0}, {"temperature": 1}, {"max_tokens": 5, "temperature": 0.7, "stop": ["a", "б"]},
                  {"stop": []}, {"request_id": "abc"}, {"request_id": 42}, {"request_id": None}, {"request_id": {"a": [1, None]}},
                  {"model": "hack-model", "base_url": "http://evil:1", "backend": "x"}, {"max_tokens": True}, {"temperature": True}, {"temperature": False}, {"max_tokens": None, "temperature": None, "stop": None}):
        case(f"A1 успех {extra}", {"messages": M, **extra})
    for msgs in ([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}],
                 [{"role": "user", "content": "🌍" * 100, "extra": "ignored"}], [{"role": "user", "content": "🌍" * 10000}], [{"role": "user", "content": "я" * 32768}], [{"role": "user", "content": "x" * 32768}],
                 [{"role": "user", "content": "x"}] * 64):
        case("A2 состав сообщений", {"messages": msgs, "request_id": "r"})
    # --- ответы backend'а ---
    for status, payload in ((500, {"error": "секрет /path/to/file"}), (401, {"error": "bad key"}), (200, {"nothing": 1}), (200, {"choices": []})):
        case(f"B1 backend {status}: наружу только backend_error", {"messages": M, "request_id": "q"}, status, payload)
    # --- проверка входа: backend не вызывается ---
    bad = [
        {}, {"messages": None}, {"messages": []}, {"messages": "x"}, {"messages": {"a": 1}}, {"messages": [1]}, {"messages": ["x"]}, {"messages": [None]},
        {"messages": [{"content": "x"}]}, {"messages": [{"role": "root", "content": "x"}]}, {"messages": [{"role": "tool", "content": "x"}]}, {"messages": [{"role": "function", "content": "x"}]}, {"messages": [{"role": None, "content": "x"}]}, {"messages": [{"role": 5, "content": "x"}]},
        {"messages": [{"role": ["user"], "content": "x"}]}, {"messages": [{"role": "User", "content": "x"}]}, {"messages": [{"role": "тест'\"", "content": "x"}]},
        {"messages": [{"role": "user"}]}, {"messages": [{"role": "user", "content": ""}]}, {"messages": [{"role": "user", "content": None}]},
        {"messages": [{"role": "user", "content": 5}]}, {"messages": [{"role": "user", "content": ["x"]}]}, {"messages": [{"role": "user", "content": "x" * 32769}]},
        {"messages": [{"role": "user", "content": "🌍" * 32769}]}, {"messages": [{"role": "user", "content": "x"}] * 65},
        {"messages": M, "max_tokens": 0}, {"messages": M, "max_tokens": -1}, {"messages": M, "max_tokens": 1.5}, {"messages": M, "max_tokens": 5.0}, {"messages": M, "max_tokens": "5"},
        {"messages": M, "max_tokens": False}, {"messages": M, "max_tokens": []},
        {"messages": M, "temperature": "hot"}, {"messages": M, "temperature": []}, {"messages": M, "temperature": {}},
        {"messages": M, "stop": "a"}, {"messages": M, "stop": [1]}, {"messages": M, "stop": ["a", None]}, {"messages": M, "stop": {"a": 1}},
        {"messages": [{"role": "root", "content": ""}], "max_tokens": 0},
    ]
    for b in bad:
        case("C1 отклонение", b)
    for b in ([], "x", 5, None, [{"messages": M}]):
        case(f"C2 не объект: {b!r}", b)
    # --- нет настройки пира: честная ошибка без сети, наружу только backend_error ---
    del CONFIG["yandi:peer-default"]
    # у Python здесь откат на Ollama (/api/chat) — в родной версии Ollama ИСКЛЮЧЁН: честная ошибка, НИ ОДНОГО запроса
    b = len(srv.records)
    r = rs({"messages": M, "request_id": 1})
    n += 1
    check("D1 алиас не настроен: backend_error без единого запроса (Ollama исключён)", r == {"status": 502, "body": {"request_id": 1, "success": False, "error": "backend_error"}} and len(srv.records) == b, str(r))
    CONFIG["yandi:peer-default"] = {"backend": "weird"}
    case("D2 плохой backend в настройке", {"messages": M})

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
