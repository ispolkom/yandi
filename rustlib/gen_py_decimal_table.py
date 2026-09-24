"""
rustlib/gen_py_decimal_table.py — генератор rustlib/yandi_rs/src/py_decimal_table.rs.

Python `float(str)` переводит любые десятичные цифры Unicode (категория Nd: арабо-индийские,
деванагари, полноширинные и т.д.) в ASCII-цифры ДО разбора. Rust `str::parse::<f64>` понимает
только ASCII. Таблица — кодовые точки "нулей" каждой такой десятичной серии (0..9 идут подряд);
строится из самого Python (`unicodedata.decimal`).

Перезапуск: python rustlib/gen_py_decimal_table.py > rustlib/yandi_rs/src/py_decimal_table.rs
"""
import sys
import unicodedata

zeros = []
for cp in range(0x110000):
    if unicodedata.decimal(chr(cp), None) == 0:
        # проверка допущения "серия из 10 подряд идущих кодовых точек"
        assert all(unicodedata.decimal(chr(cp + d), None) == d for d in range(10)), hex(cp)
        zeros.append(cp)
# и ни одна цифра не осталась вне серий
covered = {z + d for z in zeros for d in range(10)}
assert all((unicodedata.decimal(chr(cp), None) is None) == (cp not in covered) for cp in range(0x110000))

print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_py_decimal_table.py — не править руками.")
print(f"//! Python {sys.version.split()[0]}, Unicode {unicodedata.unidata_version}: кодовые точки цифры «0» каждой десятичной серии Unicode (0..9 подряд).")
print()
print(f"pub static PY_DECIMAL_ZEROS: [u32; {len(zeros)}] = [")
for z in zeros:
    print(f"    0x{z:X},")
print("];")
