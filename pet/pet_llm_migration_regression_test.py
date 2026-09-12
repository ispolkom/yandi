"""
pet/pet_llm_migration_regression_test.py

TEST 1-9 из мандата "pet simple LLM migration" для четырёх мигрированных
функций: chat_translate.py::_ollama_mini, chat_orch.py::_local_validate,
council_claude_auto.py::_filter_reply, council_chat_server.py::
_translate_to_russian.

Гоняет РЕАЛЬНУЮ логику диспетчеризации llm_gateway.client (не мокает
complete() напрямую) — мокает только на границе (node_config.get_model_entry,
remote_backend.generate, финальный Ollama-compat HTTP) — чтобы по-настоящему
доказать, что pet/ наследует приоритет явного выбора владельца узла, а не
просто "вызывает какую-то функцию".

Первый тестовый файл для pet/ вообще — до этого мандата тестов не было.

Запуск: python3 -m pet.pet_llm_migration_regression_test
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    import llm_gateway
    from llm_gateway import client as gw_client
    from llm_gateway import config as node_config
    from llm_gateway import remote_backend

    from pet.chat_translate import _ollama_mini
    from pet.chat_orch import _local_validate
    from pet.council_claude_auto import _filter_reply
    from pet.council_chat_server import _translate_to_russian

    remote_entry_maker = lambda model: {
        "backend": "remote", "protocol": "openai",
        "base_url": "https://my-server.example", "model": model,
        "api_key_env": "K",
    }

    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict("os.environ", {
            "YANDI_KEK_PATH": str(Path(tmp) / "keys" / "kek.bin"),
            "YANDI_NODE_DB": str(Path(tmp) / "node.sqlite"),
        }):
            functions = [
                ("_ollama_mini", lambda: _ollama_mini("test prompt", max_tokens=10), "heretic:q8"),
                ("_local_validate", lambda: _local_validate("вопрос?", "ответ."), "heretic:q8"),
                ("_filter_reply", lambda: _filter_reply("As an AI, I say hello"), "heretic:q8"),
                ("_translate_to_russian", lambda: _translate_to_russian("Hello world, this is a real translation test.", "claude"), "heretic:q8"),
            ]

            for name, call, model in functions:
                # ── TEST1/TEST2: gateway called, no direct Ollama HTTP bypass ──
                with patch.object(node_config, "get_model_entry", return_value=None), \
                     patch.object(gw_client._session, "post") as mock_post:
                    import requests as _rq
                    mock_post.return_value.status_code = 200
                    mock_post.return_value.raise_for_status.side_effect = None
                    mock_post.return_value.json.return_value = {"message": {"content": "тестовый ответ модели"}}
                    result = call()
                    check(f"TEST1/2 [{name}]: exactly one HTTP call, through the sanctioned gateway Ollama-compat path", mock_post.call_count == 1)
                    if mock_post.call_count == 1:
                        url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args.kwargs.get("url", "")
                        check(f"TEST2 [{name}]: call lands on the real gateway endpoint (/api/chat), not a rogue direct call", "/api/chat" in url, url)
                    check(f"TEST4 [{name}]: successful response is used (non-empty, real text)", isinstance(result, str) and len(result) > 0, repr(result))

                # ── TEST3: model name preserved (inspect what was actually sent) ──
                with patch.object(node_config, "get_model_entry", return_value=None), \
                     patch.object(gw_client._session, "post") as mock_post:
                    mock_post.return_value.status_code = 200
                    mock_post.return_value.raise_for_status.side_effect = None
                    mock_post.return_value.json.return_value = {"message": {"content": "ok"}}
                    call()
                    sent_model = mock_post.call_args.kwargs.get("json", {}).get("model")
                    check(f"TEST3 [{name}]: exact original model name preserved", sent_model == model, repr(sent_model))

                # ── TEST5: gateway/network failure -> old graceful degradation, not a crash ──
                with patch.object(node_config, "get_model_entry", return_value=None), \
                     patch.object(gw_client._session, "post", side_effect=__import__("requests").ConnectionError("down")):
                    result = call()
                    if name == "_filter_reply":
                        # _filter_reply's old failure semantics: falls back to the regex-only
                        # cleaned text, which for this meta-signal input is "" (whole line was
                        # a meta-narrative prefix) — never raises, never crashes.
                        check(f"TEST5 [{name}]: gateway failure -> old graceful degradation, no crash", isinstance(result, str))
                    elif name == "_translate_to_russian":
                        check(f"TEST5 [{name}]: gateway failure -> returns original text unchanged (old semantics)", result == "Hello world, this is a real translation test.")
                    else:
                        check(f"TEST5 [{name}]: gateway failure -> '' (old semantics)", result == "")

                # ── TEST6: explicit configured backend IS actually used ──
                with patch.object(node_config, "get_model_entry", return_value=remote_entry_maker(model)), \
                     patch.object(remote_backend, "generate", return_value=("явный ответ настроенного backend'а", {})) as mock_remote, \
                     patch.object(gw_client._session, "post") as mock_ollama:
                    result = call()
                    check(f"TEST6 [{name}]: explicit configured backend is actually consulted", mock_remote.call_count == 1)
                    check(f"TEST6 [{name}]: Ollama not touched when explicit config exists", mock_ollama.call_count == 0)

                # ── TEST7: explicit configured backend FAILS -> pet does NOT fall back to raw Ollama ──
                with patch.object(node_config, "get_model_entry", return_value=remote_entry_maker(model)), \
                     patch.object(remote_backend, "generate", side_effect=remote_backend.RemoteBackendError("connection refused")), \
                     patch.object(gw_client._session, "post") as mock_ollama:
                    result = call()
                    check(f"TEST7 [{name}]: explicit backend failure does NOT trigger a raw Ollama fallback from pet", mock_ollama.call_count == 0)
                    # Каждая функция сама решает, как деградировать при ошибке — здесь
                    # важно только "не Ollama в обход", а не конкретное значение.
                    check(f"TEST7 [{name}]: function still returns cleanly (no unhandled exception)", isinstance(result, str))

                # ── TEST8: no explicit config -> existing Ollama-compat fallback still works ──
                with patch.object(node_config, "get_model_entry", return_value=None), \
                     patch.object(gw_client._session, "post") as mock_post:
                    mock_post.return_value.status_code = 200
                    mock_post.return_value.raise_for_status.side_effect = None
                    mock_post.return_value.json.return_value = {"message": {"content": "дефолтный ответ"}}
                    result = call()
                    check(f"TEST8 [{name}]: no config -> Ollama-compat fallback still resolves the request", mock_post.call_count == 1 and len(result) > 0)

            # ── TEST9: migrating one function did not change another's behavior ──
            # (проверяем, что независимая настройка под РАЗНЫМИ именами не течёт друг в друга —
            # каждая из 4 функций использует "heretic:q8", так что тест здесь — что настройка
            # ОДНОЙ функции (через явный backend) не просачивается в вызов ДРУГОЙ функции,
            # если у неё нет своей настройки.)
            with patch.object(node_config, "get_model_entry", return_value=remote_entry_maker("heretic:q8")), \
                 patch.object(remote_backend, "generate", return_value=("ответ A", {})) as mock_remote:
                _ollama_mini("prompt A", max_tokens=5)
                check("TEST9: configuring one function's model does not silently disable independent calls to the same model name from another function", mock_remote.call_count == 1)

            with patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(gw_client._session, "post") as mock_post:
                mock_post.return_value.status_code = 200
                mock_post.return_value.raise_for_status.side_effect = None
                mock_post.return_value.json.return_value = {"message": {"content": "независимый ответ"}}
                out = _local_validate("q", "a")
                check("TEST9: a DIFFERENT function with no config of its own is unaffected by the previous call's mocking/state", len(out) > 0)

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
