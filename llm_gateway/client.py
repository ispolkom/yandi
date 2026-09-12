"""
llm_gateway.client — единая точка вызова языковой модели для agent/.

Раньше ~29 файлов в agent/ сами строили HTTP-запрос к Ollama: каждый со
своей копией OLLAMA="http://127.0.0.1:11434", каждый сам решал, бить в
/api/chat или /api/generate, каждый сам парсил ответ. Теперь вся эта
логика тут, за одной функцией complete() — call-сайты больше не знают
и не должны знать, что за бэкендом она стоит.

Phase 3 (см. память ollama-decoupling-plan): свой встроенный ДЕФОЛТНЫЙ
движок (llama.cpp, см. llamacpp_backend.py — те же GGUF-веса, что были
скачаны для Ollama, просто без HTTP-сервера Ollama между нами и
моделью) пробуется для локальных вызовов, когда явно включён через
LLM_GATEWAY_ENABLE_LOCAL (см. _LOCAL_ENABLED ниже — opt-in, не opt-out,
ради существующего тестового набора).

ГЛАВНЫЙ ИНВАРИАНТ (закреплён после аудита остаточной зависимости от
Ollama, см. YANDI_OLLAMA_DECOUPLING_AUDIT.md §5, доведён до конца
мандатом "explicit config independence"): явный выбор владельца узла
сильнее любого автоматического fallback И НЕ ЗАВИСИТ от значения
LLM_GATEWAY_ENABLE_LOCAL. Если владелец явно настроил backend для
точного имени модели (через llm_gateway.setup / secure_store) — есть
только два исхода: этот backend ответил, или наружу выходит честная
ошибка ИМЕННО этого backend'а. Никакого тихого перехода на Ollama, на
встроенный дефолт или на что-либо ещё — и это проверяется ВСЕГДА,
независимо от env-флага, способа запуска или тестового окружения.

LLM_GATEWAY_ENABLE_LOCAL управляет ТОЛЬКО автоматической загрузкой
ВСТРОЕННОГО дефолтного движка (тем, что происходит, когда владелец
узла НИЧЕГО явно не настраивал) — исторически он появился именно для
этого (защита существующего regression-набора agent/ от неожиданной
загрузки реальной модели, см. комментарий у _LOCAL_ENABLED), и никогда
не был задуман как "разрешение на всю архитектуру llm_gateway". См.
_do_complete() — три чётких шага (STEP 1/2/3).

EMBEDDINGS (см. YANDI_EMBEDDINGS_ARCHITECTURE_AUDIT.md): отдельная
способность узла, НЕ совмещённая с completion. Владелец узла может
настроить embedding-провайдер независимо от completion-провайдера —
через ту же secure_store, но под другим именем (никакой отдельной
таблицы/формата не потребовалось: имя в secure_store всегда было
произвольной строкой, выбранной владельцем, а не обязательно "именем
модели для генерации"). embed() следует тому же инварианту, что и
complete() после мандата "explicit config independence": явная
настройка владельца проверяется всегда, провал настроенного backend'а
никогда не тихо подменяется другим источником эмбеддингов. Встроенного
локального дефолта для эмбеддингов пока не существует (в отличие от
completion) — если владелец ничего не настроил явно, единственный
автоматический путь — Ollama-совместимый фоллбэк, тот же принцип
compatibility-backend, что и раньше.

ИДЕНТИЧНОСТЬ ВЕКТОРНОГО ПРОСТРАНСТВА (см. vector_space.py):
одинаковая размерность двух векторов НЕ означает, что их можно
сравнивать — они должны происходить из доказуемо совместимого
embedding-пространства. embed() всегда возвращает EmbeddingResult с
полем `space` (VectorSpaceId) — вызывающий код, который что-либо
ПЕРСИСТИРУЕТ (сохраняет вектор дольше одного запроса), обязан сохранить
`space` рядом с вектором и сверять его через vector_space.compatible()
перед любым cosine/dot сравнением с ранее сохранённым вектором.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

from . import vector_space

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


def _build_messages(
    prompt: str | None, system: str | list[str] | None, messages: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """Единая точка сборки wire-формата messages для ВСЕХ backend'ов.

    Ровно один из (prompt, messages) должен быть задан — это проверяет
    сам вызывающий код (complete()), здесь просто сборка:
    - system: одна строка (старое поведение, один system-message) ИЛИ
      список строк (мандат "chat_local gateway migration" — несколько
      независимых system-инструкций, как у pet/chat_local.py: характер,
      память отношений, формат self-report — каждая own message, не
      склеены в одну строку, чтобы не менять то, что уже живо
      протестировано против конкретной модели).
    - messages, если задан — ПОЛНАЯ история разговора (уже включая
      последнюю реплику пользователя, как её присылает клиент) —
      заменяет одиночный prompt целиком, для настоящего multi-turn чата
      (chat_local.py), а не одноразовых prompt'ов, как везде в agent/.
    """
    sys_list = system if isinstance(system, list) else ([system] if system else [])
    result = [{"role": "system", "content": s} for s in sys_list if s]
    if messages is not None:
        result.extend(messages)
    elif prompt is not None:
        result.append({"role": "user", "content": prompt})
    return result

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
    prompt: str | None,
    *,
    model: str,
    system: str | list[str] | None,
    messages: list[dict[str, str]] | None,
    temperature: float | None,
    max_tokens: int | None,
    timeout: int,
    base_url: str,
    extra_options: dict[str, object] | None,
    response_format: str | None,
    stop: list[str] | None,
) -> tuple[str, dict]:
    """HTTP-путь через Ollama — оригинальный бэкенд, теперь фоллбэк."""
    wire_messages = _build_messages(prompt, system, messages)

    options: dict[str, object] = dict(extra_options) if extra_options else {}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    if stop:
        # Ollama принимает stop-последовательности как список внутри options.
        options["stop"] = list(stop)

    payload: dict[str, object] = {"model": model, "messages": wire_messages, "stream": False}
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
    prompt: str | None,
    *,
    system: str | list[str] | None,
    messages: list[dict[str, str]] | None,
    temperature: float | None,
    max_tokens: int | None,
    response_format: str | None,
    extra_options: dict[str, object] | None,
    stop: list[str] | None,
) -> tuple[str, dict] | None:
    """Владелец узла сам настроил эту модель (llm_gateway.setup) —
    локальный файл в СВОЕЙ папке или свой удалённый сервер (свой Клод,
    свой OpenAI-совместимый сервер, что угодно). Проверяется ПЕРЕД
    встроенным дефолтом — явный выбор владельца узла всегда важнее
    зашитого в код примера. None, если для этого имени ничего не
    настроено (не ошибка — просто нечего пробовать)."""
    entry = node_config.get_model_entry(model)
    if entry is None:
        # Единственный легитимный None — нечего пробовать, NO_CONFIGURED_BACKEND.
        return None

    wire_messages = _build_messages(prompt, system, messages)

    backend = entry.get("backend")
    if backend == "llamacpp":
        spec = llamacpp_backend.ModelSpec(
            path=entry["path"],
            n_ctx=entry.get("n_ctx", 8192),
            n_gpu_layers=entry.get("n_gpu_layers", -1),
        )
        result = llamacpp_backend.generate_at_spec(
            wire_messages, spec=spec, temperature=temperature,
            max_tokens=max_tokens, response_format=response_format,
            extra_options=extra_options, stop=stop,
        )
    elif backend == "remote":
        result = remote_backend.generate(
            wire_messages,
            base_url=entry["base_url"], protocol=entry.get("protocol", "openai"),
            model=entry.get("model", model), api_key_env=entry.get("api_key_env"),
            temperature=temperature, max_tokens=max_tokens,
            response_format=response_format, stop=stop,
        )
    else:
        raise RuntimeError(f"неизвестный backend {backend!r} в настройке узла для модели {model!r}")

    # Запись НАЙДЕНА (entry не None) — значит мы уже внутри CONFIGURED,
    # не NO_CONFIGURED_BACKEND. Если backend-функция ведёт себя не по
    # контракту (вернула не (текст, метаданные), например голый None) —
    # это ОШИБКА ЭТОГО backend'а, а не сигнал "ничего не настроено".
    # Нельзя позволить такому результату случайно совпасть с легитимным
    # None выше и провалиться в автоматический fallback.
    if not (isinstance(result, tuple) and len(result) == 2):
        raise RuntimeError(
            f"backend {backend!r}, настроенный владельцем узла для модели {model!r}, "
            f"вернул некорректный результат вместо (текст, метаданные): {result!r}"
        )
    return result


def _do_complete(
    prompt: str | None,
    *,
    model: str,
    system: str | list[str] | None,
    messages: list[dict[str, str]] | None,
    temperature: float | None,
    max_tokens: int | None,
    timeout: int,
    base_url: str,
    strip_think: bool,
    extra_options: dict[str, object] | None,
    response_format: str | None,
    stop: list[str] | None,
) -> tuple[str, dict]:
    """Общая часть complete()/complete_with_meta(). Три чётких шага,
    ни один не смешивается с другим (мандат "explicit config
    independence" — явный выбор владельца узла не зависит от
    LLM_GATEWAY_ENABLE_LOCAL):

    STEP 1 — ВСЕГДА (независимо от _LOCAL_ENABLED) проверить, настроил
    ли владелец узла backend для точного имени модели
    (_try_configured_backend()). Если настроил: успех — используем как
    есть и уходим; сбой — LLMError наружу и уходим. Никакого дальнейшего
    шага в обоих случаях. Единственное условие входа в STEP 1 —
    `base_url == DEFAULT_BASE_URL`: явная настройка — это свойство ЭТОГО
    узла, а не удалённой ноды, которую валидатор опрашивает по чужому
    base_url (это вне периметра, не трогаем).

    STEP 2 — только если STEP 1 не нашёл явной настройки
    (NO_CONFIGURED_BACKEND, entry is None). ЗДЕСЬ, и только здесь,
    смотрим на LLM_GATEWAY_ENABLE_LOCAL: если включён и имя есть во
    встроенном дефолтном реестре — пробуем встроенный движок. Это
    единственная вещь, которой управляет флаг — историческая защита
    regression-набора agent/ от неожиданной загрузки реальной модели
    (см. комментарий у _LOCAL_ENABLED), а не разрешение на всю
    архитектуру llm_gateway.

    STEP 3 — если ничего из STEP 1/2 не дало ответ, Ollama HTTP —
    фоллбэк, как и раньше, только для NO_CONFIGURED_BACKEND случая.

    Возвращает очищенный текст и сырой словарь метаданных."""
    if prompt is None and messages is None:
        raise LLMError("complete() требует либо prompt, либо messages — ни один не задан")
    if prompt is not None and messages is not None:
        raise LLMError("complete() принимает либо prompt, либо messages, но не оба сразу")

    text: str | None = None
    raw: dict = {}
    resolved = False

    if base_url == DEFAULT_BASE_URL:
        # STEP 1 — явная настройка владельца узла. Проверяется ВСЕГДА,
        # LLM_GATEWAY_ENABLE_LOCAL тут ни при чём.
        try:
            configured_result = _try_configured_backend(
                model, prompt, system=system, messages=messages, temperature=temperature,
                max_tokens=max_tokens, response_format=response_format,
                extra_options=extra_options, stop=stop,
            )
        except Exception as e:
            # CONFIGURED_BACKEND_FAILED — владелец узла явно выбрал этот
            # backend для этой модели. Автоматический переход на другой
            # источник интеллекта здесь запрещён категорически (см.
            # докстринг выше) — наружу идёт честная ошибка ИМЕННО этого
            # backend'а, а не тихая подмена. Сообщение не содержит
            # api-ключей/секретов — они не попадают в текст исключений
            # backend-модулей (см. remote_backend.py/llamacpp_backend.py).
            raise LLMError(
                f"настроенный владельцем узла backend для модели {model!r} "
                f"не сработал — автоматический переход на другой источник "
                f"интеллекта запрещён явным выбором владельца: {e}"
            ) from e

        if configured_result is not None:
            # Успех явно настроенного backend'а — используем как есть,
            # ничего больше не пробуем, даже если текст оказался пустым
            # (пустой ответ — это тоже ответ ИМЕННО этого backend'а, а
            # не сигнал попробовать что-то ещё).
            text, raw = configured_result
            resolved = True
        elif _LOCAL_ENABLED and llamacpp_backend.has_model(model):
            # STEP 2 — NO_CONFIGURED_BACKEND, флаг включён, имя есть во
            # встроенном дефолтном реестре — старое автоматическое
            # поведение не меняется.
            try:
                text, raw = llamacpp_backend.generate(
                    _build_messages(prompt, system, messages), model=model, temperature=temperature,
                    max_tokens=max_tokens, response_format=response_format,
                    extra_options=extra_options, stop=stop,
                )
                resolved = True
            except Exception as e:
                print(f"[llm_gateway] встроенный дефолт не справился с {model!r} ({e}), откат на Ollama")

    if not resolved:
        # STEP 3 — Ollama HTTP. Достижимо только если владелец ничего не
        # настроил (или base_url не локальный — валидатор чужой ноды).
        text, raw = _do_complete_ollama(
            prompt, model=model, system=system, messages=messages, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, base_url=base_url,
            extra_options=extra_options, response_format=response_format, stop=stop,
        )

    if strip_think:
        text = _THINK_TAG_RE.sub("", text)
    return text.strip(), raw


def complete(
    prompt: str | None = None,
    *,
    model: str,
    system: str | list[str] | None = None,
    messages: list[dict[str, str]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    base_url: str = DEFAULT_BASE_URL,
    strip_think: bool = True,
    extra_options: dict[str, object] | None = None,
    response_format: str | None = None,
    stop: list[str] | None = None,
) -> str:
    """Запросить у модели завершение текста.

    Ровно один из (prompt, messages) должен быть задан. Голый prompt
    (+ опциональный system, бывший /api/generate-стиль) покрывает
    подавляющее большинство call-сайтов agent/ — одноразовый запрос без
    истории. messages — ПОЛНАЯ история чата (мандат "chat_local gateway
    migration": pet/chat_local.py — настоящий многоходовой чат, не
    одноразовый prompt) — список {"role": "user"|"assistant", ...},
    уже включающий последнюю реплику пользователя; system в этом случае
    добавляется отдельными system-сообщениями ПЕРЕД ним же (можно
    списком строк — несколько независимых системных инструкций, как у
    chat_local, не склеенных в одну).

    base_url переопределяется там, где call-сайт валидирует разные ноды
    на разных Ollama-инстансах (см. orch_validator.py), а не только
    локальный. extra_options — путь наружу для РЕДКИХ, специфичных для
    конкретного call-сайта/backend'а опций generation (например seed у
    валидатора нод, или repeat_penalty/repeat_last_n у chat_local —
    поддержаны там, где у backend'а есть реальный эквивалент, иначе
    задокументированно и безопасно проигнорированы, никогда не
    имитированы). stop — stop-последовательности; это ОБЩИЙ для всех
    backend'ов концепт (в отличие от repeat_penalty), поэтому у него
    есть отдельный именованный параметр — каждый backend переводит его
    в свой формат (Ollama: options.stop, OpenAI: top-level stop,
    Anthropic: stop_sequences, llama.cpp: свой stop=).
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
        prompt, model=model, system=system, messages=messages, temperature=temperature,
        max_tokens=max_tokens, timeout=timeout, base_url=base_url,
        strip_think=strip_think, extra_options=extra_options,
        response_format=response_format, stop=stop,
    )
    return text


