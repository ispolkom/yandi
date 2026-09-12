"""
llm_gateway/intelligence_bridge_regression_test.py

Мандат "Node Intelligence RPC migration": проверяет ровно то, что этот
мост обязан гарантировать —
  1. malformed/пустые/огромные запросы никогда не доходят до backend'а;
  2. поля model/base_url, даже если пир их подсунул, ПОЛНОСТЬЮ
     игнорируются (§6 — пир не выбирает backend);
  3. ошибка backend'а никогда не возвращает пиру сырые детали (URL,
     путь, имя переменной API-ключа) — TEST12;
  4. живой сквозной прогон (реальная модель отвечает) — только если
     доступен реальный движок, как и в остальных live-smoke тестах
     этого пакета.

Живой прогон использует ИЗОЛИРОВАННЫЕ YANDI_KEK_PATH/YANDI_NODE_DB
(scratch-каталог), НИКОГДА не трогает реальный secure_store узла.

Запуск: python3 -m llm_gateway.intelligence_bridge_regression_test
Живой смоук-тест: LLM_GATEWAY_RUN_LIVE_SMOKE_TEST=1 python3 -m llm_gateway.intelligence_bridge_regression_test
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from llm_gateway import intelligence_bridge as bridge

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def test_validation_unit_level() -> None:
    check(
        "empty messages list rejected before backend",
        _raises(bridge._validate_messages, []),
    )
    check(
        "non-list messages rejected",
        _raises(bridge._validate_messages, "not a list"),
    )
    check(
        "bad role rejected",
        _raises(bridge._validate_messages, [{"role": "root", "content": "hi"}]),
    )
    check(
        "empty content rejected",
        _raises(bridge._validate_messages, [{"role": "user", "content": ""}]),
    )
    check(
        "too many messages rejected",
        _raises(bridge._validate_messages, [{"role": "user", "content": "x"}] * (bridge.MAX_MESSAGES + 1)),
    )
    check(
        "oversized single message rejected",
        _raises(bridge._validate_messages, [{"role": "user", "content": "x" * (bridge.MAX_MESSAGE_CHARS + 1)}]),
    )
    check(
        "valid messages pass through unchanged",
        bridge._validate_messages([{"role": "user", "content": "hi"}]) == [{"role": "user", "content": "hi"}],
    )


def _raises(fn, *args) -> bool:
    try:
        fn(*args)
        return False
    except bridge._BridgeError:
        return True


def test_handle_infer_ignores_backend_selection_fields(monkeypatch_model: list[str]) -> None:
    """_handle_infer() must call complete_with_meta() with the FIXED
    PEER_DEFAULT_MODEL regardless of anything the request body says."""
    captured: dict = {}

    class _FakeResult:
        text = "ok"
        truncated = False
        token_count = 1

    def fake_complete_with_meta(**kwargs):
        captured.update(kwargs)
        return _FakeResult()

    orig = bridge._client.complete_with_meta
    bridge._client.complete_with_meta = fake_complete_with_meta
    try:
        bridge._handle_infer({
            "request_id": "x",
            "model": "attacker-chosen-model",
            "base_url": "http://evil:9999",
            "messages": [{"role": "user", "content": "hi"}],
        })
    finally:
        bridge._client.complete_with_meta = orig

    check(
        "complete_with_meta() called with PEER_DEFAULT_MODEL, never the caller's model field",
        captured.get("model") == bridge.PEER_DEFAULT_MODEL,
        repr(captured.get("model")),
    )
    check(
        "no base_url ever forwarded (complete_with_meta has no such kwarg passed)",
        "base_url" not in captured,
    )


def test_handle_infer_sanitizes_backend_errors() -> None:
    class _Boom(Exception):
        pass

    def fake_complete_with_meta(**kwargs):
        raise bridge._client.LLMError(
            "yandi:peer-default: connection to http://127.0.0.1:11434 failed, "
            "api_key_env=SECRET_ANTHROPIC_KEY, path=/home/iam/models/secret.gguf"
        )

    orig = bridge._client.complete_with_meta
    bridge._client.complete_with_meta = fake_complete_with_meta
    try:
        result = bridge._handle_infer({"request_id": "x", "messages": [{"role": "user", "content": "hi"}]})
    finally:
        bridge._client.complete_with_meta = orig

    check("failed backend call returns success=False", result["success"] is False)
    error_text = json.dumps(result)
    check(
        "sanitized error never contains the raw backend error text (no URL/path/key leak)",
        "11434" not in error_text and "SECRET_ANTHROPIC_KEY" not in error_text and "secret.gguf" not in error_text,
        error_text,
    )


def test_live_end_to_end() -> None:
    if not os.environ.get("LLM_GATEWAY_RUN_LIVE_SMOKE_TEST"):
        print("[skip] живой сквозной тест: LLM_GATEWAY_RUN_LIVE_SMOKE_TEST не установлен")
        return

    from llm_gateway import llamacpp_backend as be
    if be.Llama is None:
        print(f"[skip] живой сквозной тест: llama_cpp недоступен ({be.registry_error()})")
        return
    gguf_path = be._MODEL_REGISTRY["heretic:q8"].path
    if not os.path.exists(gguf_path):
        print(f"[skip] живой сквозной тест: GGUF-файл не найден ({gguf_path})")
        return

    scratch = tempfile.mkdtemp(prefix="yandi_bridge_live_")
    os.environ["YANDI_KEK_PATH"] = os.path.join(scratch, "kek.bin")
    os.environ["YANDI_NODE_DB"] = os.path.join(scratch, "db.sqlite")
    try:
        from llm_gateway import config
        config.set_model_entry(bridge.PEER_DEFAULT_MODEL, {
            "backend": "llamacpp", "path": gguf_path, "n_ctx": 4096, "n_gpu_layers": -1,
        })

        port = 18291
        server_thread = threading.Thread(target=bridge.serve, kwargs={"port": port}, daemon=True)
        server_thread.start()
        time.sleep(1.0)

        body = json.dumps({
            "request_id": "live-e2e",
            "model": "attacker-would-like-claude-opus",
            "messages": [{"role": "user", "content": "Ответь одним словом: сколько будет 2+2?"}],
            "max_tokens": 20,
            "temperature": 0.0,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/infer", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())

        check("live end-to-end request succeeds", result.get("success") is True, repr(result))
        check("live response has non-empty text", bool(result.get("text", "").strip()), repr(result))
        print(f"[info] live bridge answered: {result.get('text')!r}")
    finally:
        del os.environ["YANDI_KEK_PATH"]
        del os.environ["YANDI_NODE_DB"]
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    test_validation_unit_level()
    test_handle_infer_ignores_backend_selection_fields([])
    test_handle_infer_sanitizes_backend_errors()
    test_live_end_to_end()

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
    else:
        print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
