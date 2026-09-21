"""
pet/settings_api.py — GET/POST /api/ui/settings: настройки вкладки «YANDI» (см. pet/ui_settings.py).
Закрыто охраной pet.local_guard: страницу чужого сайта сюда не пускаем.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from pet import ui_settings
from pet.local_guard import require_local_origin

router = APIRouter(dependencies=[Depends(require_local_origin)])


@router.get("/api/ui/settings")
async def get_settings():
    return {"ok": True, **ui_settings.load()}


@router.post("/api/ui/settings")
async def set_settings(payload: dict):
    try:
        return {"ok": True, **ui_settings.save(payload)}
    except ui_settings.SettingsError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"не удалось записать файл настроек: {exc.strerror or exc}"}
