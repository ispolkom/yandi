"""
rustlib/gen_py_unassigned_table.py — генератор rustlib/yandi_rs/src/py_unassigned_table.rs.

Диапазоны кодовых точек, НЕ назначенных в Unicode самого Python (`unicodedata.category(c) == "Cn"`; 3.11.2 → Unicode 14).
Крейт `unicode-normalization` знает более новый Unicode и составляет/раскладывает символы Unicode 15/16, которых Python 3.11 не знает
(например U+113C2+U+113B8 → U+113C5). Такой символ для Python — обычный «стартер» без композиций, поэтому нормализация последовательности
равна конкатенации нормализаций кусков между ними (см. `py_text::py_nfc`).

Перезапуск: python rustlib/gen_py_unassigned_table.py > rustlib/yandi_rs/src/py_unassigned_table.rs
"""
import sys
import unicodedata


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


r = ranges(lambda cp: unicodedata.category(chr(cp)) == "Cn")
print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_py_unassigned_table.py — не править руками.")
print(f"//! Python {sys.version.split()[0]}, Unicode {unicodedata.unidata_version}: диапазоны НЕ назначенных кодовых точек (категория Cn).")
print()
print(f"pub static PY_UNASSIGNED: [(u32, u32); {len(r)}] = [")
for a, b in r:
    print(f"    (0x{a:X}, 0x{b:X}),")
print("];")
