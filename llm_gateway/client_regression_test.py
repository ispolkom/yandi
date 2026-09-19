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

import inspect
import sys
from unittest.mock import MagicMock, patch

from llm_gateway import ollama_backend

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


def _run_ollama_wire_format_checks(client) -> None:
    """Всё, что здесь проверялось до Phase 3 (свой движок) — то, как
    строится и парсится HTTP-запрос к Ollama. Локальный движок по
    умолчанию выключен (_LOCAL_ENABLED — opt-in через
    LLM_GATEWAY_ENABLE_LOCAL), поэтому эти тесты используют реальные
    имена моделей вроде "qwen3:14b" безопасно — движок даже не
    попытается их поднять, если явно не включён."""
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

    # 3d. response_format="json" -> top-level "format", не в options
    #     (claim_relation.py нужен строгий JSON-режим).
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"message": {"content": "{}"}})
        client.complete("q", model="m", response_format="json")
        sent = mock_post.call_args.kwargs["json"]
        check("response_format='json' sets top-level format", sent.get("format") == "json", repr(sent))
        check("response_format doesn't leak into options", "format" not in (sent.get("options") or {}), repr(sent))

    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _fake_response({"message": {"content": "ok"}})
        client.complete("q", model="m")
        sent = mock_post.call_args.kwargs["json"]
        check("no format key when response_format unset", "format" not in sent, repr(sent))

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


def _run_backend_dispatch_checks(client) -> None:
    """Phase 3: локальный движок первым, Ollama — тихий фоллбэк, ЕСЛИ
    явно включено (_LOCAL_ENABLED — opt-in, по умолчанию выключено ради
    десятков существующих regression-тестов по всему agent/, которые
    мокают requests.Session.post напрямую для тех же имён моделей).
    Мокает llamacpp_backend напрямую (не сам llama_cpp) — эти тесты про
    ЛОГИКУ ПЕРЕКЛЮЧЕНИЯ в client.py, а не про сам движок (у него будет
    свой отдельный, требующий реальной GGUF, живой smoke-тест)."""
    from llm_gateway import llamacpp_backend

    # По умолчанию (_LOCAL_ENABLED=False, ничего не патчим) движок не
    # трогается вообще, даже если модель зарегистрирована — именно это
    # держит все остальные regression-тесты в agent/ рабочими без
    # единой правки с их стороны.
    with patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate") as mock_gen, \
         patch.object(ollama_backend, "generate", return_value=("ответ ollama", {})) as mock_ollama:
        out = client.complete("q", model="heretic:q8")
        check("local engine untouched by default (opt-in, not opt-out)", mock_gen.call_count == 0)
        check("default behavior still goes through Ollama", out == "ответ ollama")

    # LLM_GATEWAY_ENABLE_LOCAL включён + модель есть в реестре -> движок
    # используется, Ollama даже не трогаем.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate", return_value=("ответ движка", {})) as mock_gen, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="heretic:q8")
        check("local engine used when enabled and has_model() is True", out == "ответ движка", repr(out))
        check("Ollama path never called when local engine succeeds", mock_ollama.call_count == 0)
        check("local engine received the model name", mock_gen.call_args.kwargs["model"] == "heretic:q8")

    # Включён, но модели нет в реестре движка -> прямиком в Ollama.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(llamacpp_backend, "has_model", return_value=False), \
         patch.object(llamacpp_backend, "generate") as mock_gen, \
         patch.object(ollama_backend, "generate", return_value=("ответ ollama", {})) as mock_ollama:
        out = client.complete("q", model="unknown-model")
        check("unregistered model skips local engine entirely", mock_gen.call_count == 0)
        check("unregistered model falls through to Ollama", out == "ответ ollama")

    # Включён, модель есть, но движок падает при генерации -> тихий
    # откат на Ollama, без исключения наружу.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate", side_effect=RuntimeError("GPU OOM")), \
         patch.object(ollama_backend, "generate", return_value=("ответ ollama", {})) as mock_ollama:
        out = client.complete("q", model="heretic:q8")
        check("local engine failure falls back to Ollama silently", out == "ответ ollama")
        check("Ollama is actually called on fallback", mock_ollama.call_count == 1)

    # Включён, модель есть, но base_url нелокальный (валидатор чужой
    # ноды) -> локальный движок не трогаем вообще.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate") as mock_gen, \
         patch.object(ollama_backend, "generate", return_value=("ответ удалённой ноды", {})) as mock_ollama:
        out = client.complete("q", model="heretic:q8", base_url="http://10.0.0.9:11434")
        check("non-default base_url skips local engine even if model is registered", mock_gen.call_count == 0)
        check("non-default base_url goes straight to Ollama", out == "ответ удалённой ноды")


