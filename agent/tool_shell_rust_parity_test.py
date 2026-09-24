"""
agent/tool_shell_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/tool_shell.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и
agent/tools/tool_shell.py::_allowed (охранный шлюз shell-команд агента).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Двадцать восьмой шаг переноса Python -> Rust (2026-09-24) — модуль БЕЗОПАСНОСТИ. Проверяется на всех комбинациях флагов
окружения (AGENT_SHELL_FULL: нет/"1"/"0"/""; AGENT_SHELL_NET: нет/"1"): каждая разрешённая «голова» и каждый запрещённый
токен во всех позициях, «запрет побеждает белый список» («ls rm»), границы слова `\\b` (farm, rm_x, rmdir, дrm, ударение U+0301 перед
rm — для Python это граница), метасимволы `|&;`$` и `..`, Unicode-цифры под `\\d` («python٣»), пробелы U+001C..1F/табы/переводы строк
(strip), регистр, пустая строка, фаззинг склеек.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.tool_shell_rust_parity_test
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
_OK = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _OK
    if condition:
        _OK += 1
    else:
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    try:
        import yandi_rs.tool_shell as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.tool_shell").setLevel(logging.ERROR)
    import agent.tools.tool_shell as ts

    os.environ.pop("YANDI_TOOL_SHELL_ENGINE", None)
    for k in ("AGENT_SHELL_FULL", "AGENT_SHELL_NET"):
        os.environ.pop(k, None)
    ts._rust_ts = None
    rng = random.Random(20260924)

    def run(on, cmd, full, net):
        saved = {k: os.environ.get(k) for k in ("YANDI_TOOL_SHELL_ENGINE", "AGENT_SHELL_FULL", "AGENT_SHELL_NET")}
        try:
            for k, v in (("AGENT_SHELL_FULL", full), ("AGENT_SHELL_NET", net)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if on:
                os.environ["YANDI_TOOL_SHELL_ENGINE"] = "rust"
            else:
                os.environ.pop("YANDI_TOOL_SHELL_ENGINE", None)
            ts._rust_ts = None
            try:
                return ts._allowed(cmd)
            except Exception as e:                                # noqa: BLE001
                return f"EXC:{type(e).__name__}"
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            ts._rust_ts = None

    heads = ["ls", "ls -la", "ls\t-la", "lsx", "find .", "find", "cat x", "cat", "head -1 x", "tail -f x", "grep x y", "wc -l", "echo hi", "echo", "pwd", "pwd x", "date", "date -u",
             "mkdir d", "touch f", "python", "python3", "python3.11 x", "python3.11", "python3x", "python x.py", "python٣ x", "python٣.٤ x", "python3.", "pythonx", "pytest", "pytest -q",
             "cargo test", "cargo check", "cargo build", "cargo fmt", "cargo clippy", "cargo run", "cargo", "cargo testx", "redis-cli", "redis-cli ping", "systemctl is-active x",
             "systemctl status x", "systemctl stop x", "systemctl status", "du -h", "du", "df -h", "df", "free", "free -m", "ps aux", "ps", "whoami", "id", "", " ", "\x1c", "LS", "Ls"]
    banned = ["rm", "mv", "cp", "wget", "curl", "chmod", "chown", "sudo", "su", "kill", "pkill", "reboot", "shutdown", "|", "&", ";", "`", "$", "..", "farm", "rm_x", "rmdir", "суrm",
              "дrm", "́rm", "rḿ", "cp2", "2cp", "s-u", "sudoku", "kill-9", "$x", "a;b", "a|b", "a&&b", "../x", "x/..", "x...", ".", "..."]
    cmds = list(heads) + [h + " " + b for h in heads for b in banned] + [b + " " + h for h in heads[:20] for b in banned[:20]]
    for h in heads:
        cmds += [" " + h, h + " ", "\x1c" + h + "\x1f", "\t" + h + "\n", h.upper(), h + "\n", h + "\n" + h, h + " # x"]
    for _ in range(6000):
        cmds.append(rng.choice(heads) + rng.choice(["", " ", "  ", "\x1c", "\t"]) + rng.choice(banned + heads + ["x", "-l", "file.txt", "é", "٣"]) + rng.choice(["", " x", "́"]))
    n = 0
    for full, net in ((None, None), (None, "1"), ("1", None), ("0", None), ("", None), ("", ""), ("0", "0"), ("1", "1"), (None, "")):
        for c in cmds:
            n += 1
            a, b = run(False, c, full, net), run(True, c, full, net)
            check(f"allowed FULL={full!r} NET={net!r} {c[:40]!r}", a == b, f"python={a} rust={b}")
    for bad in (None, 5, b"ls"):
        check(f"тип {bad!r}", run(False, bad, None, None) == run(True, bad, None, None))

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_TOOL_SHELL_ENGINE")
    try:
        os.environ.pop("YANDI_TOOL_SHELL_ENGINE", None)
        ts._rust_ts = None
        check("G0 по умолчанию активен Python-движок", ts._get_rust_ts() is None)
        os.environ["YANDI_TOOL_SHELL_ENGINE"] = "rust"
        ts._rust_ts = None
        check("G1 переменная окружения переключает на Rust", ts._get_rust_ts() is rs)
        ts.WHITELIST.append(r"^whoami$")
        try:
            ts._rust_ts = None
            check("G2 изменённый WHITELIST уважается (Python-путь)", ts._allowed("whoami") is True)
        finally:
            ts.WHITELIST.remove(r"^whoami$")
        os.environ.pop("YANDI_TOOL_SHELL_ENGINE", None)
        ts._rust_ts = None
        check("G3 выключение возвращает Python (кэш не залипает)", ts._get_rust_ts() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_TOOL_SHELL_ENGINE", None)
        else:
            os.environ["YANDI_TOOL_SHELL_ENGINE"] = saved
        ts._rust_ts = None
    src = (ROOT / "agent" / "tools" / "tool_shell.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(сравнений: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
