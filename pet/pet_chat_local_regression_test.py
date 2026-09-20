"""
pet/pet_chat_local_regression_test.py

TEST 1-16 из мандата "pet chat_local gateway migration". Гоняет
РЕАЛЬНУЮ логику llm_gateway.client (мок только на границе:
node_config.get_model_entry, remote_backend.generate,
llamacpp_backend.generate, финальный Ollama-совместимый HTTP) — та же
дисциплина, что и pet_llm_migration_regression_test.py.

SQL-эффекты relationship memory (shadow_add_grievance и т.п.) замокан
на границе модуля chat_local — это НЕ предмет этого мандата, полное
поведение state machine отдельно и полностью покрыто
agent/relationship_memory_regression_test.py (проверен в этом же
прогоне, зелёный).

Запуск: python3 -m pet.pet_chat_local_regression_test
"""
from __future__ import annotations

import json
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
    from llm_gateway import remote_backend, llamacpp_backend

    import pet.chat_local as chat_local

    remote_entry = lambda model: {
        "backend": "remote", "protocol": "openai",
        "base_url": "https://my-server.example", "model": model,
        "api_key_env": "K",
    }

    def fake_shadow_ctx(*, user_id, **kw):
        return None  # нет открытых обид

    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict("os.environ", {
            "YANDI_KEK_PATH": str(Path(tmp) / "keys" / "kek.bin"),
            "YANDI_NODE_DB": str(Path(tmp) / "node.sqlite"),
        }):
            base_patches = dict(
                shadow_get_relationship_context=fake_shadow_ctx,
            )

            def make_ok_response(text: str):
                import unittest.mock as um
                r = um.MagicMock()
                r.status_code = 200
                r.raise_for_status.side_effect = None
                r.json.return_value = {"message": {"content": text}}
                return r

            def semantic_json(reply: str, state: dict | None = None) -> str:
                if state is None:
                    state = {"is_insult": False, "severity": 0.0, "is_apology": False, "sincerity": 0.0}
                return json.dumps({"reply": reply, "state": state}, ensure_ascii=False)

            messages_in = [{"role": "user", "content": "Привет, как дела?"}]

            # ── TEST 1/2/3: normal request -> ONE gateway call, visible answer preserved,
            #    STATE_MARKER extracted from the SAME generation ──
            grievance_calls = []
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(chat_local, "shadow_add_grievance", lambda **kw: grievance_calls.append(("insult", kw))), \
                 patch.object(chat_local, "shadow_apply_apology", lambda **kw: grievance_calls.append(("apology", kw))), \
                 patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(gw_client._session, "post") as mock_post:
                mock_post.return_value = make_ok_response(semantic_json("Привет! Хорошо, спасибо."))
                result = chat_local._respond_with_character("heretic:q8", messages_in, 0.7)
                check("TEST1: exactly one HTTP call reaches the gateway", mock_post.call_count == 1)
                check("TEST2: visible answer preserved, tag stripped from what's shown", result == "Привет! Хорошо, спасибо.", repr(result))
                check("TEST3: no relationship event fired for a neutral message (marker correctly parsed from the SAME response)", grievance_calls == [])

                sent = mock_post.call_args.kwargs["json"]
                check("model name from the call is preserved in the wire request", sent["model"] == "heretic:q8")
                # 2026-09-16: llm_gateway/client.py's _build_messages now merges
                # multiple system strings into ONE system message — found live
                # that heretic:q8's chat template hard-rejects more than one
                # (400 "System message must be at the beginning"). A single
                # merged message is compatible with every template; keeping all
                # three as separate role:system entries was the actual bug.
                system_messages = [m for m in sent["messages"] if m["role"] == "system"]
                check(
                    "all three system instructions present, merged into ONE system message",
                    len(system_messages) == 1
                    and all(part in system_messages[0]["content"] for part in (
                        chat_local._BASE_CHARACTER_PROMPT,
                        "Верни строго один JSON-объект",
                    )),
                    repr(sent["messages"]),
                )
                check("full conversation history (messages_in) preserved after the system messages", sent["messages"][-1] == messages_in[-1])
                # 2026-09-16, mandate "structured self-report": the wire
                # request now asks Ollama for schema-constrained decoding
                # (response_format=_STATE_SCHEMA -> Ollama's top-level
                # "format") instead of relying purely on a free-text
                # worked example the model could anchor on.
                check(
                    "semantic strict JSON schema is selected by gateway for Ollama-compatible target",
                    isinstance(sent.get("format"), dict)
                    and sent["format"]["properties"]["state"] == chat_local._STATE_SCHEMA,
                    repr(sent.get("format")),
                )

            # ── TEST 4: grievance recorded only AFTER parsing, reflects what the user actually saw ──
            grievance_calls.clear()
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(chat_local, "shadow_add_grievance", lambda **kw: grievance_calls.append(("insult", kw))), \
                 patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(gw_client._session, "post") as mock_post:
                mock_post.return_value = make_ok_response(semantic_json(
                    "Ну ты и дура.",
                    {"is_insult": True, "severity": 0.6, "is_apology": False, "sincerity": 0.0, "evidence": "ты дура"},
                ))
                result = chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": "ты дура"}], 0.7)
                check("TEST4: grievance IS recorded for a real insult above threshold", len(grievance_calls) == 1 and grievance_calls[0][0] == "insult")
                check(
                    "TEST4: the recorded description is what the USER said (the grievance is about their words), not the model's reply or the raw tag",
                    grievance_calls[0][1].get("description") == "ты дура",
                    repr(grievance_calls[0][1]),
                )

            # ── TEST 4b: same grievance-detection pipeline, but the backend
            #    returns the STRUCTURED contract (no marker, no free-text
            #    tag at all) — end-to-end proof the migration didn't only
            #    work in message_intensity's own unit tests. ──
            grievance_calls.clear()
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(chat_local, "shadow_add_grievance", lambda **kw: grievance_calls.append(("insult", kw))), \
                 patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(gw_client._session, "post") as mock_post:
                mock_post.return_value = make_ok_response(semantic_json(
                    "Ну ты и дура.",
                    {"is_insult": True, "severity": 0.6, "is_apology": False, "sincerity": 0.0, "evidence": "ты дура"},
                ))
                result = chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": "ты дура"}], 0.7)
                check("TEST4b: structured contract -> visible reply extracted correctly", result == "Ну ты и дура.", repr(result))
                check("TEST4b: structured contract -> grievance IS recorded for a real insult above threshold", len(grievance_calls) == 1 and grievance_calls[0][0] == "insult")
                check(
                    "TEST4b: structured contract -> recorded description is what the USER said",
                    grievance_calls[0][1].get("description") == "ты дура",
                    repr(grievance_calls[0][1]),
                )

            # ── TEST 5: gateway error -> NO relationship update, no crash of the whole request ──
            grievance_calls.clear()
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(chat_local, "shadow_add_grievance", lambda **kw: grievance_calls.append(("insult", kw))), \
                 patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(gw_client._session, "post", side_effect=__import__("requests").ConnectionError("down")):
                try:
                    chat_local._respond_with_character("heretic:q8", messages_in, 0.7)
                    check("TEST5: gateway failure raises (caught at the endpoint level, not silently absorbed here)", False, "no exception")
                except llm_gateway.LLMError:
                    check("TEST5: gateway failure raises (caught at the endpoint level, not silently absorbed here)", True)
                check("TEST5: no relationship update happened when the generation itself failed", grievance_calls == [])

            # local_chat() endpoint itself must turn that into a clean {"ok": False, ...}, not crash the server.
            import asyncio
            real_loop = asyncio.get_event_loop()
            class InlineLoop:
                def run_in_executor(self, executor, fn):
                    async def _run():
                        return fn()
                    return _run()

            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(gw_client._session, "post", side_effect=__import__("requests").ConnectionError("down")), \
                 patch.object(chat_local.asyncio, "get_event_loop", return_value=InlineLoop()):
                resp = real_loop.run_until_complete(
                    chat_local.local_chat({"model": "heretic:q8", "messages": messages_in})
                )
                check("TEST5b: local_chat() endpoint degrades to {ok: False}, never a raw 500/crash", resp.get("ok") is False and "content" in resp)

            # ── TEST 6/7: explicit configured backend used; its failure does NOT fall back ──
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(node_config, "get_model_entry", return_value=remote_entry("heretic:q8")), \
                 patch.object(remote_backend, "generate", return_value=(semantic_json("Ответ от настроенного backend'а"), {})) as mock_remote, \
                 patch.object(gw_client._session, "post") as mock_ollama:
                result = chat_local._respond_with_character("heretic:q8", messages_in, 0.7)
                check("TEST6: explicit configured backend is actually used", mock_remote.call_count == 1)
                check("TEST6: Ollama not touched when explicit config exists", mock_ollama.call_count == 0)
                check("TEST6: visible text comes from the configured backend's response", result == "Ответ от настроенного backend'а")

            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(node_config, "get_model_entry", return_value=remote_entry("heretic:q8")), \
                 patch.object(remote_backend, "generate", side_effect=remote_backend.RemoteBackendError("connection refused")), \
                 patch.object(gw_client._session, "post") as mock_ollama:
                try:
                    chat_local._respond_with_character("heretic:q8", messages_in, 0.7)
                    check("TEST7: explicit backend failure raises, no silent fallback", False, "no exception")
                except llm_gateway.LLMError:
                    check("TEST7: explicit backend failure raises, no silent fallback", True)
                check("TEST7: Ollama was never contacted after the explicit backend failed", mock_ollama.call_count == 0)

            # ── TEST 8: two dynamic model names resolve INDEPENDENTLY ──
            def entry_only_for_b(name):
                return remote_entry("model-b") if name == "model-b" else None

            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(node_config, "get_model_entry", side_effect=entry_only_for_b), \
                 patch.object(remote_backend, "generate", return_value=(semantic_json("ответ B"), {})) as mock_remote, \
                 patch.object(gw_client._session, "post") as mock_post:
                mock_post.return_value = make_ok_response(semantic_json("ответ A"))
                result_a = chat_local._respond_with_character("model-a", messages_in, 0.7)
                result_b = chat_local._respond_with_character("model-b", messages_in, 0.7)
                check("TEST8: model A (no config) resolves via Ollama-compat default", result_a == "ответ A")
                check("TEST8: model B (explicit config) resolves via its own configured backend", result_b == "ответ B")
                check("TEST8: model A's calls never touched the remote backend meant for B", mock_remote.call_count == 1)

            # ── TEST 9: stop sequences actually reach the wire request ──
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(gw_client._session, "post") as mock_post:
                mock_post.return_value = make_ok_response(semantic_json("ok"))
                chat_local._respond_with_character("heretic:q8", messages_in, 0.7)
                sent_options = mock_post.call_args.kwargs["json"].get("options", {})
                check(
                    "TEST9: stop sequences reach the Ollama-compat wire request (options.stop)",
                    sent_options.get("stop") == chat_local._STOP_TOKENS,
                    repr(sent_options.get("stop")),
                )

                # ── TEST 10: repeat_penalty/repeat_last_n not lost for the compat backend ──
                check(
                    "TEST10: repeat_penalty/repeat_last_n preserved for the Ollama-compat backend",
                    sent_options.get("repeat_penalty") == 1.3 and sent_options.get("repeat_last_n") == 64,
                    repr(sent_options),
                )

            # ── TEST 11: a backend WITHOUT the capability doesn't get a fake substitute ──
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(node_config, "get_model_entry", return_value=remote_entry("heretic:q8")), \
                 patch.object(remote_backend, "generate", return_value=(semantic_json("ok"), {})) as mock_remote:
                chat_local._respond_with_character("heretic:q8", messages_in, 0.7)
                remote_call_kwargs = mock_remote.call_args.kwargs
                check(
                    "TEST11: remote_backend.generate() was never even offered repeat_penalty/repeat_last_n "
                    "(no fake OpenAI/Anthropic equivalent invented) — its signature has no extra_options param at all",
                    "extra_options" not in remote_call_kwargs,
                    repr(remote_call_kwargs.keys()),
                )
                check(
                    "TEST11: stop WAS honestly offered to the remote backend (a real, universal concept)",
                    "stop" in remote_call_kwargs and remote_call_kwargs["stop"] == chat_local._STOP_TOKENS,
                )

            # ── TEST 12/16: /api/local/models works without Ollama, doesn't crash ──
            with patch.object(node_config, "list_models", return_value={}), \
                 patch.object(llamacpp_backend, "list_builtin_models", return_value=["heretic:q8"]), \
                 patch.object(llamacpp_backend, "has_model", return_value=True), \
                 patch.object(gw_client._session, "get", side_effect=__import__("requests").ConnectionError("Ollama is down")), \
                 patch.object(chat_local.asyncio, "get_event_loop", return_value=InlineLoop()):
                resp = real_loop.run_until_complete(chat_local.local_models())
                check("TEST12/16: model listing works even with Ollama fully down, doesn't crash", "heretic:q8" in resp.get("models", []), repr(resp))

            # ── TEST13/14: explicit configured remote model appears; no secrets/paths leak ──
            with patch.object(node_config, "list_models", return_value={
                 "my-claude": {"backend": "remote", "protocol": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5", "api_key_env": "SUPER_SECRET_KEY_NAME"},
                 "my-local": {"backend": "llamacpp", "path": "/home/owner/private/models/secret-model.gguf"},
             }), \
                 patch.object(llamacpp_backend, "list_builtin_models", return_value=[]), \
                 patch.object(gw_client._session, "get", side_effect=__import__("requests").ConnectionError("down")), \
                 patch.object(chat_local.asyncio, "get_event_loop", return_value=InlineLoop()):
                resp = real_loop.run_until_complete(chat_local.local_models())
                names = resp.get("models", [])
                check("TEST13: explicit configured remote model appears in the listing", "my-claude" in names, repr(names))
                check("TEST13: explicit configured local model also appears", "my-local" in names, repr(names))
                full_str = str(resp)
                check("TEST14: API key ENV VAR NAME never leaks through model listing", "SUPER_SECRET_KEY_NAME" not in full_str)
                check("TEST14: local file PATH never leaks through model listing", "/home/owner/private" not in full_str)
                check("TEST14: base_url never leaks through model listing", "api.anthropic.com" not in full_str)

            # ── TEST 15: Ollama entirely absent, chat still works via an explicit remote backend ──
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(node_config, "get_model_entry", return_value=remote_entry("my-claude")), \
                 patch.object(remote_backend, "generate", return_value=(semantic_json("работает без Ollama"), {})), \
                 patch.object(gw_client._session, "post", side_effect=__import__("requests").ConnectionError("Ollama does not exist on this machine")), \
                 patch.object(gw_client._session, "get", side_effect=__import__("requests").ConnectionError("Ollama does not exist on this machine")):
                result = chat_local._respond_with_character("my-claude", messages_in, 0.7)
                check("TEST15: chat_local works completely without Ollama when an explicit backend is configured", result == "работает без Ollama", repr(result))

            # ── §7 edge cases: self-report robustness survives the gateway migration unchanged ──
            grievance_calls.clear()
            apology_calls = []
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(chat_local, "shadow_add_grievance", lambda **kw: grievance_calls.append(("insult", kw))), \
                 patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(gw_client._session, "post") as mock_post:
                # Strict semantic JSON malformed/missing -> safe fallback, no raw internal/service content shown.
                mock_post.return_value = make_ok_response("Просто обычный ответ без какого-либо маркера.")
                result = chat_local._respond_with_character("heretic:q8", messages_in, 0.7)
                check("§7: malformed strict semantic transport -> safe reply, no relationship event, no crash", result == chat_local._SEMANTIC_FAILURE_REPLY and grievance_calls == [])

            grievance_calls.clear()
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(chat_local, "shadow_add_grievance", lambda **kw: grievance_calls.append(("insult", kw))), \
                 patch.object(node_config, "get_model_entry", return_value={
                     "backend": "remote", "protocol": "anthropic",
                     "base_url": "https://api.anthropic.com", "model": "claude", "api_key_env": "K",
                 }), \
                 patch.object(remote_backend, "generate", return_value=("Ответ модели.\n\n###YANDI_STATE### {not valid json at all", {})):
                # Legacy marker fallback with malformed JSON -> visible reply survives, no event.
                result = chat_local._respond_with_character("heretic:q8", messages_in, 0.7)
                check("§7: MALFORMED marker JSON on legacy transport -> visible text still shown, no crash", result == "Ответ модели.")
                check("§7: malformed marker -> no relationship event fabricated from garbage", grievance_calls == [])

            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(chat_local, "shadow_add_grievance", lambda **kw: grievance_calls.append(("insult", kw))), \
                 patch.object(node_config, "get_model_entry", return_value={
                     "backend": "remote", "protocol": "anthropic",
                     "base_url": "https://api.anthropic.com", "model": "claude", "api_key_env": "K",
                 }), \
                 patch.object(remote_backend, "generate") as mock_remote:
                # Two marker-shaped strings in the text -> rfind() takes the LAST one (documented parser behavior).
                mock_remote.return_value = (
                    "Текст с упоминанием ###YANDI_STATE### внутри предложения, а вот настоящий ответ.\n\n"
                    '###YANDI_STATE### {"is_insult": false, "severity": 0.0, "is_apology": false, "sincerity": 0.0}',
                    {},
                )
                result = chat_local._respond_with_character("heretic:q8", messages_in, 0.7)
                check(
                    "§7: multiple marker-shaped occurrences -> parser takes the LAST one (rfind), doesn't crash",
                    result == "Текст с упоминанием ###YANDI_STATE### внутри предложения, а вот настоящий ответ.",
                    repr(result),
                )

            apology_calls = []
            with patch.object(chat_local, "shadow_get_relationship_context", return_value={
                     "grievance_id": "g1", "description": "прошлая обида", "severity": 0.6, "status": "registered",
                 }), \
                 patch.object(chat_local, "shadow_apply_apology", lambda **kw: apology_calls.append(kw)), \
                 patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(gw_client._session, "post") as mock_post:
                mock_post.return_value = make_ok_response(semantic_json(
                    "Ничего страшного, я тебя прощаю.",
                    {"is_insult": False, "severity": 0.0, "is_apology": True, "sincerity": 0.9, "evidence": "извини"},
                ))
                result = chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": "извини, был не прав"}], 0.7)
                check("§7: sincere apology -> one apply_apology (match + acknowledge + healing in the memory layer), from the SAME generation", len(apology_calls) == 1)
                check("§7: apology path still shows the model's real visible reply", result == "Ничего страшного, я тебя прощаю.")

            grievance_calls.clear()
            with patch.object(chat_local, "shadow_get_relationship_context", fake_shadow_ctx), \
                 patch.object(chat_local, "shadow_add_grievance", lambda **kw: grievance_calls.append(("insult", kw))), \
                 patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(gw_client._session, "post") as mock_post:
                mock_post.return_value = make_ok_response(
                    '{"is_insult": false, "severity": 0.35, "is_apology": true, "sincerity": null}'
                )
                result = chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": "Извини, я зря это сказал."}], 0.7)
                check("§7: state-only JSON never leaks into visible PET reply", "is_insult" not in result and "severity" not in result, repr(result))
                check("§7: state-only JSON does not mutate relationship memory", grievance_calls == [], repr(grievance_calls))

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