def _run_node_config_dispatch_checks(client) -> None:
    """Настройка владельца узла (llm_gateway.config) — приоритет НАД
    встроенным дефолтом llamacpp_backend, и работает для обоих типов
    (свой локальный файл, свой удалённый сервер)."""
    from llm_gateway import config as node_config
    from llm_gateway import llamacpp_backend, remote_backend

    # Своя локальная модель (владелец узла указал СВОЙ путь) побеждает
    # встроенный дефолт для того же имени — даже если has_model()
    # тоже сказал бы True для встроенного реестра.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value={"backend": "llamacpp", "path": "/своя/папка/модель.gguf"}), \
         patch.object(llamacpp_backend, "generate_at_spec", return_value=("ответ своей модели", {})) as mock_spec, \
         patch.object(llamacpp_backend, "generate") as mock_builtin, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("heretic:q8", model="heretic:q8")
        check("user-configured local model wins over the built-in default", out == "ответ своей модели", repr(out))
        check("built-in registry never consulted when user config exists", mock_builtin.call_count == 0)
        check("Ollama never consulted when user config succeeds", mock_ollama.call_count == 0)
        check("the user's own path reaches llama.cpp", mock_spec.call_args.kwargs["spec"].path == "/своя/папка/модель.gguf")

    # Свой удалённый сервер (например Клод) — тоже настройка узла,
    # дальше делегируется в remote_backend без изменений.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value={
             "backend": "remote", "protocol": "anthropic",
             "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5",
             "api_key_env": "MY_CLAUDE_KEY",
         }), \
         patch.object(remote_backend, "generate", return_value=("ответ клода", {})) as mock_remote, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="мой-клод")
        check("user-configured remote model is used", out == "ответ клода", repr(out))
        check("Ollama never consulted for a configured remote model", mock_ollama.call_count == 0)
        check(
            "remote_backend receives the configured protocol/url/model/key-env",
            mock_remote.call_args.kwargs["protocol"] == "anthropic"
            and mock_remote.call_args.kwargs["base_url"] == "https://api.anthropic.com"
            and mock_remote.call_args.kwargs["model"] == "claude-sonnet-5"
            and mock_remote.call_args.kwargs["api_key_env"] == "MY_CLAUDE_KEY",
            repr(mock_remote.call_args.kwargs),
        )

    # Неизвестный backend в настройке узла -> ЭТО ЯВНАЯ НАСТРОЙКА
    # ВЛАДЕЛЬЦА (запись найдена, просто битая) -> CONFIGURED_BACKEND_FAILED,
    # никакого отката на Ollama, наружу честная LLMError. (До фикса
    # "explicit backend fallback" здесь тихо подставлялся ответ Ollama —
    # это и было нарушением главного инварианта, найденным аудитом.)
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value={"backend": "carrier-pigeon"}), \
         patch.object(ollama_backend, "generate", return_value=("ответ ollama", {})) as mock_ollama:
        try:
            client.complete("q", model="странная-модель")
            check("unknown backend type in EXPLICIT node config raises, never falls back to Ollama", False, "no exception raised")
        except client.LLMError:
            check("unknown backend type in EXPLICIT node config raises, never falls back to Ollama", True)
        check("Ollama not called for a broken explicit config", mock_ollama.call_count == 0)

    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value={
             "backend": "remote", "protocol": "carrier-pigeon",
             "base_url": "https://example.invalid", "model": "m",
         }), \
         patch.object(ollama_backend, "generate", return_value=("ответ ollama", {})) as mock_ollama:
        try:
            client.complete("q", model="remote-with-unknown-protocol")
            check("unknown remote protocol in EXPLICIT node config raises, never falls back to Ollama", False, "no exception raised")
        except client.LLMError:
            check("unknown remote protocol in EXPLICIT node config raises, never falls back to Ollama", True)
        check("Ollama not called for unknown remote protocol", mock_ollama.call_count == 0)

    # Ничего не настроено под этим именем -> обычное поведение
    # встроенного дефолта, без изменений.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate", return_value=("ответ дефолта", {})) as mock_gen, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="heretic:q8")
        check("no node config for this name -> falls through to built-in default unaffected", out == "ответ дефолта", repr(out))


