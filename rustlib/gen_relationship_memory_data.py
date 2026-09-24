"""
rustlib/gen_relationship_memory_data.py — генератор rustlib/yandi_rs/src/relationship_memory_data.rs.

Таблицы стеммера агента/relationship_memory.py берутся ПРЯМО ИЗ ПИТОНА: `_STEM_SUFFIXES` (порядок важен — сортировка по длине по убыванию,
устойчивая, побеждает первый подходящий) и `_NON_CONTENT_STEMS` (стемы «неинформативных» слов, посчитанные самим `_stem`).

Перезапуск: python rustlib/gen_relationship_memory_data.py > rustlib/yandi_rs/src/relationship_memory_data.rs
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent.relationship_memory as rm  # noqa: E402


def lit(s):
    return '"' + "".join(f"\\u{{{ord(c):X}}}" if ord(c) > 127 or c in '"\\' else c for c in s) + '"'


print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_relationship_memory_data.py — не править руками.")
print()
print(f"pub static STEM_SUFFIXES: [&str; {len(rm._STEM_SUFFIXES)}] = [")
for s in rm._STEM_SUFFIXES:
    print(f"    {lit(s)},")
print("];")
print()
stems = sorted(rm._NON_CONTENT_STEMS)
print(f"pub static NON_CONTENT_STEMS: [&str; {len(stems)}] = [")
for s in stems:
    print(f"    {lit(s)},")
print("];")
