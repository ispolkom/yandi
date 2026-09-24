"""
rustlib/gen_py_case_table.py — генератор rustlib/yandi_rs/src/py_case_table.rs.

Python `str.isupper()` (unicode_isupper_impl): ложь, если ЕСТЬ хоть один символ Lowercase или Titlecase;
истина, если нет таковых и есть хоть один Uppercase. Определения «Lowercase/Uppercase» у Python — свойства
Unicode (Lowercase/Uppercase, включая Other_*), «Titlecase» — категория Lt. Rust std знает первые два по
СВОЕЙ версии Unicode и не знает категорий — поэтому три набора диапазонов строятся из самого Python.

Перезапуск: python rustlib/gen_py_case_table.py > rustlib/yandi_rs/src/py_case_table.rs
"""
import sys
import unicodedata


def ranges(pred):
    out, start = [], None
    for cp in range(0x110000):
        ok = pred(cp)
        if ok and start is None:
            start = cp
        elif not ok and start is not None:
            out.append((start, cp - 1))
            start = None
    if start is not None:
        out.append((start, 0x10FFFF))
    return out


tables = [
    ("PY_LOWER_RANGES", ranges(lambda cp: chr(cp).islower())),
    ("PY_UPPER_RANGES", ranges(lambda cp: chr(cp).isupper())),
    ("PY_TITLE_RANGES", ranges(lambda cp: unicodedata.category(chr(cp)) == "Lt")),
]
print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_py_case_table.py — не править руками.")
print(f"//! Python {sys.version.split()[0]}, Unicode {unicodedata.unidata_version}: диапазоны кодовых точек, для которых")
print("//! Py_UNICODE_ISLOWER / ISUPPER / ISTITLE истинны (основа `str.isupper()` и родственных).")
for name, r in tables:
    print()
    print(f"pub static {name}: [(u32, u32); {len(r)}] = [")
    for a, b in r:
        print(f"    (0x{a:X}, 0x{b:X}),")
    print("];")
