"""
agent/relationship_memory_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/relationship_memory.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что
стеммер agent/relationship_memory.py (`_stem`, `_stems`, `content_stems`, `_overlap`).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Тридцать второй шаг переноса Python -> Rust (2026-09-24). Стеммер зовут на каждое извинение/обещание/личный факт. Проверяется:
  * `_stem`: каждый суффикс к основам длиной 0..6 символов (граница «остаток >= 3» — В СИМВОЛАХ, не байтах), все слова из `_NON_CONTENT_WORDS`,
    случайные кириллические/латинские/цифровые слова, порядок суффиксов (первый подходящий по убыванию длины);
  * `_stems` (drop_non_content True/False): `casefold` (ß→ss, İ, знак Кельвина, Ё/ё), не-a-z/а-я/0-9 символы как разделители, Unicode-цифры, комбинирующие,
    сеты одинаковы; на КАЖДОЙ кодовой точке Unicode в двух окружениях; входы None/""/не-строки/суррогаты;
  * `content_stems`, `_overlap` целиком; изменённые таблицы стеммера уважаются (Python-путь); переключатель.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.relationship_memory_rust_parity_test
"""
from __future__ import annotations

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
        if len(FAILURES) < 25:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def outcome(fn, *a, **kw):
    try:
        return ("ok", fn(*a, **kw))
    except Exception as e:  # noqa: BLE001
        return ("exc", type(e).__name__)


