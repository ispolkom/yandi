"""
rustlib/gen_tool_shell_data.py — генератор rustlib/yandi_rs/src/tool_shell_data.rs.
WHITELIST/BANNED берутся ПРЯМО ИЗ agent/tools/tool_shell.py (охранный шлюз — опечатка в паттерне недопустима).
Перезапуск: python rustlib/gen_tool_shell_data.py > rustlib/yandi_rs/src/tool_shell_data.rs
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.tools import tool_shell  # noqa: E402


def raw(s):
    assert '"##' not in s
    return 'r##"' + s + '"##'


print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_tool_shell_data.py из agent/tools/tool_shell.py — не править руками.")
print()
print("pub static WHITELIST: &[&str] = &[" + ", ".join(raw(p) for p in tool_shell.WHITELIST) + "];")
print("pub static BANNED: &[&str] = &[" + ", ".join(raw(p) for p in tool_shell.BANNED) + "];")
