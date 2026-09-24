"""
rustlib/gen_py_lower_table.py — генератор rustlib/yandi_rs/src/py_lower_table.rs.

Python `str.lower()` знает регистр по СВОЕЙ версии Unicode (3.11.2 → Unicode 14), а Rust `str::to_lowercase` — по версии
std (новее): символы, добавленные в Unicode 15/16 (например U+A7CB, U+A7D2, U+A7D4), Rust понижает, Python — нет, а от этого
меняется, является ли результат «словесным» символом. Поэтому таблица одиночных отображений берётся из самого Python.
Финальная сигма (контекстная, CPython `handle_capital_sigma`) — правило в Rust (`py_text::py_lower`), а классы символов «cased» и
«case-ignorable» берутся у Python ЧЕРНЫМ ЯЩИКОМ: для символа c смотрим, чем кончается ("1"+c+"Σ").lower() и ("a"+c+"Σ").lower():
  A = "1"+c+"Σ" даёт «ς»  ⇒ c cased и НЕ ignorable;   B = "a"+c+"Σ" даёт «ς» и не A ⇒ c ignorable (пропускается при просмотре).

Перезапуск: python rustlib/gen_py_lower_table.py > rustlib/yandi_rs/src/py_lower_table.rs
"""
import sys
import unicodedata

rows = []
for cp in range(0x110000):
    if 0xD800 <= cp <= 0xDFFF:
        continue
    c = chr(cp)
    lo = c.lower()
    if lo != c:
        rows.append((cp, lo))
print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_py_lower_table.py — не править руками.")
print(f"//! Python {sys.version.split()[0]}, Unicode {unicodedata.unidata_version}: одиночные отображения `str.lower()` (без контекстной финальной сигмы).")
print()
print(f"pub static PY_LOWER_MAP: [(u32, &str); {len(rows)}] = [")
for cp, lo in rows:
    esc = "".join(f"\\u{{{ord(x):X}}}" for x in lo)
    print(f'    (0x{cp:X}, "{esc}"),')
print("];")


def ranges(pred):
    out, start = [], None
    for cp in range(0x110000):
        ok = (not 0xD800 <= cp <= 0xDFFF) and pred(cp)
        if ok and start is None:
            start = cp
        elif not ok and start is not None:
            out.append((start, cp - 1))
            start = None
    if start is not None:
        out.append((start, 0x10FFFF))
    return out


def final(prefix, c):
    return (prefix + chr(c) + "\u03a3").lower().endswith("\u03c2")


cased = ranges(lambda cp: final("1", cp))
ignorable = ranges(lambda cp: final("a", cp) and not final("1", cp))
for name, r in (("PY_SIGMA_CASED", cased), ("PY_SIGMA_IGNORABLE", ignorable)):
    print()
    print(f"pub static {name}: [(u32, u32); {len(r)}] = [")
    for a, b in r:
        print(f"    (0x{a:X}, 0x{b:X}),")
    print("];")
