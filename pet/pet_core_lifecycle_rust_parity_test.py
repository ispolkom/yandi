"""
pet/pet_core_lifecycle_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/core_lifecycle.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что
чистые помощники pet/core_lifecycle.py (`_decode_key`, три регулярки идентификаторов, `_validate`, `_check_key`).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Тридцать третий шаг переноса Python -> Rust (2026-09-24) — граница «узел ⇄ ядро» (контракт): именно эти проверки узел на Rust обязан делать
ТАК ЖЕ. Проверяется:
  * `_decode_key`: валидные 32-байтовые ключи; неканонические «лишние» биты последнего символа (Python принимает — на всех 64 значениях);
    завершающий `\\n` (регулярка `$` его пропускает, `b64decode(validate=True)` — нет), `\\n\\n`, пробелы, URL-safe алфавит, длины 42..45,
    Unicode-цифры/полноширинные, NUL, побайтовые порчи валидного ключа;
  * `_REQUEST_ID_RE`/`_IDEM_RE`/`_SECRET_RE` через `_id_ok`: границы длины (0/1/7/8/63/64/65 СИМВОЛОВ), завершающий `\\n`, не-ASCII;
  * `_validate`: три эндпоинта × сгенерированные документы (лишние поля, `extensions` не объект, ключ/контекст не строки, неверный контекст,
    плохой ключ, `deadline_ms`: bool/float/строка/None/0/-1/огромное/подкласс int), неизвестный эндпоинт (KeyError в обоих), подкласс dict;
  * `_check_key` против настоящего HKDF; `provision_state` в одном режиме → `verify_key` в другом (ok/wrong/unprovisioned);
  * переключатель.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m pet.pet_core_lifecycle_rust_parity_test
"""
from __future__ import annotations

import base64
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
_OK = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _OK
    if condition:
        _OK += 1
    else:
        if len(FAILURES) < 25:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def outcome(fn, *a):
    try:
        return ("ok", fn(*a))
    except Exception as e:  # noqa: BLE001
        return ("exc", type(e).__name__)


class MyInt(int):
    pass


class MyDict(dict):
    pass


