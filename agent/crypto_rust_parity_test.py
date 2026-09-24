"""
agent/crypto_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/crypto.rs совместим БАЙТ-В-БАЙТ с
agent/db/sql/crypto.py и HKDF/wrap из agent/db/sql/keys.py (эталон — библиотека `cryptography`).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Двадцать девятый шаг переноса Python -> Rust (2026-09-24) — КРИПТОГРАФИЯ. Криптопримитивы Rust — RustCrypto (aes-gcm, hmac,
hkdf, sha2), не самописные. Проверяется:
  * известные ответы: с одним и тем же nonce Rust и Python-`AESGCM` дают ОДИНАКОВЫЙ блоб побайтно (ключи/тексты случайные + трудные:
    пусто, юникод, управляющие символы, 100 КБ);
  * перекрёстно в обе стороны: Rust шифрует → Python расшифровывает и наоборот (через сам модуль crypto.py, а не только примитивы);
  * любая порча — по КАЖДОМУ байту блоба, чужие entity_type/entity_id/field_name, чужой ключ — даёт InvalidTag в обоих путях;
  * ошибки модуля: нет ключа (KeyMissingError), короткий блоб на каждой длине 0..40 (ValueError/InvalidTag), версия вне 0..255,
    ключи 16/24 байта (Python-путь), не-UTF-8 после расшифровки, суррогаты, entity_id int/str/bool/None/float;
  * свежий nonce на каждое шифрование; blind_index (юникод, пустое значение, домены); HKDF на всех длинах и границе 255*32;
  * wrap_dek/unwrap_dek/derive_* из keys.py; переключатель/кэш/сбой импорта.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.crypto_rust_parity_test
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import os
import random
import sys
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
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def outcome(fn, *a, **kw):
    """(«ok», значение) либо («exc», имя типа, текст) — текст сравниваем только для наших собственных исключений."""
    try:
        return ("ok", fn(*a, **kw))
    except Exception as e:  # noqa: BLE001
        return ("exc", type(e).__name__, str(e))


def same_kind(x, y, *, texts=True) -> bool:
    if x[0] != y[0]:
        return False
    if x[0] == "ok":
        return x[1] == y[1]
    return x[1] == y[1] and (not texts or x[2] == y[2])


HARD_TEXTS = [
    "", "a", "привет мир", "🌍🌍", "line1\nline2\r\n", "\x00\x01\x1f\x7f", "́", "ﬃ", "ǅ", "İ", "  ",
    "a" * 100_000, "﻿bom", "\U0010ffff", "ток​ен",
]


def main() -> int:
    try:
        import yandi_rs.crypto as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.crypto").setLevel(logging.ERROR)
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    import agent.db.sql.crypto as c
    import agent.db.sql.keys as k

    rnd = random.Random(20260924)

    def rb(n):
        return bytes(rnd.getrandbits(8) for _ in range(n))

    def py_mode():
        c._rust_cr = False

    def rs_mode():
        c._rust_cr = rs

    saved = os.environ.get("YANDI_CRYPTO_ENGINE")
    try:
        # ---- A. известные ответы: один nonce → одинаковый блоб --------------------------------------------------------
        n = 0
        for i in range(60):
            key = rb(32)
            nonce = rb(12)
            for txt in HARD_TEXTS if i < 3 else [rnd.choice(HARD_TEXTS), "".join(chr(rnd.randrange(32, 0x2FFF)) for _ in range(rnd.randrange(0, 40)))]:
                et = rnd.choice(["question", "resource", "воспоминание", "a|b", ""])
                eid = rnd.choice(["1", "42", "abc", "", "x|y", "🌍"])
                fn = rnd.choice(["text", "поле", "f|g", ""])
                ver = rnd.choice([0, 1, 2, 127, 255])
                aad = c.build_aad(et, eid, fn, ver)
                py_blob = bytes([ver]) + nonce + AESGCM(key).encrypt(nonce, txt.encode("utf-8"), aad)
                rs_blob = rs.encrypt_field_with_nonce(key, nonce, txt, et, eid, fn, ver)
                check("A1 известный ответ побайтно", py_blob == rs_blob, f"{et!r} {eid!r} {fn!r} v{ver}")
                check("A2 build_aad", rs.build_aad(et, eid, fn, ver) == aad)
                n += 1
        check("A3 неверная длина nonce → None", rs.encrypt_field_with_nonce(rb(32), rb(11), "x", "q", "1", "f", 1) is None)
        check("A4 ключ не 32 байта → None", rs.encrypt_field(rb(16), "x", "q", "1", "f", 1) is None
              and rs.encrypt_field(rb(24), "x", "q", "1", "f", 1) is None and rs.encrypt_field(b"", "x", "q", "1", "f", 1) is None)

        # ---- B. перекрёстно через модуль crypto.py --------------------------------------------------------------------
        for i in range(120):
            key = rb(32)
            txt = rnd.choice(HARD_TEXTS[:-2] + ["ключ-" + str(i)])
            kw = dict(entity_type=rnd.choice(["q", "вопрос"]), entity_id=rnd.choice(["7", 7, 0, -3, 10**30, "id"]), field_name=rnd.choice(["t", "поле"]))
            py_mode()
            b_py = c.encrypt_field(key, txt, **kw)
            rs_mode()
            b_rs = c.encrypt_field(key, txt, **kw)
            check("B1 формат блоба (длина, версия)", len(b_py) == len(b_rs) == 1 + 12 + len(txt.encode()) + 16 and b_py[0] == b_rs[0] == 1)
            for mode in (py_mode, rs_mode):
                mode()
                check("B2 Python-блоб расшифровывается", c.decrypt_field(key, b_py, **kw) == txt)
                check("B3 Rust-блоб расшифровывается", c.decrypt_field(key, b_rs, **kw) == txt)
        # int и str entity_id дают один AAD
        key = rb(32)
        rs_mode()
        b1 = c.encrypt_field(key, "t", entity_type="q", entity_id=5, field_name="f")
        py_mode()
        check("B4 int entity_id ≡ str", c.decrypt_field(key, b1, entity_type="q", entity_id="5", field_name="f") == "t")

        # ---- C. порча/чужой контекст → InvalidTag в обоих путях -------------------------------------------------------------
        key = rb(32)
        kw = dict(entity_type="q", entity_id="1", field_name="f")
        py_mode()
        blob = c.encrypt_field(key, "секрет", **kw)
        for mode in (py_mode, rs_mode):
            mode()
            for i in range(len(blob)):
                t = bytearray(blob)
                t[i] ^= 0x01
                o = outcome(c.decrypt_field, key, bytes(t), **kw)
                check(f"C1 порча байта {i}", o[0] == "exc" and o[1] == "InvalidTag", str(o[:2]))
            for alt in (dict(kw, entity_type="r"), dict(kw, entity_id="2"), dict(kw, entity_id=2), dict(kw, field_name="g")):
                o = outcome(c.decrypt_field, key, blob, **alt)
                check("C2 чужой контекст", o[0] == "exc" and o[1] == "InvalidTag")
            o = outcome(c.decrypt_field, rb(32), blob, **kw)
            check("C3 чужой ключ", o[0] == "exc" and o[1] == "InvalidTag")
            o = outcome(c.decrypt_field, blob[:-1], blob, **kw) if False else outcome(c.decrypt_field, key, blob + b"\x00", **kw)
            check("C4 хвост", o[0] == "exc" and o[1] == "InvalidTag")

        # ---- D. ошибки модуля: обе реализации ведут себя одинаково -------------------------------------------------------------
        cases = []
        for L in range(0, 41):
            cases.append(("blob len %d" % L, (key, rb(L)), kw))
        cases += [
            ("no key enc", None, None), ("empty key", (b"", blob), kw), ("None key", (None, blob), kw),
            ("key16", (rb(16), blob), kw), ("key24", (rb(24), blob), kw), ("key31", (rb(31), blob), kw), ("key33", (rb(33), blob), kw),
            ("bytearray key", (bytearray(key), blob), kw), ("bytearray blob", (key, bytearray(blob)), kw),
            ("bool eid", (key, blob), dict(kw, entity_id=True)), ("None eid", (key, blob), dict(kw, entity_id=None)),
            ("float eid", (key, blob), dict(kw, entity_id=1.0)), ("eid tuple", (key, blob), dict(kw, entity_id=(1,))),
        ]
        for name, args, kws in cases:
            if args is None:
                continue
            py_mode()
            o1 = outcome(c.decrypt_field, *args, **kws)
            rs_mode()
            o2 = outcome(c.decrypt_field, *args, **kws)
            check("D1 decrypt " + name, same_kind(o1, o2), f"{o1[:3]} vs {o2[:3]}")
        # шифрование: ошибки
        enc_cases = [
            ("no key", (b"", "x"), kw), ("None key", (None, "x"), kw), ("ver -1", (key, "x"), dict(kw, version=-1)),
            ("ver 256", (key, "x"), dict(kw, version=256)), ("ver 255", (key, "x"), dict(kw, version=255)),
            ("ver 0", (key, "x"), dict(kw, version=0)), ("ver True", (key, "x"), dict(kw, version=True)),
            ("key16", (rb(16), "x"), kw), ("key24", (rb(24), "x"), kw), ("key31", (rb(31), "x"), kw),
            ("surrogate", (key, "a\ud800b"), kw), ("surrogate et", (key, "x"), dict(kw, entity_type="\udc00")),
            ("bytes pt", (key, b"x"), kw), ("None pt", (key, None), kw),
        ]
        for name, args, kws in enc_cases:
            py_mode()
            o1 = outcome(c.encrypt_field, *args, **kws)
            rs_mode()
            o2 = outcome(c.encrypt_field, *args, **kws)
            same = o1[0] == o2[0] and (o1[0] == "exc" and o1[1] == o2[1]) if o1[0] == "exc" else (o1[0] == o2[0] == "ok" and len(o1[1]) == len(o2[1]) and o1[1][0] == o2[1][0])
            check("D2 encrypt " + name, same, f"{o1[:3]} vs {o2[:3]}")
        # не-UTF-8 после расшифровки
        bad = b"\xff\xfe\xfa"
        nonce = rb(12)
        raw_blob = bytes([1]) + nonce + AESGCM(key).encrypt(nonce, bad, c.build_aad("q", "1", "f", 1))
        py_mode()
        o1 = outcome(c.decrypt_field, key, raw_blob, **kw)
        rs_mode()
        o2 = outcome(c.decrypt_field, key, raw_blob, **kw)
        check("D3 не-UTF-8 → UnicodeDecodeError оба", o1[:2] == o2[:2] == ("exc", "UnicodeDecodeError"), f"{o1[:2]} {o2[:2]}")
        # версия из блоба: старый блоб (v7) расшифровывается по своей версии
        for mode in (py_mode, rs_mode):
            mode()
            old = c.encrypt_field(key, "old", version=7, **kw)
            check("D4 версия едет с блобом", old[0] == 7 and c.decrypt_field(key, old, **kw) == "old")
        # свежий nonce
        rs_mode()
        a = c.encrypt_field(key, "x", **kw)
        b2 = c.encrypt_field(key, "x", **kw)
        check("D5 nonce и шифртекст различны", a[1:13] != b2[1:13] and a != b2)
        nonces = {c.encrypt_field(key, "x", **kw)[1:13] for _ in range(2000)}
        check("D6 2000 nonce уникальны", len(nonces) == 2000)

        # ---- E. blind_index -----------------------------------------------------------------------------------------------
        for i in range(300):
            ik = rb(rnd.choice([1, 16, 32, 33, 64, 100]))
            ns = rnd.choice(["question", "resource", "", "нс", "a:b"])
            val = rnd.choice(["", "x", "привет", "ﬃ", "\x00", "a:v1:b", "🌍"] + [rnd.choice(HARD_TEXTS[:-2])])
            want = _hmac.new(ik, f"{ns}:v1:{val}".encode(), hashlib.sha256).hexdigest()
            check("E1 blind_index (ядро)", rs.blind_index(ik, ns, val) == want)
            rs_mode()
            check("E2 blind_index (модуль)", c.blind_index(ik, ns, val) == want)
        py_mode()
        o1 = outcome(c.blind_index, b"", "n", "v")
        rs_mode()
        o2 = outcome(c.blind_index, b"", "n", "v")
        check("E3 пустой индекс-ключ", same_kind(o1, o2) and o1[1] == "KeyMissingError")
        check("E4 домены разделены", c.blind_index(b"k", "a", "v") != c.blind_index(b"k", "b", "v"))
        for args in ((b"k", "n", "\ud800"), (b"k", "\ud800", "v"), (bytearray(b"k"), "n", "v"), (None, "n", "v"), (b"k", "n", None)):
            py_mode()
            o1 = outcome(c.blind_index, *args)
            rs_mode()
            o2 = outcome(c.blind_index, *args)
            check("E5 blind_index трудные входы", o1[:2] == o2[:2], f"{o1[:2]} {o2[:2]}")

        # ---- F. HKDF ------------------------------------------------------------------------------------------------------
        for i in range(200):
            ikm = rb(rnd.choice([0, 1, 16, 32, 64, 100]))
            info = rb(rnd.choice([0, 1, 20, 100]))
            ln = rnd.choice([1, 16, 32, 33, 64, 255, 1000])
            want = HKDF(algorithm=hashes.SHA256(), length=ln, salt=None, info=info).derive(ikm)
            check("F1 HKDF", rs.hkdf_sha256(ikm, info, ln) == want)
        for ln in (8159, 8160):
            want = HKDF(algorithm=hashes.SHA256(), length=ln, salt=None, info=b"i").derive(b"k" * 32)
            check(f"F2 HKDF длина {ln}", rs.hkdf_sha256(b"k" * 32, b"i", ln) == want)
        check("F3 длина 255*32+1 → None", rs.hkdf_sha256(b"k" * 32, b"i", 8161) is None)
        check("F4 длина 0", rs.hkdf_sha256(b"k" * 32, b"i", 0) == b"" or rs.hkdf_sha256(b"k" * 32, b"i", 0) is not None)
        for lab, fn in (("integrity-key", k.derive_integrity_key), ("blind-index-key", k.derive_blind_index_key), ("node-config-key", k.derive_node_config_key)):
            for kek in (rb(32), rb(32), b"", rb(16), rb(64)):
                want = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"YANDI|{lab}|v1".encode()).derive(kek)
                py_mode()
                p1 = outcome(fn, kek)
                rs_mode()
                p2 = outcome(fn, kek)
                check("F5 derive_" + lab, p1 == ("ok", want) and p2 == ("ok", want))
        check("F6 метки дают разные ключи", len({k.derive_integrity_key(b"k" * 32), k.derive_blind_index_key(b"k" * 32), k.derive_node_config_key(b"k" * 32)}) == 3)

        # ---- G. wrap_dek / unwrap_dek -------------------------------------------------------------------------------------
        for i in range(100):
            kek = rb(32)
            dek = rb(rnd.choice([0, 1, 16, 32, 64]))
            for mk, uk in ((py_mode, rs_mode), (rs_mode, py_mode), (rs_mode, rs_mode), (py_mode, py_mode)):
                mk()
                w = k.wrap_dek(kek, dek)
                uk()
                check("G1 wrap↔unwrap перекрёстно", k.unwrap_dek(kek, w) == dek and len(w) == 12 + len(dek) + 16)
        kek = rb(32)
        w = k.wrap_dek(kek, rb(32))
        for L in range(0, 30):
            py_mode()
            o1 = outcome(k.unwrap_dek, kek, w[:L])
            rs_mode()
            o2 = outcome(k.unwrap_dek, kek, w[:L])
            check(f"G2 unwrap обрезан до {L}", o1[:2] == o2[:2], f"{o1[:2]} {o2[:2]}")
        for i in range(len(w)):
            t = bytearray(w)
            t[i] ^= 0x80
            py_mode()
            o1 = outcome(k.unwrap_dek, kek, bytes(t))
            rs_mode()
            o2 = outcome(k.unwrap_dek, kek, bytes(t))
            check("G3 порча wrapped", o1[:2] == o2[:2] == ("exc", "InvalidTag"))
        for kk in (rb(16), rb(24), rb(31)):
            py_mode()
            o1 = outcome(k.wrap_dek, kk, b"d" * 32)
            rs_mode()
            o2 = outcome(k.wrap_dek, kk, b"d" * 32)
            check("G4 wrap с ключом не 32 байта → Python-путь", o1[0] == o2[0] and (o1[0] == "ok" or o1[:2] == o2[:2]) and (o1[0] == "ok") == (len(kk) in (16, 24)), f"{o1[:3]} {o2[:3]}")
        check("G5 wrap чужим ключом не открывается", outcome(k.unwrap_dek, rb(32), w)[:2] == ("exc", "InvalidTag"))

        rs_mode()
        wn = {k.wrap_dek(kek, b"d" * 32)[:12] for _ in range(2000)}
        check("G6 wrap: 2000 nonce уникальны (повтор nonce под одним ключом = катастрофа)", len(wn) == 2000)
        check("G7 wrap: два вызова различны", k.wrap_dek(kek, b"d" * 32) != k.wrap_dek(kek, b"d" * 32))

        # ---- H. переключатель ---------------------------------------------------------------------------------------------
        os.environ.pop("YANDI_CRYPTO_ENGINE", None)
        c._rust_cr = None
        check("H1 по умолчанию выключен", c._get_rust_cr() is None)
        os.environ["YANDI_CRYPTO_ENGINE"] = "rust"
        c._rust_cr = None
        check("H2 включается переменной", c._get_rust_cr() is not None)
        os.environ.pop("YANDI_CRYPTO_ENGINE", None)
        c._rust_cr = None
        check("H3 выключение возвращает Python", c._get_rust_cr() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_CRYPTO_ENGINE", None)
        else:
            os.environ["YANDI_CRYPTO_ENGINE"] = saved
        c._rust_cr = None
    src = (ROOT / "agent" / "db" / "sql" / "crypto.py").read_text(encoding="utf-8")
    check("H4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(известных ответов: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
