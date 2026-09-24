"""
rustlib/gen_py_icase_table.py — генератор rustlib/yandi_rs/src/py_icase_table.rs.

`re.IGNORECASE` в Python для str-паттернов сравнивает `_sre.unicode_tolower(символ)` (простое нижнее
отображение) плюс дополнительные эквивалентности `re._casefix._EXTRA_CASES` (i~ı, s~ſ, µ~μ, ...). Крейт
`regex` использует Unicode simple case FOLDING — это ДРУГОЕ отношение (например İ, ı, ſ, K, µ ведут себя иначе).
Таблица — группы кодовых точек, которые Python считает одним и тем же символом при IGNORECASE (только группы
из >=2 элементов); её использует транслятор паттернов rustlib/yandi_rs/src/py_regex.rs. Построено из самого
Python и проверено против настоящего `re.I` (0 расхождений).

Перезапуск: python rustlib/gen_py_icase_table.py > rustlib/yandi_rs/src/py_icase_table.rs
"""
import collections
import sys
import unicodedata

import _sre
import re._casefix as cf

low = _sre.unicode_tolower
parent = {}


def find(a):
    parent.setdefault(a, a)
    while parent[a] != a:
        parent[a] = parent[parent[a]]
        a = parent[a]
    return a


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[max(ra, rb)] = min(ra, rb)


for k, vs in cf._EXTRA_CASES.items():
    for v in vs:
        union(k, v)
groups = collections.defaultdict(list)
for cp in range(0x110000):
    if 0xD800 <= cp <= 0xDFFF:
        continue
    groups[find(low(cp))].append(cp)
multi = sorted(v for v in groups.values() if len(v) > 1)
print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_py_icase_table.py — не править руками.")
print(f"//! Python {sys.version.split()[0]}, Unicode {unicodedata.unidata_version}: группы кодовых точек, эквивалентных при re.IGNORECASE.")
print()
print(f"pub static PY_ICASE_GROUPS: [&[u32]; {len(multi)}] = [")
for g in multi:
    print("    &[" + ", ".join(f"0x{c:X}" for c in g) + "],")
print("];")
