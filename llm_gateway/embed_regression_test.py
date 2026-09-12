"""
llm_gateway/embed_regression_test.py

TEST 1-17 из мандата "llm_gateway embeddings foundation" — покрывает
client.embed(): single/batch, malformed response, empty vector,
NaN/Inf, batch count mismatch, dimension mismatch, explicit-config
independence (тот же инвариант, что и у complete()), vector-space
compatibility guard.

Запуск: python3 -m llm_gateway.embed_regression_test
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
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
    import tempfile as _tempfile
    from pathlib import Path as _Path

    from llm_gateway import client
    from llm_gateway import config as node_config
    from llm_gateway import remote_backend, llamacpp_backend, vector_space as vs

    with _tempfile.TemporaryDirectory() as tmp:
        with patch.dict("os.environ", {
            "YANDI_KEK_PATH": str(_Path(tmp) / "keys" / "kek.bin"),
            "YANDI_NODE_DB": str(_Path(tmp) / "node.sqlite"),
        }):
            remote_entry = {
                "backend": "remote", "protocol": "openai",
                "base_url": "https://my-embedder.example", "model": "text-embed-1",
                "api_key_env": "K",
            }
            local_entry = {"backend": "llamacpp", "path": "/владелец/своя/embed-модель.gguf"}

            def ollama_trap():
                return patch.object(client, "_do_embed_ollama", return_value=(
                    [[9.0, 9.0]], vs.VectorSpaceId(backend="ollama-compat", protocol="ollama-embed", model="ПОДМЕНА", dimension=2, normalized=False)
                ))

            # ── TEST 1: single embedding через Ollama compat backend ──
            with patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(client._session, "post", return_value=_fake_response({"embeddings": [[0.1, 0.2, 0.3]]})):
                result = client.embed("привет", model="nomic-embed-text:latest")
                check("TEST1: single embedding via Ollama compat backend", result.vectors == [[0.1, 0.2, 0.3]])
                check("TEST1: space reflects ollama-compat backend", result.space.backend == "ollama-compat")

            # ── TEST 2: batch embedding ──
            with patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(client._session, "post", return_value=_fake_response({"embeddings": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]})):
                result = client.embed(["a", "b", "c"], model="m")
                check("TEST2: batch embedding returns one vector per input, in order", result.vectors == [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]])

            # ── TEST 3: malformed response ──
            with patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(client._session, "post", return_value=_fake_response({"totally": "unexpected"})):
                try:
                    client.embed("q", model="m")
                    check("TEST3: malformed response raises EmbedError", False, "no exception")
                except client.EmbedError:
                    check("TEST3: malformed response raises EmbedError", True)

            # ── TEST 4: empty vector ──
            with patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(client._session, "post", return_value=_fake_response({"embeddings": [[]]})):
                try:
                    client.embed("q", model="m")
                    check("TEST4: empty vector raises EmbedError", False, "no exception")
                except client.EmbedError:
                    check("TEST4: empty vector raises EmbedError", True)

            # ── TEST 5: NaN/Inf ──
            with patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(client._session, "post", return_value=_fake_response({"embeddings": [[0.1, float("nan"), 0.3]]})):
                try:
                    client.embed("q", model="m")
                    check("TEST5a: NaN in vector raises EmbedError", False, "no exception")
                except client.EmbedError:
                    check("TEST5a: NaN in vector raises EmbedError", True)
            with patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(client._session, "post", return_value=_fake_response({"embeddings": [[0.1, float("inf"), 0.3]]})):
                try:
                    client.embed("q", model="m")
                    check("TEST5b: Inf in vector raises EmbedError", False, "no exception")
                except client.EmbedError:
                    check("TEST5b: Inf in vector raises EmbedError", True)

            # ── TEST 6: batch вернул меньше vectors, чем inputs ──
            with patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(client._session, "post", return_value=_fake_response({"embeddings": [[0.1, 0.2]]})):
                try:
                    client.embed(["a", "b", "c"], model="m")
                    check("TEST6: batch count mismatch raises EmbedError", False, "no exception")
                except client.EmbedError:
                    check("TEST6: batch count mismatch raises EmbedError", True)

            # ── TEST 7: разные dimensions в одном batch'е ──
            with patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(client._session, "post", return_value=_fake_response({"embeddings": [[0.1, 0.2], [0.1, 0.2, 0.3]]})):
                try:
                    client.embed(["a", "b"], model="m")
                    check("TEST7: mismatched dimensions within one batch raises EmbedError", False, "no exception")
                except client.EmbedError:
                    check("TEST7: mismatched dimensions within one batch raises EmbedError", True)

            # ── TEST 8: explicit backend упал -> честная ошибка, никакого silent fallback ──
            with patch.object(node_config, "get_model_entry", return_value=remote_entry), \
                 patch.object(remote_backend, "embed", side_effect=remote_backend.RemoteBackendError("connection refused")), \
                 ollama_trap() as mock_ollama:
                try:
                    client.embed("q", model="my-embedder")
                    check("TEST8: explicit embedding backend failure raises, no fallback", False, "no exception")
                except client.EmbedError:
                    check("TEST8: explicit embedding backend failure raises, no fallback", True)
                check("TEST8: Ollama not called", mock_ollama.call_count == 0)

            # ── TEST 9: no explicit config -> Ollama fallback работает ──
            with patch.object(node_config, "get_model_entry", return_value=None), \
                 patch.object(client._session, "post", return_value=_fake_response({"embeddings": [[0.1, 0.2]]})):
                result = client.embed("q", model="какая-то-модель")
                check("TEST9: no explicit config -> Ollama fallback works", result.vectors == [[0.1, 0.2]])

            # ── TEST 10: два vector'а одной model/provider/space -> сравнение разрешено ──
            space_a = vs.VectorSpaceId(backend="ollama-compat", protocol="ollama-embed", model="nomic-embed-text:latest", dimension=3, normalized=False)
            space_a2 = vs.VectorSpaceId(backend="ollama-compat", protocol="ollama-embed", model="nomic-embed-text:latest", dimension=3, normalized=False)
            check("TEST10: same model/provider/space -> comparison allowed", vs.compatible(space_a, space_a2) is True)

            # ── TEST 11: одинаковая dimension, но разный model identity -> сравнение запрещено ──
            space_b = vs.VectorSpaceId(backend="ollama-compat", protocol="ollama-embed", model="embeddinggemma:latest", dimension=3, normalized=False)
            check("TEST11: same dimension, different model identity -> comparison FORBIDDEN", vs.compatible(space_a, space_b) is False)

            # ── TEST 12: одинаковое имя модели, но доказанно разная vector-space identity ──
            space_c_diff_backend = vs.VectorSpaceId(backend="remote", protocol="remote-openai-embeddings", model="nomic-embed-text:latest", dimension=3, normalized=False)
            check("TEST12: same model name, different backend/protocol -> comparison FORBIDDEN", vs.compatible(space_a, space_c_diff_backend) is False)

            # ── TEST 13: legacy stored vector без identity -> не сравнивается молча ──
            check("TEST13: legacy (UNKNOWN) stored vector never silently compared", vs.compatible(vs.UNKNOWN_VECTOR_SPACE, space_a) is False)

            # ── TEST 16/17: смена completion provider не меняет embedding provider и наоборот ──
            # (доказывается архитектурно: полностью независимые lookup'ы по РАЗНЫМ именам в
            # той же secure_store — проверяем, что embed() для одного имени не трогает
            # get_model_entry для completion-имени и наоборот.)
            completion_entry = {"backend": "remote", "protocol": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5", "api_key_env": "CK"}
            call_log = []

            def logging_get_model_entry(name):
                call_log.append(name)
                return {"my-completion-model": completion_entry, "my-embedder": remote_entry}.get(name)

            with patch.object(node_config, "get_model_entry", side_effect=logging_get_model_entry), \
                 patch.object(remote_backend, "generate", return_value=("completion ok", {})), \
                 patch.object(remote_backend, "embed", return_value=([[0.1, 0.2]], {"dimension": 2, "normalized": False})), \
                 patch.object(client, "_do_complete_ollama") as mock_ollama_complete, \
                 ollama_trap() as mock_ollama_embed:
                out_completion = client.complete("q", model="my-completion-model")
                out_embed = client.embed("q", model="my-embedder")
                check(
                    "TEST16/17: completion and embedding providers looked up independently by their own names",
                    call_log == ["my-completion-model", "my-embedder"], repr(call_log),
                )
                check("TEST16: completion succeeded via its own configured backend", out_completion == "completion ok")
                check("TEST17: embedding succeeded via its own configured backend", out_embed.vectors == [[0.1, 0.2]])
                check("TEST16/17: neither path touched Ollama", mock_ollama_complete.call_count == 0 and mock_ollama_embed.call_count == 0)

            # ── Дополнительно: configured local (llamacpp) embedding backend работает ──
            with patch.object(node_config, "get_model_entry", return_value=local_entry), \
                 patch.object(llamacpp_backend, "embed_at_spec", return_value=([[1.0, 2.0]], {"dimension": 2, "normalized": False})), \
                 ollama_trap() as mock_ollama:
                result = client.embed("q", model="my-local-embedder")
                check("configured LOCAL embedding backend is used", result.vectors == [[1.0, 2.0]])
                check("configured LOCAL: Ollama not called", mock_ollama.call_count == 0)
                check("configured LOCAL: space.backend reflects llamacpp", result.space.backend == "llamacpp")

            # ── Дополнительно: Anthropic protocol explicitly rejected for embeddings ──
            anthropic_entry = {"backend": "remote", "protocol": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5", "api_key_env": "CK"}
            with patch.object(node_config, "get_model_entry", return_value=anthropic_entry), \
                 ollama_trap() as mock_ollama:
                try:
                    client.embed("q", model="my-claude-as-embedder")
                    check("Anthropic protocol explicitly rejected for embeddings (no fake support)", False, "no exception")
                except client.EmbedError:
                    check("Anthropic protocol explicitly rejected for embeddings (no fake support)", True)
                check("Anthropic-embed-rejection: Ollama not called", mock_ollama.call_count == 0)

            # ── explicit config independence: flag doesn't exist for embeddings at all,
            #    confirm explicit config is checked regardless of _LOCAL_ENABLED state ──
            for flag in (True, False):
                with patch.object(client, "_LOCAL_ENABLED", flag), \
                     patch.object(node_config, "get_model_entry", return_value=remote_entry), \
                     patch.object(remote_backend, "embed", return_value=([[7.0]], {"dimension": 1, "normalized": False})), \
                     ollama_trap() as mock_ollama:
                    result = client.embed("q", model="my-embedder")
                    check(f"explicit embed config honored regardless of _LOCAL_ENABLED={flag}", result.vectors == [[7.0]])
                    check(f"Ollama not called (flag={flag})", mock_ollama.call_count == 0)

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
