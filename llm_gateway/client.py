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
from dataclasses import dataclass

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


@dataclass(frozen=True)
class CompletionResult:
    """Бэкенд-независимая обёртка над generation-метаданными — для тех
    редких call-сайтов, которым мало голого текста (см.
    final_claim_coverage.py: нужно отличить "модель написала мусор" от
    "не хватило токенов дописать валидный JSON"). Поля названы по
    смыслу, а не по сырым именам полей Ollama (done_reason/eval_count),
    чтобы будущая смена бэкенда не потребовала переименований у
    call-сайтов, которые в это заглядывают."""

    text: str
    truncated: bool
    token_count: int | None


def _do_complete(
    prompt: str,
    *,
    model: str,
    system: str | None,
    temperature: float | None,
    max_tokens: int | None,
    timeout: int,
    base_url: str,
    strip_think: bool,
    extra_options: dict[str, object] | None,
) -> tuple[str, dict]:
    """Общая часть complete()/complete_with_meta() — один HTTP-вызов,
    возвращает и очищенный текст, и сырой JSON-ответ (для тех, кому
    нужны метаданные генерации)."""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    options: dict[str, object] = dict(extra_options) if extra_options else {}
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
        raw = resp.json()
        text = raw["message"]["content"]
    except (KeyError, ValueError, TypeError) as e:
        raise LLMError(f"{model}: неожиданный формат ответа: {e}") from e

    if strip_think:
        text = _THINK_TAG_RE.sub("", text)
    return text.strip(), raw


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
    extra_options: dict[str, object] | None = None,
) -> str:
    """Запросить у модели завершение текста.

    Один вызов покрывает оба паттерна, что раньше были размазаны по
    agent/: голый prompt (бывший /api/generate) и prompt+system (бывший
    /api/chat) — внутри всегда используется чат-эндпоинт Ollama, вторая
    форма для него просто частный случай без system-сообщения.

    base_url переопределяется там, где call-сайт валидирует разные ноды
    на разных Ollama-инстансах (см. orch_validator.py), а не только
    локальный. extra_options — путь наружу для редких, специфичных для
    конкретного call-сайта опций generation (например seed у валидатора
    нод), не заслуживающих собственного именованного параметра здесь.

    Бросает LLMError при сетевой ошибке, ошибке бэкенда или неожиданном
    формате ответа — вызывающий код сам решает, ловить её или нет,
    вместо старого соглашения возвращать строку "[error: ...]".

    Нужны метаданные генерации (обрезал ли лимит токенов ответ)? См.
    complete_with_meta().
    """
    text, _raw = _do_complete(
        prompt, model=model, system=system, temperature=temperature,
        max_tokens=max_tokens, timeout=timeout, base_url=base_url,
        strip_think=strip_think, extra_options=extra_options,
    )
    return text


def complete_with_meta(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    base_url: str = DEFAULT_BASE_URL,
    strip_think: bool = True,
    extra_options: dict[str, object] | None = None,
) -> CompletionResult:
    """Как complete(), но также сообщает, была ли генерация оборвана
    лимитом токенов (а не завершилась естественно) — нужно только
    call-сайтам, которым важно отличить "модель дала мусор" от "модели
    не хватило места дописать ответ"."""
    text, raw = _do_complete(
        prompt, model=model, system=system, temperature=temperature,
        max_tokens=max_tokens, timeout=timeout, base_url=base_url,
        strip_think=strip_think, extra_options=extra_options,
    )
    return CompletionResult(
        text=text,
        truncated=raw.get("done_reason") == "length",
        token_count=raw.get("eval_count"),
    )


def is_available(base_url: str = DEFAULT_BASE_URL, timeout: float = 2.0) -> bool:
    """Жив ли бэкенд прямо сейчас — для health-check и будущего фоллбэка
    (Phase 3 плана: свой движок первым, Ollama — подстраховка)."""
    try:
        r = _session.get(f"{base_url}/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False
