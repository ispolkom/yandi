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
    messages: list[dict[str, str]],
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    temperature: float | None,
    max_tokens: int | None,
    response_format: str | None,
    stop: list[str] | None,
    timeout: int,
) -> tuple[str, dict]:
    # OpenAI-совместимый формат принимает role="system" прямо внутри
    # messages (в отличие от Anthropic ниже) — messages уже собран
    # client.py._build_messages(), никакой доп. обработки не нужно.
    payload: dict[str, object] = {"model": model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if response_format == "json":
        payload["response_format"] = {"type": "json_object"}
    if stop:
        payload["stop"] = list(stop)

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
    messages: list[dict[str, str]],
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    temperature: float | None,
    max_tokens: int | None,
    stop: list[str] | None,
    timeout: int,
) -> tuple[str, dict]:
    # Anthropic, В ОТЛИЧИЕ от OpenAI, НЕ принимает role="system" внутри
    # messages — только отдельным top-level полем. Извлекаем все
    # system-сообщения (их может быть несколько — chat_local.py шлёт
    # три независимых) и склеиваем в один system-текст; остальное —
    # обычные user/assistant реплики как есть. Честный перевод формата,
    # а не притворство, что Anthropic понимает то же, что и OpenAI.
    system_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    convo = [m for m in messages if m.get("role") != "system"]

    payload: dict[str, object] = {
        "model": model,
        "messages": convo,
        "max_tokens": max_tokens or 4096,  # обязательное поле у Anthropic, дефолт разумный
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if temperature is not None:
        payload["temperature"] = temperature
    if stop:
        # Anthropic называет это stop_sequences, не stop — честный
        # перевод имени поля, не выдумывание поддержки.
        payload["stop_sequences"] = list(stop)

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


def _embed_openai(
    texts: list[str],
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    timeout: int,
) -> tuple[list[list[float]], dict]:
    """OpenAI-совместимый /embeddings — принимает список строк в одном
    запросе (batch), возвращает по одной записи на вход с полем
    `index`, порядок в ответе НЕ гарантирован спецификацией — сортируем
    по `index` явно, а не полагаемся на порядок массива."""
    payload: dict[str, object] = {"model": model, "input": texts}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        resp = _session.post(
            f"{base_url.rstrip('/')}/embeddings",
            json=payload, headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RemoteBackendError(f"{model} @ {base_url}: {e}") from e

    try:
        raw = resp.json()
        items = sorted(raw["data"], key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in items]
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise RemoteBackendError(f"{model} @ {base_url}: неожиданный формат embedding-ответа: {e}") from e

    dimension = len(vectors[0]) if vectors else 0
    return vectors, {"dimension": dimension, "normalized": False}


def embed(
    texts: list[str],
    *,
    base_url: str,
    protocol: str,
    model: str,
    api_key_env: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[list[float]], dict]:
    """Единая точка входа для embeddings по сети. Пока только
    OpenAI-совместимый протокол — у Anthropic нативного embeddings API
    не существует, и притворяться, что он есть через чужой формат,
    означало бы делать вид, что поддержка есть, когда её нет (тот же
    принцип, что уже применён к completion в этом файле)."""
    api_key = os.environ.get(api_key_env) if api_key_env else None

    if protocol == "openai":
        return _embed_openai(texts, base_url=base_url, api_key=api_key, model=model, timeout=timeout)
    if protocol == "anthropic":
        raise RemoteBackendError(
            "Anthropic API не предоставляет embeddings — настрой отдельный "
            "embedding-провайдер (OpenAI-совместимый self-hosted сервер или "
            "другой явный remote-backend), генерация и эмбеддинги — разные "
            "способности узла, не обязаны совпадать"
        )
    raise RemoteBackendError(f"неизвестный протокол {protocol!r} для embeddings (ожидался 'openai')")


def generate(
    messages: list[dict[str, str]],
    *,
    base_url: str,
    protocol: str,
    model: str,
    api_key_env: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: str | None = None,
    stop: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[str, dict]:
    """Единая точка входа для обоих протоколов. messages — уже готовый
    wire-формат (client.py._build_messages(), system-сообщения внутри
    как role="system"). api_key_env — имя переменной окружения, где
    лежит ключ (ключ никогда не хранится в самом конфиге узла — только
    имя переменной, по той же дисциплине, что уже принята для KEK/DEK в
    agent/db/sql/keys.py)."""
    api_key = os.environ.get(api_key_env) if api_key_env else None

    if protocol == "openai":
        return _generate_openai(
            messages, base_url=base_url, api_key=api_key, model=model,
            temperature=temperature, max_tokens=max_tokens,
            response_format=response_format, stop=stop, timeout=timeout,
        )
    if protocol == "anthropic":
        return _generate_anthropic(
            messages, base_url=base_url, api_key=api_key, model=model,
            temperature=temperature, max_tokens=max_tokens,
            stop=stop, timeout=timeout,
        )
    raise RemoteBackendError(f"неизвестный протокол {protocol!r} (ожидался 'openai' или 'anthropic')")
