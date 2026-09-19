"""Ollama-compatible transport implementation for llm_gateway adapters."""
from __future__ import annotations

import requests

_session = requests.Session()
_session.trust_env = False


class OllamaBackendError(RuntimeError):
    """Ollama-compatible endpoint failed or returned an unexpected shape."""


def generate(
    messages: list[dict[str, str]],
    *,
    model: str,
    base_url: str,
    temperature: float | None,
    max_tokens: int | None,
    timeout: int,
    extra_options: dict[str, object] | None,
    response_format: object | None,
    stop: list[str] | None,
) -> tuple[str, dict]:
    options: dict[str, object] = dict(extra_options) if extra_options else {}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    if stop:
        options["stop"] = list(stop)

    payload: dict[str, object] = {"model": model, "messages": messages, "stream": False}
    if response_format is not None:
        payload["format"] = response_format
    if options:
        payload["options"] = options

    try:
        resp = _session.post(f"{base_url}/api/chat", json=payload, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise OllamaBackendError(f"{model}: {e}") from e

    try:
        raw = resp.json()
        text = raw["message"]["content"]
    except (KeyError, ValueError, TypeError) as e:
        raise OllamaBackendError(f"{model}: неожиданный формат ответа: {e}") from e

    return text, raw
