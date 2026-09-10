"""
llm_gateway.config — per-node model configuration. Одна нода — один
свой выбор модели (или несколько), сделанный владельцем ноды, а не
зашитый в код. Файл настроек живёт ВНЕ репозитория (в домашней папке
пользователя) — это личная настройка конкретной машины, не то, что
должно лежать в git рядом с кодом.

Формат конфига (JSON):
{
  "models": {
    "<имя, под которым код запрашивает модель>": {
      "backend": "llamacpp",
      "path": "/путь/к/файлу.gguf",
      "n_ctx": 8192,          // необязательно
      "n_gpu_layers": -1      // необязательно
    },
    "<другое имя>": {
      "backend": "remote",
      "protocol": "openai" | "anthropic",
      "base_url": "https://...",
      "model": "имя модели на стороне сервера",
      "api_key_env": "MY_API_KEY"   // имя переменной окружения с ключом,
                                    // НЕ сам ключ — ключ в конфиг не пишем
    }
  }
}

Настройка через llm_gateway/setup.py (интерактивно) или руками —
формат простой и человекочитаемый специально для этого.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "yandi" / "llm_models.json"


def config_path() -> Path:
    override = os.environ.get("YANDI_LLM_CONFIG")
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {"models": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"models": {}}
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return {"models": {}}
    return data


def save_config(data: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_model_entry(model: str) -> dict[str, Any] | None:
    """Настройка узла для этого имени модели, если пользователь её
    задал. None, если нет записи — вызывающий код (client.py) сам
    решает, что делать дальше (встроенный дефолт или Ollama)."""
    return load_config().get("models", {}).get(model)


def set_model_entry(model: str, entry: dict[str, Any]) -> None:
    data = load_config()
    data.setdefault("models", {})[model] = entry
    save_config(data)


def remove_model_entry(model: str) -> bool:
    data = load_config()
    removed = data.get("models", {}).pop(model, None) is not None
    if removed:
        save_config(data)
    return removed


def list_models() -> dict[str, Any]:
    return load_config().get("models", {})