def _run_explicit_backend_invariant_checks(client) -> None:
    """Мандат "explicit backend fallback fix" (после
    YANDI_OLLAMA_DECOUPLING_AUDIT.md §5): явный выбор владельца узла
    сильнее любого автоматического fallback. TEST 1-10 из мандата,
    один в один."""
    from llm_gateway import config as node_config
    from llm_gateway import llamacpp_backend, remote_backend
    import requests

    remote_entry = {
        "backend": "remote", "protocol": "openai",
        "base_url": "https://my-own-server.example", "model": "my-model",
        "api_key_env": "MY_KEY",
    }
    anthropic_entry = {
        "backend": "remote", "protocol": "anthropic",
        "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5",
        "api_key_env": "MY_CLAUDE_KEY",
    }
    local_entry = {"backend": "llamacpp", "path": "/владелец/своя/модель.gguf"}

    # TEST 1 — явный remote OpenAI-compatible backend отвечает успешно -> Ollama не трогаем.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend, "generate", return_value=("успех", {})) as mock_remote, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="test-remote")
        check("TEST1: explicit remote backend success -> returned as-is", out == "успех", repr(out))
        check("TEST1: Ollama not called", mock_ollama.call_count == 0)

    # TEST 2 — явный remote backend, connection refused -> ошибка наружу, Ollama не трогаем.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend._session, "post", side_effect=requests.ConnectionError("connection refused")), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        try:
            client.complete("q", model="test-remote")
            check("TEST2: connection refused on explicit remote -> raises LLMError", False, "no exception")
        except client.LLMError:
            check("TEST2: connection refused on explicit remote -> raises LLMError", True)
        check("TEST2: Ollama not called", mock_ollama.call_count == 0)

    # TEST 3 — явный Anthropic backend, HTTP 401 -> ошибка наружу, Ollama не трогаем.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=anthropic_entry), \
         patch.object(remote_backend._session, "post", return_value=_fake_response({"error": "unauthorized"}, status_code=401)), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        try:
            client.complete("q", model="my-claude")
            check("TEST3: HTTP 401 on explicit Anthropic backend -> raises LLMError", False, "no exception")
        except client.LLMError:
            check("TEST3: HTTP 401 on explicit Anthropic backend -> raises LLMError", True)
        check("TEST3: Ollama not called", mock_ollama.call_count == 0)

    # TEST 4 — явный Anthropic backend, HTTP 429 (rate limit) -> ошибка наружу, Ollama не трогаем.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=anthropic_entry), \
         patch.object(remote_backend._session, "post", return_value=_fake_response({"error": "rate_limited"}, status_code=429)), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        try:
            client.complete("q", model="my-claude")
            check("TEST4: HTTP 429 on explicit Anthropic backend -> raises LLMError", False, "no exception")
        except client.LLMError:
            check("TEST4: HTTP 429 on explicit Anthropic backend -> raises LLMError", True)
        check("TEST4: Ollama not called", mock_ollama.call_count == 0)

    # TEST 5 — битая/неизвестная конфигурация явного backend'а -> ошибка наружу, Ollama не трогаем.
    # (покрыто также переписанным тестом в _run_node_config_dispatch_checks, дублируем здесь
    #  явно по номеру мандата, плюс отсутствующее обязательное поле как отдельный вариант.)
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value={"backend": "remote"}), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        # protocol отсутствует -> remote_backend.generate() получит protocol="openai" по
        # умолчанию (см. _try_configured_backend), а base_url будет KeyError -> раскрывается
        # как настоящая ошибка конфигурации, не тихий переход на Ollama.
        try:
            client.complete("q", model="битая-настройка")
            check("TEST5: broken explicit config (missing base_url) -> raises LLMError", False, "no exception")
        except client.LLMError:
            check("TEST5: broken explicit config (missing base_url) -> raises LLMError", True)
        except KeyError:
            check("TEST5: broken explicit config (missing base_url) -> raises LLMError", False, "raised raw KeyError, not wrapped as LLMError")
        check("TEST5: Ollama not called", mock_ollama.call_count == 0)

    # TEST 6 — явный local llama.cpp backend падает при загрузке модели -> ошибка наружу,
    # Ollama не трогаем, ДАЖЕ ХОТЯ встроенный дефолт формально мог бы обслужить то же имя.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=local_entry), \
         patch.object(llamacpp_backend, "generate_at_spec", side_effect=RuntimeError("GGUF-файл не найден")), \
         patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate") as mock_builtin_gen, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        try:
            client.complete("heretic:q8", model="heretic:q8")
            check("TEST6: explicit local backend load failure -> raises LLMError", False, "no exception")
        except client.LLMError:
            check("TEST6: explicit local backend load failure -> raises LLMError", True)
        check("TEST6: built-in default not silently substituted", mock_builtin_gen.call_count == 0)
        check("TEST6: Ollama not called", mock_ollama.call_count == 0)

    # TEST 7 — ничего не настроено, встроенный локальный дефолт работает -> штатное поведение.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate", return_value=("дефолт сработал", {})) as mock_gen, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="heretic:q8")
        check("TEST7: no config, built-in default works -> used normally", out == "дефолт сработал")
        check("TEST7: Ollama not called when built-in default succeeds", mock_ollama.call_count == 0)

    # TEST 8 — ничего не настроено, локального дефолта нет/не подходит, Ollama доступна ->
    # старый допустимый fallback работает.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(llamacpp_backend, "has_model", return_value=False), \
         patch.object(ollama_backend, "generate", return_value=("ответ ollama", {})) as mock_ollama:
        out = client.complete("q", model="совсем-неизвестная-модель")
        check("TEST8: no config, no local candidate -> falls back to Ollama (allowed, Case A)", out == "ответ ollama")

    # TEST 9 — ничего не настроено, ни локальный дефолт, ни Ollama не работают -> честная
    # конечная ошибка, не тишина и не подмена.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate", side_effect=RuntimeError("GPU OOM")), \
         patch.object(ollama_backend, "generate", side_effect=client.LLMError("ollama тоже недоступна")):
        try:
            client.complete("q", model="heretic:q8")
            check("TEST9: nothing works (no config) -> raises a real LLMError", False, "no exception")
        except client.LLMError:
            check("TEST9: nothing works (no config) -> raises a real LLMError", True)

    # TEST 10 — успешный ответ явно настроенного backend'а возвращается БЕЗ дополнительных
    # попыток обратиться к встроенному дефолту или к Ollama (обе стороны молчат).
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend, "generate", return_value=("единственный ответ", {})), \
         patch.object(llamacpp_backend, "generate") as mock_builtin, \
         patch.object(llamacpp_backend, "has_model") as mock_has_model, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="test-remote")
        check("TEST10: successful explicit backend result returned as-is", out == "единственный ответ")
        check("TEST10: built-in default never even checked", mock_has_model.call_count == 0 and mock_builtin.call_count == 0)
        check("TEST10: Ollama never called", mock_ollama.call_count == 0)

    # ── Адверсариальные варианты (не по номеру, дополнительно) ──────────
    # Пустая строка от явно настроенного backend'а — это ВСЁ РАВНО успех
    # этого backend'а, не сигнал попробовать что-то ещё.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend, "generate", return_value=("", {})), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="test-remote")
        check("empty string from explicit backend is treated as success, not a trigger to fall back", out == "")
        check("Ollama not called for an empty-but-successful explicit response", mock_ollama.call_count == 0)

    # Malformed JSON (неожиданный формат ответа) от явно настроенного remote backend'а.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend._session, "post", return_value=_fake_response({"totally": "unexpected"})), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        try:
            client.complete("q", model="test-remote")
            check("malformed response from explicit backend -> raises, no fallback", False, "no exception")
        except client.LLMError:
            check("malformed response from explicit backend -> raises, no fallback", True)
        check("Ollama not called for malformed explicit response", mock_ollama.call_count == 0)

    # HTTP 500 от явно настроенного backend'а.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend._session, "post", return_value=_fake_response({"error": "internal"}, status_code=500)), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        try:
            client.complete("q", model="test-remote")
            check("HTTP 500 from explicit backend -> raises, no fallback", False, "no exception")
        except client.LLMError:
            check("HTTP 500 from explicit backend -> raises, no fallback", True)
        check("Ollama not called for HTTP 500 explicit response", mock_ollama.call_count == 0)

    # Backend-функция нарушает контракт (текст, метаданные) и возвращает голый None —
    # это ОШИБКА этого backend'а (запись найдена, значит мы уже CONFIGURED), а не
    # повод молча провалиться в "как будто ничего не настроено" -> Ollama.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend, "generate", return_value=None), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        try:
            client.complete("q", model="test-remote")
            check("backend violating (text, meta) contract (returns bare None) -> raises, no fallback", False, "no exception")
        except client.LLMError:
            check("backend violating (text, meta) contract (returns bare None) -> raises, no fallback", True)
        check("Ollama not called when configured backend returns bare None", mock_ollama.call_count == 0)

    # base_url совпадает с DEFAULT_BASE_URL — явная настройка всё равно проверяется первой
    # (это дефолтное значение параметра base_url= у complete(), не признак "это Ollama").
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend, "generate", return_value=("ответ при дефолтном base_url", {})), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="test-remote", base_url=client.DEFAULT_BASE_URL)
        check("explicit config still checked first even when base_url equals DEFAULT_BASE_URL", out == "ответ при дефолтном base_url")
        check("Ollama not called", mock_ollama.call_count == 0)


