"""
rustlib/gen_policy_data.py — генератор rustlib/yandi_rs/src/policy_data.rs.

Паттерны сканера секретов, белые списки, allow/block-списки shell и сети берутся ПРЯМО ИЗ agent/policy.py — не переписываются
руками (в безопасностном модуле опечатка в паттерне = пропущенный секрет).
Перезапуск: python rustlib/gen_policy_data.py > rustlib/yandi_rs/src/policy_data.rs
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import policy  # noqa: E402


def raw(s):
    assert '"##' not in s
    return 'r##"' + s + '"##'


def strs(name, items):
    print(f"pub static {name}: &[&str] = &[" + ", ".join(raw(i) for i in items) + "];")


print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_policy_data.py из agent/policy.py — не править руками.")
print()
print("/// (тип находки, исходный текст паттерна Python `re`) в порядке сканирования")
print("pub static SECRET_PATTERNS: &[(&str, &str)] = &[")
for name, pat in policy._SECRET_PATTERNS:
    print(f"    ({raw(name)}, {raw(pat.pattern)}),")
print("];")
strs("SECRET_WHITELIST", sorted(policy._SECRET_WHITELIST))
strs("SAFE_HOSTS", sorted(policy._SAFE_HOSTS))
strs("SHELL_ALLOWLIST", policy._SHELL_ALLOWLIST)
strs("SHELL_BLOCKLIST", policy._SHELL_BLOCKLIST)
strs("NETWORK_ALLOW", policy._NETWORK_ALLOW)