def main() -> int:
    try:
        import yandi_rs.relationship_memory as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.relationship_memory").setLevel(logging.ERROR)
    import agent.relationship_memory as rm

    rnd = random.Random(20260924)

    def py():
        rm._rust_rm = False

    def rst():
        rm._rust_rm = rs

    def both(fn, *a, **kw):
        py()
        p = outcome(fn, *a, **kw)
        rst()
        r = outcome(fn, *a, **kw)
        return p, r

    saved = os.environ.get("YANDI_RELATIONSHIP_MEMORY_ENGINE")
    n = 0
    try:
        # ---- A. _stem ----------------------------------------------------------------------------------------------------
        bases = ["", "а", "ав", "авт", "авто", "автом", "автомо", "книг", "сл", "слов", "ёж", "ёжик", "x", "xy", "xyz", "test", "12", "123", "1234"]
        for suf in rm._STEM_SUFFIXES:
            for b in bases:
                tok = b + suf
                check("A1 _stem суффикс×основа", rm._stem(tok) == rs.stem(tok), f"{tok!r}: {rm._stem(tok)!r} vs {rs.stem(tok)!r}")
                n += 1
        for w in rm._NON_CONTENT_WORDS:
            check("A2 _stem слова-не-содержание", rm._stem(w) == rs.stem(w), w)
            n += 1
        alpha = list("абвгдежзийклмнопрстуфхцчшщъыьэюя") + list("abcxyz") + list("0123456789")
        for _ in range(20000):
            tok = "".join(rnd.choice(alpha) for _ in range(rnd.randrange(0, 9)))
            check("A3 _stem случайные токены", rm._stem(tok) == rs.stem(tok), tok)
            n += 1

        # ---- B. _stems ---------------------------------------------------------------------------------------------------
        pool = ["Извини", "прости", "ДУРАКОМ", "дураком", "назвал", "тебя", "меня", "Ёжик", "ёлка", "ЁЁЁ", "книгами", "окна", "ума", "он", "я", "ты", "вчера",
                "слова", "Слово", "сказал", "сказала", "обидел", "обидела", "ß", "Straße", "İstanbul", "Kelvin", "ſ", "ﬃ", "ǅ", "Σ", "abc", "ABC", "test1", "1test",
                "123", "45", "²", "٣", "١٢٣", "ａｂｃ", "é", "é", "日本語", "🌍", "́", "​", "-", "_", "'", ".", ",", "!", "?", " ", "  ", "\t", "\n", "\x00",
                "Ɤ", "Ᲊ", "\U000113c2", "ẞ", "Ǆ", "ª"]
        for _ in range(20000):
            text = "".join(rnd.choice(pool) + rnd.choice(["", " ", " ", "-", ","]) for _ in range(rnd.randrange(0, 8)))
            for drop in (True, False):
                p, r = both(rm._stems, text, drop_non_content=drop)
                check("B1 _stems фаззинг", p == r, f"{text!r} drop={drop}: {p} vs {r}")
                n += 1
        for text in ["", " ", "я", "он", "Извини, я назвал тебя дураком!", "ПРОСТИ ПРОСТИ прости", "ёёё ЁЁЁ еее", "a" * 1000, "слово " * 500,
                     "ß" * 5, "SS" + "ß", "ĳ", "ǳ"]:
            for drop in (True, False):
                p, r = both(rm._stems, text, drop_non_content=drop)
                check("B2 _stems граничные", p == r, f"{text[:30]!r}")
                n += 1

        # ---- C. каждая кодовая точка --------------------------------------------------------------------------------------------
        bad = []
        for cp in range(0x110000):
            if 0xD800 <= cp <= 0xDFFF:
                continue
            ch = chr(cp)
            for t in ("абв" + ch + "где", ch + "привет" + ch, "тест" + ch * 3):
                py()
                p = rm._stems(t, drop_non_content=False)
                r = rs.stems(t, False)
                n += 1
                if p != r:
                    bad.append((hex(cp), t))
        check("C1 _stems по всем кодовым точкам (3 окружения)", not bad, f"{len(bad)}: {bad[:4]}")

        # ---- D. входы --------------------------------------------------------------------------------------------------------
        for v in (None, "", 0, 5, 1.5, b"abc", ["дурак"], {"a": 1}, ("x",), True, False, "\ud800дурак", "дурак\udc00"):
            for drop in (True, False):
                p, r = both(rm._stems, v, drop_non_content=drop)
                check("D1 _stems типы входа", p == r, f"{v!r}: {p} vs {r}")
                n += 1

        # ---- E. content_stems и _overlap ------------------------------------------------------------------------------------------
        for txt in ["Извини, я назвал тебя дураком", "прости меня за вчерашнее", "", None, "Я обещаю прийти завтра"]:
            p, r = both(rm.content_stems, txt)
            check("E1 content_stems", p == r, f"{txt!r}")
            n += 1
        for a in ("Извини, я назвал тебя дураком", "прости за слова", "", "он"):
            for g in ({"description": "Назвал дураком"}, {"description": None}, {}, {"description": "дурак"}, {"description": "слова про мать"}):
                py()
                ap = rm._stems(a, drop_non_content=True)
                p = outcome(rm._overlap, ap, g)
                rst()
                r = outcome(rm._overlap, ap, g)
                check("E2 _overlap", p == r and repr(p) == repr(r), f"{a!r} {g!r}: {p} vs {r}")
                n += 1

        # ---- F. таблицы изменены / переключатель ------------------------------------------------------------------------------------
        os.environ.pop("YANDI_RELATIONSHIP_MEMORY_ENGINE", None)
        rm._rust_rm = None
        check("F1 по умолчанию выключен", rm._get_rust_rm() is None)
        os.environ["YANDI_RELATIONSHIP_MEMORY_ENGINE"] = "rust"
        rm._rust_rm = None
        check("F2 включается переменной", rm._get_rust_rm() is not None)
        rm._STEM_SUFFIXES.append("ок")
        try:
            got = rm._stems("домишок", drop_non_content=False)
            check("F3 изменённые суффиксы уважаются (Python-путь)", got == {"домиш"}, str(got))
        finally:
            rm._STEM_SUFFIXES.remove("ок")
        check("F4 после отката снова Rust", rm._stems("домишок", drop_non_content=False) == {"домишок"})
        os.environ.pop("YANDI_RELATIONSHIP_MEMORY_ENGINE", None)
        rm._rust_rm = None
        check("F5 выключение возвращает Python", rm._get_rust_rm() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_RELATIONSHIP_MEMORY_ENGINE", None)
        else:
            os.environ["YANDI_RELATIONSHIP_MEMORY_ENGINE"] = saved
        rm._rust_rm = None
    src = (ROOT / "agent" / "relationship_memory.py").read_text(encoding="utf-8")
    check("F6 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(сравнений: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
