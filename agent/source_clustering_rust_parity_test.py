"""
agent/source_clustering_rust_parity_test.py — доказательство, что
rustlib/yandi_rs/src/source_clustering.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что и
agent/source_clustering.py::assign_source_clusters и сигналы схожести из
agent/source_independence_prototype.py (title_similarity, content_fingerprint_similarity).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Пятнадцатый шаг переноса Python -> Rust (2026-09-24). Методология — rustlib/README.md.
Здесь три уровня доказательства, потому что порт нетривиален:
  A. Python-`\\w` (`ch.isalnum() or ch == '_'`) — сверка по ВСЕМ 1 114 112 кодовым точкам.
  B. difflib.SequenceMatcher.ratio — дифференциальный ФАЗЗИНГ против настоящего difflib
     (фиксированный seed): маленькие алфавиты (максимум повторов — ловят ошибки выбора
     совпадения при равенстве), длинные строки >=200 (включают autojunk "популярных" символов).
  C. Сигналы схожести и весь assign_source_clusters — на реальных и случайных пулах evidence.
Регрессионный контракт самого модуля — agent/epistemic_source_cluster_regression_test.py и др.

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop`) — если не установлен,
тест пропускается (SKIP), не падает.

Run: python -m agent.source_clustering_rust_parity_test
"""
from __future__ import annotations

import copy
import os
import random
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
_NUM_OK = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _NUM_OK
    if condition:
        _NUM_OK += 1
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def check_quiet(name: str, condition: bool, detail: str = "") -> None:
    """Для массовых сверок (десятки тысяч): печатаем только провалы, успехи считаем."""
    global _NUM_OK
    if condition:
        _NUM_OK += 1
    else:
        print(f"[FAIL] {name} — {detail}")
        FAILURES.append(name)


