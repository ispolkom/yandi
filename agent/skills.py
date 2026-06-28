#!/usr/bin/env python3
"""
assistant/skills.py — скиллы демона.

ShellSkill:      запустить команду → результат в Redis
CodeSearchSkill: умный grep по проекту с реестром проверенных файлов
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import redis

BASE        = Path(__file__).parent.parent
SKILLS_DIR  = BASE / "registry" / "skills"
SEARCH_DIR  = SKILLS_DIR / "code_search"
FOUND_DIR   = SEARCH_DIR / "found"
NEG_DIR     = SEARCH_DIR / "negative"
INDEX_FILE  = SEARCH_DIR / "index.json"

for d in (FOUND_DIR, NEG_DIR):
    d.mkdir(parents=True, exist_ok=True)

REPORT_CH  = "council:skill:report"   # Redis канал для отчётов
REPORT_KEY = "council:skill:reports"  # Redis list (последние 50)

ALLOWED_CMDS = {
    # Управление демоном
    "pet_start", "pet_stop", "pet_log",
    # Датасет
    "dataset_build", "dataset_filter", "dataset_stats",
    # Статус
    "status", "tokens",
    # Git (read-only)
    "git_status", "git_log", "git_diff",
}


# ── Redis helper ──────────────────────────────────────────────────────────────

def _publish(r: redis.Redis, report: dict):
    payload = json.dumps(report, ensure_ascii=False)
    r.lpush(REPORT_KEY, payload)
    r.ltrim(REPORT_KEY, 0, 49)
    r.publish(REPORT_CH, payload)


# ── ShellSkill ────────────────────────────────────────────────────────────────

class ShellSkill:
    """Запускает разрешённые команды и возвращает отчёт в Redis."""

    def __init__(self, r: redis.Redis, project: Path = BASE):
        self.r       = r
        self.project = project
        try:
            from agent.policy import PolicyEngine, CommandAudit
            self._policy = PolicyEngine()
            self._audit  = CommandAudit()
        except Exception:
            self._policy = None
            self._audit  = None

    def is_safe(self, cmd: str) -> bool:
        if self._policy:
            return self._policy.check_shell(cmd)["allowed"]
        # fallback
        safe = (
            "ls ", "cat ", "head ", "tail ", "wc ", "find ",
            "grep ", "rg ", "python3 ", "python ",
            "git status", "git log", "git diff", "./ctl ",
        )
        return any(cmd.strip().startswith(p) for p in safe)

    def run(self, cmd: str, timeout: int = 30, actor: str = "daemon") -> dict:
        ts = datetime.now().strftime("%H:%M:%S")
        allowed = self.is_safe(cmd)
        if not allowed:
            if self._audit:
                self._audit.log(actor, cmd, False, reason="policy blocked")
            report = {
                "skill": "shell", "ts": ts, "cmd": cmd,
                "status": "blocked",
                "output": f"[shell] команда заблокирована политикой: {cmd[:80]}",
            }
            _publish(self.r, report)
            return report

        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=str(self.project),
                env={**os.environ,
                     "PYTHONPATH": str(self.project),
                     "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1"},
            )
            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()
            output = stdout if stdout else stderr
            status = "ok" if proc.returncode == 0 else f"exit:{proc.returncode}"
        except subprocess.TimeoutExpired:
            output = f"[shell] timeout после {timeout}s"
            status = "timeout"
        except Exception as e:
            output = f"[shell] ошибка: {e}"
            status = "error"

        if self._audit:
            self._audit.log(actor, cmd, True, result=status)

        report = {
            "skill": "shell", "ts": ts, "cmd": cmd,
            "status": status,
            "output": output[:2000],
        }
        _publish(self.r, report)
        return report


# ── CodeSearchSkill ───────────────────────────────────────────────────────────

class CodeSearchSkill:
    """
    Умный поиск по коду проекта.

    - Использует ripgrep (rg) или grep — без загрузки файлов в контекст
    - Ведёт реестр проверенных файлов (не повторяет работу)
    - found/  — файлы с совпадениями
    - negative/ — файлы где точно нет паттерна
    - Отчёт → Redis
    """

    def __init__(self, r: redis.Redis, project: Path = BASE):
        self.r       = r
        self.project = project
        self._index  = self._load_index()

    def _load_index(self) -> dict:
        if INDEX_FILE.exists():
            try:
                return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"searches": {}, "file_cache": {}}

    def _save_index(self):
        INDEX_FILE.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _rg_available(self) -> bool:
        try:
            subprocess.run(["rg", "--version"], capture_output=True, timeout=3)
            return True
        except Exception:
            return False

    def search(self, query: str, pattern: str = None,
               extensions: list[str] = None, reset: bool = False) -> dict:
        """
        query    — человеческое описание (для отчёта)
        pattern  — regex/literal для поиска (если None — берётся из query)
        extensions — список расширений: ["py", "js"] (None = все)
        reset    — сбросить кеш для этого паттерна
        """
        ts = datetime.now().strftime("%H:%M:%S")
        if pattern is None:
            # Извлечь первое слово/идентификатор из запроса
            pattern = re.sub(r'[^\w\.\-_]', ' ', query).split()[0] if query.split() else query

        cache_key = f"{pattern}|{','.join(extensions or [])}"

        if reset and cache_key in self._index["searches"]:
            del self._index["searches"][cache_key]

        # Уже делали этот поиск?
        if cache_key in self._index["searches"] and not reset:
            cached = self._index["searches"][cache_key]
            report = {
                "skill": "code_search", "ts": ts,
                "query": query, "pattern": pattern,
                "status": "cached",
                "found_count": cached["found_count"],
                "found_files": cached["found_files"][:10],
                "report_file": cached.get("report_file"),
                "note": "результат из кеша (reset=True чтобы пересчитать)",
            }
            _publish(self.r, report)
            return report

        # Строим команду поиска
        use_rg = self._rg_available()
        ext_args = []
        if extensions:
            for ext in extensions:
                ext_args += ["-g", f"*.{ext}"] if use_rg else ["--include", f"*.{ext}"]

        if use_rg:
            cmd = ["rg", "--line-number", "--no-heading",
                   "--max-count", "5",
                   "-e", pattern] + ext_args + [str(self.project)]
        else:
            cmd = ["grep", "-rn", "--max-count=5"] + ext_args + [pattern, str(self.project)]

        # Исключаем шум
        excludes = [".git", "__pycache__", "node_modules", ".pyc",
                    "registry/dataset", "registry/flood", "registry/skills"]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                cwd=str(self.project),
            )
            raw_lines = proc.stdout.strip().split("\n") if proc.stdout.strip() else []
        except subprocess.TimeoutExpired:
            raw_lines = []

        # Парсим результаты
        found: dict[str, list[str]] = {}  # file → [match lines]
        for line in raw_lines:
            if not line:
                continue
            skip = False
            for exc in excludes:
                if exc in line:
                    skip = True
                    break
            if skip:
                continue

            # формат rg: path:line:content  или grep: path:line:content
            parts = line.split(":", 2)
            if len(parts) >= 2:
                fpath = parts[0].replace(str(self.project) + "/", "")
                match_line = parts[2].strip() if len(parts) > 2 else ""
                found.setdefault(fpath, []).append(match_line[:120])

        # Записываем результаты
        ts_slug = datetime.now().strftime("%Y%m%d_%H%M%S")
        pat_slug = re.sub(r"[^\w]", "_", pattern)[:30]
        report_name = f"{ts_slug}_{pat_slug}.md"

        report_path = FOUND_DIR / report_name
        lines_out = [
            f"# Поиск: `{pattern}`",
            f"Запрос: {query}",
            f"Дата: {datetime.now().isoformat()}",
            f"Найдено файлов: {len(found)}",
            "",
        ]
        for fpath, matches in sorted(found.items()):
            lines_out.append(f"## {fpath}")
            for m in matches[:5]:
                lines_out.append(f"  {m}")
            lines_out.append("")

        report_path.write_text("\n".join(lines_out), encoding="utf-8")

        # Обновляем индекс
        self._index["searches"][cache_key] = {
            "query":       query,
            "pattern":     pattern,
            "ts":          ts,
            "found_count": len(found),
            "found_files": list(found.keys()),
            "report_file": str(report_path),
        }
        self._save_index()

        report = {
            "skill": "code_search", "ts": ts,
            "query": query, "pattern": pattern,
            "status": "ok",
            "found_count": len(found),
            "found_files": list(found.keys())[:10],
            "report_file": str(report_path),
        }
        _publish(self.r, report)
        return report

    def find_definition(self, name: str) -> dict:
        """Найти где определяется функция/класс/переменная."""
        patterns = [
            f"def {name}",
            f"class {name}",
            f"{name} =",
            f"{name}:",
        ]
        results = {}
        for pat in patterns:
            r = self.search(
                query=f"определение {name}",
                pattern=pat,
                extensions=["py", "js", "ts", "rs"],
            )
            if r["found_count"] > 0:
                results[pat] = r["found_files"]

        ts = datetime.now().strftime("%H:%M:%S")
        report = {
            "skill": "code_search", "ts": ts,
            "query": f"definition:{name}",
            "status": "ok" if results else "not_found",
            "found_patterns": results,
            "summary": f"'{name}' найден в {sum(len(v) for v in results.values())} местах",
        }
        _publish(self.r, report)
        return report

    def find_usage(self, name: str, extensions: list[str] = None) -> dict:
        """Найти все использования имени в проекте."""
        return self.search(
            query=f"использования {name}",
            pattern=name,
            extensions=extensions or ["py", "js", "ts"],
        )

    def stats(self) -> dict:
        s = self._index.get("searches", {})
        return {
            "cached_searches": len(s),
            "total_found_files": sum(v["found_count"] for v in s.values()),
            "report_dir": str(FOUND_DIR),
        }


# ── CLI для тестирования ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    r = redis.from_url("redis://127.0.0.1:6379/0")

    cmd  = sys.argv[1] if len(sys.argv) > 1 else "search"
    arg  = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Orchestrator"

    if cmd == "shell":
        s = ShellSkill(r)
        result = s.run(arg)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "search":
        cs = CodeSearchSkill(r)
        result = cs.search(query=arg)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "def":
        cs = CodeSearchSkill(r)
        result = cs.find_definition(arg)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "usage":
        cs = CodeSearchSkill(r)
        result = cs.find_usage(arg)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "stats":
        cs = CodeSearchSkill(r)
        print(json.dumps(cs.stats(), ensure_ascii=False, indent=2))
