"""
pet/settings_api.py — GET/POST /api/ui/settings: настройки вкладки «YANDI» (см. pet/ui_settings.py).
Закрыто охраной pet.local_guard: страницу чужого сайта сюда не пускаем.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from pet import ui_settings, voice
from pet.local_guard import require_local_origin

router = APIRouter(dependencies=[Depends(require_local_origin)])


@router.get("/api/ui/settings")
async def get_settings():
    settings = ui_settings.load()
    return {"ok": True, **settings, **voice.status(settings)}


@router.post("/api/ui/settings")
async def set_settings(payload: dict):
    """Проверить, ПРИМЕНИТЬ (локальный Голос регистрируется в шлюзе), и только потом сохранить: «сохранено» не значит «не действует»."""
    try:
        clean = ui_settings.validate(payload)
    except ui_settings.SettingsError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        outcome = await asyncio.get_running_loop().run_in_executor(None, lambda: voice.apply(clean))
    except voice.VoiceError as exc:
        return {"ok": False, "error": f"не применено, настройки не сохранены: {exc}"}
    try:
        saved = ui_settings.save(payload)                # проверяет ещё раз и пишет атомарно
    except ui_settings.SettingsError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"не удалось записать файл настроек: {exc.strerror or exc}"}
    return {"ok": True, **saved, **outcome}
