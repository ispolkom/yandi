"""
pet/ui_settings.py — настройки вкладки «YANDI» (какая модель отвечает, какие модели советуют).

Хранятся в ФАЙЛЕ в папке пользователя (~/.local/share/yandi/web_settings.json, права 0600, запись атомарная:
временный файл + замена), а не в коде и не в репозитории. Сам ключ API здесь НЕ хранится: где его держать
(зашифрованное хранилище узла или переменная окружения), решает владелец отдельно; форма его принимает, но
сохранение ключа пока не подключено, и сервер отвергает документ, в котором ключ пришёл.

    {"saved": true,
     "voice": "local" | "remote" | "api" | "",      кто формулирует ответ (ровно один)
     "advisors": ["local", "api", ...],             чьё мнение запрашивается и записывается (любое число)
     "local":  {"path": "/любой/диск/model.gguf"},
     "remote": {"address": "http://192.168.1.5:8080", "model": "qwen-14b"},
     "api":    {"service": "openai" | "anthropic" | "other", "model": "..."}}
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# Rust-перенос (2026-09-24): rustlib/yandi_rs/src/ui_settings.rs — `validate`. Доказан тестом pet/pet_ui_settings_rust_parity_test.py. По умолчанию
# ВЫКЛЮЧЕН; включается YANDI_UI_SETTINGS_ENGINE=rust ПОСЛЕ сборки rustlib/yandi_rs. Файл/права/запись (`load`/`save`) остаются здесь.
# Посторонние типы входа и одинокие суррогаты идут прежним Python-путём; делегирует, только пока константы не менялись.
_rust_us = None          # None = ещё не пробовали; False = не запрошено/не собрано; модуль = подключён
_DEFAULT_CONSTS = (("local", "remote", "api"), ("openai", "anthropic", "other"), r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}", r"https?://[^\s/$.?#][^\s]*", 1024)


def _get_rust_us():
    global _rust_us
    if _rust_us is None:
        if os.environ.get("YANDI_UI_SETTINGS_ENGINE") == "rust":
            try:
                import yandi_rs.ui_settings as _rs
                _rust_us = _rs
            except ImportError:
                _rust_us = False
        else:
            _rust_us = False
    return _rust_us or None


KINDS = ("local", "remote", "api")
SERVICES = ("openai", "anthropic", "other")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}")
_ADDR_RE = re.compile(r"https?://[^\s/$.?#][^\s]*")
MAX_PATH = 1024


class SettingsError(ValueError):
    pass


def default_path() -> Path:
    override = os.environ.get("YANDI_WEB_SETTINGS")
    return Path(override) if override else Path.home() / ".local" / "share" / "yandi" / "web_settings.json"


def defaults() -> dict[str, Any]:
    return {"saved": False, "voice": "", "advisors": [],
            "local": {"path": ""}, "remote": {"address": "", "model": ""}, "api": {"service": "openai", "model": ""}}


def _text(value: Any, name: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SettingsError(f"{name}: нужна строка")
    value = value.strip()
    if len(value) > limit:
        raise SettingsError(f"{name}: слишком длинное значение")
    return value


def validate(doc: Any) -> dict[str, Any]:
    """Документ от формы -> проверенный документ; неизвестное и ключ API — ошибка, а не молчаливая правка."""
    rs = _get_rust_us()
    if rs is not None and (KINDS, SERVICES, _MODEL_RE.pattern, _ADDR_RE.pattern, MAX_PATH) == _DEFAULT_CONSTS:
        try:
            r = rs.validate(doc)
        except UnicodeEncodeError:
            r = None
        if r is not None:
            if r[0]:
                return r[1]
            raise SettingsError(r[1])
    if not isinstance(doc, dict):
        raise SettingsError("ожидался JSON-объект")
    unknown = set(doc) - {"voice", "advisors", "local", "remote", "api"}
    if unknown:
        raise SettingsError(f"неизвестные поля: {', '.join(sorted(unknown))}")
    api_in = doc.get("api") or {}
    if not isinstance(api_in, dict):
        raise SettingsError("api: нужен объект")
    if any(k in api_in for k in ("key", "api_key", "token", "secret")):
        raise SettingsError("ключ API здесь не сохраняется (хранение ключа ещё не подключено)")

    out = defaults()
    voice = doc.get("voice", "")
    if voice not in ("",) + KINDS:
        raise SettingsError("voice: local, remote или api")
    out["voice"] = voice
    advisors = doc.get("advisors", [])
    if not isinstance(advisors, list) or any(a not in KINDS for a in advisors):
        raise SettingsError("advisors: список из local / remote / api")
    out["advisors"] = [k for k in KINDS if k in advisors]

    local, remote = doc.get("local") or {}, doc.get("remote") or {}
    if not isinstance(local, dict) or not isinstance(remote, dict):
        raise SettingsError("local и remote: нужны объекты")
    out["local"]["path"] = _text(local.get("path"), "local.path", MAX_PATH)
    address = _text(remote.get("address"), "remote.address", 512)
    if address and not _ADDR_RE.fullmatch(address):
        raise SettingsError("remote.address: адрес вида http://хост:порт")
    out["remote"]["address"] = address
    for kind, model in (("remote", _text(remote.get("model"), "remote.model", 128)), ("api", _text(api_in.get("model"), "api.model", 128))):
        if model and not _MODEL_RE.fullmatch(model):
            raise SettingsError(f"{kind}.model: имя модели (буквы, цифры и . _ : / @ + -)")
        out[kind]["model"] = model
    service = api_in.get("service", "openai")
    if service not in SERVICES:
        raise SettingsError("api.service: openai, anthropic или other")
    out["api"]["service"] = service

    # Голос обязан быть выбран и настроен; советник — только настроенный
    def filled(kind: str) -> bool:
        if kind == "local":
            return bool(out["local"]["path"])
        if kind == "remote":
            return bool(out["remote"]["address"] and out["remote"]["model"])
        return bool(out["api"]["model"])
    names = {"local": "Локальная", "remote": "Удалённая", "api": "API"}
    if not out["voice"]:
        raise SettingsError("выберите Голос: тот, кто будет отвечать")
    for kind in {out["voice"], *out["advisors"]}:
        if not filled(kind):
            raise SettingsError(f"блок «{names[kind]}» выбран, но не заполнен")
    return out


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or default_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("saved") is not True:
            return defaults()
        loaded = validate({k: data.get(k) for k in ("voice", "advisors", "local", "remote", "api")})
        loaded["saved"] = True
        return loaded
    except (OSError, ValueError, AttributeError):
        return defaults()        # нет файла, повреждён или не проходит проверку: как в первый запуск


def save(doc: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or default_path()
    clean = validate(doc)
    clean["saved"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".web_settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return clean