def main() -> int:
    try:
        import yandi_rs.core_lifecycle as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import pet.core_lifecycle as cl

    rnd = random.Random(20260924)

    def py():
        cl._rust_cl = False

    def rst():
        cl._rust_cl = rs

    def both(fn, *a):
        py()
        p = outcome(fn, *a)
        rst()
        r = outcome(fn, *a)
        return p, r

    saved = os.environ.get("YANDI_CORE_LIFECYCLE_ENGINE")
    n = 0
    tmp = tempfile.mkdtemp(prefix="yandi-cl-parity-")
    try:
        # ---- A. _decode_key ------------------------------------------------------------------------------------------------
        ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        valid = [base64.b64encode(bytes(rnd.getrandbits(8) for _ in range(32))).decode() for _ in range(300)]
        valid += [base64.b64encode(b"\x00" * 32).decode(), base64.b64encode(b"\xff" * 32).decode()]
        for k in valid:
            p, r = both(cl._decode_key, k)
            check("A1 валидный ключ", p == r and p[0] == "ok" and len(p[1]) == 32, f"{k}: {p} vs {r}")
            n += 1
        # неканонические последние символы (лишние биты)
        base = "A" * 42
        for ch in ALPHA:
            k = base + ch + "="
            p, r = both(cl._decode_key, k)
            check("A2 неканонический последний символ", p == r, f"{k}: {p} vs {r}")
            n += 1
        variants = []
        for k in valid[:40]:
            variants += [k + "\n", k + "\n\n", "\n" + k, " " + k, k + " ", k[:-1], k[:-2], k + "=", k[:-1] + "A", k.replace("+", "-").replace("/", "_"),
                         k[:20] + "\x00" + k[21:], k[:10] + "٣" + k[11:], k[:5] + "Ａ" + k[6:], k[:-1] + "\n", "=" + k[1:], k[:43] + "A", k.rstrip("=") + "==",
                         k[:30] + "=" + k[31:], k.lower(), k.upper()]
        for i in range(0, 44):
            variants += [valid[0][:i] + c + valid[0][i + 1:] for c in ("!", "-", "_", " ", "\n", "=", "é")]
        for L in (0, 1, 42, 43, 44, 45, 46, 100):
            variants.append("A" * L)
            variants.append("A" * (L - 1) + "=" if L else "=")
        for k in variants:
            p, r = both(cl._decode_key, k)
            check("A3 трудные ключи", p == r, f"{k!r}: {p} vs {r}")
            n += 1
        for v in (None, b"AAAA", 5, ["x"], "\ud800" + "A" * 42 + "="):
            p, r = both(cl._decode_key, v)
            check("A4 типы", p == r, f"{v!r}: {p} vs {r}")
            n += 1

        # ---- B. регулярки идентификаторов -------------------------------------------------------------------------------------
        charsets = ["abcdefABCDEF0123456789_-", "abcdef0123456789", "abc", "-_", "ABC", "абв", "٣٣٣", "Ａ", "é", "\n", " ", ".", "!", "\x00", "a\n", "İ"]
        for kind, rx in ((0, cl._REQUEST_ID_RE), (1, cl._IDEM_RE), (2, cl._SECRET_RE)):
            for L in (0, 1, 2, 7, 8, 9, 20, 63, 64, 65, 66, 100):
                for _ in range(120):
                    t = "".join(rnd.choice(rnd.choice(charsets)) for _ in range(L))
                    if rnd.random() < 0.3:
                        t += "\n"
                    if rnd.random() < 0.1:
                        t += "\n"
                    py()
                    p = cl._id_ok(rx, t)
                    rst()
                    r = cl._id_ok(rx, t)
                    check("B1 регулярка идентификатора", p == r == (rx.match(t) is not None), f"{kind} {t!r}: {p} vs {r}")
                    n += 1
            for L in (1, 8, 63, 64):
                for tail in ("", "\n", "\n\n", " ", "\r\n"):
                    t = ("a" * L) + tail
                    py()
                    p = cl._id_ok(rx, t)
                    rst()
                    r = cl._id_ok(rx, t)
                    check("B2 граница + хвост", p == r == (rx.match(t) is not None), f"{kind} {t!r}")
                    n += 1
            # каждая длина 0..70 из СПЛОШНО допустимых символов (и с одним недопустимым) — границы длины и алфавита
            for L in range(0, 71):
                for ch in ("a", "F", "f", "9", "_", "-", "A", "g", "G"):
                    for t in (ch * L, ch * L + "\n", ("a" * L)[: L - 1] + ch if L else ""):
                        py()
                        p = cl._id_ok(rx, t)
                        rst()
                        r = cl._id_ok(rx, t)
                        check("B4 границы длины/алфавита", p == r == (rx.match(t) is not None), f"{kind} {t!r}")
                        n += 1
            for v in (None, b"abcdefgh", 5):
                p, r = both(cl._id_ok, rx, v)
                check("B3 не-строки", p == r, f"{v!r}: {p} vs {r}")
                n += 1

        # ---- C. _validate --------------------------------------------------------------------------------------------------
        good_key = valid[0]
        KEYS = [good_key, "short", "", None, 5, True, ["k"], good_key + "\n", "A" * 44, "A" * 43 + "=", base64.b64encode(b"x" * 31).decode(), "é" * 44]
        CTX = ["yandi/core/v1", "yandi/core/v2", "", None, 5, "YANDI/CORE/V1", "yandi/core/v1 ", ["yandi/core/v1"]]
        EXT = [{}, {"a": 1}, [], None, "x", 0, True, MyDict(), 1.5]
        DL = [1, 2, 5000, 0, -1, True, False, 1.5, "5", None, 10 ** 30, -10 ** 30, MyInt(3), MyInt(0), [1], {}]
        EXTRA = ["foo", "Key", "", "deadline", "extensions "]
        for endpoint in ("unlock", "lock", "shutdown"):
            for _ in range(6000):
                doc = {}
                if rnd.random() < 0.8:
                    doc["key"] = rnd.choice(KEYS)
                if rnd.random() < 0.8:
                    doc["context"] = rnd.choice(CTX)
                if rnd.random() < 0.5:
                    doc["extensions"] = rnd.choice(EXT)
                if rnd.random() < 0.6:
                    doc["deadline_ms"] = rnd.choice(DL)
                if rnd.random() < 0.15:
                    doc[rnd.choice(EXTRA)] = rnd.choice([1, "x", None])
                if endpoint == "unlock" and rnd.random() < 0.6:
                    doc["key"], doc["context"] = good_key, "yandi/core/v1"
                p, r = both(cl._validate, endpoint, doc)
                check("C1 _validate", p == r, f"{endpoint} {doc!r}: {p} vs {r}")
                n += 1
        for endpoint, doc in (("bogus", {}), ("unlock", None), ("unlock", []), ("unlock", "x"), ("lock", MyDict(extensions={})), ("lock", {}), ("shutdown", {}),
                              ("unlock", {"key": good_key, "context": "yandi/core/v1"}), ("unlock", {"key": good_key, "context": "yandi/core/v1", "extensions": {}}),
                              ("shutdown", {"deadline_ms": 1}), ("shutdown", {"deadline_ms": 10 ** 40}), ("lock", {1: 2}), (None, {}), ("Unlock", {})):
            p, r = both(cl._validate, endpoint, doc)
            check("C2 _validate трудные", p == r, f"{endpoint} {doc!r}: {p} vs {r}")
            n += 1

        # ---- D. _check_key и provision/verify ------------------------------------------------------------------------------------
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        for L in (0, 1, 16, 31, 32, 33, 64, 100):
            dk = bytes(rnd.getrandbits(8) for _ in range(L))
            want = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=cl._CHECK_INFO).derive(dk)
            py()
            a = cl._check_key(dk)
            rst()
            b = cl._check_key(dk)
            check("D1 _check_key", a == b == want, f"L={L}")
            n += 1
        for pm, vm in ((py, rst), (rst, py), (rst, rst), (py, py)):
            d = Path(tmp) / f"s{n}"
            key = bytes(rnd.getrandbits(8) for _ in range(32))
            pm()
            cl.provision_state(d, key)
            vm()
            check("D2 provision→verify ok", cl.verify_key(d, key) == "ok")
            check("D3 неверный ключ", cl.verify_key(d, bytes(rnd.getrandbits(8) for _ in range(32))) == "wrong")
            check("D4 нет состояния", cl.verify_key(Path(tmp) / f"none{n}", key) == "unprovisioned")
            n += 1

        # ---- E. переключатель ----------------------------------------------------------------------------------------------------
        os.environ.pop("YANDI_CORE_LIFECYCLE_ENGINE", None)
        cl._rust_cl = None
        check("E1 по умолчанию выключен", cl._get_rust_cl() is None)
        os.environ["YANDI_CORE_LIFECYCLE_ENGINE"] = "rust"
        cl._rust_cl = None
        check("E2 включается переменной", cl._get_rust_cl() is not None)
        os.environ.pop("YANDI_CORE_LIFECYCLE_ENGINE", None)
        cl._rust_cl = None
        check("E3 выключение возвращает Python", cl._get_rust_cl() is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if saved is None:
            os.environ.pop("YANDI_CORE_LIFECYCLE_ENGINE", None)
        else:
            os.environ["YANDI_CORE_LIFECYCLE_ENGINE"] = saved
        cl._rust_cl = None
    src = (ROOT / "pet" / "core_lifecycle.py").read_text(encoding="utf-8")
    check("E4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(сравнений: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