def complete_with_meta(
    prompt: str | None = None,
    *,
    model: str,
    system: str | list[str] | None = None,
    messages: list[dict[str, str]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    base_url: str = DEFAULT_BASE_URL,
    strip_think: bool = True,
    extra_options: dict[str, object] | None = None,
    response_format: str | None = None,
    stop: list[str] | None = None,
) -> CompletionResult:
    """Как complete(), но также сообщает, была ли генерация оборвана
    лимитом токенов (а не завершилась естественно) — нужно только
    call-сайтам, которым важно отличить "модель дала мусор" от "модели
    не хватило места дописать ответ"."""
    text, raw = _do_complete(
        prompt, model=model, system=system, messages=messages, temperature=temperature,
        max_tokens=max_tokens, timeout=timeout, base_url=base_url,
        strip_think=strip_think, extra_options=extra_options,
        response_format=response_format, stop=stop,
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


@dataclass(frozen=True)
class ModelInfo:
    """Безопасное для показа пользователю (UI) описание одной ЛОГИЧЕСКОЙ
    модели, доступной этой ноде — никогда не путей/URL/имён env-
    переменных с ключами. Мандат "chat_local gateway migration":
    "local models = Ollama models" перестало быть верным, как только
    появился явный per-node выбор backend'а — list_models() отражает
    ЭТО, а не конкретно Ollama."""

    name: str
    kind: str  # "local" (explicit llamacpp / встроенный дефолт) | "remote" (explicit) | "ollama" (compat)
    available: bool


def list_models(base_url: str = DEFAULT_BASE_URL, timeout: float = 2.0) -> list[ModelInfo]:
    """Все ЛОГИЧЕСКИЕ модели, которые реально можно запросить у этой
    ноды через complete()/embed() под именем name — с трёх источников,
    ни один не обязателен:
    1. Явно настроенные владельцем узла (secure_store) — приоритетные,
       как и при обычном вызове complete().
    2. Встроенный локальный дефолтный реестр (llamacpp_backend).
    3. Ollama-совместимый список моделей, ЕСЛИ Ollama реально отвечает
       прямо сейчас — best-effort, отсутствие Ollama НЕ роняет вызов,
       просто этот источник ничего не добавляет.
    Ни один физический путь к файлу, URL удалённого сервера или имя
    env-переменной с ключом сюда никогда не попадает — см. ModelInfo."""
    seen: set[str] = set()
    result: list[ModelInfo] = []

    try:
        for name, entry in node_config.list_models().items():
            kind = "local" if entry.get("backend") == "llamacpp" else "remote"
            result.append(ModelInfo(name=name, kind=kind, available=True))
            seen.add(name)
    except Exception:
        # secure_store недоступна/повреждена — не роняем весь listing
        # ради этого одного источника, остальные два всё ещё работают.
        pass

    for name in llamacpp_backend.list_builtin_models():
        if name in seen:
            continue
        result.append(ModelInfo(name=name, kind="local", available=llamacpp_backend.has_model(name)))
        seen.add(name)

    try:
        resp = _session.get(f"{base_url}/api/tags", timeout=timeout)
        if resp.status_code == 200:
            for m in resp.json().get("models", []):
                name = m.get("name")
                if name and name not in seen:
                    result.append(ModelInfo(name=name, kind="ollama", available=True))
                    seen.add(name)
    except requests.RequestException:
        pass  # Ollama недоступна — этот источник просто пуст, не ошибка

    return result


# ============================================================================
# EMBEDDINGS — отдельная способность узла, не совмещённая с completion.
# См. YANDI_EMBEDDINGS_ARCHITECTURE_AUDIT.md для фактической карты того,
# что этот слой заменяет (10 живых мест в agent/, две модели, два
# Ollama-эндпоинта, ни одно не абстрагировано раньше).
# ============================================================================


class EmbedError(RuntimeError):
    """Embedding-backend недоступен, вернул ошибку, malformed response,
    или batch/размерность не совпадают с ожиданием. Отдельный от
    LLMError класс — генерация текста и эмбеддинги содержательно разные
    способности узла, ловить одну ошибку не должно означать ловить
    другую по случайности общего типа исключения."""


@dataclass(frozen=True)
class EmbeddingResult:
    """vectors — по одному списку float на каждый входной текст, в том
    же порядке. space — VectorSpaceId, обязателен: любой код, который
    ПЕРСИСТИРУЕТ вектор (сохраняет дольше одного запроса), обязан
    сохранить это поле рядом с вектором — см. vector_space.py."""

    vectors: list[list[float]]
    space: vector_space.VectorSpaceId


def _validate_embedding_batch(vectors: object, *, expected_count: int, context: str) -> None:
    """Проверяет embedding-ответ ДО того, как он дойдёт до вызывающего
    кода — malformed embedding не должен превращаться в "как-нибудь
    сравним". Проверяет: вектор существует, batch — реально список,
    количество совпадает со входом, каждый вектор непустой и числовой,
    нет NaN/Inf, все векторы одного batch'а одной размерности."""
    if not isinstance(vectors, list) or len(vectors) == 0:
        raise EmbedError(f"{context}: пустой или некорректный список векторов")
    if len(vectors) != expected_count:
        raise EmbedError(
            f"{context}: batch вернул {len(vectors)} векторов вместо {expected_count} входных текстов"
        )
    dims: set[int] = set()
    for i, v in enumerate(vectors):
        if not isinstance(v, list) or len(v) == 0:
            raise EmbedError(f"{context}: вектор #{i} пуст или не является списком")
        for x in v:
            if not isinstance(x, (int, float)) or isinstance(x, bool):
                raise EmbedError(f"{context}: вектор #{i} содержит нечисловое значение {x!r}")
            if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
                raise EmbedError(f"{context}: вектор #{i} содержит NaN/Inf")
        dims.add(len(v))
    if len(dims) > 1:
        raise EmbedError(f"{context}: векторы одного batch'а разной размерности: {sorted(dims)}")


def _try_configured_embedding_backend(
    model: str, texts: list[str]
) -> tuple[list[list[float]], vector_space.VectorSpaceId] | None:
    """Владелец узла сам настроил embedding-backend под этим именем —
    та же secure_store, что и для completion (config.py), просто имя
    может быть другим: embedding-провайдер и completion-провайдер не
    обязаны совпадать, владелец выбирает независимо. None, если для
    этого имени ничего не настроено (NO_CONFIGURED_BACKEND — не
    ошибка)."""
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
        vectors, meta = llamacpp_backend.embed_at_spec(texts, spec=spec)
        backend_kind, protocol = "llamacpp", "llamacpp-embed"
        model_identity = entry.get("path", model)
    elif backend == "remote":
        protocol_name = entry.get("protocol", "openai")
        vectors, meta = remote_backend.embed(
            texts,
            base_url=entry["base_url"], protocol=protocol_name,
            model=entry.get("model", model), api_key_env=entry.get("api_key_env"),
        )
        backend_kind, protocol = "remote", f"remote-{protocol_name}-embeddings"
        model_identity = entry.get("model", model)
    else:
        raise RuntimeError(f"неизвестный backend {backend!r} в настройке узла для embedding-модели {model!r}")

    if not (isinstance(vectors, list) and isinstance(meta, dict) and "dimension" in meta):
        raise RuntimeError(
            f"backend {backend!r}, настроенный владельцем узла для embedding-модели {model!r}, "
            f"вернул некорректный результат вместо (векторы, метаданные): {(vectors, meta)!r}"
        )

    _validate_embedding_batch(
        vectors, expected_count=len(texts),
        context=f"настроенный владельцем backend {backend!r} для {model!r}",
    )
    space = vector_space.VectorSpaceId(
        backend=backend_kind, protocol=protocol, model=model_identity,
        dimension=meta["dimension"], normalized=bool(meta.get("normalized", False)),
    )
    return vectors, space


def _do_embed_ollama(
    texts: list[str], *, model: str, base_url: str, timeout: int,
) -> tuple[list[list[float]], vector_space.VectorSpaceId]:
    """Ollama-совместимый фоллбэк — единственный автоматический путь,
    когда владелец ничего явно не настроил. Единообразно использует
    /api/embed (batch-способный) независимо от того, какое имя модели
    передано — вызывающий код никогда не должен сам знать про этот
    эндпоинт или формат ответа Ollama."""
    try:
        resp = _session.post(
            f"{base_url}/api/embed", json={"model": model, "input": texts}, timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise EmbedError(f"{model}: {e}") from e

    try:
        raw = resp.json()
        vectors = raw["embeddings"]
    except (KeyError, ValueError, TypeError) as e:
        raise EmbedError(f"{model}: неожиданный формат embedding-ответа: {e}") from e

    _validate_embedding_batch(vectors, expected_count=len(texts), context=f"Ollama/{model}")
    dimension = len(vectors[0])
    space = vector_space.VectorSpaceId(
        backend="ollama-compat", protocol="ollama-embed", model=model,
        dimension=dimension, normalized=False,
    )
    return vectors, space


def embed(
    texts: list[str] | str,
    *,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = DEFAULT_TIMEOUT,
) -> EmbeddingResult:
    """Embedding-граница — параллельная complete(), не смешанная с ней.

    Тот же инвариант, что и у complete() после мандата "explicit config
    independence": explicit configuration is always authoritative.

    STEP 1 — ВСЕГДА (независимо от каких-либо флагов) проверить, настроил
    ли владелец узла embedding-backend для точного имени модели. Успех —
    используем как есть; сбой — EmbedError наружу, никакого перехода на
    другой источник эмбеддингов.

    STEP 2 — встроенного локального embedding-дефолта пока не
    существует (в отличие от completion) — этот шаг просто отсутствует.

    STEP 3 — если владелец ничего явно не настроил, Ollama-совместимый
    фоллбэк (тот же compatibility-принцип, что и у Ollama для
    completion).

    Как и у complete(), это только для локального base_url — не
    трогаем удалённую валидацию нод в этом мандате.

    Принимает и одну строку, и список — при одной строке результат
    всё равно содержит список из одного вектора (единообразный
    контракт для вызывающего кода)."""
    text_list = [texts] if isinstance(texts, str) else list(texts)
    if not text_list:
        raise EmbedError("embed() вызван с пустым списком текстов")

    vectors: list[list[float]] | None = None
    space: vector_space.VectorSpaceId | None = None

    if base_url == DEFAULT_BASE_URL:
        try:
            configured_result = _try_configured_embedding_backend(model, text_list)
        except Exception as e:
            raise EmbedError(
                f"настроенный владельцем узла embedding-backend для модели {model!r} "
                f"не сработал — автоматический переход на другой источник эмбеддингов "
                f"запрещён явным выбором владельца: {e}"
            ) from e

        if configured_result is not None:
            vectors, space = configured_result

    if vectors is None:
        vectors, space = _do_embed_ollama(text_list, model=model, base_url=base_url, timeout=timeout)

    return EmbeddingResult(vectors=vectors, space=space)