def main() -> int:
    try:
        import yandi_rs.source_clustering as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.source_clustering as sc
    from agent.source_independence_prototype import (
        content_fingerprint_similarity as py_content_sim,
        title_similarity as py_title_sim,
    )

    import logging
    logging.getLogger("yandi.source_clustering").setLevel(logging.ERROR)   # тест сбрасывает кэш переключателя намеренно

    os.environ.pop("YANDI_SOURCE_CLUSTERING_ENGINE", None)
    sc._rust_sc = None

    # ── A: Python-\w по ВСЕМ кодовым точкам ──
    bad = [cp for cp in range(0x110000) if (chr(cp).isalnum() or chr(cp) == "_") != rs.is_word_char(cp)]
    check("A0 is_word_char совпадает с Python `isalnum() or '_'` на всех 1 114 112 кодовых точках",
          not bad, f"расхождений: {len(bad)}, первые: {[hex(c) for c in bad[:8]]}")
    for cp in (0xB2, 0x0301, 0x0663, 0x2160, 0x5D0, 0x4E00, ord("_"), ord("'"), ord(" ")):
        check(f"A1 is_word_char U+{cp:04X} совпадает", rs.is_word_char(cp) == (chr(cp).isalnum() or chr(cp) == "_"))

    # ── B: SequenceMatcher.ratio — фиксированные и фаззинг ──
    fixed_pairs = [
        ("abcd", "bcde"), ("", ""), ("abc", ""), ("", "abc"), ("abc", "abc"), ("a", "a"), ("a", "b"),
        ("abab", "baba"), ("aaaa", "aa"), ("aa", "aaaa"), ("abcabcabc", "abc"),
        ("новая экзопланета найдена", "найдена новая экзопланета"),
        ("привет мир", "мир привет"),
    ]
    for i, (a, b) in enumerate(fixed_pairs):
        check(f"B0.{i} ratio совпадает — {a[:20]!r}/{b[:20]!r}",
              SequenceMatcher(None, a, b).ratio() == rs.sequence_ratio(a, b))

    rng = random.Random(20260924)

    def rand_str(alphabet: str, lo: int, hi: int) -> str:
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(lo, hi)))

    n_fuzz = 0
    for alphabet in ("ab", "abc", "abcdefgh", "абвгд", "ab ", "аб вг д"):
        for _ in range(500):
            a, b = rand_str(alphabet, 0, 40), rand_str(alphabet, 0, 40)
            n_fuzz += 1
            check_quiet(f"B1 fuzz малые ({alphabet!r}) a={a!r} b={b!r}",
                        SequenceMatcher(None, a, b).ratio() == rs.sequence_ratio(a, b),
                        f"python={SequenceMatcher(None, a, b).ratio()} rust={rs.sequence_ratio(a, b)}")
    # Длинные (>=200 в b) — включается autojunk: популярные символы выбрасываются из индекса
    for alphabet in ("ab", "abcd", "abcdefghijklmnopqrstuvwxyz ", "абвгдежзиклмнопрстуф "):
        for _ in range(120):
            a = rand_str(alphabet, 150, 420)
            b = rand_str(alphabet, 195, 420)   # 195..420: пересекает порог 200 по обе стороны
            n_fuzz += 1
            check_quiet(f"B2 fuzz длинные ({alphabet[:6]!r}) len(a)={len(a)} len(b)={len(b)}",
                        SequenceMatcher(None, a, b).ratio() == rs.sequence_ratio(a, b),
                        f"python={SequenceMatcher(None, a, b).ratio()} rust={rs.sequence_ratio(a, b)}")
    # Граница autojunk ровно 199/200/201 символов в b
    for nb in (199, 200, 201):
        base = rand_str("abcde ", nb, nb)
        for _ in range(40):
            a = rand_str("abcde ", 100, 260)
            check_quiet(f"B3 граница autojunk len(b)={nb}",
                        SequenceMatcher(None, a, base).ratio() == rs.sequence_ratio(a, base))
    # Схожие строки с правками (то, что реально сравнивается: заголовки-перепечатки)
    for _ in range(300):
        a = rand_str("абвгдежзиклмн ", 30, 120)
        b = list(a)
        for _ in range(rng.randint(0, 6)):
            if b and rng.random() < 0.5:
                b.pop(rng.randrange(len(b)))
            else:
                b.insert(rng.randrange(len(b) + 1), rng.choice("абвгдежз "))
        b = "".join(b)
        n_fuzz += 1
        check_quiet(f"B4 правки a={a!r}",
                    SequenceMatcher(None, a, b).ratio() == rs.sequence_ratio(a, b))
    check("B9 фаззинг выполнен (число сравнений)", n_fuzz > 3000, f"n={n_fuzz}")

    # ── C: сигналы схожести на реалистичных и случайных текстах ──
    titles = [
        "Учёные нашли новую экзопланету", "Ученые нашли новую экзопланету!", "УЧЁНЫЕ НАШЛИ НОВУЮ ЭКЗОПЛАНЕТУ...",
        "Совсем другая новость про футбол", "", "   ", "Wikipedia — Юпитер", "Jupiter - Wikipedia",
        "Don't stop believin'", "Dont stop believing", "Café au lait", "Café au lait",
        "x² + y² = z²", "Ａｂｃ　ｄｅｆ",
    ]
    for i, ta in enumerate(titles):
        for j, tb in enumerate(titles):
            check(f"C0.{i}.{j} title_similarity совпадает", py_title_sim(ta, tb) == rs.title_similarity(ta, tb),
                  f"{ta!r} {tb!r}")
    contents = [
        "", "один два три", "один два три четыре пять шесть семь",
        "The quick brown fox jumps over the lazy dog near the river bank today",
        "The quick brown fox jumps over the lazy dog near the river bank yesterday",
        "Учёные нашли новую экзопланету в созвездии Лебедя, сообщает агентство",
        "Ученые нашли новую экзопланету в созвездии Лебедя, сообщает агентство!",
        "don't stop-me now it's x² y² z_1 café café ok ok ok ok ok ok",
        "a b c d e f g", "a b c d e", "a b c d", "   ", "!!! ??? ...",
    ]
    for i, ca in enumerate(contents):
        for j, cb in enumerate(contents):
            check(f"C1.{i}.{j} content_fingerprint_similarity совпадает",
                  py_content_sim(ca, cb) == rs.content_fingerprint_similarity(ca, cb), f"{ca!r} {cb!r}")
    words = ["альфа", "бета", "гамма", "delta", "epsilon", "x²", "it's", "мир", "война", "2026", "юпитер", "_z"]
    for _ in range(400):
        ta = " ".join(rng.choice(words) for _ in range(rng.randint(0, 9)))
        tb = " ".join(rng.choice(words) for _ in range(rng.randint(0, 9)))
        check_quiet(f"C2 fuzz title/content a={ta!r} b={tb!r}",
                    py_title_sim(ta, tb) == rs.title_similarity(ta, tb)
                    and py_content_sim(ta, tb) == rs.content_fingerprint_similarity(ta, tb))
    for ta in titles[:6]:
        for tb in titles[:6]:
            for ca in contents[:6]:
                for cb in contents[3:8]:
                    py_r = sc._similar(sc.SourceCandidate(url="", title=ta, content_excerpt=ca),
                                       sc.SourceCandidate(url="", title=tb, content_excerpt=cb))
                    check_quiet("C3 similar совпадает", py_r == rs.similar(ta, tb, ca, cb),
                                f"{ta!r} {tb!r} {ca!r} {cb!r}")

    # ── D: assign_source_clusters целиком: результат и возвращаемое число на разных пулах ──
    def make_ev(eid, title, content, uri=""):
        return {"evidence_id": eid, "source_title": title, "content_excerpt": content, "source_uri": uri}

    story = "Учёные нашли новую экзопланету в созвездии Лебедя сообщает агентство науки"
    fixed_pools = [
        [],
        [make_ev("e1", "A", "")],
        [make_ev("e1", "Учёные нашли новую экзопланету", story), make_ev("e2", "Ученые нашли новую экзопланету!", story),
         make_ev("e3", "Футбол: итоги тура", "команда выиграла матч со счётом три два в гостях"), make_ev("e4", "Итоги тура по футболу", "")],
        [make_ev("e1", "same", ""), make_ev("e1", "same", ""), make_ev("e2", "same", "")],           # дубли evidence_id
        [{"source_title": "no id", "content_excerpt": ""}, make_ev("", "empty id", ""), make_ev("e9", "ok", "")],
        [make_ev("e1", "", ""), make_ev("e2", "", ""), make_ev("e3", "   ", "   ")],               # пустое — не сливается
        [make_ev("e1", "t", None), make_ev("e2", None, "x y z"), {"evidence_id": "e3"}],            # None/нет полей
        [make_ev(i, f"Заголовок номер {i % 3}", "") for i in range(1, 9)],                           # числовые id
    ]
    pools = list(fixed_pools)
    for _ in range(150):
        base_titles = [" ".join(rng.choice(words) for _ in range(rng.randint(2, 6))) for _ in range(rng.randint(1, 4))]
        pool = []
        for k in range(rng.randint(1, 14)):
            t = rng.choice(base_titles)
            if rng.random() < 0.4:
                t = t + rng.choice(["", "!", " .", " - новость"])
            c = " ".join(rng.choice(words) for _ in range(rng.randint(0, 14))) if rng.random() < 0.6 else ""
            pool.append(make_ev(f"ev{k}", t, c))
        pools.append(pool)

    def run(pool, engine):
        saved = os.environ.get("YANDI_SOURCE_CLUSTERING_ENGINE")
        try:
            if engine == "rust":
                os.environ["YANDI_SOURCE_CLUSTERING_ENGINE"] = "rust"
            else:
                os.environ.pop("YANDI_SOURCE_CLUSTERING_ENGINE", None)
            sc._rust_sc = None
            data = copy.deepcopy(pool)
            n = sc.assign_source_clusters(data)
            return n, data
        finally:
            if saved is None:
                os.environ.pop("YANDI_SOURCE_CLUSTERING_ENGINE", None)
            else:
                os.environ["YANDI_SOURCE_CLUSTERING_ENGINE"] = saved
            sc._rust_sc = None

    for i, pool in enumerate(pools):
        py_n, py_data = run(pool, "python")
        rs_n, rs_data = run(pool, "rust")
        check_quiet(f"D{i} assign_source_clusters: результат и число совпадают",
                    py_n == rs_n and py_data == rs_data, f"python={py_n, py_data} rust={rs_n, rs_data}")
    # Нестроковое поле: PyO3 не принимает -> вызов уходит на Python-путь и не падает, результат тот же
    weird = [make_ev("e1", 12345, "x"), make_ev("e2", "12345", "x"), make_ev("e3", ["список"], "y")]
    py_n, py_data = run(weird, "python")
    rs_n, rs_data = run(weird, "rust")
    check("D90 нестроковые поля: с включённым Rust не падает, результат = Python-путь",
          py_n == rs_n and py_data == rs_data, f"python={py_n, py_data} rust={rs_n, rs_data}")

    # ── G: переключатель ──
    saved_env = os.environ.get("YANDI_SOURCE_CLUSTERING_ENGINE")
    try:
        os.environ.pop("YANDI_SOURCE_CLUSTERING_ENGINE", None)
        sc._rust_sc = None
        check("G0 по умолчанию (без переменной) активен Python-движок", sc._get_rust_sc() is None)
        os.environ["YANDI_SOURCE_CLUSTERING_ENGINE"] = "rust"
        sc._rust_sc = None
        check("G1 переменная окружения реально переключает на собранный Rust-модуль", sc._get_rust_sc() is rs)
        os.environ.pop("YANDI_SOURCE_CLUSTERING_ENGINE", None)
        sc._rust_sc = None
        check("G3 выключение переменной возвращает Python-движок (кэш не залипает)", sc._get_rust_sc() is None)
    finally:
        if saved_env is None:
            os.environ.pop("YANDI_SOURCE_CLUSTERING_ENGINE", None)
        else:
            os.environ["YANDI_SOURCE_CLUSTERING_ENGINE"] = saved_env
        sc._rust_sc = None

    src = (ROOT / "agent" / "source_clustering.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError) и не падает наружу", "except ImportError as e:" in src)

    print(f"\n(успешных проверок всего: {_NUM_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:10]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
