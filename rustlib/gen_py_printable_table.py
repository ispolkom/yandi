"""
rustlib/gen_py_printable_table.py — генератор rustlib/yandi_rs/src/py_printable_table.rs.

Python `repr(str)` экранирует символы, для которых `str.isprintable()` ложно (управляющие, форматные,
неназначенные, разделители кроме пробела…) как \\xNN / \\uNNNN / \\UNNNNNNNN. Таблица — диапазоны
НЕпечатаемых кодовых точек >= 0x80 (ASCII обрабатывается кодом), строится из самого Python.

Перезапуск: python rustlib/gen_py_printable_table.py > rustlib/yandi_rs/src/py_printable_table.rs
"""
import sys
import unicodedata

r, start = [], None
for cp in range(0x80, 0x110000):
    bad = not chr(cp).isprintable()
    if bad and start is None:
        start = cp
    elif not bad and start is not None:
        r.append((start, cp - 1))
        start = None
if start is not None:
    r.append((start, 0x10FFFF))
print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_py_printable_table.py — не править руками.")
print(f"//! Python {sys.version.split()[0]}, Unicode {unicodedata.unidata_version}: диапазоны кодовых точек >= 0x80, для которых `str.isprintable()` ложно.")
print()
print(f"pub static PY_NONPRINTABLE_RANGES: [(u32, u32); {len(r)}] = [")
for a, b in r:
    print(f"    (0x{a:X}, 0x{b:X}),")
print("];")
