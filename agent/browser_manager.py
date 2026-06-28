#!/usr/bin/env python3
"""
assistant/browser_manager.py — открыть вкладки AI-чатов в браузере пользователя.

Принцип: НЕ запускаем отдельные браузеры.
Открываем вкладки в уже запущенном Firefox пользователя через xdg-open.
Авторизация, куки, fingerprint — всё родное, риска бана нет.

Redis не нужен для управления PID — вкладки не убиваем,
закрыть может сам пользователь или ./ctl browser_close (закрывает через wmctrl).

Команды через демон / ./ctl:
  browser_open [model]   — открыть вкладку(и) в текущем Firefox
  browser_status         — проверить запущен ли Firefox вообще
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

URLS: dict[str, str] = {
    "claude":   "https://claude.ai/new",
    "gpt":      "https://chatgpt.com/",
    "deepseek": "https://chat.deepseek.com/",
}


def _firefox_running() -> bool:
    try:
        out = subprocess.check_output(
            ["pgrep", "-x", "firefox", "firefox-esr"],
            stderr=subprocess.DEVNULL, text=True,
        )
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


class BrowserManager:

    def __init__(self, log_fn=None):
        self._log = log_fn or print

    # ── защита чужих сессий (оставляем для совместимости с daemon.py) ─────────

    def snapshot_existing(self):
        """Ничего не делаем — чужой Firefox трогать не будем в принципе."""
        self._log("[browser] режим: открываем вкладки в браузере пользователя")

    # ── открытие вкладок ──────────────────────────────────────────────────────

    def start(self, model: str = None, headless: bool = False) -> dict[str, bool]:
        """Открыть вкладку(и) в текущем Firefox. Возвращает {model: ok}."""
        targets = [model] if model and model in URLS else list(URLS)
        result  = {}
        for m in targets:
            ok = self._open_tab(m)
            result[m] = ok
            time.sleep(0.5)  # небольшая пауза между вкладками
        return result

    def _open_tab(self, model: str) -> bool:
        url = URLS[model]
        try:
            # firefox URL → открывает вкладку в существующем Firefox
            # если Firefox не запущен — запустит его с родным профилем
            subprocess.Popen(
                ["firefox", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._log(f"[browser] ✓ открываю {model} → {url}")
            return True
        except FileNotFoundError:
            # попробуем firefox-esr
            try:
                subprocess.Popen(
                    ["firefox-esr", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._log(f"[browser] ✓ открываю {model} → {url}")
                return True
            except Exception as e:
                self._log(f"[browser] ❌ {model}: {e}")
                return False
        except Exception as e:
            self._log(f"[browser] ❌ {model}: {e}")
            return False

    # ── "закрытие" — просто сообщаем пользователю ────────────────────────────

    def stop(self, model: str = None):
        """Мы не убиваем вкладки пользователя. Просто информируем."""
        targets = [model] if model and model in URLS else list(URLS)
        for m in targets:
            self._log(f"[browser] ℹ️  {m}: закрой вкладку {URLS[m]} вручную")

    def reset(self, model: str = None, headless: bool = False) -> dict[str, bool]:
        """Открыть вкладки заново (старые закрывать не будем)."""
        return self.start(model, headless)

    # ── статус ────────────────────────────────────────────────────────────────

    def status(self) -> dict[str, dict]:
        running = _firefox_running()
        return {
            m: {"running": running, "url": url}
            for m, url in URLS.items()
        }

    def log_status(self):
        running = _firefox_running()
        if running:
            self._log("  🟢 Firefox запущен — вкладки могут быть открыты")
        else:
            self._log("  ⚫ Firefox не запущен")
        for model, url in URLS.items():
            self._log(f"     {model:10} → {url}")

    # ── совместимость с daemon.py ─────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return _firefox_running()

    @property
    def pid(self) -> dict:
        return {}  # не отслеживаем PID — это браузер пользователя

    def setup(self, model: str = None):
        """Alias для start() — просто открываем вкладки."""
        self.start(model)
