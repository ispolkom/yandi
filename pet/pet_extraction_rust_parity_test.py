"""
pet/pet_extraction_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/pet_extraction.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что
чистые помощники pet/event_extraction.py (segment_words, _json_object, _validate_candidate), pet/fact_extraction.py (_validate,
looks_secret) и pet/commitment_verification.py (inside_quotation).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Тридцатый шаг переноса Python -> Rust (2026-09-24). Эти функции бегут на КАЖДОЕ сообщение пользователя вокруг вызова модели.
Дифференциальный фаззинг против настоящего Python (оригинальный код вызывается с выключенным переключателем):
  * segment_words — пробелы U+001C..1F/U+0085/U+00A0/U+2028/U+3000 (пробельные по Python) и U+200B/U+FEFF (НЕ пробельные), суррогаты, эмодзи;
  * _json_object — ограждения ```, скобки, обратные апострофы, пробелы вокруг, невалидный JSON, не-строки;
  * _validate_candidate / _validate — сгенерированные кандидаты из «родных» типов JSON и посторонних (bool как int, огромные целые, NaN,
    кортеж, подкласс dict/int, Decimal): результат (кортеж/словарь + текст причины) совпадает ТОЧНО, границы: span 0/last/len, 30/31 слов,
    statement 7/8/200/201 символов ПОСЛЕ strip (в СИМВОЛАХ, не байтах), `\\n` внутри, known 29/30/35 элементов, отсутствующий fact_id (KeyError);
  * looks_secret — руками написанные лукахеды против настоящей регулярки: прогоны 19/20/21, только буквы/только цифры, Unicode-цифра
    сразу за прогоном (закрывает `\\d` — квирк оригинала), «²» (не decimal), ключ `-----BEGIN … KEY-----`;
  * inside_quotation — все пары кавычек, незакрытые, позиции 0..len+2, отрицательные, огромные, bool;
плюс собственные тесты модулей с включённым переключателем (запускает test-all) и работа переключателя.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m pet.pet_extraction_rust_parity_test
"""
from __future__ import annotations

import decimal
import json
import math
import os
import random
import re
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
        if len(FAILURES) < 25:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def outcome(fn, *a):
    try:
        return ("ok", fn(*a))
    except Exception as e:  # noqa: BLE001
        return ("exc", type(e).__name__)


def norm(x):
    """repr с учётом типов (True != 1, 1 != 1.0 для проверки)."""
    return repr(x)


class MyInt(int):
    pass


class MyDict(dict):
    pass


