"""
llm_gateway/client_regression_test.py

Дешёвый offline regression suite для llm_gateway.client — не требует
запущенной Ollama, весь HTTP замокан. Проверяет, что единая обвязка
корректно воспроизводит поведение, которое раньше было размазано по
~29 файлам agent/ (голый prompt, prompt+system, temperature/max_tokens,
очистка <think> у reasoning-моделей, обработка сетевых и форматных
ошибок через LLMError вместо старой строки "[error: ...]").

Запуск: python3 -m llm_gateway.client_regression_test
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def _fake_response(json_body: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    if status_code >= 400:
        import requests

        resp.raise_for_status.side_effect = requests.HTTPError(f"{status_code}")
    else:
        resp.raise_for_status.side_effect = None
    return resp


def main() -> int:
    from llm_gateway import client

    # 1. Голый prompt (бывший /api/generate-стиль вызывающего кода) —
    #    должен уйти одним user-сообщением, без system.
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"message": {"content": "  привет  "}})
        out = client.complete("скажи привет", model="qwen3:14b")
        check("bare prompt returns stripped content", out == "привет", repr(out))
        sent = mock_post.call_args.kwargs["json"]
        check(
            "bare prompt sends exactly one user message, no system",
            sent["messages"] == [{"role": "user", "content": "скажи привет"}],
            repr(sent["messages"]),
        )
        check("stream is always False", sent["stream"] is False, repr(sent))
        check("no options key when temperature/max_tokens unset", "options" not in sent, repr(sent))

    # 2. prompt+system (бывший /api/chat-стиль).
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"message": {"content": "ok"}})
        client.complete("вопрос", model="m", system="ты полезный ассистент")
        sent = mock_post.call_args.kwargs["json"]
        check(
            "system message goes first when provided",
            sent["messages"]
            == [
                {"role": "system", "content": "ты полезный ассистент"},
                {"role": "user", "content": "вопрос"},
            ],
            repr(sent["messages"]),
        )

    # 3. temperature/max_tokens проброс в options (как делали
    #    contrarian_check.py и другие с явными generation options).
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"message": {"content": "ok"}})
        client.complete("q", model="m", temperature=0.2, max_tokens=256)
        sent = mock_post.call_args.kwargs["json"]
        check(
            "temperature/max_tokens map to options.temperature/num_predict",
            sent.get("options") == {"temperature": 0.2, "num_predict": 256},
            repr(sent.get("options")),
        )

    # 3b. extra_options (например seed у orch_validator.py) сливается с
    #     temperature/max_tokens, не заменяет их.
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"message": {"content": "ok"}})
        client.complete("q", model="m", temperature=0.2, extra_options={"seed": 7})
        sent = mock_post.call_args.kwargs["json"]
        check(
            "extra_options merges with temperature, doesn't replace it",
            sent.get("options") == {"seed": 7, "temperature": 0.2},
            repr(sent.get("options")),
        )

    # 3c. base_url переопределяется для не-локальных нод.
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"message": {"content": "ok"}})
        client.complete("q", model="m", base_url="http://10.0.0.5:11434")
        check(
            "base_url override hits the given host, not the default",
            mock_post.call_args.args[0] == "http://10.0.0.5:11434/api/chat",
            repr(mock_post.call_args.args),
        )

    # 4. <think>...</think> вырезается по умолчанию (orchestrator.py's
    #    старое поведение для reasoning-моделей).
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response(
            {"message": {"content": "<think>рассуждаю долго</think>ответ"}}
        )
        out = client.complete("q", model="deepseek-r1:14b")
        check("think tags stripped by default", out == "ответ", repr(out))

    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response(
            {"message": {"content": "<think>x</think>ответ"}}
        )
        out = client.complete("q", model="m", strip_think=False)
        check("strip_think=False keeps raw content", "<think>" in out, repr(out))

    # 5. Сетевая ошибка -> LLMError, а не "[error: ...]" строка и не
    #    сырое исключение requests наружу.
    with patch.object(client._session, "post") as mock_post:
        import requests

        mock_post.side_effect = requests.ConnectionError("боже, всё упало")
        try:
            client.complete("q", model="m")
            check("network failure raises LLMError", False, "no exception raised")
        except client.LLMError:
            check("network failure raises LLMError", True)
        except Exception as e:  # noqa: BLE001
            check("network failure raises LLMError", False, f"raised {type(e).__name__}: {e}")

    # 5b. complete_with_meta(): truncated=True когда done_reason="length",
    #     token_count берётся из eval_count.
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response(
            {"message": {"content": "обрублен"}, "done_reason": "length", "eval_count": 512}
        )
        result = client.complete_with_meta("q", model="m")
        check("complete_with_meta detects truncation", result.truncated is True, repr(result))
        check("complete_with_meta reports token_count", result.token_count == 512, repr(result))
        check("complete_with_meta still strips/returns text", result.text == "обрублен", repr(result))

    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response(
            {"message": {"content": "нормально завершилось"}, "done_reason": "stop", "eval_count": 40}
        )
        result = client.complete_with_meta("q", model="m")
        check("complete_with_meta: done_reason=stop is not truncated", result.truncated is False, repr(result))

    # 6. Неожиданный формат ответа (нет message.content) -> LLMError,
    #    не голый KeyError наружу.
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"unexpected": "shape"})
        try:
            client.complete("q", model="m")
            check("malformed response raises LLMError", False, "no exception raised")
        except client.LLMError:
            check("malformed response raises LLMError", True)
        except Exception as e:  # noqa: BLE001
            check("malformed response raises LLMError", False, f"raised {type(e).__name__}: {e}")

    # 7. is_available() — health-check для будущего фоллбэка (Phase 3).
    with patch.object(client._session, "get") as mock_get:
        mock_get.return_value = _fake_response({}, status_code=200)
        check("is_available True on 200", client.is_available() is True)

    with patch.object(client._session, "get") as mock_get:
        import requests

        mock_get.side_effect = requests.ConnectionError("down")
        check("is_available False when backend unreachable", client.is_available() is False)

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
