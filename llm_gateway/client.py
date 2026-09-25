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
import json
from dataclasses import dataclass

from . import vector_space

import requests

from . import adapters
from . import config as node_config
from . import llamacpp_backend
from . import ollama_backend
from . import remote_backend
from .types import (
    GenerationRequest, OutputContract, ResolvedInferenceTarget,
    SemanticCompletionResult, SemanticOutputRequirement,
)

# HTTP_PROXY/HTTPS_PROXY выставлены в системе глобально и по умолчанию
# заворачивают даже localhost-трафик — тот же самый источник багов,
# что не раз всплывал в других частях проекта. trust_env=False обходит
# это раз и навсегда прямо тут, а не в каждом файле по отдельности.
from . import node_gateway
_session = ollama_backend._session

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 180


def _build_messages(
    prompt: str | None, system: str | list[str] | None, messages: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """Единая точка сборки wire-формата messages для ВСЕХ backend'ов.

    Ровно один из (prompt, messages) должен быть задан — это проверяет
    сам вызывающий код (complete()), здесь просто сборка:
    - system: одна строка ИЛИ список строк (chat_local.py передаёт
      несколько независимых system-инструкций: характер, память
      отношений, формат self-report). Live-тест 2026-09-16 против
      реально установленной модели (heretic:q8) нашёл, что предыдущее
      поведение (каждая строка — отдельное role:system-сообщение) не
      универсально: у этой модели Jinja-шаблон чата жёстко требует РОВНО
      ОДНО system-сообщение и именно в начале — "System message must be
      at the beginning", 400 Bad Request на что угодно ещё. Список
      теперь склеивается в ОДНО system-сообщение (двойным переводом
      строки — абзацами, содержимое не меняется) — это принимает любой
      шаблон, тогда как N отдельных system-сообщений принимают не все.
    - messages, если задан — ПОЛНАЯ история разговора (уже включая
      последнюю реплику пользователя, как её присылает клиент) —
      заменяет одиночный prompt целиком, для настоящего multi-turn чата
      (chat_local.py), а не одноразовых prompt'ов, как везде в agent/.
    """
    sys_list = system if isinstance(system, list) else ([system] if system else [])
    sys_list = [s for s in sys_list if s]
    result = [{"role": "system", "content": "\n\n".join(sys_list)}] if sys_list else []
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
    """Compatibility wrapper for the Ollama-compatible transport."""
    wire_messages = _build_messages(prompt, system, messages)
    try:
        return ollama_backend.generate(
            wire_messages,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            extra_options=extra_options,
            response_format=response_format,
            stop=stop,
        )
    except ollama_backend.OllamaBackendError as e:
        raise LLMError(f"{model}: {e}") from e


def _contract_from_response_format(response_format: str | None) -> OutputContract:
    return OutputContract(
        name="json_object" if response_format == "json" else "plain_text",
        response_format=response_format,
    )


_SEMANTIC_STATE_MARKER = "###YANDI_STATE###"

_SEMANTIC_REPLY_STATE_INSTRUCTION = (
    "Сформируй один результат из двух частей: обычная человеческая реплика пользователю "
    "и внутреннее состояние. Пользователь видит только реплику. Внутреннее состояние "
    "никогда не является текстом ответа."
)

_SEMANTIC_JSON_INSTRUCTION = (
    _SEMANTIC_REPLY_STATE_INSTRUCTION
    + " Верни строго один JSON-объект с полями \"reply\" (непустая строка) и "
    "\"state\" (объект или null). В поле state, если оно есть, используй только "
    "ожидаемые внутренние поля состояния."
)

_SEMANTIC_LEGACY_INSTRUCTION = (
    _SEMANTIC_REPLY_STATE_INSTRUCTION
    + f" Сначала напиши только видимую пользователю реплику. Затем на новой строке "
    f"добавь {_SEMANTIC_STATE_MARKER} и JSON-объект внутреннего состояния. "
    "Маркер и JSON не являются частью реплики пользователя."
)


def _state_schema_prompt_hint(state_schema: dict | None) -> str:
    """Name the expected state fields in words, from the caller's own schema.

    Live smoke test 2026-09-19 (real llama.cpp target, json_object
    contract): the shared instruction said only "state (object or null),
    use only the expected fields" and never NAMED them. With no decoder-
    enforced schema on that contract the model returned "state": null on
    4 of 5 turns and an invented {"mood": ..., "response_style": ...} on
    the fifth — so no state ever reached the caller, and an insult/apology
    changed nothing in persistent memory. After the field names were added,
    a second live finding: the model put null into a listed field that did
    not apply to that turn ("sincerity": null on a non-apology), the gateway
    (which only checks that required KEYS are present) passed it, and the
    caller's strict float() conversion then discarded the whole state — so
    the hint also says listed fields may not be null. Where the decoder enforces the
    schema (semantic_json_schema) the field names travel in the schema
    itself; where it does not, the only place they can travel is the
    prompt. Rendered generically from the schema (names, types, optional
    range/description) — no PET-specific wording lives in the gateway.
    """
    if not isinstance(state_schema, dict):
        return ""
    props = state_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(state_schema.get("required") or [])
    parts = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        details = [str(spec.get("type", "any"))]
        if "minimum" in spec and "maximum" in spec:
            details.append(f"от {spec['minimum']} до {spec['maximum']}")
        if name in required:
            details.append("обязательно")
        if spec.get("description"):
            details.append(str(spec["description"]))
        parts.append(f'"{name}" ({", ".join(details)})')
    return (
        " Объект state должен содержать ровно эти поля: " + "; ".join(parts) + ". "
        "Других полей не добавляй. Значения этих полей не могут быть null: если "
        "поле неприменимо, используй 0 для числа и false для boolean. Всегда "
        "заполняй state, когда можешь оценить ситуацию; null допустим только для "
        "самого state, и только если оценить её невозможно."
    )


def _semantic_result_schema(requirement: SemanticOutputRequirement) -> dict:
    state_schema = requirement.state_schema or {"type": "object"}
    required = ["reply"]
    if requirement.state_required:
        required.append("state")
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "minLength": 1},
            "state": state_schema,
        },
        "required": required,
    }


