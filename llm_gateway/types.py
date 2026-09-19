"""Shared llm_gateway transport types.

This module is intentionally neutral: adapters and the central client may
both import it without creating a dependency cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BackendCapabilities:
    """Transport-level guarantees of one resolved inference target."""

    plain_text: bool = True
    json_object: bool = False
    json_schema: bool = False
    streaming: bool = False


@dataclass(frozen=True)
class OutputContract:
    """Selected output transport for one generation attempt."""

    name: str
    response_format: Any | None


@dataclass(frozen=True)
class GenerationRequest:
    """Backend-neutral generation request handed to an adapter."""

    messages: list[dict[str, str]]
    model: str
    temperature: float | None
    max_tokens: int | None
    timeout: int
    extra_options: dict[str, object] | None = None
    stop: list[str] | None = None


@dataclass(frozen=True)
class ResolvedInferenceTarget:
    """One concrete inference target selected for one generation attempt."""

    logical_model: str
    resolved_model: str
    adapter_id: str
    adapter: Any
    capabilities: BackendCapabilities
    resolution_reason: str
    attempt: int
    source: str
    location: str | None = None
    runtime: str | None = None
    provider: str | None = None
    config_ref: str | None = None
    fallback_allowed: bool = False
    fallback_reason: str | None = None
    target: dict[str, object] | None = None
