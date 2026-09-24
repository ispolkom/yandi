"""
rustlib/gen_orch_query_framer_data.py — генератор rustlib/yandi_rs/src/orch_query_framer_data.rs.

Таблицы agent/orch_query_framer.py берутся ПРЯМО ИЗ ПИТОНА: `_SAFE_DOMAINS`, `_GENERIC_OBJECTS` (множества) и `_MISSING_TO_QUESTION`
(словарь: порядок вставки важен — побеждает первое вхождение ключа в первый элемент `missing`).

Перезапуск: python rustlib/gen_orch_query_framer_data.py > rustlib/yandi_rs/src/orch_query_framer_data.rs
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent.orch_query_framer as q  # noqa: E402


def lit(s):
    return '"' + "".join(f"\\u{{{ord(c):X}}}" if ord(c) > 127 or c in '"\\{}' or ord(c) < 32 else c for c in s) + '"'


print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_orch_query_framer_data.py — не править руками.")
print()
sd = sorted(q._SAFE_DOMAINS)
print(f"pub static SAFE_DOMAINS: [&str; {len(sd)}] = [")
for s in sd:
    print(f"    {lit(s)},")
print("];")
print()
go = sorted(q._GENERIC_OBJECTS)
print(f"pub static GENERIC_OBJECTS: [&str; {len(go)}] = [")
for s in go:
    print(f"    {lit(s)},")
print("];")
print()
items = list(q._MISSING_TO_QUESTION.items())
print(f"pub static MISSING_TO_QUESTION: [(&str, &str); {len(items)}] = [")
for k, v in items:
    print(f"    ({lit(k)}, {lit(v)}),")
print("];")
