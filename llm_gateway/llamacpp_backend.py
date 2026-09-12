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

# Embedding-режим требует Llama(embedding=True) на конструкторе —
# нельзя переиспользовать инстанс, загруженный для чата (_loaded выше);
# та же модель загружается второй раз, отдельно, только когда реально
# понадобился embed_at_spec() для этого пути. Дороже по памяти, но
# llama.cpp не позволяет переключать embedding-режим у уже созданного
# контекста.
_loaded_embed: dict[str, "Llama"] = {}


def registry_error() -> str | None:
    """Почему локальный движок недоступен прямо сейчас, если недоступен."""
    if Llama is None:
        return f"llama_cpp не установлен или не импортируется: {_import_error}"
    return None


def has_model(model: str) -> bool:
    spec = _MODEL_REGISTRY.get(model)
    return spec is not None and Path(spec.path).exists()


def list_builtin_models() -> list[str]:
    """Имена встроенного дефолтного реестра — для llm_gateway.list_models()."""
    return list(_MODEL_REGISTRY.keys())


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
    messages: list[dict[str, str]],
    *,
    model: str,
    temperature: float | None,
    max_tokens: int | None,
    response_format: str | None,
    stop: list[str] | None = None,
    extra_options: dict[str, object] | None = None,
) -> tuple[str, dict]:
    """Встроенный дефолт: генерация по алиасу из _MODEL_REGISTRY. Для
    модели, которую владелец узла настроил сам (своя папка, свой файл —
    см. llm_gateway.config), используется generate_at_spec() напрямую с
    его путём, эта функция её не знает. messages — уже готовый wire-
    формат (client.py._build_messages()), не prompt/system по отдельности."""
    spec = _MODEL_REGISTRY.get(model)
    if spec is None:
        raise RuntimeError(f"нет встроенной GGUF-записи для модели {model!r}")
    return generate_at_spec(
        messages, spec=spec, temperature=temperature,
        max_tokens=max_tokens, response_format=response_format,
        extra_options=extra_options, stop=stop,
    )


def generate_at_spec(
    messages: list[dict[str, str]],
    *,
    spec: ModelSpec,
    temperature: float | None,
    max_tokens: int | None,
    response_format: str | None,
    stop: list[str] | None = None,
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

    kwargs: dict[str, object] = dict(extra_options) if extra_options else {}
    # repeat_last_n — НЕ параметр create_chat_completion() в llama-cpp-
    # python (проверено интроспекцией сигнатуры) — только конструктора
    # (last_n_tokens_size), а его библиотечный дефолт уже 64 (то самое
    # значение, что исторически запрашивал pet/chat_local.py), и не
    # может меняться per-call для уже загруженного инстанса без
    # перезагрузки модели. Явно выкидываем этот ОДИН конкретный ключ,
    # задокументированно и безопасно (значение и так совпадает), а не
    # даём create_chat_completion() упасть с TypeError на неизвестном
    # kwarg — это не общая политика "тихо не поддерживаем", это разбор
    # одного конкретного, проверенного случая.
    kwargs.pop("repeat_last_n", None)
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if response_format == "json":
        kwargs["response_format"] = {"type": "json_object"}
    if stop:
        kwargs["stop"] = list(stop)

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


def _get_or_load_embed(spec: ModelSpec) -> "Llama":
    with _lock:
        llm = _loaded_embed.get(spec.path)
        if llm is None:
            llm = Llama(
                model_path=spec.path,
                n_ctx=spec.n_ctx,
                n_gpu_layers=spec.n_gpu_layers,
                embedding=True,
                verbose=False,
            )
            _loaded_embed[spec.path] = llm
        return llm


def embed_at_spec(texts: list[str], *, spec: ModelSpec) -> tuple[list[list[float]], dict]:
    """Embeddings по явному ModelSpec — та же модель, что и для
    completion, но загруженная ОТДЕЛЬНЫМ инстансом в embedding-режиме
    (llama.cpp не позволяет переключить это у уже созданного
    контекста). Бросает исключение при любой проблеме — client.py
    решает, как деградировать, этот модуль ничего не скрывает."""
    if Llama is None:
        raise RuntimeError(f"llama_cpp недоступен: {_import_error}")
    if not Path(spec.path).exists():
        raise RuntimeError(f"GGUF-файл не найден: {spec.path}")
    if not texts:
        raise RuntimeError("embed_at_spec() вызван с пустым списком текстов")

    llm = _get_or_load_embed(spec)

    with _lock:
        # normalize=False — гейтвей не трогает содержимое вектора, та же
        # дисциплина, что и у remote/Ollama путей (см. vector_space.py).
        vectors = llm.embed(list(texts), normalize=False)

    if not isinstance(vectors, list) or not vectors or not isinstance(vectors[0], list):
        raise RuntimeError(
            f"llama_cpp.Llama.embed() вернул неожиданную форму для батча из {len(texts)} "
            f"текстов: {type(vectors)!r}"
        )

    dimension = len(vectors[0])
    return vectors, {"dimension": dimension, "normalized": False}
