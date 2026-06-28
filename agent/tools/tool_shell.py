"""
tool_shell.py — sandbox bash для агента.
Только команды из белого списка. Рабочая директория — PROJECT_ROOT.
"""
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent  # yandi/

import os as _os_mod

WHITELIST = [
    r"^ls(\s|$)", r"^find\s", r"^cat\s", r"^head\s", r"^tail\s",
    r"^grep\s", r"^wc\s", r"^echo\s", r"^pwd$", r"^date$",
    r"^mkdir\s", r"^touch\s",
    r"^python(\d[\d.]*)?(\s|$)", r"^python3(\s|$)",
    r"^pytest(\s|$)",
    r"^cargo\s(test|check|build|fmt|clippy)",
    r"^redis-cli(\s|$)",
    r"^systemctl\s(is-active|status)\s",
    r"^du\s", r"^df\s", r"^free(\s|$)",
    r"^ps\s",
]

BANNED = [
    r"\brm\b", r"\bmv\b", r"\bcp\b", r"\bwget\b", r"\bcurl\b",
    r"\bchmod\b", r"\bchown\b", r"\bsudo\b", r"\bsu\b",
    r"\bkill\b", r"\bpkill\b", r"\breboot\b", r"\bshutdown\b",
    r"[|&;`$]",  # пайпы и подстановки запрещены
    r"\.\.",     # выход за пределы директории
]


def _allowed(cmd: str) -> bool:
    cmd = cmd.strip()
    # Полный доступ если разрешён
    if _os_mod.environ.get("AGENT_SHELL_FULL"):
        return True
    # Сеть разрешена?
    net_banned = [r"\bcurl\b", r"\bwget\b"] if not _os_mod.environ.get("AGENT_SHELL_NET") else []
    for ban in BANNED:
        if ban in (r"\bcurl\b", r"\bwget\b") and _os_mod.environ.get("AGENT_SHELL_NET"):
            continue
        if re.search(ban, cmd):
            return False
    for wl in WHITELIST:
        if re.match(wl, cmd):
            return True
    return False


def run(cmd: str, timeout: int = 30) -> dict:
    if not _allowed(cmd):
        return {"ok": False, "error": f"Команда запрещена: {cmd}"}
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(PROJECT_ROOT),
        )
        return {
            "ok": r.returncode == 0,
            "stdout": r.stdout[:4000],
            "stderr": r.stderr[:1000],
            "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Timeout {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def allowed_commands() -> list[str]:
    return [
        "ls, find, cat, head, tail, grep, wc, echo, pwd, date",
        "mkdir, touch",
        "python, python3, pytest",
        "cargo test/check/build/fmt/clippy",
        "redis-cli",
        "systemctl is-active/status",
        "du, df, free, ps",
    ]