def _run_flag_independence_checks(client) -> None:
    """Мандат "explicit config independence": LLM_GATEWAY_ENABLE_LOCAL
    управляет ТОЛЬКО автоматической загрузкой встроенного дефолтного
    движка (STEP 2) — явная настройка владельца узла (STEP 1)
    проверяется и уважается независимо от значения этого флага. TEST
    1-11 из мандата, один в один, плюс честная проверка "заморожен ли
    _LOCAL_ENABLED на момент импорта" через реальный importlib.reload()."""
    from llm_gateway import config as node_config
    from llm_gateway import llamacpp_backend, remote_backend
    import requests

    remote_entry = {"backend": "remote", "protocol": "openai",
                     "base_url": "https://my-own-server.example", "model": "my-model",
                     "api_key_env": "MY_KEY"}
    anthropic_entry = {"backend": "remote", "protocol": "anthropic",
                        "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5",
                        "api_key_env": "MY_CLAUDE_KEY"}
    local_entry = {"backend": "llamacpp", "path": "/владелец/своя/модель.gguf"}

    # TEST 1 — флаг отсутствует, explicit remote OpenAI-compatible работает -> используется.
    with patch.object(client, "_LOCAL_ENABLED", False), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend, "generate", return_value=("ответ remote", {})), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="m")
        check("TEST1: flag absent, explicit remote works -> used", out == "ответ remote")
        check("TEST1: Ollama not called", mock_ollama.call_count == 0)

    # TEST 2 — флаг отсутствует, explicit Anthropic работает -> используется.
    with patch.object(client, "_LOCAL_ENABLED", False), \
         patch.object(node_config, "get_model_entry", return_value=anthropic_entry), \
         patch.object(remote_backend, "generate", return_value=("ответ клода", {})), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="my-claude")
        check("TEST2: flag absent, explicit Anthropic works -> used", out == "ответ клода")
        check("TEST2: Ollama not called", mock_ollama.call_count == 0)

    # TEST 3 — флаг отсутствует, explicit remote падает -> честная LLMError, не Ollama.
    with patch.object(client, "_LOCAL_ENABLED", False), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend._session, "post", side_effect=requests.ConnectionError("down")), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        try:
            client.complete("q", model="m")
            check("TEST3: flag absent, explicit remote fails -> raises LLMError", False, "no exception")
        except client.LLMError:
            check("TEST3: flag absent, explicit remote fails -> raises LLMError", True)
        check("TEST3: Ollama not called", mock_ollama.call_count == 0)

    # TEST 4 — флаг эквивалентен "0" (см. отдельную проверку самой формулы ниже), explicit
    # remote работает -> всё равно используется.
    with patch.object(client, "_LOCAL_ENABLED", False), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend, "generate", return_value=("ответ remote", {})), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="m")
        check("TEST4: flag='0'-equivalent, explicit remote still used", out == "ответ remote")
        check("TEST4: Ollama not called", mock_ollama.call_count == 0)

    # TEST 5 — неожиданное значение флага (в любую сторону) не меняет приоритет explicit config.
    for flag_value in (True, False):
        with patch.object(client, "_LOCAL_ENABLED", flag_value), \
             patch.object(node_config, "get_model_entry", return_value=remote_entry), \
             patch.object(remote_backend, "generate", return_value=("ответ remote", {})), \
             patch.object(ollama_backend, "generate") as mock_ollama:
            out = client.complete("q", model="m")
            check(f"TEST5: explicit config wins regardless of flag value ({flag_value})", out == "ответ remote")
            check(f"TEST5: Ollama not called (flag={flag_value})", mock_ollama.call_count == 0)

    # TEST 6 — флаг отсутствует, explicit configured LOCAL модель -> используется именно она,
    # встроенный дефолт даже не проверяется.
    with patch.object(client, "_LOCAL_ENABLED", False), \
         patch.object(node_config, "get_model_entry", return_value=local_entry), \
         patch.object(llamacpp_backend, "generate_at_spec", return_value=("ответ своей локальной модели", {})), \
         patch.object(llamacpp_backend, "has_model") as mock_has_model, \
         patch.object(llamacpp_backend, "generate") as mock_builtin, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("heretic:q8", model="heretic:q8")
        check("TEST6: flag absent, explicit LOCAL config still used", out == "ответ своей локальной модели")
        check("TEST6: built-in default (has_model/generate) never even checked", mock_has_model.call_count == 0 and mock_builtin.call_count == 0)
        check("TEST6: Ollama not called", mock_ollama.call_count == 0)

    # TEST 7 — флаг отсутствует, explicit config нет -> встроенный дефолт НЕ грузится автоматически.
    with patch.object(client, "_LOCAL_ENABLED", False), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(llamacpp_backend, "has_model") as mock_has_model, \
         patch.object(llamacpp_backend, "generate") as mock_gen, \
         patch.object(ollama_backend, "generate", return_value=("ответ ollama", {})) as mock_ollama:
        out = client.complete("q", model="heretic:q8")
        check("TEST7: flag absent, no config -> built-in default never even checked", mock_has_model.call_count == 0 and mock_gen.call_count == 0)
        check("TEST7: falls to Ollama (allowed, Case A)", out == "ответ ollama")

    # TEST 8 — флаг=1, explicit config нет -> встроенный дефолт работает как раньше.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate", return_value=("ответ дефолта", {})), \
         patch.object(ollama_backend, "generate") as mock_ollama:
        out = client.complete("q", model="heretic:q8")
        check("TEST8: flag=1, no config -> built-in default used", out == "ответ дефолта")
        check("TEST8: Ollama not called", mock_ollama.call_count == 0)

    # TEST 9 — флаг отсутствует, explicit config нет, Ollama доступна -> старый фоллбэк работает.
    with patch.object(client, "_LOCAL_ENABLED", False), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(ollama_backend, "generate", return_value=("ответ ollama", {})) as mock_ollama:
        out = client.complete("q", model="совсем-неизвестная-модель")
        check("TEST9: flag absent, no config -> Ollama fallback still works", out == "ответ ollama")

    # TEST 10 — флаг=1, explicit config ЕСТЬ и падает -> НИКАКОГО перехода ни на local, ни на
    # Ollama, несмотря на то, что флаг разрешает local default.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend._session, "post", side_effect=requests.ConnectionError("down")), \
         patch.object(llamacpp_backend, "has_model") as mock_has_model, \
         patch.object(llamacpp_backend, "generate") as mock_gen, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        try:
            client.complete("q", model="m")
            check("TEST10: flag=1, explicit config fails -> raises, no local/Ollama fallback", False, "no exception")
        except client.LLMError:
            check("TEST10: flag=1, explicit config fails -> raises, no local/Ollama fallback", True)
        check("TEST10: built-in default never even checked despite flag=1", mock_has_model.call_count == 0 and mock_gen.call_count == 0)
        check("TEST10: Ollama not called", mock_ollama.call_count == 0)

    # TEST 11 — флаг реально меняется через os.environ + importlib.reload() между вызовами
    # (не просто patch.object) — проверяем и честную семантику "заморожен на момент импорта"
    # (адверсариальный пункт §7), и что explicit config уважается в любом из состояний.
    import importlib
    import os as _os

    orig_env = _os.environ.get("LLM_GATEWAY_ENABLE_LOCAL")
    try:
        for env_value, expected_flag in [(None, False), ("", False), ("0", False), ("1", True), ("garbage", True)]:
            if env_value is None:
                _os.environ.pop("LLM_GATEWAY_ENABLE_LOCAL", None)
            else:
                _os.environ["LLM_GATEWAY_ENABLE_LOCAL"] = env_value
            reloaded = importlib.reload(client)
            check(
                f"TEST11: _LOCAL_ENABLED is fixed at import time for env={env_value!r} -> {expected_flag}",
                reloaded._LOCAL_ENABLED == expected_flag, repr(reloaded._LOCAL_ENABLED),
            )
            with patch.object(node_config, "get_model_entry", return_value=remote_entry), \
                 patch.object(remote_backend, "generate", return_value=("явный ответ", {})), \
                patch.object(ollama_backend, "generate") as mock_ollama:
                out = reloaded.complete("q", model="m")
                check(f"TEST11: explicit config honored after reload regardless of flag (env={env_value!r})", out == "явный ответ")
                check(f"TEST11: Ollama not called (env={env_value!r})", mock_ollama.call_count == 0)
    finally:
        if orig_env is None:
            _os.environ.pop("LLM_GATEWAY_ENABLE_LOCAL", None)
        else:
            _os.environ["LLM_GATEWAY_ENABLE_LOCAL"] = orig_env
        importlib.reload(client)


