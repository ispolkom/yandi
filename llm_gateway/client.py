"""
llm_gateway.client — единая точка вызова языковой модели для agent/.

Раньше ~29 файлов в agent/ сами строили HTTP-запрос к Ollama: каждый со
своей копией OLLAMA="http://127.0.0.1:11434", каждый сам решал, бить в
/api/chat или /api/generate, каждый сам парсил ответ. Теперь вся эта
логика тут, за одной функцией complete() — call-сайты больше не знают
и не должны знать, что за бэкендом она стоит.

Phase 3 (см. память ollama-decoupling-plan): свой движок (llama.cpp,
см. llamacpp_backend.py — те же GGUF-веса, что были скачаны для Ollama,
просто без HTTP-сервера Ollama между нами и моделью) пробуется ПЕРВЫМ
для локальных вызовов, когда явно включён через LLM_GATEWAY_ENABLE_LOCAL
(см. _LOCAL_ENABLED ниже — opt-in, не opt-out, ради существующего
тестового набора). Ollama остаётся автоматическим фоллбэком — если
своего движка нет, модели нет в его реестре, или он упал по любой
причине, тихо откатываемся на тот же HTTP-путь, что был всегда. Ни один
call-сайт этого не видит и не должен видеть.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import requests

from . import config as node_config
from . import llamacpp_backend
from . import remote_backend

# HTTP_PROXY/HTTPS_PROXY выставлены в системе глобально и по умолчанию
# заворачивают даже localhost-трафик — тот же самый источник багов,
# что не раз всплывал в других частях проекта. trust_env=False обходит
# это раз и навсегда прямо тут, а не в каждом файле по отдельности.
_session = requests.Session()
_session.trust_env = False

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 180

# Локальный движок (встроенный дефолт ИЛИ то, что владелец узла сам
# настроил через llm_gateway.setup/config — локальный файл или свой
# удалённый сервер) — opt-in, не opt-out. Причина: десятки существующих
# regression-тестов по всему agent/ мокают requests.Session.post
# напрямую для тех же имён моделей (heretic:q8, qwen3:14b), что теперь
# зарегистрированы в llamacpp_backend — если бы движок пробовался по
# умолчанию, эти тесты молча перестали бы проверять то, что думают, что
# проверяют (мок никогда не вызовется, вместо этого поднимется реальная
# 10ГБ модель на реальном GPU). Явное включение через переменную
# окружения защищает весь существующий тестовый набор бесплатно, ценой
# одной строчки при реальном запуске демона.
_LOCAL_ENABLED = os.environ.get("LLM_GATEWAY_ENABLE_LOCAL", "") not in ("", "0")


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


def _do_complete_ollama(
    prompt: str,
    *,
    model: str,
    system: str | None,
    temperature: float | None,
    max_tokens: int | None,
    timeout: int,
    base_url: str,
    extra_options: dict[str, object] | None,
    response_format: str | None,
) -> tuple[str, dict]:
    """HTTP-путь через Ollama — оригинальный бэкенд, теперь фоллбэк."""
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
    if response_format is not None:
        payload["format"] = response_format
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

    return text, raw


def _try_configured_backend(
    model: str,
    prompt: str,
    *,
    system: str | None,
    temperature: float | None,
    max_tokens: int | None,
    response_format: str | None,
    extra_options: dict[str, object] | None,
) -> tuple[str, dict] | None:
    """Владелец узла сам настроил эту модель (llm_gateway.setup) —
    локальный файл в СВОЕЙ папке или свой удалённый сервер (свой Клод,
    свой OpenAI-совместимый сервер, что угодно). Проверяется ПЕРЕД
    встроенным дефолтом — явный выбор владельца узла всегда важнее
    зашитого в код примера. None, если для этого имени ничего не
    настроено (не ошибка — просто нечего пробовать)."""
    entry = node_config.get_model_entry(model)
    if entry is None:
        return None

    backend = entry.get("backend")
    if backend == "llamacpp":
        spec = llamacpp_backend.ModelSpec(
            path=entry["path"],
            n_ctx=entry.get("n_ctx", 8192),
            n_gpu_layers=entry.get("n_gpu_layers", -1),
        )
        return llamacpp_backend.generate_at_spec(
            prompt, spec=spec, system=system, temperature=temperature,
            max_tokens=max_tokens, response_format=response_format,
            extra_options=extra_options,
        )
    if backend == "remote":
        return remote_backend.generate(
            prompt,
            base_url=entry["base_url"], protocol=entry.get("protocol", "openai"),
            model=entry.get("model", model), api_key_env=entry.get("api_key_env"),
            system=system, temperature=temperature, max_tokens=max_tokens,
            response_format=response_format,
        )
    raise RuntimeError(f"неизвестный backend {backend!r} в настройке узла для модели {model!r}")


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
    response_format: str | None,
) -> tuple[str, dict]:
    """Общая часть complete()/complete_with_meta(). Порядок: 1) то, что
    владелец узла сам настроил под это имя модели, 2) встроенный
    дефолт (llama.cpp со своим реестром), 3) Ollama HTTP — фоллбэк,
    если первые два недоступны, не настроены или упали. Всё это только
    для локального base_url — удалённые ноды валидатора всегда идут в
    Ollama напрямую, у своего движка нет их адресов. Возвращает
    очищенный текст и сырой словарь метаданных (для тех, кому нужны
    метаданные генерации)."""
    text: str | None = None
    raw: dict = {}

    if _LOCAL_ENABLED and base_url == DEFAULT_BASE_URL:
        try:
            result = _try_configured_backend(
                model, prompt, system=system, temperature=temperature,
                max_tokens=max_tokens, response_format=response_format,
                extra_options=extra_options,
            )
            if result is not None:
                text, raw = result
            elif llamacpp_backend.has_model(model):
                text, raw = llamacpp_backend.generate(
                    prompt, model=model, system=system, temperature=temperature,
                    max_tokens=max_tokens, response_format=response_format,
                    extra_options=extra_options,
                )
        except Exception as e:
            print(f"[llm_gateway] локальный движок не справился с {model!r} ({e}), откат на Ollama")

    if text is None:
        text, raw = _do_complete_ollama(
            prompt, model=model, system=system, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, base_url=base_url,
            extra_options=extra_options, response_format=response_format,
        )

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
    response_format: str | None = None,
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
    response_format="json" — строгий JSON-режим бэкенда (Ollama:
    top-level "format", не options) для call-сайтов, где парсинг ответа
    как JSON обязателен (claim_relation.py и т.п.).

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
        response_format=response_format,
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
    response_format: str | None = None,
) -> CompletionResult:
    """Как complete(), но также сообщает, была ли генерация оборвана
    лимитом токенов (а не завершилась естественно) — нужно только
    call-сайтам, которым важно отличить "модель дала мусор" от "модели
    не хватило места дописать ответ"."""
    text, raw = _do_complete(
        prompt, model=model, system=system, temperature=temperature,
        max_tokens=max_tokens, timeout=timeout, base_url=base_url,
        strip_think=strip_think, extra_options=extra_options,
        response_format=response_format,
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