def _semantic_contract_from_target(
    requirement: SemanticOutputRequirement, target: ResolvedInferenceTarget,
) -> tuple[OutputContract, str]:
    if requirement.kind != "reply_state":
        raise LLMError(f"unsupported semantic output kind {requirement.kind!r}")
    if target.capabilities.json_schema:
        return (
            OutputContract(name="semantic_json_schema", response_format=_semantic_result_schema(requirement)),
            _SEMANTIC_JSON_INSTRUCTION,
        )
    hint = _state_schema_prompt_hint(requirement.state_schema)
    if target.capabilities.json_object:
        return (
            OutputContract(name="semantic_json_object", response_format="json"),
            _SEMANTIC_JSON_INSTRUCTION + hint,
        )
    return OutputContract(name="semantic_legacy_marker", response_format=None), _SEMANTIC_LEGACY_INSTRUCTION + hint


def _append_system_instruction(system: str | list[str] | None, instruction: str) -> str | list[str]:
    if isinstance(system, list):
        return [*system, instruction]
    if system:
        return [system, instruction]
    return instruction


def _strip_think_blocks(text: str) -> str:
    return _THINK_TAG_RE.sub("", text).replace("<think>", "").replace("</think>", "")


def _looks_like_internal_state_fragment(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    state_keys = ("is_insult", "severity", "is_apology", "sincerity")
    if sum(1 for key in state_keys if key in stripped) >= 2:
        return True
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return False
        if isinstance(data, dict):
            return sum(1 for key in state_keys if key in data) >= 2
    return False


def _state_valid_for_requirement(state: object, requirement: SemanticOutputRequirement) -> bool:
    if state is None:
        return not requirement.state_required
    if not isinstance(state, dict):
        return False
    schema = requirement.state_schema or {}
    required = schema.get("required") if isinstance(schema, dict) else None
    if isinstance(required, list):
        return all(isinstance(key, str) and key in state for key in required)
    return True


def _normalize_structured_semantic(
    content: str, requirement: SemanticOutputRequirement,
) -> tuple[str, dict[str, object] | None, bool, bool, bool, str | None]:
    stripped = _strip_think_blocks(content).strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        return "", None, False, False, False, f"malformed semantic JSON: {e}"
    if not isinstance(data, dict):
        return "", None, False, False, False, "semantic JSON root is not an object"

    reply = data.get("reply")
    state = data.get("state")
    reply_ok = isinstance(reply, str) and (bool(reply.strip()) or not requirement.reply_required)
    state_present = "state" in data and state is not None
    state_ok = _state_valid_for_requirement(state, requirement)
    if not reply_ok:
        return "", None, False, False, False, "semantic result missing visible reply"
    if state_present and not state_ok:
        return reply.strip(), None, True, False, False, "semantic state malformed"
    if requirement.state_required and not state_present:
        return reply.strip(), None, True, False, False, "semantic result missing required state"
    return reply.strip(), state if isinstance(state, dict) else None, True, state_ok, True, None


def _normalize_legacy_semantic(
    content: str, requirement: SemanticOutputRequirement,
) -> tuple[str, dict[str, object] | None, bool, bool, bool, str | None]:
    text = _strip_think_blocks(content).strip()
    if not text:
        return "", None, False, False, False, "empty semantic response"
    marker_pos = text.rfind(_SEMANTIC_STATE_MARKER)
    if marker_pos < 0:
        if _looks_like_internal_state_fragment(text):
            return "", None, False, False, False, "state-only response withheld from visible reply"
        if requirement.state_required:
            return "", None, False, False, False, "legacy semantic response missing required state marker"
        return text, None, True, True, True, None

    reply = text[:marker_pos].strip()
    tail = text[marker_pos + len(_SEMANTIC_STATE_MARKER):]
    if not reply and requirement.reply_required:
        return "", None, False, False, False, "legacy semantic response missing visible reply"
    match = re.search(r"\{.*\}", tail, re.DOTALL)
    if not match:
        if requirement.state_required:
            return reply, None, bool(reply), False, False, "legacy semantic marker missing JSON state"
        return reply, None, bool(reply), False, bool(reply), "legacy semantic marker missing JSON state"
    try:
        state = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        if requirement.state_required:
            return reply, None, bool(reply), False, False, f"legacy semantic state malformed: {e}"
        return reply, None, bool(reply), False, bool(reply), f"legacy semantic state malformed: {e}"
    state_ok = _state_valid_for_requirement(state, requirement)
    if not state_ok:
        if requirement.state_required:
            return reply, None, bool(reply), False, False, "legacy semantic state malformed"
        return reply, None, bool(reply), False, bool(reply), "legacy semantic state malformed"
    return reply, state if isinstance(state, dict) else None, bool(reply), True, bool(reply), None


def _normalize_semantic_completion(
    content: str,
    *,
    requirement: SemanticOutputRequirement,
    contract: OutputContract,
    metadata: dict,
) -> SemanticCompletionResult:
    if contract.name in ("semantic_json_schema", "semantic_json_object"):
        reply, state, reply_ok, state_ok, parse_ok, error = _normalize_structured_semantic(content, requirement)
    else:
        reply, state, reply_ok, state_ok, parse_ok, error = _normalize_legacy_semantic(content, requirement)
    semantic_meta = {
        "semantic_kind": requirement.kind,
        "reply_required": requirement.reply_required,
        "state_required": requirement.state_required,
        "selected_output_contract": contract.name,
        "semantic_parse_ok": parse_ok,
        "reply_present": reply_ok,
        "state_present": state is not None,
        "state_valid": state_ok,
    }
    if error:
        semantic_meta["semantic_error"] = error
    result_meta = dict(metadata)
    result_meta["_llm_gateway_semantic"] = semantic_meta
    return SemanticCompletionResult(
        reply=reply,
        state=state if state_ok else None,
        reply_ok=reply_ok,
        state_ok=state_ok,
        parse_ok=parse_ok,
        error=error,
        metadata=result_meta,
    )


def resolve_target(
    model: str,
    *,
    base_url: str,
    attempt: int = 1,
    fallback_from: ResolvedInferenceTarget | None = None,
) -> ResolvedInferenceTarget:
    """Resolve exactly one inference target for one generation attempt.

    This function decides identity and capabilities only. It never
    generates text. Every fallback is a new call to resolve_target(), so
    the fallback attempt gets its own capabilities and output contract.
    """
    if fallback_from is not None:
        if fallback_from.source != "builtin_registry":
            raise RuntimeError(f"target from {fallback_from.source!r} has no automatic fallback")
        adapter = adapters.get_adapter("ollama_compatible")
        target = {"base_url": base_url}
        return ResolvedInferenceTarget(
            logical_model=model,
            resolved_model=model,
            adapter_id=adapter.adapter_id,
            adapter=adapter,
            capabilities=adapter.capabilities(target),
            resolution_reason="legacy compatibility fallback after builtin llama.cpp failed",
            attempt=attempt,
            source="builtin_fallback",
            location=base_url,
            runtime="external_server",
            provider="ollama_compatible",
            config_ref="legacy:ollama_fallback",
            fallback_reason=f"fallback after {fallback_from.adapter_id} target failed",
            target=target,
        )

    if base_url != DEFAULT_BASE_URL:
        adapter = adapters.get_adapter("ollama_compatible")
        target = {"base_url": base_url}
        return ResolvedInferenceTarget(
            logical_model=model,
            resolved_model=model,
            adapter_id=adapter.adapter_id,
            adapter=adapter,
            capabilities=adapter.capabilities(target),
            resolution_reason="legacy non-default base_url normalized as Ollama-compatible target",
            attempt=attempt,
            source="explicit_base_url",
            location=base_url,
            runtime="external_server",
            provider="ollama_compatible",
            config_ref="legacy:explicit_base_url",
            target=target,
        )

    entry = node_config.get_model_entry(model)
    if entry is not None:
        backend = entry.get("backend")
        if backend == "llamacpp":
            adapter = adapters.get_adapter("llama_cpp")
            spec = llamacpp_backend.ModelSpec(
                path=entry["path"],
                n_ctx=entry.get("n_ctx", 8192),
                n_gpu_layers=entry.get("n_gpu_layers", -1),
            )
            target = {"spec": spec, "location": entry["path"], "runtime": "llama_cpp"}
            return ResolvedInferenceTarget(
                logical_model=model,
                resolved_model=model,
                adapter_id=adapter.adapter_id,
                adapter=adapter,
                capabilities=adapter.capabilities(target),
                resolution_reason="legacy explicit node config normalized from backend=llamacpp",
                attempt=attempt,
                source="explicit_config",
                location=entry["path"],
                runtime="llama_cpp",
                provider="local",
                config_ref=f"secure_store:{model}",
                target=target,
            )
        if backend == "remote":
            protocol = entry.get("protocol", "openai")
            if protocol == "openai":
                adapter_id = "openai_compatible"
            elif protocol == "anthropic":
                adapter_id = "anthropic"
            else:
                raise RuntimeError(f"неизвестный remote protocol {protocol!r} в настройке узла для модели {model!r}")
            adapter = adapters.get_adapter(adapter_id)
            target = {
                "base_url": entry["base_url"],
                "provider": protocol,
                "runtime": "external_server",
                "api_key_env": entry.get("api_key_env"),
            }
            provider = "anthropic" if protocol == "anthropic" else "openai_compatible"
            runtime = "provider" if protocol == "anthropic" else "external_server"
            return ResolvedInferenceTarget(
                logical_model=model,
                resolved_model=entry.get("model", model),
                adapter_id=adapter.adapter_id,
                adapter=adapter,
                capabilities=adapter.capabilities(target),
                resolution_reason=f"legacy explicit node config normalized from backend=remote protocol={protocol}",
                attempt=attempt,
                source="explicit_config",
                location=entry["base_url"],
                runtime=runtime,
                provider=provider,
                config_ref=f"secure_store:{model}",
                target=target,
            )
        raise RuntimeError(f"неизвестный backend {backend!r} в настройке узла для модели {model!r}")

    if _LOCAL_ENABLED and llamacpp_backend.has_model(model):
        adapter = adapters.get_adapter("llama_cpp")
        target = {"runtime": "llama_cpp", "registry": "builtin"}
        return ResolvedInferenceTarget(
            logical_model=model,
            resolved_model=model,
            adapter_id=adapter.adapter_id,
            adapter=adapter,
            capabilities=adapter.capabilities(target),
            resolution_reason="no explicit config; local engine enabled and builtin registry has model",
            attempt=attempt,
            source="builtin_registry",
            runtime="llama_cpp",
            provider="local",
            config_ref="builtin_registry",
            fallback_allowed=True,
            target=target,
        )

    adapter = adapters.get_adapter("ollama_compatible")
    target = {"base_url": base_url}
    return ResolvedInferenceTarget(
        logical_model=model,
        resolved_model=model,
        adapter_id=adapter.adapter_id,
        adapter=adapter,
        capabilities=adapter.capabilities(target),
        resolution_reason="no explicit config and no builtin local candidate; legacy Ollama-compatible fallback",
        attempt=attempt,
        source="ollama_fallback",
        location=base_url,
        runtime="external_server",
        provider="ollama_compatible",
        config_ref="legacy:ollama_fallback",
        target=target,
    )


def _location_kind(location: str | None) -> str:
    if not location:
        return "none"
    if location.startswith(("http://", "https://")):
        return "url"
    return "file"


def _trace_attempt(
    target: ResolvedInferenceTarget, contract: OutputContract, *, result: str, error: Exception | None = None,
) -> dict:
    item: dict[str, object] = {
        "attempt": target.attempt,
        "logical_model": target.logical_model,
        "resolved_model": target.resolved_model,
        "adapter_id": target.adapter_id,
        "runtime": target.runtime,
        "provider": target.provider,
        "location_kind": _location_kind(target.location),
        "source": target.source,
        "resolution_reason": target.resolution_reason,
        "fallback_reason": target.fallback_reason,
        "capabilities": {
            "plain_text": target.capabilities.plain_text,
            "json_object": target.capabilities.json_object,
            "json_schema": target.capabilities.json_schema,
            "streaming": target.capabilities.streaming,
        },
        "output_contract": contract.name,
        "result": result,
    }
    if error is not None:
        item["error_class"] = type(error).__name__
        item["error"] = str(error)
    return item


def _generate_with_target(
    target: ResolvedInferenceTarget,
    prompt: str | None,
    *,
    system: str | list[str] | None,
    messages: list[dict[str, str]] | None,
    temperature: float | None,
    max_tokens: int | None,
    timeout: int,
    extra_options: dict[str, object] | None,
    contract: OutputContract,
    stop: list[str] | None,
) -> tuple[str, dict]:
    """Generate through the already resolved inference target."""
    wire_messages = _build_messages(prompt, system, messages)
    request = GenerationRequest(
        messages=wire_messages,
        model=target.resolved_model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        extra_options=extra_options,
        stop=stop,
    )
    try:
        result = target.adapter.generate(request, target.target or {}, contract)
    except ollama_backend.OllamaBackendError as e:
        raise LLMError(str(e)) from e

    if target.source == "explicit_config" and not (isinstance(result, tuple) and len(result) == 2):
        raise RuntimeError(
            f"adapter {target.adapter_id!r}, настроенный владельцем узла для модели {target.logical_model!r}, "
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
    ли владелец узла explicit inference target для точного имени модели
    (resolve_target()). Если настроил: успех — используем как
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
    if node_gateway.enabled():
        # шлюз узла (Rust): настройки владельца и собственный движок живут там; локальный Python-путь не задействуется и не служит откатом
        res = node_gateway.call(
            "gw_complete",
            dict(prompt=prompt, model=model, system=system, messages=messages, temperature=temperature, max_tokens=max_tokens,
                 timeout=timeout, base_url=base_url, strip_think=strip_think, extra_options=extra_options,
                 response_format=response_format, stop=stop),
            timeout=timeout,
        )
        if "error" in res:
            raise LLMError(res["error"]["msg"])
        return res["ok"]["text"], res["ok"]["raw"]
    if prompt is None and messages is None:
        raise LLMError("complete() требует либо prompt, либо messages — ни один не задан")
    if prompt is not None and messages is not None:
        raise LLMError("complete() принимает либо prompt, либо messages, но не оба сразу")

    trace: list[dict] = []
    contract = _contract_from_response_format(response_format)

    try:
        target = resolve_target(model, base_url=base_url, attempt=1)
    except Exception as e:
        raise LLMError(
            f"настроенный владельцем узла backend для модели {model!r} "
            f"не сработал — автоматический переход на другой источник "
            f"интеллекта запрещён явным выбором владельца: {e}"
        ) from e

    try:
        text, raw = _generate_with_target(
            target, prompt, system=system, messages=messages, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, extra_options=extra_options,
            contract=contract, stop=stop,
        )
        trace.append(_trace_attempt(target, contract, result="success"))
    except Exception as e:
        trace.append(_trace_attempt(target, contract, result="failed", error=e))
        if target.source == "explicit_config":
            # CONFIGURED_TARGET_FAILED — владелец узла явно выбрал этот
            # target для этой модели. Автоматический переход на другой
            # источник интеллекта здесь запрещён категорически.
            raise LLMError(
                f"настроенный владельцем узла backend для модели {model!r} "
                f"не сработал — автоматический переход на другой источник "
                f"интеллекта запрещён явным выбором владельца: {e}"
            ) from e
        if not target.fallback_allowed:
            raise

        print(f"[llm_gateway] встроенный дефолт не справился с {model!r} ({e}), откат на Ollama")
        fallback = resolve_target(model, base_url=base_url, attempt=target.attempt + 1, fallback_from=target)
        fallback_contract = _contract_from_response_format(response_format)
        try:
            text, raw = _generate_with_target(
                fallback, prompt, system=system, messages=messages, temperature=temperature,
                max_tokens=max_tokens, timeout=timeout, extra_options=extra_options,
                contract=fallback_contract, stop=stop,
            )
            trace.append(_trace_attempt(fallback, fallback_contract, result="success"))
        except Exception as fallback_error:
            trace.append(_trace_attempt(fallback, fallback_contract, result="failed", error=fallback_error))
            raise

    if strip_think:
        text = _THINK_TAG_RE.sub("", text)
    raw = dict(raw)
    raw["_llm_gateway_trace"] = trace
    return text.strip(), raw


def complete_semantic(
    prompt: str | None = None,
    *,
    model: str,
    requirement: SemanticOutputRequirement,
    system: str | list[str] | None = None,
    messages: list[dict[str, str]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    base_url: str = DEFAULT_BASE_URL,
    extra_options: dict[str, object] | None = None,
    stop: list[str] | None = None,
) -> SemanticCompletionResult:
    """Generate a semantic result while keeping transport details in the gateway.

    The caller states the semantic need (currently reply + internal
    state). The gateway resolves the concrete inference target, chooses
    an output contract from that target's capabilities, generates on
    the same target, and normalizes the response before returning it.
    """
    if node_gateway.enabled():
        import dataclasses
        res = node_gateway.call(
            "gw_complete_semantic",
            dict(prompt=prompt, model=model, requirement=dataclasses.asdict(requirement), system=system, messages=messages,
                 temperature=temperature, max_tokens=max_tokens, timeout=timeout, base_url=base_url,
                 extra_options=extra_options, stop=stop),
            timeout=timeout,
        )
        if "error" in res:
            raise LLMError(res["error"]["msg"])
        return SemanticCompletionResult(**res["ok"])
    if prompt is None and messages is None:
        raise LLMError("complete_semantic() требует либо prompt, либо messages — ни один не задан")
    if prompt is not None and messages is not None:
        raise LLMError("complete_semantic() принимает либо prompt, либо messages, но не оба сразу")

    trace: list[dict] = []
    try:
        target = resolve_target(model, base_url=base_url, attempt=1)
    except Exception as e:
        raise LLMError(
            f"настроенный владельцем узла backend для модели {model!r} "
            f"не сработал — автоматический переход на другой источник "
            f"интеллекта запрещён явным выбором владельца: {e}"
        ) from e

    contract, instruction = _semantic_contract_from_target(requirement, target)
    semantic_system = _append_system_instruction(system, instruction)
    try:
        text, raw = _generate_with_target(
            target, prompt, system=semantic_system, messages=messages, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, extra_options=extra_options,
            contract=contract, stop=stop,
        )
        trace.append(_trace_attempt(target, contract, result="success"))
    except Exception as e:
        trace.append(_trace_attempt(target, contract, result="failed", error=e))
        if target.source == "explicit_config":
            raise LLMError(
                f"настроенный владельцем узла backend для модели {model!r} "
                f"не сработал — автоматический переход на другой источник "
                f"интеллекта запрещён явным выбором владельца: {e}"
            ) from e
        if not target.fallback_allowed:
            raise

        print(f"[llm_gateway] встроенный дефолт не справился с {model!r} ({e}), откат на Ollama")
        fallback = resolve_target(model, base_url=base_url, attempt=target.attempt + 1, fallback_from=target)
        fallback_contract, fallback_instruction = _semantic_contract_from_target(requirement, fallback)
        fallback_system = _append_system_instruction(system, fallback_instruction)
        try:
            text, raw = _generate_with_target(
                fallback, prompt, system=fallback_system, messages=messages, temperature=temperature,
                max_tokens=max_tokens, timeout=timeout, extra_options=extra_options,
                contract=fallback_contract, stop=stop,
            )
            trace.append(_trace_attempt(fallback, fallback_contract, result="success"))
            contract = fallback_contract
        except Exception as fallback_error:
            trace.append(_trace_attempt(fallback, fallback_contract, result="failed", error=fallback_error))
            raise

    raw = dict(raw)
    raw["_llm_gateway_trace"] = trace
    return _normalize_semantic_completion(text, requirement=requirement, contract=contract, metadata=raw)


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
    if node_gateway.enabled():
        res = node_gateway.call("gw_embed", dict(texts=text_list, model=model, base_url=base_url, timeout=timeout), timeout=timeout)
        if "error" in res:
            raise EmbedError(res["error"]["msg"])
        return EmbeddingResult(vectors=res["ok"]["vectors"], space=vector_space.VectorSpaceId(**{k: v for k, v in res["ok"]["space"].items() if k != "fingerprint"}))

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
