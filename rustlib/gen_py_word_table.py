"""
rustlib/gen_py_word_table.py — генератор rustlib/yandi_rs/src/py_word_table.rs.

Python `re` для str-паттернов считает символ «словесным» (`\\w`) так: `ch.isalnum() or ch == '_'`.
Крейт `regex` в Rust определяет `\\w` по другим свойствам Unicode (Alphabetic + M + Nd + Pc...) —
они расходятся (например, '²', комбинирующие знаки). Чтобы Rust-перенос не гадал, а копировал
Python ТОЧНО, таблица диапазонов строится из самого Python.

Перезапуск (если сменилась версия Python/Unicode):
    python rustlib/gen_py_word_table.py > rustlib/yandi_rs/src/py_word_table.rs
"""
import sys
import unicodedata


def ranges():
    out, start = [], None
    for cp in range(0x110000):
        ch = chr(cp)
        ok = ch.isalnum() or ch == "_"
        if ok and start is None:
            start = cp
        elif not ok and start is not None:
            out.append((start, cp - 1))
            start = None
    if start is not None:
        out.append((start, 0x10FFFF))
    return out


r = ranges()
print(f"//! АВТОГЕНЕРИРОВАНО rustlib/gen_py_word_table.py — не править руками.")
print(f"//! Python {sys.version.split()[0]}, Unicode {unicodedata.unidata_version}: диапазоны кодовых точек,")
print("//! для которых Python `ch.isalnum() or ch == '_'` истинно (это и есть `\\w` в `re` для str).")
print()
print(f"pub static PY_WORD_RANGES: [(u32, u32); {len(r)}] = [")
for a, b in r:
    print(f"    (0x{a:X}, 0x{b:X}),")
print("];")
