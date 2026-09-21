"""
pet/voice.py — Голос из вкладки «YANDI» становится тем, кто отвечает: настройка перестаёт быть только записью в файл.

    сохранить настройки  ->  register/apply: файл .gguf регистрируется в шлюзе этой ноды под именем «yandi-voice»
    ответ Помощницы      ->  effective_model(): пока Голос — локальная модель и она зарегистрирована, отвечает ОНА

Подключён ТОЛЬКО локальный Голос. Удалённая модель и API-сервис сохраняются как выбор, но честно помечены «не применено»:
им нужны ключи и ворота «наружу выходит только вопрос» (docs/…), это отдельный шаг. Ничего не подставляется молча: если файл
пропал или движок не загрузит его, шлюз скажет это ошибкой (ответ модели не подменяется другой).

Регистрация идёт через llm_gateway.config (зашифрованное хранилище узла); запись под одним именем не правится «на месте» —
старая удаляется, новая создаётся. Уже загруженные в память модели ключуются путём файла (llamacpp_backend._loaded), поэтому смена
файла загружает новый; предыдущий остаётся в памяти до перезапуска (ограничение, записано в docs/WEB_UI_VOICE.md).
"""
from __future__ import annotations

from typing import Any

from pet import ui_settings

VOICE_ALIAS = "yandi-voice"


class VoiceError(Exception):
    """Голос выбран, но применить его нельзя (файл, шлюз). Текст безопасен для показа."""


def _registered_path() -> str | None:
    from llm_gateway import config as cfg
    entry = cfg.get_model_entry(VOICE_ALIAS)
    if entry and entry.get("backend") == "llamacpp":
        return str(entry.get("path") or "") or None
    return None


def register_local(path: str) -> str:
    """Сделать файл `path` моделью под именем VOICE_ALIAS. Возвращает имя файла."""
    from llm_gateway import config as cfg
    from llm_gateway.setup import build_local_entry
    from pet.chat_models import _check_gguf
    checked, error = _check_gguf(path)
    if error:
        raise VoiceError(error)
    if _registered_path() == str(checked):
        return checked.name
    try:
        if cfg.get_model_entry(VOICE_ALIAS) is not None:
            cfg.remove_model_entry(VOICE_ALIAS)
        cfg.set_model_entry(VOICE_ALIAS, build_local_entry(checked))
    except Exception as exc:  # noqa: BLE001
        raise VoiceError(f"не удалось записать модель в настройки узла: {type(exc).__name__}") from None
    return checked.name


def apply(settings: dict[str, Any]) -> dict[str, Any]:
    """Применить проверенные настройки. Бросает VoiceError, если выбран локальный Голос, а применить его нельзя."""
    voice = settings.get("voice")
    if voice == "local":
        name = register_local(settings["local"]["path"])
        return {"applied": True, "note": f"отвечает локальная модель {name}; она загрузится при первом ответе (может занять время)"}
    label = {"remote": "Удалённая модель", "api": "API-сервис"}.get(voice, "Этот Голос")
    return {"applied": False, "note": f"{label} пока не подключена(ы): выбор сохранён, а отвечает прежняя локальная модель"}


def status(settings: dict[str, Any]) -> dict[str, Any]:
    """Что действует ПРЯМО СЕЙЧАС для сохранённых настроек (для страницы: «Сейчас используется» не должно врать)."""
    if not settings.get("saved"):
        return {"applied": False, "note": ""}
    if settings.get("voice") == "local":
        try:
            ok = _registered_path() == str(settings["local"]["path"]) or _same_file(_registered_path(), settings["local"]["path"])
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            return {"applied": True, "note": "отвечает выбранная локальная модель"}
        return {"applied": False, "note": "локальная модель выбрана, но не зарегистрирована в узле: нажмите «Применить» ещё раз"}
    return {"applied": False, "note": "выбранный Голос пока не подключён: отвечает прежняя локальная модель"}


def _same_file(a: str | None, b: str) -> bool:
    from pathlib import Path
    try:
        return bool(a) and Path(a).resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False


def effective_model(requested: str) -> str:
    """Имя модели, которой отвечает Помощница: Голос, если он локальный и применён, иначе то, что запросили."""
    try:
        settings = ui_settings.load()
        if settings.get("saved") and settings.get("voice") == "local" and status(settings)["applied"]:
            return VOICE_ALIAS
    except Exception:  # noqa: BLE001 — сбой настроек не должен ронять чат: отвечает как раньше
        pass
    return requested