def _run_unified_resolver_checks(client) -> None:
    """Phase 1 unified backend resolver: backend identity/capability is
    resolved once per generation attempt, and fallback is a second attempt
    with its own resolved backend."""
    from llm_gateway import config as node_config
    from llm_gateway import llamacpp_backend, remote_backend

    remote_entry = {
        "backend": "remote", "protocol": "openai",
        "base_url": "https://my-own-server.example", "model": "my-model",
        "api_key_env": "MY_KEY",
    }

    # Explicit config wins deterministically and no builtin/Ollama routing is attempted.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry) as mock_entry, \
         patch.object(remote_backend, "generate", return_value=("ok", {"done_reason": "stop"})), \
         patch.object(llamacpp_backend, "has_model") as mock_has_model, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        text, raw = client._do_complete(
            "q", model="heretic:q8", system=None, messages=None, temperature=None,
            max_tokens=None, timeout=client.DEFAULT_TIMEOUT, base_url=client.DEFAULT_BASE_URL,
            strip_think=True, extra_options=None, response_format="json", stop=None,
        )
        trace = raw["_llm_gateway_trace"]
        check("resolver: explicit config returns text", text == "ok", repr(text))
        check("resolver: explicit config resolved once", mock_entry.call_count == 1, repr(mock_entry.call_count))
        check("resolver: explicit config skips builtin has_model", mock_has_model.call_count == 0)
        check("resolver: explicit config skips Ollama", mock_ollama.call_count == 0)
        check("resolver: trace records remote adapter", trace[0]["adapter_id"] == "openai_compatible", repr(trace))
        check("resolver: trace records logical model", trace[0]["logical_model"] == "heretic:q8", repr(trace))
        check("resolver: trace records resolved model", trace[0]["resolved_model"] == "my-model", repr(trace))
        check("resolver: response_format='json' contract preserved", trace[0]["output_contract"] == "json_object", repr(trace))

    # Explicit failure remains fail-loud and does not silently fallback.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=remote_entry), \
         patch.object(remote_backend, "generate", side_effect=RuntimeError("remote down")), \
         patch.object(llamacpp_backend, "has_model") as mock_has_model, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        try:
            client.complete("q", model="heretic:q8")
            check("resolver: explicit failure raises", False, "no exception")
        except client.LLMError:
            check("resolver: explicit failure raises", True)
        check("resolver: explicit failure does not check builtin", mock_has_model.call_count == 0)
        check("resolver: explicit failure does not call Ollama", mock_ollama.call_count == 0)

    # Builtin success is one attempt; Ollama is not touched.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(llamacpp_backend, "has_model", return_value=True) as mock_has_model, \
         patch.object(llamacpp_backend, "generate", return_value=("builtin ok", {"done_reason": "stop"})) as mock_gen, \
         patch.object(ollama_backend, "generate") as mock_ollama:
        text, raw = client._do_complete(
            "q", model="heretic:q8", system=None, messages=None, temperature=None,
            max_tokens=None, timeout=client.DEFAULT_TIMEOUT, base_url=client.DEFAULT_BASE_URL,
            strip_think=True, extra_options=None, response_format=None, stop=None,
        )
        trace = raw["_llm_gateway_trace"]
        check("resolver: builtin success text", text == "builtin ok", repr(text))
        check("resolver: builtin has_model checked once", mock_has_model.call_count == 1, repr(mock_has_model.call_count))
        check("resolver: builtin generate called once", mock_gen.call_count == 1, repr(mock_gen.call_count))
        check("resolver: builtin success does not call Ollama", mock_ollama.call_count == 0)
        check("resolver: builtin trace has one attempt", len(trace) == 1 and trace[0]["adapter_id"] == "llama_cpp", repr(trace))

    # Builtin failure resolves Ollama as a second attempt; contract is attached to the second backend too.
    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=None) as mock_entry, \
         patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate", side_effect=RuntimeError("GPU OOM")), \
         patch.object(ollama_backend, "generate", return_value=("ollama ok", {"done_reason": "stop"})) as mock_ollama:
        text, raw = client._do_complete(
            "q", model="heretic:q8", system=None, messages=None, temperature=None,
            max_tokens=None, timeout=client.DEFAULT_TIMEOUT, base_url=client.DEFAULT_BASE_URL,
            strip_think=True, extra_options=None, response_format="json", stop=None,
        )
        trace = raw["_llm_gateway_trace"]
        check("resolver: builtin fallback text", text == "ollama ok", repr(text))
        check("resolver: fallback does not re-read node_config", mock_entry.call_count == 1, repr(mock_entry.call_count))
        check("resolver: fallback calls Ollama once", mock_ollama.call_count == 1, repr(mock_ollama.call_count))
        check(
            "resolver: fallback trace is two attempts with new backend",
            len(trace) == 2
            and trace[0]["adapter_id"] == "llama_cpp"
            and trace[0]["result"] == "failed"
            and trace[1]["adapter_id"] == "ollama_compatible"
            and trace[1]["result"] == "success",
            repr(trace),
        )
        check("resolver: fallback attempt keeps response_format='json'", trace[1]["output_contract"] == "json_object", repr(trace))


