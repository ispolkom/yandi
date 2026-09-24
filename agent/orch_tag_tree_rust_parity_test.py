"""
agent/orch_tag_tree_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/orch_tag_tree.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что
agent/orch_tag_tree.py (`_tokenize`, `_lsh_bucket`, `lsh_entropy`, пересчёт энтропии узла в `TagTree.update`).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Тридцать первый шаг переноса Python -> Rust (2026-09-24). Проверяется:
  * стоп-слова, вшитые в Rust, равны `_STOPWORDS` оригинала;
  * `.lower()` + `\\b[а-яёa-z]{3,}\\b` на КАЖДОЙ кодовой точке Unicode в трёх окружениях (ловит расхождения `to_lowercase` с Python и границ `\\w`,
    например «K» (знак Кельвина) → 'k'), плюс фаззинг трудных смесей (İ, финальная сигма, комбинирующие знаки, `_`, цифры, Ё, стоп-слова);
  * `_lsh_bucket` на любых n_buckets (1, 2, 32, 2**40, огромные), `lsh_entropy` побитово (включая границы округления до 4 знаков);
  * `TagTree.update`: гистограмма/энтропия после серии обновлений совпадают; испорченная гистограмма (не 32, float, bool, отрицательные) идёт в Python;
  * переключатель; изменённый `_STOPWORDS` уважается (Python-путь).
Дерево на диск НЕ пишется (метод `_save` заглушён) — реальный tag_tree.json не трогаем.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.orch_tag_tree_rust_parity_test
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


def outcome(fn, *a):
    try:
        return ("ok", fn(*a))
    except Exception as e:  # noqa: BLE001
        return ("exc", type(e).__name__)


def main() -> int:
    try:
        import yandi_rs.orch_tag_tree as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.orch_tag_tree").setLevel(logging.ERROR)
    import agent.orch_tag_tree as tt

    rnd = random.Random(20260924)

    def py():
        tt._rust_tt = False

    def rst():
        tt._rust_tt = rs

    def both(fn, *a):
        py()
        p = outcome(fn, *a)
        rst()
        r = outcome(fn, *a)
        return p, r

    saved = os.environ.get("YANDI_ORCH_TAG_TREE_ENGINE")
    try:
        # ---- A. стоп-слова -------------------------------------------------------------------------------------------------
        check("A1 стоп-слова Rust == Python", set(rs.stopwords()) == set(tt._STOPWORDS), f"{set(rs.stopwords()) ^ set(tt._STOPWORDS)}")

        # ---- B. каждая кодовая точка ---------------------------------------------------------------------------------------
        bad = 0
        n = 0
        for cp in range(0x110000):
            if 0xD800 <= cp <= 0xDFFF:
                continue
            ch = chr(cp)
            for text in ("abc" + ch + "def", ch + "abc", "abc " + ch + ch + " x" + ch):
                tt._rust_tt = False
                p = tt._tokenize(text)
                r = rs.tokenize(text)
                n += 1
                if p != r:
                    bad += 1
                    if bad <= 5:
                        print(f"[FAIL] B1 U+{cp:04X} {text!r}: {p} vs {r}")
        check("B1 tokenize по всем кодовым точкам (3 окружения)", bad == 0, f"{bad} расхождений")

        # ---- C. фаззинг токенизации -----------------------------------------------------------------------------------------
        pool = ["привет", "мир", "Как", "ЧТО", "где", "the", "Is", "DO", "do", "abc", "ab", "a", "Ёж", "ёлка", "ЁЁЁ", "ёё", "тест", "test", "test1", "1test",
                "_x", "x_", "-", ".", " ", "  ", "\t", "\n", "́", "̇", "İ", "İstanbul", "K", "Kelvin", "ſ", "ß", "ǅ", "Σ", "ΑΣ", "ΑΣ ",
                "é", "ñandú", "日本語", "🌍", "​", "\x00", "Ᲊ", "Ꙋ", "ⓐⓑⓒ", "ａｂｃ", "٣", "²", "ｋ", "ǰ", "ﬃ", "K", "kk", "kkk"]
        for _ in range(20000):
            text = "".join(rnd.choice(pool) for _ in range(rnd.randrange(0, 9)))
            if rnd.random() < 0.5:
                text = text.replace("  ", " ")
            p, r = both(tt._tokenize, text)
            check("C1 tokenize фаззинг", p == r, f"{text!r}: {p} vs {r}")
        for bad_in in (None, 5, b"abc", ["abc"], "\ud800abc", "abc\udc00 def"):
            p, r = both(tt._tokenize, bad_in)
            check("C2 tokenize не-строка/суррогат", p == r, f"{bad_in!r}: {p} vs {r}")

        # ---- D. lsh_bucket -------------------------------------------------------------------------------------------------
        toks = ["abc", "привет", "ёжик", "", "a", "🌍🌍", "x" * 500, "z" * 3, "K", "ﬃ"]
        for _ in range(3000):
            tok = rnd.choice(toks) if rnd.random() < 0.5 else "".join(chr(rnd.randrange(32, 0x3000)) for _ in range(rnd.randrange(0, 12)))
            nb = rnd.choice([1, 2, 3, 7, 32, 64, 1000, 2 ** 31, 2 ** 40, 2 ** 63, 2 ** 64 - 1, 2 ** 64, 10 ** 30, 0, -5])
            p, r = both(tt._lsh_bucket, tok, nb)
            check("D1 lsh_bucket", p == r, f"{tok!r} {nb}: {p} vs {r}")
        for args in (("abc", True), ("abc", 32.0), ("abc", None), (None, 32), (b"abc", 32), ("\ud800", 32)):
            p, r = both(tt._lsh_bucket, *args)
            check("D2 lsh_bucket трудные", p == r, f"{args!r}: {p} vs {r}")

        # ---- E. lsh_entropy ------------------------------------------------------------------------------------------------
        words = ["привет", "мир", "сервер", "python", "ошибка", "база", "данные", "запрос", "настроить", "починить", "тормоза", "linux", "docker",
                 "kubernetes", "ёжик", "яблоко", "the", "как"]
        for _ in range(3000):
            qs = [" ".join(rnd.choice(words) for _ in range(rnd.randrange(0, 6))) for _ in range(rnd.randrange(0, 8))]
            nb = rnd.choice([2, 3, 5, 8, 16, 32, 64, 100, 1000])
            p, r = both(tt.lsh_entropy, qs, nb)
            check("E1 lsh_entropy", p == r and repr(p) == repr(r), f"{qs!r} {nb}: {p} vs {r}")
        # много запросов: равномерное и перекошенное распределение (границы округления)
        for nb in (2, 4, 32):
            for skew in (0, 1, 2, 5):
                qs = [f"{w}" for w in words * (1 + skew)] + words[:skew]
                p, r = both(tt.lsh_entropy, qs, nb)
                check("E2 lsh_entropy распределения", p == r and repr(p) == repr(r), f"{nb} {skew}: {p} vs {r}")
        for t in range(2000):
            qs = ["".join(rnd.choice("абвгдежзиклмн") for _ in range(rnd.randrange(3, 8))) for _ in range(rnd.randrange(1, 40))]
            p, r = both(tt.lsh_entropy, qs, 32)
            check("E3 lsh_entropy случайные слова", p == r and repr(p) == repr(r), f"{p} vs {r}")
        for args in (([], 32), ([""], 32), (["привет"], 32), (["привет привет"], 32), (["a b c"], 32), (None, 32), (["привет"], 1), (["привет"], 0), (["привет"], 32.0),
                     (("привет",), 32), ([b"x"], 32), ([None], 32), (["привет\ud800"], 32), (["привет"], True), (["привет мир"], 2 ** 62)):
            p, r = both(tt.lsh_entropy, *args)
            check("E4 lsh_entropy трудные", p == r or (p[0] == r[0] == "exc" and p[1] == r[1]), f"{args!r}: {p} vs {r}")

        # ---- F. TagTree.update без записи на диск -------------------------------------------------------------------------------
        def fresh_tree():
            t = tt.TagTree.__new__(tt.TagTree)
            t._nodes = {}
            t._save = lambda: None
            return t

        for _ in range(400):
            qs = [" ".join(rnd.choice(words) for _ in range(rnd.randrange(0, 6))) for _ in range(rnd.randrange(1, 12))]
            trees = {}
            for mode in (py, rst):
                mode()
                t = fresh_tree()
                for q in qs:
                    t.update("tech", q)
                nd = t._nodes["tech"]
                trees[mode.__name__] = (nd.count, nd.entropy, list(nd.lsh_hist))
            check("F1 update: гистограмма и энтропия", trees["py"] == trees["rst"] and repr(trees["py"][1]) == repr(trees["rst"][1]), f"{qs!r}: {trees}")
        # испорченные гистограммы → идентично оригиналу (Python-путь)
        weird = [[0] * 31, [0] * 33, [0.5] * 32, [True] * 32, [-1] + [1] * 31, [10 ** 30] + [0] * 31, [0] * 32, "x" * 32, None, [1] * 32, [2 ** 50] + [0] * 31]
        for hist in weird:
            res = {}
            for mode in (py, rst):
                mode()
                t = fresh_tree()
                nd = tt.TagNode("tech")
                nd.lsh_hist = list(hist) if isinstance(hist, list) else hist
                t._nodes["tech"] = nd
                try:
                    t.update("tech", "привет мир сервер")
                    res[mode.__name__] = ("ok", nd.entropy, nd.lsh_hist if not isinstance(nd.lsh_hist, list) else list(nd.lsh_hist))
                except Exception as e:  # noqa: BLE001
                    res[mode.__name__] = ("exc", type(e).__name__)
            check("F2 update: испорченная гистограмма", repr(res["py"]) == repr(res["rst"]), f"{hist!r}: {res}")
        # пустой запрос при нулевой гистограмме: энтропия не меняется (остаётся 0.0 / прежней)
        for mode in (py, rst):
            mode()
            t = fresh_tree()
            nd = tt.TagNode("tech")
            nd.entropy = 0.42
            t._nodes["tech"] = nd
            t.update("tech", "как что где")
            check("F3 update: сумма 0 → энтропия не меняется", nd.entropy == 0.42 and nd.count == 1)

        # ---- G. переключатель и изменённые стоп-слова ----------------------------------------------------------------------------
        os.environ.pop("YANDI_ORCH_TAG_TREE_ENGINE", None)
        tt._rust_tt = None
        check("G1 по умолчанию выключен", tt._get_rust_tt() is None)
        os.environ["YANDI_ORCH_TAG_TREE_ENGINE"] = "rust"
        tt._rust_tt = None
        check("G2 включается переменной", tt._get_rust_tt() is not None and tt._rust_ok() is not None)
        tt._STOPWORDS.add("сервер")
        try:
            check("G3 изменённый _STOPWORDS уважается (Python-путь)", tt._rust_ok() is None and "сервер" not in tt._tokenize("сервер привет"))
        finally:
            tt._STOPWORDS.discard("сервер")
        check("G4 после отката снова Rust", tt._rust_ok() is not None and tt._tokenize("сервер привет") == ["сервер", "привет"])
        os.environ.pop("YANDI_ORCH_TAG_TREE_ENGINE", None)
        tt._rust_tt = None
        check("G5 выключение возвращает Python", tt._get_rust_tt() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_ORCH_TAG_TREE_ENGINE", None)
        else:
            os.environ["YANDI_ORCH_TAG_TREE_ENGINE"] = saved
        tt._rust_tt = None
    src = (ROOT / "agent" / "orch_tag_tree.py").read_text(encoding="utf-8")
    check("G6 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(кодовых точек проверено: {n // 3}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
