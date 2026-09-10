"""
llm_gateway.client — единая точка вызова языковой модели для agent/.

Раньше ~29 файлов в agent/ сами строили HTTP-запрос к Ollama: каждый со
своей копией OLLAMA="http://127.0.0.1:11434", каждый сам решал, бить в
/api/chat или /api/generate, каждый сам парсил ответ. Теперь вся эта
логика тут, за одной функцией complete() — call-сайты больше не знают
и не должны знать, что за бэкендом она стоит. Сегодня внутри всё ещё
Ollama; когда появится свой движок (см. память ollama-decoupling-plan),
поменяется только этот файл, а не десятки мест, которые его вызывают.
"""
from __future__ import annotations

import re

import requests

# HTTP_PROXY/HTTPS_PROXY выставлены в системе глобально и по умолчанию
# заворачивают даже localhost-трафик — тот же самый источник багов,
# что не раз всплывал в других частях проекта. trust_env=False обходит
# это раз и навсегда прямо тут, а не в каждом файле по отдельности.
_session = requests.Session()
_session.trust_env = False

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 180


class LLMError(RuntimeError):
    """Бэкенд недоступен, вернул ошибку или неожиданный формат ответа."""


def complete(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    base_url: str = DEFAULT_BASE_URL,
    strip_think: bool = True,
) -> str:
    """Запросить у модели завершение текста.

    Один вызов покрывает оба паттерна, что раньше были размазаны по
    agent/: голый prompt (бывший /api/generate) и prompt+system (бывший
    /api/chat) — внутри всегда используется чат-эндпоинт Ollama, вторая
    форма для него просто частный случай без system-сообщения.

    Бросает LLMError при сетевой ошибке, ошибке бэкенда или неожиданном
    формате ответа — вызывающий код сам решает, ловить её или нет,
    вместо старого соглашения возвращать строку "[error: ...]".
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    options: dict[str, float | int] = {}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens is not None:
        options["num_predict"] = max_tokens

    payload: dict[str, object] = {"model": model, "messages": messages, "stream": False}
    if options:
        payload["options"] = options

    try:
        resp = _session.post(f"{base_url}/api/chat", json=payload, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise LLMError(f"{model}: {e}") from e

    try:
        text = resp.json()["message"]["content"]
    except (KeyError, ValueError, TypeError) as e:
        raise LLMError(f"{model}: неожиданный формат ответа: {e}") from e

    if strip_think:
        text = _THINK_TAG_RE.sub("", text)
    return text.strip()


def is_available(base_url: str = DEFAULT_BASE_URL, timeout: float = 2.0) -> bool:
    """Жив ли бэкенд прямо сейчас — для health-check и будущего фоллбэка
    (Phase 3 плана: свой движок первым, Ollama — подстраховка)."""
    try:
        r = _session.get(f"{base_url}/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False