def _run_adapter_layer_checks(client) -> None:
    """Phase A/B adapter layer: resolution picks an adapter once, then
    generation calls that adapter rather than a central backend switch."""
    from llm_gateway import adapters, config as node_config
    from llm_gateway import llamacpp_backend, remote_backend
    from llm_gateway.types import BackendCapabilities, GenerationRequest, OutputContract

    ids = adapters.list_adapter_ids()
    expected = {"llama_cpp", "ollama_compatible", "openai_compatible", "anthropic"}
    check("adapters: registry contains current adapters", expected.issubset(set(ids)), repr(ids))
    check("adapters: adapter ids are unique", len(ids) == len(set(ids)), repr(ids))

    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value={"backend": "llamacpp", "path": "/x/model.gguf"}):
        resolved = client.resolve_target("local-model", base_url=client.DEFAULT_BASE_URL)
        check("adapters: explicit llama.cpp resolves LlamaCppAdapter", resolved.adapter_id == "llama_cpp", repr(resolved))
        check("target: legacy llama.cpp config records logical_model", resolved.logical_model == "local-model", repr(resolved))
        check("target: legacy llama.cpp config records resolved_model", resolved.resolved_model == "local-model", repr(resolved))
        check("target: legacy llama.cpp config records runtime", resolved.runtime == "llama_cpp", repr(resolved))
        check("target: legacy llama.cpp config records provider=local", resolved.provider == "local", repr(resolved))
        check("target: legacy llama.cpp config records file location", resolved.location == "/x/model.gguf", repr(resolved))
        check("target: legacy llama.cpp config has sanitized config_ref", resolved.config_ref == "secure_store:local-model", repr(resolved))

    with patch.object(client, "_LOCAL_ENABLED", False), \
         patch.object(node_config, "get_model_entry", return_value=None):
        resolved = client.resolve_target("unknown", base_url=client.DEFAULT_BASE_URL)
        check("adapters: default fallback resolves OllamaCompatAdapter", resolved.adapter_id == "ollama_compatible", repr(resolved))

    openai_entry = {
        "backend": "remote", "protocol": "openai",
        "base_url": "https://server.example", "model": "provider-model",
        "api_key_env": "K",
    }
    with patch.object(node_config, "get_model_entry", return_value=openai_entry):
        resolved = client.resolve_target("remote-openai", base_url=client.DEFAULT_BASE_URL)
        check("adapters: OpenAI-compatible remote resolves OpenAICompatibleAdapter", resolved.adapter_id == "openai_compatible", repr(resolved))
        check("target: OpenAI legacy config supports logical_model != resolved_model", resolved.logical_model == "remote-openai" and resolved.resolved_model == "provider-model", repr(resolved))
        check("target: OpenAI legacy config records runtime", resolved.runtime == "external_server", repr(resolved))
        check("target: OpenAI legacy config records provider separately from adapter", resolved.provider == "openai_compatible" and resolved.provider != "remote", repr(resolved))
        check("target: OpenAI legacy config records URL as location data", resolved.location == "https://server.example", repr(resolved))

    anthropic_entry = {
        "backend": "remote", "protocol": "anthropic",
        "base_url": "https://api.anthropic.com", "model": "claude",
        "api_key_env": "K",
    }
    with patch.object(node_config, "get_model_entry", return_value=anthropic_entry):
        resolved = client.resolve_target("remote-anthropic", base_url=client.DEFAULT_BASE_URL)
        check("adapters: Anthropic remote resolves AnthropicAdapter", resolved.adapter_id == "anthropic", repr(resolved))
        check("target: Anthropic legacy config records provider", resolved.provider == "anthropic", repr(resolved))
        check("target: Anthropic legacy config records runtime=provider", resolved.runtime == "provider", repr(resolved))

    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(llamacpp_backend, "has_model") as mock_has_model:
        resolved = client.resolve_target("any-model", base_url="http://10.0.0.9:11434")
        check("target: legacy non-default base_url normalizes to explicit Ollama-compatible target", resolved.adapter_id == "ollama_compatible", repr(resolved))
        check("target: non-default base_url is stored as location data", resolved.location == "http://10.0.0.9:11434", repr(resolved))
        check("target: non-default base_url skips builtin registry", mock_has_model.call_count == 0, repr(mock_has_model.call_count))

    http_openai_entry = {
        "backend": "remote", "protocol": "openai",
        "base_url": "http://host:9000", "model": "server-model",
        "api_key_env": "K",
    }
    with patch.object(node_config, "get_model_entry", return_value=http_openai_entry):
        resolved = client.resolve_target("http-openai", base_url=client.DEFAULT_BASE_URL)
        check("target: URL location does not force Ollama adapter after normalization", resolved.adapter_id == "openai_compatible", repr(resolved))

    class FakeAdapter:
        adapter_id = "fake_test_adapter"

        def capabilities(self, target):
            return BackendCapabilities(json_object=True)

        def generate(self, request: GenerationRequest, target, contract: OutputContract):
            return f"{self.adapter_id}:{request.model}:{contract.name}", {"done_reason": "stop"}

    fake = FakeAdapter()
    fake_registry = adapters.AdapterRegistry()
    fake_registry.register(fake)
    from llm_gateway.types import ResolvedInferenceTarget

    resolved = ResolvedInferenceTarget(
        logical_model="fake-logical",
        resolved_model="fake-model",
        adapter_id=fake.adapter_id,
        adapter=fake,
        capabilities=fake.capabilities({}),
        resolution_reason="test-only adapter",
        attempt=1,
        source="test",
        provider="test",
        runtime="test",
        target={},
    )
    text, raw = client._generate_with_target(
        resolved, "q", system=None, messages=None, temperature=None, max_tokens=None,
        timeout=client.DEFAULT_TIMEOUT, extra_options=None,
        contract=OutputContract(name="json_object", response_format="json"), stop=None,
    )
    check("adapters: fake adapter can generate without central dispatch branch", text == "fake_test_adapter:fake-model:json_object", repr((text, raw)))

    with patch.object(client, "_LOCAL_ENABLED", False), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(ollama_backend, "generate", return_value=("json ok", {"done_reason": "stop"})) as mock_ollama:
        out = client.complete("q", model="m", response_format="json")
        check("adapters: response_format='json' behavior unchanged", out == "json ok", repr(out))
        check("adapters: response_format reaches Ollama adapter", mock_ollama.call_args.kwargs["response_format"] == "json", repr(mock_ollama.call_args))

    with patch.object(client, "_LOCAL_ENABLED", True), \
         patch.object(node_config, "get_model_entry", return_value=None), \
         patch.object(llamacpp_backend, "has_model", return_value=True), \
         patch.object(llamacpp_backend, "generate", side_effect=RuntimeError("GPU OOM")), \
         patch.object(ollama_backend, "generate", return_value=("fallback ok", {"done_reason": "stop"})):
        text, raw = client._do_complete(
            "q", model="heretic:q8", system=None, messages=None, temperature=None,
            max_tokens=None, timeout=client.DEFAULT_TIMEOUT, base_url=client.DEFAULT_BASE_URL,
            strip_think=True, extra_options=None, response_format=None, stop=None,
        )
        trace = raw["_llm_gateway_trace"]
        check("adapters: fallback semantics unchanged", text == "fallback ok", repr(text))
        check(
            "adapters: fallback gets a new resolved adapter",
            trace[0]["adapter_id"] == "llama_cpp" and trace[1]["adapter_id"] == "ollama_compatible",
            repr(trace),
        )
        check("target: fallback creates second attempt", trace[0]["attempt"] == 1 and trace[1]["attempt"] == 2, repr(trace))
        check("target: fallback records reason", trace[1]["fallback_reason"] is not None, repr(trace))
        check("target: fallback capabilities come from fallback adapter", trace[1]["capabilities"]["json_schema"] is True, repr(trace))

    generated_source = inspect.getsource(client._generate_with_target)
    check(
        "target: central generation path has no adapter_id/backend dispatch",
        "adapter_id ==" not in generated_source and "backend_id" not in generated_source,
        generated_source,
    )