def main() -> int:
    try:
        import yandi_rs.pet_extraction as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.pet_extraction").setLevel(logging.ERROR)
    import pet.event_extraction as ee
    import pet.fact_extraction as fe
    import pet.commitment_verification as cv

    rnd = random.Random(20260924)

    def py():
        ee._rust_pe = False

    def rst():
        ee._rust_pe = rs

    def both(fn, *a):
        py()
        p = outcome(fn, *a)
        rst()
        r = outcome(fn, *a)
        return p, r

    saved = os.environ.get("YANDI_PET_EXTRACTION_ENGINE")
    try:
        # ---- A. segment_words ----------------------------------------------------------------------------------------
        pool = ["a", "б", "слово", "x1", " ", " ", "\t", "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", "\x85", "\xa0",
                " ", " ", " ", " ", " ", " ", " ", "　", "​", "﻿", "‎", "́",
                "🌍", "\U0010ffff", "İ", "ﬃ", "-", "«", "»", "\x00", "\x7f"]
        n = 0
        for _ in range(6000):
            msg = "".join(rnd.choice(pool) for _ in range(rnd.randrange(0, 30)))
            p, r = both(ee.segment_words, msg)
            check("A1 segment_words", p == r, f"{msg!r}: {p} vs {r}")
            n += 1
        for msg in ["", " ", "a", "  a  ", "a b", "\x1cx\x1d", "a\ud800b", "\ud800", "😀 x"]:
            p, r = both(ee.segment_words, msg)
            check("A2 segment_words hard", p == r, f"{msg!r}")
        big = " ".join(f"w{i}" for i in range(5000))
        p, r = both(ee.segment_words, big)
        check("A3 segment_words 5000 слов", p == r)
        for bad in (None, 5, b"x", ["a"]):
            p, r = both(ee.segment_words, bad)
            check("A4 segment_words не-строка", p[:2] == r[:2] or p[0] == r[0] == "exc", f"{bad!r}")

        # ---- B. _json_object -------------------------------------------------------------------------------------------
        jp = ['{"a":1}', "{}", "[]", "1", '"s"', "null", "```", "```json", '```json\n{"a": 1}\n```', '```{"a":1}```', "`{}`", "``{}``", "``", "` ``{}", "``` {} ```",
              "{", "}", "{{", "}}", "x{y}z", '{"a":{"b":[1,2]}}', "NaN", '{"a":NaN}', '{"a":1e999}', " {} ", "\x1c{}\x1d", "﻿{}",
              "```\n```", "```json\n[1,2]\n```", "``` {\"a\":", "text {\"a\":1} tail", "```x} {```", "\n", "", "   "]
        for _ in range(3000):
            s = "".join(rnd.choice(jp) for _ in range(rnd.randrange(1, 4)))
            p, r = both(ee._json_object, s)
            check("B1 _json_object", p == r, f"{s!r}: {p} vs {r}")
        for v in (None, 5, b"{}", ["x"], {"a": 1}, "\ud800{}"):
            p, r = both(ee._json_object, v)
            check("B2 _json_object не-строка/суррогат", p == r, f"{v!r}: {p} vs {r}")

        # ---- C. _validate_candidate ------------------------------------------------------------------------------------
        VALS = [None, True, False, 0, 1, 2, -1, 5, 29, 30, 31, 10 ** 30, -10 ** 30, 0.0, 0.5, 1.0, 1.5, -0.1, float("nan"), float("inf"),
                "insult", "apology", "promise", "fulfilment_claim", "none", "", "x", 7, MyInt(1), decimal.Decimal("0.5"), (0, 1), [], {}]
        TYPES = ["insult", "apology", "promise", "fulfilment_claim", "unknown", None, 1, ["insult"], "Insult", " insult", MyInt(2)]
        SPANS = [[0, 0], [0, 1], [1, 0], [0, 2], [2, 2], [3, 3], [0, 29], [0, 30], [5, 40], [0, 1, 2], [0], [], [True, 1], [0, False],
                 [0.0, 1], [0, 1.0], ["0", "1"], [-1, 0], [10 ** 30, 10 ** 30], [0, 10 ** 30], [-10 ** 30, 0], (0, 1), None, 0, "01",
                 [MyInt(0), 1], [[0], [1]], [None, None]]
        ITEMS_CAP = 0
        for words_n in (0, 1, 3, 31, 40):
            words = [("w", i, i + 1) for i in range(words_n)]
            for _ in range(2500):
                item = {}
                if rnd.random() < 0.95:
                    item["type"] = rnd.choice(TYPES)
                if rnd.random() < 0.95:
                    item["span"] = rnd.choice(SPANS)
                if rnd.random() < 0.7:
                    item["severity"] = rnd.choice(VALS)
                if rnd.random() < 0.7:
                    item["sincerity"] = rnd.choice(VALS)
                p, r = both(ee._validate_candidate, item, words)
                check("C1 _validate_candidate", norm(p) == norm(r), f"{item!r} n={words_n}: {p} vs {r}")
                ITEMS_CAP += 1
        words = [("w", i, i + 1) for i in range(40)]
        for item in (None, 5, "s", [], [1], MyDict(type="insult", span=[0, 0], severity=0.5), {"type": "insult", "span": [0, 0], "severity": "0.5"},
                     {"type": "apology", "span": [0, 0], "sincerity": True}, {"type": "insult", "span": [0, 0], "severity": 1}, {"type": "apology", "span": [0, 0], "sincerity": 0},
                     {"type": "promise", "span": [0, 29]}, {"type": "promise", "span": [0, 30]}, {"type": "promise", "span": [10, 39]},
                     {"type": "promise", "span": [39, 39]}, {"type": "promise", "span": [39, 40]}):
            p, r = both(ee._validate_candidate, item, words)
            check("C2 _validate_candidate hard", norm(p) == norm(r), f"{item!r}: {p} vs {r}")
        p, r = both(ee._validate_candidate, {"type": "insult", "span": [0, 0], "severity": 0.5}, tuple(words))
        check("C3 words не list → Python-путь", norm(p) == norm(r))
        py()
        ok = ee._validate_candidate({"type": "insult", "span": [1, 2], "severity": 1}, words)
        rst()
        ok2 = ee._validate_candidate({"type": "insult", "span": [1, 2], "severity": 1}, words)
        check("C4 типы результата (int/float)", ok == ok2 and type(ok2[0][1]) is int and type(ok2[0][3]) is float and ok2[0][3] == 1.0)

        # ---- D. fact _validate --------------------------------------------------------------------------------------------
        def mk_known(n, kind="ok"):
            if kind == "ok":
                return [{"fact_id": f"f{i}", "statement": f"s{i}"} for i in range(n)]
            if kind == "nofid":
                return [{"statement": "s"} for _ in range(n)]
            if kind == "nondict":
                return [None for _ in range(n)]
            if kind == "ids":
                return [{"fact_id": i} for i in range(n)]
            if kind == "tuple":
                return tuple({"fact_id": f"f{i}"} for i in range(n))
            return None

        STMTS = ["", "1234567", "12345678", "x" * 200, "x" * 201, " " * 3 + "12345678" + " " * 3, "\x1c\x1c12345678\x1d", " 12345678 ",
                 "привет мир!", "п" * 8, "п" * 7, "п" * 200, "п" * 201, "a\nb1234567", "12345678\n", "12345678\r", " " + "abcdefgh", "ab​cdefg",
                 None, 5, ["x"], "  short ", "🌍" * 8, "🌍" * 7]
        CLASSES = ["possession", "relationship", "project", "preference", "life_fact", "location", "skill_interest", "other_stable", "secret",
                   "Secret", "", None, 1, ["secret"], "unknown"]
        POLS = ["affirmed", "negated", "x", None, 1, True, ["affirmed"]]
        TIMES_ = ["current", "past", "future", None, 0]
        STAB = ["stable", "unstable", None, True, 1, "Stable"]
        RELS = ["none", "same", "replaces", "x", None, 1, ["same"], "NONE"]
        TARGETS = [None, 0, 1, 2, 29, 30, 31, 35, -1, True, False, 1.0, "0", 10 ** 30, -10 ** 30, MyInt(1), [0]]
        for kind in ("ok", "nofid", "nondict", "ids", "tuple", "none"):
            for kn in (0, 2, 30, 35):
                known = mk_known(kn, kind)
                for _ in range(700):
                    item = {"span": rnd.choice(SPANS[:14]) if rnd.random() < 0.9 else rnd.choice(SPANS)}
                    for key, pool_ in (("class", CLASSES), ("statement", STMTS), ("polarity", POLS), ("time", TIMES_), ("stability", STAB), ("relation", RELS), ("target", TARGETS)):
                        if rnd.random() < 0.93:
                            item[key] = rnd.choice(pool_)
                    # чаще делаем «почти годные» кандидаты, чтобы добираться до relation/target
                    if rnd.random() < 0.6:
                        item.update({"class": "preference", "statement": rnd.choice(["12345678", "привет мир!", "  x" * 4]), "polarity": "affirmed",
                                     "time": "current", "stability": "stable", "span": [0, rnd.choice([0, 1, 2])]})
                    words = [("w", i, i + 1) for i in range(rnd.choice([3, 40]))]
                    p, r = both(fe._validate, item, words, known)
                    check("D1 _validate", norm(p) == norm(r), f"{item!r} known={kind}/{kn}: {p} vs {r}")
        for item in (None, [], "s", MyDict(span=[0, 0])):
            p, r = both(fe._validate, item, words, [])
            check("D2 _validate не-dict", norm(p) == norm(r))
        py()
        gp = fe._validate({"span": [0, 1], "class": "preference", "statement": "  привет мир  ", "polarity": "affirmed", "time": "past", "stability": "stable",
                           "relation": "replaces", "target": 1}, words, mk_known(3))
        rst()
        gr = fe._validate({"span": [0, 1], "class": "preference", "statement": "  привет мир  ", "polarity": "affirmed", "time": "past", "stability": "stable",
                           "relation": "replaces", "target": 1}, words, mk_known(3))
        check("D3 успешный кандидат идентичен (порядок ключей, типы)", gp == gr and list(gp[0]) == list(gr[0]) and norm(gp) == norm(gr) and gr[0]["target"] == "f1")

        # ---- E. looks_secret ----------------------------------------------------------------------------------------------
        SRE = re.compile(r"(?=[A-Za-z0-9_\-+/=]*\d)(?=[A-Za-z0-9_\-+/=]*[A-Za-z])[A-Za-z0-9_\-+/=]{20,}|-----BEGIN [A-Z ]*KEY-----")
        alpha = list("abcXYZ") + list("0123456789") + list("_-+/=") + ["٣", "१", "１", "²", "٣", " ", ".", "é", "ж", "-----BEGIN ", "KEY-----",
                                                                          "BEGIN ", " KEY", "PRIVATE ", "\n", "%"]
        for _ in range(12000):
            t = "".join(rnd.choice(alpha) for _ in range(rnd.randrange(0, 34)))
            p, r = both(fe.looks_secret, t)
            check("E1 looks_secret", p == r and p[1] == bool(SRE.search(t)), f"{t!r}: {p} vs {r}")
        for L in (18, 19, 20, 21, 22):
            for body in ("a" * (L - 1) + "1", "1" * (L - 1) + "a", "a" * L, "1" * L, "a" * (L - 1) + "٣", "a" * (L - 1) + "²", "a" * (L - 2) + "1-"):
                for tail in ("", " ", "٣", "٣x"):
                    t = "x " + body + tail
                    p, r = both(fe.looks_secret, t)
                    check("E2 looks_secret границы", p == r and p[1] == bool(SRE.search(t)), f"{t!r}")
        for t in (None, "", "-----BEGIN KEY-----", "-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN  KEY-----", "-----BEGIN rsa KEY-----", "-----BEGIN A KEY----",
                  "x-----BEGIN A B C KEY-----y", "-----BEGIN -----BEGIN KEY-----", "-----BEGIN A\nKEY-----", 5, b"x", "\ud800" * 25):
            p, r = both(fe.looks_secret, t)
            check("E3 looks_secret ключи/типы", p == r, f"{t!r}: {p} vs {r}")

        # ---- F. inside_quotation -----------------------------------------------------------------------------------------
        qp = list("«»„“”\"'") + ["a", " ", "слово", "🌍", "“", "„", "”"]
        for _ in range(6000):
            msg = "".join(rnd.choice(qp) for _ in range(rnd.randrange(0, 14)))
            for pos in (0, 1, 3, len(msg) - 1, len(msg), len(msg) + 2, 100, -1, -3):
                p, r = both(cv.inside_quotation, msg, pos)
                check("F1 inside_quotation", p == r, f"{msg!r} {pos}: {p} vs {r}")
        for msg, pos in (("«a", 10 ** 30), ("«a", -10 ** 30), ("«a", True), ("«a", False), ("«a", 1.0), ("«a", None), (None, 1), ("\ud800«", 3), ("«»«", 3), ("„a“b”", 4)):
            p, r = both(cv.inside_quotation, msg, pos)
            check("F2 inside_quotation трудные", p == r, f"{msg!r} {pos}: {p} vs {r}")

        # ---- G. переключатель ----------------------------------------------------------------------------------------------
        os.environ.pop("YANDI_PET_EXTRACTION_ENGINE", None)
        ee._rust_pe = None
        check("G1 по умолчанию выключен", ee._get_rust_pe() is None)
        os.environ["YANDI_PET_EXTRACTION_ENGINE"] = "rust"
        ee._rust_pe = None
        check("G2 включается переменной", ee._get_rust_pe() is not None)
        os.environ.pop("YANDI_PET_EXTRACTION_ENGINE", None)
        ee._rust_pe = None
        check("G3 выключение возвращает Python", ee._get_rust_pe() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_PET_EXTRACTION_ENGINE", None)
        else:
            os.environ["YANDI_PET_EXTRACTION_ENGINE"] = saved
        ee._rust_pe = None
    src = (ROOT / "pet" / "event_extraction.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(кандидатов-событий: {ITEMS_CAP}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
