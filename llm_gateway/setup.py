"""
llm_gateway.setup — интерактивный выбор модели для ЭТОЙ ноды.

Запуск вручную в любой момент:
    python3 -m llm_gateway.setup

Скрипты запуска ноды (start.sh/start_headless.sh) сами вызывают
maybe_prompt_first_run() — если конфиг уже есть, или если это
неинтерактивный запуск (например systemd без TTY), ничего не
спрашивает и не блокирует старт: нода просто продолжает работать на
встроенном дефолте/Ollama, как и раньше. Спрашивает только тогда,
когда СПОСОБНА спросить и когда пока нечего использовать как выбор
пользователя.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import config as cfg


def discover_gguf_files(directory: Path) -> list[Path]:
    """Файлы моделей в указанной пользователем папке — не глубже одного
    уровня вложенности, чтобы не тралить весь диск, если человек указал
    что-то слишком общее."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.gguf")) + sorted(directory.glob("*/*.gguf"))


def build_local_entry(path: Path, n_ctx: int = 8192, n_gpu_layers: int = -1) -> dict[str, Any]:
    return {
        "backend": "llamacpp",
        "path": str(path),
        "n_ctx": n_ctx,
        "n_gpu_layers": n_gpu_layers,
    }


def build_remote_entry(protocol: str, base_url: str, model: str, api_key_env: str) -> dict[str, Any]:
    return {
        "backend": "remote",
        "protocol": protocol,
        "base_url": base_url,
        "model": model,
        "api_key_env": api_key_env,
    }


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or (default or "")


def _interactive_add_model() -> None:
    alias = _ask("Имя, под которым код будет запрашивать эту модель (например 'heretic:q8' — так вызывается сейчас в коде, или своё)")
    if not alias:
        print("Пустое имя — отменено.")
        return

    if cfg.get_model_entry(alias) is not None:
        print(f"Под именем '{alias}' уже что-то настроено.")
        print("Изменить на месте нельзя — только удалить и настроить заново.")
        answer = _ask("Удалить старую запись и настроить заново? (да/нет)", "нет")
        if answer.lower() not in ("да", "yes", "y", "д"):
            print("Отменено, старая запись осталась без изменений.")
            return
        cfg.remove_model_entry(alias)
        print("Старая запись удалена. Настраиваем заново.")

    print("Откуда модель?")
    print("  1) Папка на диске (локальный файл .gguf)")
    print("  2) Удалённый сервер по API (свой Клод/OpenAI/self-hosted)")
    choice = _ask("Выбор", "1")

    if choice == "2":
        protocol = _ask("Протокол: 'openai' (большинство серверов) или 'anthropic' (родной Клод)", "openai")
        base_url = _ask("Адрес сервера (например https://api.anthropic.com)")
        remote_model = _ask("Имя модели на стороне сервера (например claude-sonnet-5 или gpt-...)")
        api_key_env = _ask("Имя переменной окружения с ключом (сам ключ сюда не пишем — только имя переменной)", "YANDI_MODEL_API_KEY")
        if not base_url or not remote_model:
            print("Адрес и имя модели обязательны — отменено.")
            return
        cfg.set_model_entry(alias, build_remote_entry(protocol, base_url, remote_model, api_key_env))
        print(f"Готово: '{alias}' -> {protocol} @ {base_url} (ключ берётся из ${api_key_env})")
        return

    dir_str = _ask("Папка, где лежит файл модели (любой диск, не обязательно системный)")
    directory = Path(dir_str).expanduser()
    found = discover_gguf_files(directory)
    if not found:
        print(f"В {directory} не нашёл файлов .gguf — проверь путь.")
        return

    if len(found) == 1:
        picked = found[0]
    else:
        print("Нашёл несколько файлов:")
        for i, f in enumerate(found, 1):
            size_gb = f.stat().st_size / (1024 ** 3)
            print(f"  {i}) {f.name} ({size_gb:.1f} ГБ)")
        idx = _ask("Какой использовать (номер)", "1")
        try:
            picked = found[int(idx) - 1]
        except (ValueError, IndexError):
            print("Некорректный выбор — отменено.")
            return

    cfg.set_model_entry(alias, build_local_entry(picked))
    print(f"Готово: '{alias}' -> {picked}")


def main() -> int:
    print("=== Настройка модели узла YANDI ===")
    print(f"База настроек (зашифрована, редактировать вручную нельзя): {cfg.config_path()}")
    existing = cfg.list_models()
    if existing:
        print("\nУже настроено:")
        for name, entry in existing.items():
            if entry.get("backend") == "llamacpp":
                print(f"  {name}: локально, {entry.get('path')}")
            else:
                print(f"  {name}: удалённо, {entry.get('protocol')} @ {entry.get('base_url')}")
    else:
        print("\nПока ничего не настроено — будет использован встроенный дефолт/Ollama.")

    print()
    while True:
        print("1) Добавить модель (или заменить существующую — удалить и настроить заново)")
        print("2) Удалить модель")
        print("3) Выход")
        choice = _ask("Выбор", "3")
        if choice == "1":
            _interactive_add_model()
        elif choice == "2":
            name = _ask("Какое имя удалить")
            print("Удалено." if cfg.remove_model_entry(name) else "Такого имени нет.")
        else:
            break
        print()

    return 0


def maybe_prompt_first_run() -> None:
    """Безопасный вызов из start.sh/start_headless.sh/демона: спрашивает
    только если есть с кем говорить (реальный терминал) И пока ничего не
    настроено. Никогда не блокирует headless/systemd-запуск — там просто
    печатает подсказку и едет дальше на дефолте."""
    if cfg.list_models():
        return
    if not sys.stdin.isatty():
        print("[llm_gateway] Модель узла не настроена явно — используется встроенный дефолт/Ollama.")
        print("[llm_gateway] Настроить свою: python3 -m llm_gateway.setup")
        return
    print("[llm_gateway] Модель узла ещё не настроена.")
    answer = _ask("Настроить сейчас? (да/нет)", "нет")
    if answer.lower() in ("да", "yes", "y", "д"):
        main()


if __name__ == "__main__":
    sys.exit(main())