def main() -> int:
    import tempfile
    from pathlib import Path

    from llm_gateway import client

    # После мандата "explicit config independence" STEP 1 в _do_complete()
    # ВСЕГДА обращается к node_config.get_model_entry(), независимо от
    # LLM_GATEWAY_ENABLE_LOCAL — значит, любой тест ниже, который вызывает
    # client.complete()/complete_with_meta() и НЕ мокает get_model_entry
    # явно, теперь реально трогает secure_store. Изолируем весь прогон во
    # временном KEK/DB, чтобы тесты никогда не зависели от того, что
    # реально настроено на машине (тот же паттерн, что и в
    # secure_store_regression_test.py) — иначе тесты вроде "default
    # behavior still goes through Ollama" стали бы хрупкими: они бы молча
    # ломались, если владелец этой машины когда-нибудь настроит модель
    # с именем "m"/"heretic:q8"/"qwen3:14b" через настоящий llm_gateway.setup.
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict("os.environ", {
            "YANDI_KEK_PATH": str(Path(tmp) / "keys" / "kek.bin"),
            "YANDI_NODE_DB": str(Path(tmp) / "node.sqlite"),
        }):
            _run_ollama_wire_format_checks(client)

            _run_backend_dispatch_checks(client)

            _run_node_config_dispatch_checks(client)

            _run_explicit_backend_invariant_checks(client)

            _run_flag_independence_checks(client)

            _run_unified_resolver_checks(client)
            _run_adapter_layer_checks(client)

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
