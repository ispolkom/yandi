"""
llm_gateway/remote_backend_regression_test.py

Дешёвый offline suite для remote_backend — весь HTTP замокан, ни один
реальный запрос никуда не уходит. Проверяет оба протокола (OpenAI-
совместимый и родной Anthropic Messages API) отдельно, поскольку у них
реально разные форматы запроса/ответа/заголовков.

Мандат "chat_local gateway migration": generate() теперь принимает
messages (уже собранный wire-формат, см. client.py._build_messages()),
не prompt/system по отдельности — этот suite тестирует remote_backend.py
в изоляции, поэтому строит messages вручную, как это делал бы client.py.

Запуск: python3 -m llm_gateway.remote_backend_regression_test
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
    from llm_gateway import remote_backend as rb

    # ── OpenAI-совместимый протокол ──────────────────────────────────
    with patch.object(rb._session, "post") as mock_post, \
         patch.dict("os.environ", {"MY_KEY": "sk-secret-123"}):
        mock_post.return_value = _fake_response({
            "choices": [{"message": {"content": "привет"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 5},
        })
        text, meta = rb.generate(
            [{"role": "system", "content": "будь краток"}, {"role": "user", "content": "скажи привет"}],
            base_url="https://my-server.example/v1", protocol="openai",
            model="my-model", api_key_env="MY_KEY", temperature=None, max_tokens=None,
            response_format=None, stop=None,
        )
        check("openai: returns text", text == "привет", repr(text))
        check("openai: done_reason=stop maps to not-truncated", meta["done_reason"] == "stop", repr(meta))
        check("openai: token count from usage.completion_tokens", meta["eval_count"] == 5, repr(meta))

        url = mock_post.call_args.args[0]
        sent = mock_post.call_args.kwargs["json"]
        headers = mock_post.call_args.kwargs["headers"]
        check("openai: hits {base_url}/chat/completions", url == "https://my-server.example/v1/chat/completions", url)
        check(
            "openai: messages carry system first, then user, passed straight through",
            sent["messages"] == [{"role": "system", "content": "будь краток"}, {"role": "user", "content": "скажи привет"}],
            repr(sent["messages"]),
        )
        check("openai: api key goes in Authorization: Bearer", headers.get("Authorization") == "Bearer sk-secret-123", repr(headers))
        check("openai: real key value never lands in the JSON body", "sk-secret-123" not in str(sent))

    # response_format="json" -> OpenAI-style response_format object.
    with patch.object(rb._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
        rb.generate([{"role": "user", "content": "q"}], base_url="https://x", protocol="openai", model="m",
                    temperature=None, max_tokens=None, response_format="json", stop=None)
        check(
            "openai: response_format='json' becomes {'type': 'json_object'}",
            mock_post.call_args.kwargs["json"].get("response_format") == {"type": "json_object"},
            repr(mock_post.call_args.kwargs["json"]),
        )

    # stop sequences -> top-level "stop" field, OpenAI's own name for it.
    with patch.object(rb._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
        rb.generate([{"role": "user", "content": "q"}], base_url="https://x", protocol="openai", model="m",
                    temperature=None, max_tokens=None, response_format=None, stop=["</s>", "\nuser:"])
        check(
            "openai: stop sequences go in top-level 'stop', OpenAI's own field name",
            mock_post.call_args.kwargs["json"].get("stop") == ["</s>", "\nuser:"],
            repr(mock_post.call_args.kwargs["json"]),
        )

    # done_reason="length" -> truncated.
    with patch.object(rb._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"choices": [{"message": {"content": "обрубл"}, "finish_reason": "length"}]})
        _, meta = rb.generate([{"role": "user", "content": "q"}], base_url="https://x", protocol="openai", model="m",
                               temperature=None, max_tokens=None, response_format=None, stop=None)
        check("openai: finish_reason='length' maps to done_reason='length'", meta["done_reason"] == "length", repr(meta))

    # No api_key_env at all -> no Authorization header sent (self-hosted, no auth needed).
    with patch.object(rb._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
        rb.generate([{"role": "user", "content": "q"}], base_url="https://x", protocol="openai", model="m",
                    api_key_env=None, temperature=None, max_tokens=None, response_format=None, stop=None)
        check("openai: no api_key_env -> no Authorization header", "Authorization" not in mock_post.call_args.kwargs["headers"])

    # ── Anthropic Messages API ───────────────────────────────────────
    with patch.object(rb._session, "post") as mock_post, \
         patch.dict("os.environ", {"CLAUDE_KEY": "ant-secret-456"}):
        mock_post.return_value = _fake_response({
            "content": [{"type": "text", "text": "Париж"}],
            "stop_reason": "end_turn",
            "usage": {"output_tokens": 3},
        })
        text, meta = rb.generate(
            [{"role": "system", "content": "отвечай одним словом"}, {"role": "user", "content": "столица Франции?"}],
            base_url="https://api.anthropic.com", protocol="anthropic",
            model="claude-sonnet-5", api_key_env="CLAUDE_KEY",
            temperature=None, max_tokens=20, stop=None,
        )
        check("anthropic: returns text extracted from content blocks", text == "Париж", repr(text))
        check("anthropic: stop_reason=end_turn maps to not-truncated", meta["done_reason"] == "stop", repr(meta))
        check("anthropic: token count from usage.output_tokens", meta["eval_count"] == 3, repr(meta))

        url = mock_post.call_args.args[0]
        sent = mock_post.call_args.kwargs["json"]
        headers = mock_post.call_args.kwargs["headers"]
        check("anthropic: hits {base_url}/v1/messages", url == "https://api.anthropic.com/v1/messages", url)
        check("anthropic: system message EXTRACTED to a top-level field, not left in messages", sent.get("system") == "отвечай одним словом", repr(sent))
        check("anthropic: messages carry only the user turn (system stripped out)", sent["messages"] == [{"role": "user", "content": "столица Франции?"}], repr(sent["messages"]))
        check("anthropic: api key goes in x-api-key, not Authorization", headers.get("x-api-key") == "ant-secret-456", repr(headers))
        check("anthropic: anthropic-version header present", "anthropic-version" in headers, repr(headers))
        check("anthropic: max_tokens is required and passed through", sent.get("max_tokens") == 20, repr(sent))

    # Multiple system messages (chat_local.py sends 3 independent ones) -> joined into ONE system field.
    with patch.object(rb._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn"})
        rb.generate(
            [
                {"role": "system", "content": "первая инструкция"},
                {"role": "system", "content": "вторая инструкция"},
                {"role": "user", "content": "привет"},
            ],
            base_url="https://x", protocol="anthropic", model="m",
            temperature=None, max_tokens=None, stop=None,
        )
        sent = mock_post.call_args.kwargs["json"]
        check(
            "anthropic: MULTIPLE system messages joined into one system field, in order",
            sent.get("system") == "первая инструкция\n\nвторая инструкция",
            repr(sent.get("system")),
        )
        check("anthropic: only the real user turn remains in messages", sent["messages"] == [{"role": "user", "content": "привет"}], repr(sent["messages"]))

    # Anthropic stop -> stop_sequences, its own field name, not OpenAI's "stop".
    with patch.object(rb._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn"})
        rb.generate([{"role": "user", "content": "q"}], base_url="https://x", protocol="anthropic", model="m",
                    temperature=None, max_tokens=None, stop=["STOP1", "STOP2"])
        sent = mock_post.call_args.kwargs["json"]
        check("anthropic: stop sequences go in 'stop_sequences', its own field name (honest translation, not OpenAI's 'stop')",
              sent.get("stop_sequences") == ["STOP1", "STOP2"], repr(sent))
        check("anthropic: no top-level 'stop' key (that would be the wrong, OpenAI-shaped field)", "stop" not in sent, repr(sent))

    # stop_reason="max_tokens" -> truncated.
    with patch.object(rb._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"content": [{"type": "text", "text": "x"}], "stop_reason": "max_tokens"})
        _, meta = rb.generate([{"role": "user", "content": "q"}], base_url="https://x", protocol="anthropic", model="m",
                               temperature=None, max_tokens=None, stop=None)
        check("anthropic: stop_reason='max_tokens' maps to done_reason='length'", meta["done_reason"] == "length", repr(meta))

    # max_tokens omitted entirely -> Anthropic still gets a sane default (its API requires the field).
    with patch.object(rb._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn"})
        rb.generate([{"role": "user", "content": "q"}], base_url="https://x", protocol="anthropic", model="m",
                    temperature=None, max_tokens=None, stop=None)
        check(
            "anthropic: max_tokens always present even when caller didn't pass one (API requires it)",
            isinstance(mock_post.call_args.kwargs["json"].get("max_tokens"), int),
            repr(mock_post.call_args.kwargs["json"]),
        )

    # ── Errors ────────────────────────────────────────────────────────
    with patch.object(rb._session, "post") as mock_post:
        import requests
        mock_post.side_effect = requests.ConnectionError("сервер недоступен")
        try:
            rb.generate([{"role": "user", "content": "q"}], base_url="https://x", protocol="openai", model="m",
                        temperature=None, max_tokens=None, response_format=None, stop=None)
            check("network failure raises RemoteBackendError", False, "no exception raised")
        except rb.RemoteBackendError:
            check("network failure raises RemoteBackendError", True)
        except Exception as e:  # noqa: BLE001
            check("network failure raises RemoteBackendError", False, f"raised {type(e).__name__}: {e}")

    with patch.object(rb._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"unexpected": "shape"})
        try:
            rb.generate([{"role": "user", "content": "q"}], base_url="https://x", protocol="openai", model="m",
                        temperature=None, max_tokens=None, response_format=None, stop=None)
            check("malformed openai response raises RemoteBackendError", False, "no exception raised")
        except rb.RemoteBackendError:
            check("malformed openai response raises RemoteBackendError", True)
        except Exception as e:  # noqa: BLE001
            check("malformed openai response raises RemoteBackendError", False, f"raised {type(e).__name__}: {e}")

    try:
        rb.generate([{"role": "user", "content": "q"}], base_url="https://x", protocol="carrier-pigeon", model="m",
                    temperature=None, max_tokens=None, response_format=None, stop=None)
        check("unknown protocol raises RemoteBackendError", False, "no exception raised")
    except rb.RemoteBackendError:
        check("unknown protocol raises RemoteBackendError", True)
    except Exception as e:  # noqa: BLE001
        check("unknown protocol raises RemoteBackendError", False, f"raised {type(e).__name__}: {e}")

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
