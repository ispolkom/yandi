"""
llm_gateway.config — публичный вход к настройке модели узла.

Реальное хранилище — llm_gateway.secure_store (зашифрованный, вставить-
или-стереть-но-никогда-не-изменить локальный SQLite, см. его собственный
докстринг). Этот модуль — тонкая, стабильная точка входа для остального
кода (client.py, setup.py) поверх него: сигнатуры функций специально не
меняются при смене реализации хранилища, чтобы такие смены не требовали
трогать вызывающий код.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import secure_store as _store


def config_path() -> Path:
    """Где физически лежит база настроек узла (для показа пользователю
    в setup.py — не для ручного редактирования, редактировать её нельзя
    даже владельцу узла)."""
    return _store.db_path()


def load_config() -> dict[str, Any]:
    """Совместимость со старым JSON-форматом (`{"models": {...}}`) для
    любого кода, который ожидал именно такую форму."""
    return {"models": _store.list_models()}


def get_model_entry(model: str) -> dict[str, Any] | None:
    return _store.get_model_entry(model)


def set_model_entry(model: str, entry: dict[str, Any]) -> None:
    """Создаёт НОВУЮ запись. Если под этим именем уже что-то настроено
    — бросает secure_store.SecureStoreError, а не переписывает: явное
    удаление (remove_model_entry) обязательно первым шагом, изменение
    "на месте" не существует как операция."""
    _store.set_model_entry(model, entry)


def remove_model_entry(model: str) -> bool:
    return _store.remove_model_entry(model)


def list_models() -> dict[str, Any]:
    return _store.list_models()
