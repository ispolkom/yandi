#!/usr/bin/env python3
"""
assistant/policy.py — Policy & Safety Layer для PET-системы.

Компоненты:
  PolicyEngine   — проверяет команды/запросы против правил
  SecretScanner  — ищет секреты в текстах/файлах перед сохранением
  CommandAudit   — audit-лог всех команд (append-only JSONL)
  NetworkGuard   — проверяет исходящие запросы (allowlist хостов)

Команды CLI:
  python3 assistant/policy.py scan <file>   — сканировать файл на секреты
  python3 assistant/policy.py audit         — последние N записей аудита
  python3 assistant/policy.py stats         — статистика политики
  python3 assistant/policy.py check <cmd>   — проверить команду
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE       = Path(__file__).parent.parent
AUDIT_FILE = BASE / "registry" / "policy" / "audit.jsonl"
POLICY_CFG = BASE / "registry" / "policy" / "config.json"

AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Secret patterns ───────────────────────────────────────────────────────────

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("api_key",       re.compile(r'(?i)(api[_-]?key|apikey)\s*[=:]\s*["\']?([A-Za-z0-9_\-]{20,})["\']?')),
    ("sk_token",      re.compile(r'\bsk-[A-Za-z0-9]{20,}\b')),
    ("aws_access",    re.compile(r'\bAKIA[A-Z0-9]{16}\b')),
    ("aws_secret",    re.compile(r'(?i)aws[_\-]?secret[_\-]?access[_\-]?key\s*[=:]\s*["\']?([A-Za-z0-9+/]{40})["\']?')),
    ("password",      re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*["\']([^"\'\s]{6,})["\']')),
    ("private_key",   re.compile(r'-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ("token",         re.compile(r'(?i)(token|secret)\s*[=:]\s*["\']([A-Za-z0-9_\-\.]{16,})["\']')),
    ("hf_token",      re.compile(r'\bhf_[A-Za-z0-9]{30,}\b')),
    ("proxy_creds",   re.compile(r'https?://[^:]+:[^@]+@[a-zA-Z0-9._-]+')),
]

# Исключения: эти строки безопасны даже если попали под паттерн
_SECRET_WHITELIST = {
    "password123", "example", "changeme", "your_token_here",
    "sk-xxxx", "api_key_here", "<TOKEN>", "YOUR_KEY",
}

# Паттерн proxy_creds пропускаем если хост — локальный или известный AI сервис
_SAFE_HOSTS = {
    "127.0.0.1", "localhost", "claude.ai", "chatgpt.com", "chat.deepseek.com",
    "api.anthropic.com", "api.openai.com", "huggingface.co",
    "user:pass@host",  # шаблонный пример
}


# ── Shell command allowlist ───────────────────────────────────────────────────

_SHELL_ALLOWLIST: list[str] = [
    "ls", "cat", "head", "tail", "wc", "find", "grep", "rg",
    "python3", "python",
    "git status", "git log", "git diff", "git show",
    "redis-cli", "./ctl",
    "du ", "df ", "free ", "uptime", "ps aux",
    "journalctl", "systemctl status",
    "sqlite3",
]

_SHELL_BLOCKLIST: list[str] = [
    "rm -rf", "rm -fr", "sudo rm",
    "dd if=", "mkfs",
    "> /dev/", "chmod 777",
    "curl | bash", "wget | bash", "curl -o- | bash",
    "eval $(", "`",
    "nc -", "netcat",
    "; rm ", "&& rm ",
    "passwd ", "adduser ", "userdel ",
    "iptables -F", "ufw disable",
]

# ── Network allowlist ─────────────────────────────────────────────────────────

_NETWORK_ALLOW: list[str] = [
    "127.0.0.1", "localhost",
    "api.anthropic.com",
    "api.openai.com",
    "api.deepseek.com",
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "pypi.org", "files.pythonhosted.org",
]


# ── SecretScanner ─────────────────────────────────────────────────────────────

class SecretScanner:
    def scan_text(self, text: str, source: str = "<text>") -> list[dict]:
        findings = []
        for name, pattern in _SECRET_PATTERNS:
            for m in pattern.finditer(text):
                matched = m.group(0)
                if any(w.lower() in matched.lower() for w in _SECRET_WHITELIST):
                    continue
                # proxy_creds: пропускаем локальные/известные хосты
                if name == "proxy_creds":
                    if any(h in matched for h in _SAFE_HOSTS):
                        continue
                findings.append({
                    "type": name,
                    "match": matched[:80],
                    "pos": m.start(),
                    "source": source,
                })
        return findings

    def scan_file(self, path: Path) -> list[dict]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            return self.scan_text(text, str(path))
        except Exception as e:
            return [{"type": "error", "match": str(e), "source": str(path)}]

    def scan_dir(self, directory: Path, patterns: list[str] = None) -> list[dict]:
        patterns = patterns or ["*.py", "*.json", "*.jsonl", "*.env", "*.yaml", "*.yml"]
        all_findings = []
        skip_dirs = {".git", "__pycache__", "adapters", "embeddings_cache", "browser_data"}
        for glob in patterns:
            for f in directory.rglob(glob):
                if any(s in f.parts for s in skip_dirs):
                    continue
                all_findings.extend(self.scan_file(f))
        return all_findings


# ── PolicyEngine ──────────────────────────────────────────────────────────────

class PolicyEngine:
    def __init__(self):
        self.scanner = SecretScanner()
        self._load_config()

    def _load_config(self):
        if POLICY_CFG.exists():
            try:
                self._cfg = json.loads(POLICY_CFG.read_text())
            except Exception:
                self._cfg = {}
        else:
            self._cfg = {}

    def check_shell(self, cmd: str) -> dict:
        cmd = cmd.strip()
        # Blocklist first
        for bad in _SHELL_BLOCKLIST:
            if bad in cmd:
                return {"allowed": False, "reason": f"blocklist: {bad}", "cmd": cmd}
        # Allowlist
        for ok in _SHELL_ALLOWLIST:
            if cmd.startswith(ok):
                return {"allowed": True, "reason": "allowlist", "cmd": cmd}
        return {"allowed": False, "reason": "not in allowlist", "cmd": cmd}

    def check_network(self, host: str) -> dict:
        for allowed in _NETWORK_ALLOW:
            if host == allowed or host.endswith("." + allowed):
                return {"allowed": True, "host": host}
        return {"allowed": False, "host": host, "reason": "not in network allowlist"}

    def check_text_for_secrets(self, text: str) -> dict:
        findings = self.scanner.scan_text(text)
        return {
            "clean": len(findings) == 0,
            "findings": findings,
        }


# ── CommandAudit ──────────────────────────────────────────────────────────────

class CommandAudit:
    def log(self, actor: str, cmd: str, allowed: bool,
            reason: str = "", result: str = "") -> None:
        record = {
            "ts": datetime.now().isoformat(),
            "actor": actor,
            "cmd": cmd[:200],
            "allowed": allowed,
            "reason": reason,
            "result": result[:200],
        }
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def tail(self, n: int = 20) -> list[dict]:
        if not AUDIT_FILE.exists():
            return []
        lines = AUDIT_FILE.read_text(encoding="utf-8").strip().splitlines()
        result = []
        for line in lines[-n:]:
            try:
                result.append(json.loads(line))
            except Exception:
                pass
        return result

    def stats(self) -> dict:
        if not AUDIT_FILE.exists():
            return {"total": 0}
        lines = AUDIT_FILE.read_text(encoding="utf-8").strip().splitlines()
        total = allowed = blocked = 0
        actors: dict[str, int] = {}
        for line in lines:
            try:
                r = json.loads(line)
                total += 1
                if r.get("allowed"):
                    allowed += 1
                else:
                    blocked += 1
                actors[r.get("actor", "?")] = actors.get(r.get("actor", "?"), 0) + 1
            except Exception:
                pass
        return {
            "total": total,
            "allowed": allowed,
            "blocked": blocked,
            "actors": actors,
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    arg = sys.argv[2] if len(sys.argv) > 2 else ""

    engine = PolicyEngine()
    audit  = CommandAudit()

    if cmd == "scan":
        if arg:
            findings = engine.scanner.scan_file(Path(arg))
        else:
            findings = engine.scanner.scan_dir(BASE)
        if findings:
            print(f"⚠️  Найдено {len(findings)} совпадений:")
            for f in findings:
                print(f"  [{f['type']}] {f['source']}: {f['match'][:60]}")
        else:
            print("✓ Секретов не найдено")
        print(json.dumps(findings, ensure_ascii=False, indent=2))

    elif cmd == "check":
        result = engine.check_shell(arg)
        status = "✓ разрешено" if result["allowed"] else "✗ заблокировано"
        print(f"{status}: {arg!r}")
        print(f"  причина: {result['reason']}")

    elif cmd == "audit":
        records = audit.tail(20)
        for r in records:
            icon = "✓" if r.get("allowed") else "✗"
            print(f"  {icon} [{r['ts'][:19]}] {r['actor']}: {r['cmd'][:60]}")

    elif cmd == "stats":
        s = audit.stats()
        sc = engine.scanner.scan_dir(BASE)
        print(json.dumps({
            "audit": s,
            "secrets_found": len(sc),
        }, ensure_ascii=False, indent=2))

    else:
        print(f"Unknown: {cmd}")
        print("Usage: policy.py scan [file]|check <cmd>|audit|stats")


if __name__ == "__main__":
    main()
