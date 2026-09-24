"""
llm_gateway/native_remote_parity_test.py — доказательство, что РОДНОЙ Rust-бэкенд удалённых моделей (rustlib/yandi_llm/src/remote.rs) делает то же самое,
что llm_gateway/remote_backend.py: ОДИН И ТОТ ЖЕ локальный HTTP-сервер получает запрос и от Python (`requests`), и от Rust (`reqwest`), после чего сравниваются
(1) ЧТО ушло на сервер: путь, метод, значимые заголовки, тело (значение и ПОРЯДОК ключей) и (2) ЧТО вернулось вызывающему: текст и метаданные — либо ошибка.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Сценарии: OpenAI chat completions (ключ есть/нет/пустой, temperature/max_tokens/stop/json-режим, хвостовые слэши в base_url, content=null/""/строка, finish_reason length/stop,
usage есть/нет/пустой), Anthropic Messages (system-сообщения уходят в top-level поле и склеиваются, max_tokens по умолчанию 4096, stop_sequences, x-api-key пустой без ключа,
блоки content, stop_reason), OpenAI embeddings (порядок по `index`, отсутствующий index, пустой data), статусы HTTP 400/401/404/429/500/503 (текст ошибки — как у `requests`:
"500 Server Error: … for url: …"), тело не JSON / не тот формат, неизвестный протокол, Anthropic embeddings (точный текст отказа), соединение отклонено, таймаут.
РАСХОЖДЕНИЯ, которые тест НЕ считает ошибкой: деталь ошибки СОЕДИНЕНИЯ/разбора JSON (текст исключения Python не воспроизводится — сравнивается префикс "модель @ адрес: ");
непустое НЕстроковое content в ответе OpenAI (Python отдаёт как есть, родной Rust — ошибка формата).

Требует собранного моста yandi_llm — если не установлен, SKIP.

Run: python -m llm_gateway.native_remote_parity_test
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
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


class Server:
    def __init__(self):
        self.records: list[dict] = []
        self.reply = (200, b"{}", 0.0)
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(n)
                outer.records.append({
                    "method": "POST", "path": self.raw_requestline.decode("latin-1").split()[1],   # сырой путь: http.server склеивает ведущие "//"
                    "headers": {k.lower(): v for k, v in self.headers.items() if k.lower() in ("authorization", "x-api-key", "anthropic-version", "content-type")},
                    "body": body.decode("utf-8", "replace"),
                })
                status, payload, delay = outer.reply
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()


def main() -> int:
    try:
        import yandi_llm
    except ImportError as e:
        print(f"SKIP: yandi_llm не собран ({e})")
        return 0
    from llm_gateway import remote_backend as rb
    from llm_gateway import ollama_backend as ob

    srv = Server()
    n = 0

    def rs(name, **args):
        return json.loads(yandi_llm.call(name, json.dumps(args, ensure_ascii=False)))

    def py_call(fn, **kw):
        try:
            return {"ok": fn(**kw)}
        except rb.RemoteBackendError as e:
            return {"error": str(e)}
        except (AttributeError, KeyError, TypeError, IndexError) as e:        # не пойманное оригиналом исключение — родной Rust обязан вернуть ошибку
            return {"error": f"uncaught:{type(e).__name__}"}

    def sent(before):
        return srv.records[before:]

    def norm_req(r):
        body = r["body"]
        try:
            body = json.dumps(json.loads(body), ensure_ascii=False)     # значение и порядок ключей, без различий в пробелах
        except ValueError:
            pass
        h = dict(r["headers"])
        return {"path": r["path"], "headers": h, "body": body}

    def compare(label, status, payload, gen_kwargs, model="m", protocol="openai", kind="generate", delay=0.0, exact_error=True):
        """Один сценарий: одинаковый ответ сервера → Python и Rust; сравнить запросы и результаты."""
        nonlocal n
        srv.reply = (status, payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8"), delay)
        kw = dict(base_url=srv.url + gen_kwargs.pop("_slashes", ""), protocol=protocol, model=model, api_key_env=gen_kwargs.pop("api_key_env", None))
        if kind == "generate":
            args = {"messages": gen_kwargs.get("messages", [{"role": "user", "content": "привет"}]), **{k: v for k, v in gen_kwargs.items() if k != "messages"}}
            py_kw = dict(messages=args["messages"], **kw, **{k: v for k, v in gen_kwargs.items() if k != "messages"})
            b = len(srv.records)
            p = py_call(rb.generate, **py_kw)
            py_sent = sent(b)
            b = len(srv.records)
            r = rs("remote_generate", messages=args["messages"], **kw, **{k: v for k, v in gen_kwargs.items() if k != "messages"})
            rs_sent = sent(b)
            if "ok" in p:
                p = {"ok": {"text": p["ok"][0], "meta": p["ok"][1]}}
        else:
            b = len(srv.records)
            p = py_call(rb.embed, texts=gen_kwargs["texts"], base_url=kw["base_url"], protocol=protocol, model=model, api_key_env=kw["api_key_env"], **({"timeout": gen_kwargs["timeout"]} if "timeout" in gen_kwargs else {}))
            py_sent = sent(b)
            b = len(srv.records)
            r = rs("remote_embed", texts=gen_kwargs["texts"], base_url=kw["base_url"], protocol=protocol, model=model, api_key_env=kw["api_key_env"], **({"timeout": gen_kwargs["timeout"]} if "timeout" in gen_kwargs else {}))
            rs_sent = sent(b)
            if "ok" in p:
                p = {"ok": {"vectors": p["ok"][0], "meta": p["ok"][1]}}
        n += 1
        same_req = [norm_req(x) for x in py_sent] == [norm_req(x) for x in rs_sent]
        if "error" in p or "error" in r:
            if "error" in p and p["error"].startswith("uncaught:"):
                same_res = "error" in r
            elif exact_error:
                same_res = p == r
            else:
                prefix = f"{model} @ {kw['base_url']}: "
                same_res = "error" in p and "error" in r and p["error"].startswith(prefix) and r["error"].startswith(prefix)
        else:
            same_res = json.dumps(p, ensure_ascii=False) == json.dumps(r, ensure_ascii=False)
        check(label, same_req and same_res, f"\n py_sent={[norm_req(x) for x in py_sent]}\n rs_sent={[norm_req(x) for x in rs_sent]}\n py={p}\n rs={r}")

    try:
        os.environ["YANDI_TEST_KEY"] = "sk-test"
        os.environ["YANDI_EMPTY_KEY"] = ""
        os.environ.pop("YANDI_NO_KEY", None)
        MSGS = [
            [{"role": "user", "content": "привет"}],
            [{"role": "system", "content": "сис1"}, {"role": "user", "content": "q"}],
            [{"role": "system", "content": "a"}, {"role": "system", "content": "b"}, {"role": "user", "content": "q"}, {"role": "assistant", "content": "ответ"}, {"role": "user", "content": "ещё"}],
            [{"role": "system", "content": ""}, {"role": "user", "content": "q"}],
            [{"content": "без роли"}],
            [],
        ]
        # ---- OpenAI: запросы и ответы ----
        OK = [
            {"choices": [{"message": {"content": "ответ"}, "finish_reason": "stop"}], "usage": {"completion_tokens": 7}},
            {"choices": [{"message": {"content": None}, "finish_reason": "length"}]},
            {"choices": [{"message": {"content": ""}}], "usage": {}},
            {"choices": [{"message": {"content": "x"}, "finish_reason": None}], "usage": None},
            {"choices": [{"message": {"content": "x"}, "finish_reason": "content_filter"}], "usage": {"completion_tokens": 0, "prompt_tokens": 3}},
            {"choices": [{"message": {"content": "🌍\nстрока"}, "finish_reason": "length"}, {"message": {"content": "второй"}}], "usage": {"completion_tokens": 12}},
        ]
        for ok in OK:
            for env in (None, "YANDI_TEST_KEY", "YANDI_EMPTY_KEY", "YANDI_NO_KEY"):
                compare("A1 OpenAI ответ/ключ", 200, ok, {"api_key_env": env, "messages": MSGS[0]})
        for msgs in MSGS:
            for extra in ({}, {"temperature": 0.0}, {"temperature": 0.7, "max_tokens": 100}, {"max_tokens": 0}, {"response_format": "json"}, {"response_format": "text"},
                          {"stop": ["\n", "END"]}, {"stop": []}, {"temperature": 1e-7, "max_tokens": 5, "response_format": "json", "stop": ["x"]}, {"_slashes": "//"}, {"_slashes": "/"}):
                compare("A2 OpenAI запрос", 200, OK[0], {"messages": msgs, **extra})
        # ---- Anthropic ----
        AOK = [
            {"content": [{"type": "text", "text": "ответ"}], "stop_reason": "end_turn", "usage": {"output_tokens": 9}},
            {"content": [{"type": "text", "text": "a"}, {"type": "tool_use", "id": "x"}, {"type": "text", "text": "b"}], "stop_reason": "max_tokens"},
            {"content": [], "usage": {}},
            {"stop_reason": None},
            {"content": [{"type": "text"}], "usage": None},
        ]
        for ok in AOK:
            for env in (None, "YANDI_TEST_KEY", "YANDI_EMPTY_KEY"):
                compare("B1 Anthropic ответ/ключ", 200, ok, {"api_key_env": env}, protocol="anthropic")
        for msgs in MSGS:
            for extra in ({}, {"temperature": 0.3}, {"max_tokens": 50}, {"max_tokens": 0}, {"stop": ["a", "b"]}, {"stop": []}, {"temperature": 0.0, "max_tokens": 1, "stop": ["z"]}):
                compare("B2 Anthropic запрос", 200, AOK[0], {"messages": msgs, **extra}, protocol="anthropic")
        # ---- embeddings ----
        EOKS = [
            {"data": [{"index": 1, "embedding": [3.0, 4.0]}, {"index": 0, "embedding": [1.0, 2.0]}]},
            {"data": [{"embedding": [1.0]}, {"embedding": [2.0]}]},
            {"data": []},
            {"data": [{"index": 2, "embedding": [0.5, 0.25, 0.125]}, {"index": 0, "embedding": [1, 2, 3]}, {"index": 1, "embedding": [-1e-7, 1e16, 0.1]}]},
        ]
        for ok in EOKS:
            for env in (None, "YANDI_TEST_KEY"):
                for texts in (["a"], ["a", "б"], []):
                    compare("C1 embeddings", 200, ok, {"texts": texts, "api_key_env": env}, kind="embed")
        compare("C2 embeddings anthropic", 200, {}, {"texts": ["a"]}, protocol="anthropic", kind="embed")
        compare("C3 embeddings неизвестный протокол", 200, {}, {"texts": ["a"]}, protocol="grpc", kind="embed")
        # ---- Ollama /api/chat (полный сырой ответ возвращается как есть) ----
        def ollama_case(label, status, payload, exact_error=True, **kw):
            nonlocal n
            srv.reply = (status, payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8"), 0.0)
            base = dict(model="m", base_url=srv.url, temperature=None, max_tokens=None, timeout=30, extra_options=None, response_format=None, stop=None)
            base.update(kw)
            msgs = base.pop("messages", MSGS[1])
            b = len(srv.records)
            try:
                p = {"ok": list(ob.generate(msgs, **base))}
            except ob.OllamaBackendError as e:
                p = {"error": str(e)}
            except (AttributeError, KeyError, TypeError) as e:
                p = {"error": f"uncaught:{type(e).__name__}"}
            py_sent = sent(b)
            b = len(srv.records)
            r = rs("ollama_generate", messages=msgs, **base)
            rs_sent = sent(b)
            if "ok" in p:
                p = {"ok": {"text": p["ok"][0], "raw": p["ok"][1]}}
            n += 1
            same_req = [norm_req(x) for x in py_sent] == [norm_req(x) for x in rs_sent]
            if "error" in p or "error" in r:
                if p.get("error", "").startswith("uncaught:"):
                    same_res = "error" in r
                elif exact_error:
                    same_res = p == r
                else:
                    same_res = "error" in p and "error" in r and p["error"].startswith("m: ") and r["error"].startswith("m: ")
            else:
                same_res = json.dumps(p, ensure_ascii=False) == json.dumps(r, ensure_ascii=False)
            check(label, same_req and same_res, f"\n py_sent={[norm_req(x) for x in py_sent]}\n rs_sent={[norm_req(x) for x in rs_sent]}\n py={p}\n rs={r}")

        OLL = [{"message": {"content": "ответ"}, "done": True, "done_reason": "stop", "eval_count": 5}, {"message": {"role": "assistant", "content": ""}}, {"message": {"content": "🌍\nx"}, "extra": [1, {"a": None}]}]
        for ok in OLL:
            for msgs in MSGS:
                for extra in ({}, {"temperature": 0.0}, {"temperature": 0.3, "max_tokens": 64}, {"max_tokens": 0}, {"stop": ["a"]}, {"stop": []}, {"response_format": "json"},
                              {"response_format": {"type": "object", "properties": {"a": {"type": "string"}}}}, {"extra_options": {"seed": 1, "repeat_penalty": 1.1}},
                              {"extra_options": {}}, {"extra_options": {"temperature": 9}, "temperature": 0.5}, {"extra_options": {"seed": 7}, "stop": ["z"], "max_tokens": 3, "temperature": 1e-7}):
                    ollama_case("H1 Ollama запрос/ответ", 200, ok, messages=msgs, **extra)
        for status in (400, 404, 500, 503):
            ollama_case(f"H2 Ollama HTTP {status}", status, {"error": "x"})
        for body in (b"not json", b"[]", b"{}", b'{"message": {}}', b'{"message": null}', b"null", b""):
            ollama_case("H3 Ollama кривое тело", 200, body, exact_error=False)
        ollama_case("H4 Ollama без слэшей у адреса", 200, OLL[0], base_url=srv.url + "/")   # оригинал НЕ отрезает хвостовой слэш → путь "//api/chat"
        # соединение отклонено: деталь исключения не воспроизводится, префикс "модель: " обязателен
        for kw_ in ({}, {"timeout": 2}):
            try:
                pe = {"error": str(ob.generate(MSGS[0], model="m", base_url="http://127.0.0.1:1", temperature=None, max_tokens=None, timeout=kw_.get("timeout", 5), extra_options=None, response_format=None, stop=None))}
            except ob.OllamaBackendError as e:
                pe = {"error": str(e)}
            re_ = rs("ollama_generate", messages=MSGS[0], model="m", base_url="http://127.0.0.1:1", timeout=kw_.get("timeout", 5))
            n += 1
            check("H5 Ollama соединение отклонено", "error" in pe and "error" in re_ and pe["error"].startswith("m: ") and re_["error"].startswith("m: "), f"{pe} {re_}")
        # ---- таймаут по умолчанию (180 с в оригинале) не должен срабатывать на медленном, но живом сервере ----
        compare("D0 таймаут по умолчанию", 200, OK[0], {"messages": MSGS[0]}, delay=1.6)
        compare("D0b таймаут по умолчанию (embeddings)", 200, EOKS[0], {"texts": ["a"]}, kind="embed", delay=1.6)
        # ---- статусы HTTP ----
        for status in (400, 401, 403, 404, 422, 429, 500, 502, 503):
            for protocol in ("openai", "anthropic"):
                compare(f"D1 HTTP {status} {protocol}", status, {"error": {"message": "x"}}, {}, protocol=protocol)
            compare(f"D2 HTTP {status} embeddings", status, {}, {"texts": ["a"]}, kind="embed")
        # ---- кривые тела ----
        for body in (b"not json", b"[]", b"{}", b'{"choices": []}', b'{"choices": [{}]}', b'{"choices": [{"message": {}}]}', b'{"choices": null}', b"", b"null", b'"str"',
                     ):
            compare("E1 OpenAI кривое тело", 200, body, {}, exact_error=False)
        for body in (b"not json", b"[]", b"{}", b'{"content": "x"}', b'{"content": [1]}', b'{"content": [{"type": "text", "text": 5}]}', b"null", b""):
            compare("E2 Anthropic кривое тело", 200, body, {}, protocol="anthropic", exact_error=False)
        for body in (b"not json", b"[]", b"{}", b'{"data": null}', b'{"data": [{}]}', b'{"data": [{"index": "a", "embedding": [1]}]}', b'{"data": [{"index": "a", "embedding": [1]}, {"index": 1, "embedding": [2]}]}',
                     b'{"data": [{"index": "b", "embedding": [1]}, {"index": "a", "embedding": [2]}]}', b'{"data": [{"index": null, "embedding": [1]}, {"index": 0, "embedding": [2]}]}', b'{"data": [1]}'):
            compare("E3 embeddings кривое тело", 200, body, {"texts": ["a"]}, kind="embed", exact_error=False)
        # ---- неизвестный протокол / соединение / таймаут ----
        compare("F1 неизвестный протокол generate", 200, {}, {}, protocol="grpc")
        compare("F2 неизвестный протокол (юникод)", 200, {}, {}, protocol="протокол'\"")
        # соединение отклонено
        for protocol in ("openai", "anthropic"):
            kw = dict(base_url="http://127.0.0.1:1", protocol=protocol, model="m", api_key_env=None)
            p = py_call(rb.generate, messages=MSGS[0], **kw)
            r = rs("remote_generate", messages=MSGS[0], **kw)
            n += 1
            check("G1 соединение отклонено", "error" in p and "error" in r and p["error"].startswith("m @ http://127.0.0.1:1: ") and r["error"].startswith("m @ http://127.0.0.1:1: "), f"{p} {r}")
        # таймаут
        srv.reply = (200, b"{}", 3.0)
        kw = dict(base_url=srv.url, protocol="openai", model="m", api_key_env=None, timeout=1)
        t0 = time.time()
        p = py_call(rb.generate, messages=MSGS[0], **kw)
        t1 = time.time()
        r = rs("remote_generate", messages=MSGS[0], **kw)
        t2 = time.time()
        n += 1
        check("G2 таймаут", "error" in p and "error" in r and p["error"].startswith(f"m @ {srv.url}: ") and r["error"].startswith(f"m @ {srv.url}: ") and (t1 - t0) < 2.5 and (t2 - t1) < 2.5,
              f"{p} {r} {t1 - t0:.1f}s {t2 - t1:.1f}s")
    finally:
        srv.stop()
    print(f"\n(сценариев: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
