"""Inference adapters and static registry for llm_gateway."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from . import llamacpp_backend
from . import ollama_backend
from . import remote_backend
from .types import BackendCapabilities, GenerationRequest, OutputContract


class InferenceAdapter(Protocol):
    """Transport/runtime-specific generation adapter."""

    adapter_id: str

    def capabilities(self, target: dict[str, object]) -> BackendCapabilities:
        ...

    def generate(
        self,
        request: GenerationRequest,
        target: dict[str, object],
        contract: OutputContract,
    ) -> tuple[str, dict]:
        ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._items: dict[str, InferenceAdapter] = {}

    def register(self, adapter: InferenceAdapter) -> None:
        if adapter.adapter_id in self._items:
            raise ValueError(f"adapter {adapter.adapter_id!r} already registered")
        self._items[adapter.adapter_id] = adapter

    def get(self, adapter_id: str) -> InferenceAdapter:
        try:
            return self._items[adapter_id]
        except KeyError as e:
            raise KeyError(f"unknown inference adapter {adapter_id!r}") from e

    def list_ids(self) -> list[str]:
        return sorted(self._items)


@dataclass(frozen=True)
class LlamaCppAdapter:
    adapter_id: str = "llama_cpp"

    def capabilities(self, target: dict[str, object]) -> BackendCapabilities:
        return BackendCapabilities(json_object=True, json_schema=False)

    def generate(
        self,
        request: GenerationRequest,
        target: dict[str, object],
        contract: OutputContract,
    ) -> tuple[str, dict]:
        spec = target.get("spec")
        if isinstance(spec, llamacpp_backend.ModelSpec):
            return llamacpp_backend.generate_at_spec(
                request.messages,
                spec=spec,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                response_format=contract.response_format,
                extra_options=request.extra_options,
                stop=request.stop,
            )
        return llamacpp_backend.generate(
            request.messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            response_format=contract.response_format,
            extra_options=request.extra_options,
            stop=request.stop,
        )


@dataclass(frozen=True)
class OllamaCompatAdapter:
    adapter_id: str = "ollama_compatible"

    def capabilities(self, target: dict[str, object]) -> BackendCapabilities:
        return BackendCapabilities(json_object=True, json_schema=True)

    def generate(
        self,
        request: GenerationRequest,
        target: dict[str, object],
        contract: OutputContract,
    ) -> tuple[str, dict]:
        return ollama_backend.generate(
            request.messages,
            model=request.model,
            base_url=str(target["base_url"]),
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            timeout=request.timeout,
            extra_options=request.extra_options,
            response_format=contract.response_format,
            stop=request.stop,
        )


@dataclass(frozen=True)
class OpenAICompatibleAdapter:
    adapter_id: str = "openai_compatible"

    def capabilities(self, target: dict[str, object]) -> BackendCapabilities:
        return BackendCapabilities(json_object=True, json_schema=False)

    def generate(
        self,
        request: GenerationRequest,
        target: dict[str, object],
        contract: OutputContract,
    ) -> tuple[str, dict]:
        return remote_backend.generate(
            request.messages,
            base_url=str(target["base_url"]),
            protocol="openai",
            model=request.model,
            api_key_env=target.get("api_key_env"),
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            response_format=contract.response_format,
            stop=request.stop,
            timeout=request.timeout,
        )


@dataclass(frozen=True)
class AnthropicAdapter:
    adapter_id: str = "anthropic"

    def capabilities(self, target: dict[str, object]) -> BackendCapabilities:
        return BackendCapabilities(json_object=False, json_schema=False)

    def generate(
        self,
        request: GenerationRequest,
        target: dict[str, object],
        contract: OutputContract,
    ) -> tuple[str, dict]:
        return remote_backend.generate(
            request.messages,
            base_url=str(target["base_url"]),
            protocol="anthropic",
            model=request.model,
            api_key_env=target.get("api_key_env"),
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            response_format=contract.response_format,
            stop=request.stop,
            timeout=request.timeout,
        )


DEFAULT_REGISTRY = AdapterRegistry()
DEFAULT_REGISTRY.register(LlamaCppAdapter())
DEFAULT_REGISTRY.register(OllamaCompatAdapter())
DEFAULT_REGISTRY.register(OpenAICompatibleAdapter())
DEFAULT_REGISTRY.register(AnthropicAdapter())


def get_adapter(adapter_id: str) -> InferenceAdapter:
    return DEFAULT_REGISTRY.get(adapter_id)


def list_adapter_ids() -> list[str]:
    return DEFAULT_REGISTRY.list_ids()
