#!/usr/bin/env python3
"""
assistant/watcher.py — реактивный file watcher.

Следит за registry/council/sessions/ — как только появляется новый файл сессии,
автоматически запускает dataset pipeline.

Запускается как фоновый поток внутри daemon.py.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import redis

BASE         = Path(__file__).parent.parent
SESSION_DIR  = BASE / "registry" / "council" / "sessions"
FLOOD_DIR    = BASE / "registry" / "flood"

CTRL_CH      = "council:daemon:control"


class FileWatcher(threading.Thread):
    """
    Polling watcher — проверяет новые файлы каждые POLL_SEC секунд.
    Использует watchdog если доступен, иначе — простой mtime-polling.
    """

    POLL_SEC  = 5
    WATCH_EXT = {".md", ".json", ".jsonl"}

    def __init__(self, r: redis.Redis, log_fn: Callable = print):
        super().__init__(daemon=True, name="FileWatcher")
        self.r      = r
        self.log    = log_fn
        self._known: dict[str, float] = {}   # path → mtime
        self._stop  = threading.Event()
        self._use_watchdog = self._try_watchdog()

    def _try_watchdog(self) -> bool:
        try:
            import watchdog  # noqa
            return True
        except ImportError:
            return False

    def stop(self):
        self._stop.set()

    # ── watchdog-путь ─────────────────────────────────────────────────────────

    def _run_watchdog(self):
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        watcher = self

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    watcher._on_new_file(Path(event.src_path))

            def on_modified(self, event):
                if not event.is_directory:
                    watcher._on_modified_file(Path(event.src_path))

        observer = Observer()
        observer.schedule(Handler(), str(SESSION_DIR), recursive=False)
        observer.schedule(Handler(), str(FLOOD_DIR),   recursive=False)
        observer.start()
        self.log("[watcher] watchdog запущен")
        try:
            while not self._stop.is_set():
                time.sleep(1)
        finally:
            observer.stop()
            observer.join()

    # ── polling-путь ─────────────────────────────────────────────────────────

    def _run_polling(self):
        self.log(f"[watcher] polling (каждые {self.POLL_SEC}s)")
        # Инициализируем known из текущих файлов
        for d in (SESSION_DIR, FLOOD_DIR):
            if d.exists():
                for f in d.iterdir():
                    if f.suffix in self.WATCH_EXT:
                        self._known[str(f)] = f.stat().st_mtime

        while not self._stop.is_set():
            self._stop.wait(self.POLL_SEC)
            if self._stop.is_set():
                break
            for d in (SESSION_DIR, FLOOD_DIR):
                if not d.exists():
                    continue
                for f in d.iterdir():
                    if f.suffix not in self.WATCH_EXT:
                        continue
                    key   = str(f)
                    mtime = f.stat().st_mtime
                    if key not in self._known:
                        self._known[key] = mtime
                        self._on_new_file(f)
                    elif mtime > self._known[key] + 0.5:
                        self._known[key] = mtime
                        self._on_modified_file(f)

    # ── обработчики событий ───────────────────────────────────────────────────

    def _on_new_file(self, path: Path):
        ts = datetime.now().strftime("%H:%M:%S")

        if path.parent == SESSION_DIR and path.suffix == ".md":
            # Новая сессия сохранена → запускаем датасет
            if "autostart" not in path.name:
                self.log(f"[watcher] новая сессия: {path.name} → dataset run")
                self._cmd({"cmd": "dataset", "sub": "run"})

        elif path.parent == FLOOD_DIR:
            # Новый flood-файл → просто логируем (можно расширить)
            self.log(f"[watcher] новый flood: {path.name}")

    def _on_modified_file(self, path: Path):
        # Сессия была дозаписана (например, demon дописал summary)
        if path.parent == SESSION_DIR and "autostart" not in path.name:
            if path.suffix == ".md" and path.stat().st_size > 500:
                pass  # пока не реагируем на изменения, только на создание

    def _cmd(self, payload: dict):
        try:
            self.r.publish(CTRL_CH, json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            self.log(f"[watcher] redis error: {e}")

    # ── точка входа потока ────────────────────────────────────────────────────

    def run(self):
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        FLOOD_DIR.mkdir(parents=True, exist_ok=True)
        if self._use_watchdog:
            try:
                self._run_watchdog()
                return
            except Exception as e:
                self.log(f"[watcher] watchdog failed ({e}), fallback to polling")
        self._run_polling()
