"""
tool_browser.py — управление подключением к AI чатам.

Проверяет статус через heartbeat ноды (расширение пингует /api/ext/poll).
Открывает вкладки браузера через xdg-open если не подключены.
"""
import subprocess
import time
import requests

PET_URL = "http://127.0.0.1:9010"

MODELS = {
    "claude":   "https://claude.ai",
    "gpt":      "https://chatgpt.com",
    "deepseek": "https://chat.deepseek.com",
    "kimi":     "https://www.kimi.com",
    "qwen":     "https://chat.qwen.ai",
}


def _s() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def status(models: list[str] = None) -> dict:
    """Проверить статус подключения к AI чатам."""
    try:
        r = _s().get(f"{PET_URL}/api/council/connections", timeout=5)
        r.raise_for_status()
        data = r.json()
        if models:
            return {m: data[m] for m in models if m in data}
        return data
    except Exception as e:
        return {"error": str(e)}


def open_tab(model: str) -> dict:
    """Открыть вкладку браузера для указанной модели."""
    if model not in MODELS:
        return {"ok": False, "error": f"Неизвестная модель: {model}. Доступны: {list(MODELS)}"}
    url = MODELS[model]
    try:
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "model": model, "url": url, "action": "opened"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def connect(models: list[str] = None, wait_sec: int = 8) -> dict:
    """
    Проверить подключение к моделям. Если вкладка не открыта — открыть.
    Ждёт wait_sec секунд после открытия и проверяет снова.

    models=None → все пять моделей.
    """
    targets = models or list(MODELS)
    unknown = [m for m in targets if m not in MODELS]
    if unknown:
        return {"ok": False, "error": f"Неизвестные модели: {unknown}"}

    # Текущий статус
    current = status(targets)
    if "error" in current:
        return {"ok": False, "error": current["error"]}

    result = {}
    opened = []

    for model in targets:
        info = current.get(model, {})
        if info.get("connected"):
            result[model] = {"status": "🟢 подключён", "action": "none"}
        else:
            r = open_tab(model)
            if r["ok"]:
                opened.append(model)
                result[model] = {"status": "🟡 открываем...", "action": "opened", "url": MODELS[model]}
            else:
                result[model] = {"status": "❌ ошибка", "action": "failed", "error": r.get("error")}

    # Ждём если что-то открывали
    if opened:
        time.sleep(wait_sec)
        updated = status(opened)
        for model in opened:
            info = updated.get(model, {})
            if info.get("connected"):
                result[model]["status"] = "🟢 подключён"
            else:
                result[model]["status"] = "🟡 ждём авторизации"

    all_ok = all(v.get("status", "").startswith("🟢") for v in result.values())
    return {"ok": all_ok, "models": result, "opened": opened}


def disconnect_all() -> dict:
    """Просто возвращает статус — закрывать вкладки программно не можем."""
    return {"ok": True, "note": "Закрой вкладки браузера вручную", "status": status()}
