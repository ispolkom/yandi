"""
llm_gateway.remote_backend — подключение ЛЮБОЙ внешней модели по сети:
свой Клод, свой OpenAI, любой self-hosted сервер (vLLM, llama.cpp
server, LM Studio и т.п.). Одна нода — один выбор, сделанный самим
владельцем ноды через config.py/setup.py, а не зашитый в код.

Два реальных протокола, не выдуманных: OpenAI-совместимый chat
completions (то, что уже понимает подавляющее большинство
self-hosted серверов и провайдеров) и родной Anthropic Messages API
(потому что формат Клода реально отличается от OpenAI и подсовывать
его через чужой формат означало бы делать вид, что поддержка есть,
когда её нет).
"""
from __future__ import annotations

import os

import requests

_session = requests.Session()
_session.trust_env = False

DEFAULT_TIMEOUT = 180


class RemoteBackendError(RuntimeError):
    """Внешний сервер недоступен, вернул ошибку или неожиданный формат."""


def _generate_openai(
    prompt: str,
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    system: str | None,
    temperature: float | None,
    max_tokens: int | None,
    response_format: str | None,
    timeout: int,
) -> tuple[str, dict]:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, object] = {"model": model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if response_format == "json":
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        resp = _session.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=payload, headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RemoteBackendError(f"{model} @ {base_url}: {e}") from e

    try:
        raw = resp.json()
        choice = raw["choices"][0]
        text = choice["message"]["content"] or ""
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise RemoteBackendError(f"{model} @ {base_url}: неожиданный формат ответа: {e}") from e

    finish_reason = choice.get("finish_reason")
    usage = raw.get("usage") or {}
    return text, {
        "done_reason": "length" if finish_reason == "length" else "stop",
        "eval_count": usage.get("completion_tokens"),
    }


def _generate_anthropic(
    prompt: str,
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    system: str | None,
    temperature: float | None,
    max_tokens: int | None,
    timeout: int,
) -> tuple[str, dict]:
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens or 4096,  # обязательное поле у Anthropic, дефолт разумный
    }
    if system:
        payload["system"] = system
    if temperature is not None:
        payload["temperature"] = temperature

    headers = {
        "x-api-key": api_key or "",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        resp = _session.post(
            f"{base_url.rstrip('/')}/v1/messages",
            json=payload, headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RemoteBackendError(f"{model} @ {base_url}: {e}") from e

    try:
        raw = resp.json()
        text = "".join(
            block.get("text", "") for block in raw.get("content", []) if block.get("type") == "text"
        )
    except (ValueError, TypeError, AttributeError) as e:
        raise RemoteBackendError(f"{model} @ {base_url}: неожиданный формат ответа: {e}") from e

    stop_reason = raw.get("stop_reason")
    usage = raw.get("usage") or {}
    return text, {
        "done_reason": "length" if stop_reason == "max_tokens" else "stop",
        "eval_count": usage.get("output_tokens"),
    }


def generate(
    prompt: str,
    *,
    base_url: str,
    protocol: str,
    model: str,
    api_key_env: str | None = None,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[str, dict]:
    """Единая точка входа для обоих протоколов. api_key_env — имя
    переменной окружения, где лежит ключ (ключ никогда не хранится
    в самом конфиге узла — только имя переменной, по той же дисциплине,
    что уже принята для KEK/DEK в agent/db/sql/keys.py)."""
    api_key = os.environ.get(api_key_env) if api_key_env else None

    if protocol == "openai":
        return _generate_openai(
            prompt, base_url=base_url, api_key=api_key, model=model,
            system=system, temperature=temperature, max_tokens=max_tokens,
            response_format=response_format, timeout=timeout,
        )
    if protocol == "anthropic":
        return _generate_anthropic(
            prompt, base_url=base_url, api_key=api_key, model=model,
            system=system, temperature=temperature, max_tokens=max_tokens,
            timeout=timeout,
        )
    raise RemoteBackendError(f"неизвестный протокол {protocol!r} (ожидался 'openai' или 'anthropic')")
