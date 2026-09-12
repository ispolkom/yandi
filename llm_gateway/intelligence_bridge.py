"""
llm_gateway.intelligence_bridge — локальный HTTP-мост для YANDI Node
Intelligence RPC.

Проблема, которую это решает (мандат "Node Intelligence RPC migration"):
нода A не должна знать, каким backend'ом отвечает нода B (Ollama/Claude/
GPT/локальный GGUF/DeepSeek/будущий backend) — только Rust-транспорт B
(node/src/ai_rpc/*) физически принимает межнодовые запросы, а весь код
выбора backend'а (explicit config → local → Ollama-фоллбэк,
secure_store, честные ошибки без тихой подмены) уже существует и должен
остаться ТОЛЬКО в llm_gateway (Python). Транспорт не должен становиться
вторым gateway.

Этот модуль — тонкий мост между ними: слушает ТОЛЬКО 127.0.0.1 (никогда
не виден по сети — как и node/src/web/ai_rpc_server.rs:18082), Rust
дергает его вместо прямого обращения к Ollama.

Ключевое архитектурное решение (закрывает дыру §6 мандата: "нода A не
должна уметь выбирать backend ноды B"): вызывающий (Rust, от имени
удалённого пира) НЕ может передать сюда ни `model`, ни `base_url`, ни
любое другое поле выбора backend'а — даже если такое поле присутствует
в теле запроса, оно молча игнорируется. Все интеллектуальные запросы от
пиров всегда используют один и тот же логический алиас модели
(PEER_DEFAULT_MODEL), настроенный владельцем ЭТОЙ ноды через обычный
llm_gateway.setup (secure_store) — ровно так же, как любой другой
логический алиас (см. pet/shared.py::OLLAMA_MOD). Если владелец ничего
не настроил — сработает штатная логика client.py (STEP2 локальный движок
/ STEP3 Ollama-фоллбэк), а не какая-то отдельная "peer-specific" ветка.

Запуск: python3 -m llm_gateway.intelligence_bridge [port]
(или ./start_intelligence_bridge.sh — см. корень репозитория)
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import client as _client

# Логическое имя модели для ЛЮБОГО межнодового запроса от пира. Не путать
# с моделью, которую нода использует для СВОИХ СОБСТВЕННЫХ (agent/, pet/)
# задач — та настраивается отдельными алиасами. Владелец ноды явно
# настраивает этот алиас через llm_gateway.setup, если хочет отвечать
# пирам конкретным backend'ом; иначе действуют штатные STEP2/STEP3
# client.py.
PEER_DEFAULT_MODEL = "yandi:peer-default"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18083

# Верхняя граница тела запроса — не даёт малформед/огромным запросам
# даже дойти до парсинга (см. мандат TEST9: "malformed RPC request
# никогда не должен вызывать backend").
MAX_BODY_BYTES = 64 * 1024
MAX_MESSAGES = 64
MAX_MESSAGE_CHARS = 32_768


class _BridgeError(Exception):
    """Ошибка валидации запроса — backend НЕ вызывается."""


def _validate_messages(raw) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise _BridgeError("messages must be a non-empty list")
    if len(raw) > MAX_MESSAGES:
        raise _BridgeError("too many messages")
    out = []
    for m in raw:
        if not isinstance(m, dict):
            raise _BridgeError("each message must be an object")
        role = m.get("role")
        content = m.get("content")
        if role not in ("system", "user", "assistant"):
            raise _BridgeError(f"invalid role: {role!r}")
        if not isinstance(content, str) or not content:
            raise _BridgeError("message content must be a non-empty string")
        if len(content) > MAX_MESSAGE_CHARS:
            raise _BridgeError("message content too large")
        out.append({"role": role, "content": content})
    return out


def _handle_infer(body: dict) -> dict:
    """Выполняет один запрос через llm_gateway.complete_with_meta().

    Намеренно НЕ читает body["model"], body["base_url"] или что-либо
    похожее на выбор backend'а — эти поля, если присутствуют, полностью
    игнорируются. request_id — непрозрачный для нас идентификатор
    трассировки вызывающей стороны, мы только эхом возвращаем его
    обратно, ничего не решаем на его основе.
    """
    request_id = body.get("request_id")
    messages = _validate_messages(body.get("messages"))

    max_tokens = body.get("max_tokens")
    if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens <= 0):
        raise _BridgeError("max_tokens must be a positive integer")

    temperature = body.get("temperature")
    if temperature is not None and not isinstance(temperature, (int, float)):
        raise _BridgeError("temperature must be a number")

    stop = body.get("stop")
    if stop is not None:
        if not isinstance(stop, list) or not all(isinstance(s, str) for s in stop):
            raise _BridgeError("stop must be a list of strings")

    try:
        result = _client.complete_with_meta(
            messages=messages,
            model=PEER_DEFAULT_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            stop=stop,
        )
    except _client.LLMError as e:
        # Мандат TEST12: пиру никогда не должны уйти сырые детали ошибки
        # backend'а (могут содержать URL, путь к файлу, имя переменной
        # окружения API-ключа и т.п. — см. client.py/remote_backend.py
        # сообщения об ошибках). Полный текст — только в локальный лог
        # ЭТОЙ ноды, наружу — только факт и категория.
        print(f"[intelligence_bridge] backend error: {e}", file=sys.stderr)
        return {
            "request_id": request_id,
            "success": False,
            "error": "backend_error",
        }

    return {
        "request_id": request_id,
        "success": True,
        "text": result.text,
        "truncated": result.truncated,
        "tokens_used": result.token_count,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "YandiIntelligenceBridge/1"

    def log_message(self, fmt, *args):  # noqa: A002 — сигнатура BaseHTTPRequestHandler
        pass  # тихо: это внутренний loopback-мост, не публичный сервис

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 — сигнатура BaseHTTPRequestHandler
        if self.path != "/infer":
            self._send_json(404, {"success": False, "error": "not_found"})
            return

        length = self.headers.get("Content-Length")
        try:
            length_i = int(length) if length is not None else -1
        except ValueError:
            length_i = -1
        if length_i < 0 or length_i > MAX_BODY_BYTES:
            self._send_json(400, {"success": False, "error": "invalid_content_length"})
            return

        raw = self.rfile.read(length_i)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise _BridgeError("body must be a JSON object")
        except (json.JSONDecodeError, _BridgeError):
            self._send_json(400, {"success": False, "error": "malformed_request"})
            return

        try:
            result = _handle_infer(body)
        except _BridgeError as e:
            self._send_json(400, {"success": False, "error": str(e)})
            return

        self._send_json(200 if result["success"] else 502, result)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("intelligence_bridge must bind to loopback only")
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"[intelligence_bridge] listening on http://{host}:{port}/infer (loopback only)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main() -> int:
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    serve(port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
