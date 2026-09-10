"""
llm_gateway.llamacpp_backend — Phase 2/3 of the Ollama-decoupling plan
(см. память ollama-decoupling-plan): собственный движок инференса на
базе llama.cpp, читающий те же GGUF-веса, что уже скачаны для Ollama
(найдены на диске напрямую — Ollama и llama.cpp это по сути один и тот
же движок под капотом, просто разные обвязки сверху).

Этот модуль НЕ знает про Ollama и не должен — client.py решает, когда
его использовать и когда откатываться на HTTP-бэкенд.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

_import_error: Exception | None = None
try:
    from llama_cpp import Llama
except Exception as e:  # pragma: no cover - depends on optional native package
    Llama = None  # type: ignore[assignment]
    _import_error = e


@dataclass(frozen=True)
class ModelSpec:
    path: str
    n_ctx: int = 8192
    n_gpu_layers: int = -1  # -1 = выгрузить все слои на GPU


# Единственная реальная рабочая модель во всей agent/ — все её алиасы
# (heretic:q8, qwen3:14b, qwen9b:q8) по `ollama list` указывают на один
# и тот же ID (aa9dba9b5610) — то есть буквально одни и те же веса под
# разными именами в Modelfile. GGUF-файл найден напрямую на диске
# (совпадает по размеру и имени с алиасом hf.co/.../HERETIC...Q8_0).
_MODEL_REGISTRY: dict[str, ModelSpec] = {
    name: ModelSpec(
        path="/mnt/backup/models/Qwen3.5-9B-Claude-4.6-HighIQ-INSTRUCT-HERETIC-UNCENSORED.Q8_0.gguf",
    )
    for name in ("heretic:q8", "qwen3:14b", "qwen9b:q8")
}

_lock = threading.Lock()
_loaded: dict[str, "Llama"] = {}  # keyed by resolved file path, not alias


def registry_error() -> str | None:
    """Почему локальный движок недоступен прямо сейчас, если недоступен."""
    if Llama is None:
        return f"llama_cpp не установлен или не импортируется: {_import_error}"
    return None


def has_model(model: str) -> bool:
    spec = _MODEL_REGISTRY.get(model)
    return spec is not None and Path(spec.path).exists()


def _get_or_load(spec: ModelSpec) -> "Llama":
    with _lock:
        llm = _loaded.get(spec.path)
        if llm is None:
            llm = Llama(
                model_path=spec.path,
                n_ctx=spec.n_ctx,
                n_gpu_layers=spec.n_gpu_layers,
                verbose=False,
            )
            _loaded[spec.path] = llm
        return llm


def generate(
    prompt: str,
    *,
    model: str,
    system: str | None,
    temperature: float | None,
    max_tokens: int | None,
    response_format: str | None,
    extra_options: dict[str, object] | None = None,
) -> tuple[str, dict]:
    """Встроенный дефолт: генерация по алиасу из _MODEL_REGISTRY. Для
    модели, которую владелец узла настроил сам (своя папка, свой файл —
    см. llm_gateway.config), используется generate_at_spec() напрямую с
    его путём, эта функция её не знает."""
    spec = _MODEL_REGISTRY.get(model)
    if spec is None:
        raise RuntimeError(f"нет встроенной GGUF-записи для модели {model!r}")
    return generate_at_spec(
        prompt, spec=spec, system=system, temperature=temperature,
        max_tokens=max_tokens, response_format=response_format,
        extra_options=extra_options,
    )


def generate_at_spec(
    prompt: str,
    *,
    spec: ModelSpec,
    system: str | None,
    temperature: float | None,
    max_tokens: int | None,
    response_format: str | None,
    extra_options: dict[str, object] | None = None,
) -> tuple[str, dict]:
    """То же самое, что generate(), но по явному ModelSpec, а не по
    имени из встроенного реестра — то, что реально вызывается и для
    дефолтных алиасов, и для моделей, настроенных владельцем узла
    (llm_gateway.config), одной и той же логикой загрузки/генерации.
    Бросает исключение при любой проблеме — client.py решает,
    откатываться на Ollama или нет, сам этот модуль ничего не скрывает
    и не подставляет fallback.
    """
    if Llama is None:
        raise RuntimeError(f"llama_cpp недоступен: {_import_error}")
    if not Path(spec.path).exists():
        raise RuntimeError(f"GGUF-файл не найден: {spec.path}")

    llm = _get_or_load(spec)

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs: dict[str, object] = dict(extra_options) if extra_options else {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if response_format == "json":
        kwargs["response_format"] = {"type": "json_object"}

    # Один Llama-контекст не потокобезопасен для одновременной генерации
    # — тот же дисциплинированный подход, что GENERATION_SEMAPHORE уже
    # применяет к Ollama в нескольких файлах agent/.
    with _lock:
        result = llm.create_chat_completion(messages=messages, **kwargs)

    choice = result["choices"][0]
    text = choice["message"]["content"] or ""
    finish_reason = choice.get("finish_reason")
    usage = result.get("usage") or {}

    return text, {
        "done_reason": "length" if finish_reason == "length" else "stop",
        "eval_count": usage.get("completion_tokens"),
    }
