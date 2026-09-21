"""
pet/chat_models.py — помощники вкладки «⚙ YANDI»: выбор файла локальной модели на любом диске.

    GET  /api/models/browse?path=   обзор папок ЛЮБОГО диска: подпапки и файлы .gguf (только чтение)
    POST /api/models/check          проверить файл модели: существует, .gguf, заголовок GGUF (ничего не сохраняет)

Оба закрыты охраной pet.local_guard: сервер отвечает CORS «*», а обзор диска не должен быть доступен странице
чужого сайта. Сохранение выбора — в pet/settings_api.py (файл настроек), регистрация модели в шлюзе — отдельный шаг.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path

from fastapi import APIRouter, Depends

from pet.local_guard import require_local_origin

router = APIRouter(dependencies=[Depends(require_local_origin)])

GGUF_MAGIC = b"GGUF"
MAX_BROWSE_ENTRIES = 1000


def _shortcuts() -> list[dict]:
    home = Path.home()
    user = getpass.getuser()
    candidates = [("Домашняя папка", home), ("Корень /", Path("/")), ("/mnt", Path("/mnt")), ("/media", Path("/media")),
                  (f"/media/{user}", Path("/media") / user), (f"/run/media/{user}", Path("/run/media") / user)]
    return [{"label": label, "path": str(p)} for label, p in candidates if p.is_dir()]


@router.get("/api/models/browse")
async def models_browse(path: str = "", hidden: bool = False):
    """Только чтение: подпапки и файлы .gguf выбранной папки."""
    try:
        target = Path(path).expanduser() if path else Path.home()
        if not target.is_absolute():
            return {"ok": False, "error": "путь должен быть абсолютным (например /home/имя/models)"}
        target = target.resolve()
        if target.is_file():
            target = target.parent
        if not target.is_dir():
            return {"ok": False, "error": f"папки нет: {target}"}
        entries, truncated = [], False
        with os.scandir(target) as it:
            for e in it:
                if not hidden and e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        entries.append({"name": e.name, "kind": "dir", "size": None})
                    elif e.is_file() and e.name.lower().endswith(".gguf"):
                        entries.append({"name": e.name, "kind": "gguf", "size": e.stat().st_size})
                except OSError:
                    continue                                       # битая ссылка и т.п.
                if len(entries) >= MAX_BROWSE_ENTRIES:
                    truncated = True
                    break
        entries.sort(key=lambda x: (x["kind"] != "dir", x["name"].casefold()))
        parent = str(target.parent) if target.parent != target else None
        return {"ok": True, "path": str(target), "parent": parent, "entries": entries, "truncated": truncated,
                "shortcuts": _shortcuts()}
    except PermissionError:
        return {"ok": False, "error": "нет доступа к этой папке"}
    except OSError as exc:
        return {"ok": False, "error": f"не удалось прочитать папку: {exc.strerror or exc}"}


def _check_gguf(raw_path: str):
    """(Path, "") для существующего файла модели GGUF, иначе (None, причина). Читает только первые 4 байта."""
    raw_path = (raw_path or "").strip()
    if not raw_path:
        return None, "укажи путь к файлу .gguf"
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            return None, "путь должен быть абсолютным"
        path = path.resolve()
        if not path.is_file():
            return None, f"файла нет: {path}"
        if path.suffix.lower() != ".gguf":
            return None, "нужен файл модели с расширением .gguf"
        with open(path, "rb") as fh:
            if fh.read(4) != GGUF_MAGIC:
                return None, "файл не похож на модель GGUF (не тот заголовок)"
        return path, ""
    except PermissionError:
        return None, "нет прав прочитать этот файл"
    except OSError as exc:
        return None, f"не удалось открыть файл: {exc.strerror or exc}"


@router.post("/api/models/check")
async def models_check(payload: dict):
    path, error = _check_gguf(str(payload.get("path") or ""))
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "file": path.name, "size_gb": round(path.stat().st_size / 1024 ** 3, 2)}
