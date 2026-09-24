"""
rustlib/gen_py_casefold_table.py — генератор rustlib/yandi_rs/src/py_casefold_table.rs.

Python `str.casefold()` — полная свёртка регистра (CaseFolding.txt, статусы C+F) по СВОЕЙ версии Unicode (3.11.2 → Unicode 14).
Крейт `caseless` знает другую версию и расходится на символах, добавленных позже (U+1C89, U+A7CB, U+A7CC…), — поэтому таблица одиночных
отображений берётся из самого Python. Свёртка контекстно-независима (в отличие от финальной сигмы `lower()`).

Перезапуск: python rustlib/gen_py_casefold_table.py > rustlib/yandi_rs/src/py_casefold_table.rs
"""
import sys
import unicodedata

rows = []
for cp in range(0x110000):
    if 0xD800 <= cp <= 0xDFFF:
        continue
    c = chr(cp)
    f = c.casefold()
    if f != c:
        rows.append((cp, f))
print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_py_casefold_table.py — не править руками.")
print(f"//! Python {sys.version.split()[0]}, Unicode {unicodedata.unidata_version}: одиночные отображения `str.casefold()`.")
print()
print(f"pub static PY_CASEFOLD_MAP: [(u32, &str); {len(rows)}] = [")
for cp, f in rows:
    esc = "".join(f"\\u{{{ord(x):X}}}" for x in f)
    print(f'    (0x{cp:X}, "{esc}"),')
print("];")
