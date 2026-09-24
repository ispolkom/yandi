"""
agent/orch_query_framer_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/orch_query_framer.rs даёт ПОСТРОЧНО ТЕ ЖЕ ответы, что
agent/orch_query_framer.py (`decide_policy`, `_auto_cq`, `_is_safe_domain`).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Тридцать шестой шаг переноса Python -> Rust (2026-09-24). Оркестратор считает эти решения на КАЖДЫЙ запрос. Проверяется:
  * `_is_safe_domain`/`decide_policy`: КАЖДЫЙ безопасный домен (в разных регистрах, с приставками/суффиксами — проверка «подстрока», не равенство), КАЖДЫЙ
    «общий» объект (регистры, пробелы вокруг — не считаются пустыми), объект/действие/ограничения во всех комбинациях (пустая строка, None, 0, пустой/непустой dict),
    регистр по Python `.lower()` (İ, Σ, знак Кельвина, символы новее Unicode 14);
  * `_auto_cq`: каждый ключ шаблона в первом элементе `missing` (регистры, вхождение внутри слова; порядок словаря — побеждает первый), без missing, действие
    None/пусто/с фигурными скобками/юникодом, шаблон с `{action}` и без него; посторонние типы (не-строка в missing, missing не список, action не строка) → Python;
  * изменение таблиц уважается (Python-путь); переключатель.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.orch_query_framer_rust_parity_test
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
        import yandi_rs.orch_query_framer as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.orch_query_framer").setLevel(logging.ERROR)
    import agent.orch_query_framer as q

    rnd = random.Random(20260924)

    def py():
        q._rust_qf = False

    def rst():
        q._rust_qf = rs

    def both(fn, *a):
        py()
        p = outcome(fn, *a)
        rst()
        r = outcome(fn, *a)
        return p, r

    saved = os.environ.get("YANDI_ORCH_QUERY_FRAMER_ENGINE")
    n = 0
    try:
        # ---- A. таблицы Rust == Python (через поведение на каждом элементе) ------------------------------------------------------
        variants = lambda s: [s, s.upper(), s.capitalize(), s.title(), "  " + s + "  ", "x" + s, s + "y", "до " + s + " после", s[:-1], s[1:]]
        for d in sorted(q._SAFE_DOMAINS):
            for v in variants(d):
                p, r = both(q._is_safe_domain, v)
                check("A1 безопасный домен", p == r, f"{v!r}: {p} vs {r}")
                n += 1
        for o in sorted(q._GENERIC_OBJECTS):
            for v in variants(o):
                fr = q.QueryFrame(raw_query="r", enriched_query="e", obj=v, action="a", constraints={"k": 1}, domain="general")
                p, r = both(q.decide_policy, fr)
                check("A2 общий объект", p == r, f"{v!r}: {p} vs {r}")
                n += 1

        # ---- B. decide_policy: комбинации -----------------------------------------------------------------------------------------
        DOM = ["general", "", "Медицина", "МЕДИЦИНА", "финансы и налоги", "политика", "religion", "İстория", "Σ", "K", "право", "правовой", "здоровье!", "x", "Ɤ",
               "ДОМ ПРАВА", "юриспруденция"]
        OBJ = [None, "", " ", "пример", "Пример", "ПРИМЕР", " пример", "пример ", "двигатель", "что-то", "ЧТО-ТО", "example", "Example", "task", "TEXT", "İ", "K", "текст.", "x"]
        ACT = [None, "", "купить", "0", " ", 0, 5]
        CTX = [{}, {"a": 1}, {"a": None}, [], None, 0, 1, "", "x"]
        for dom in DOM:
            for obj in OBJ:
                for act in ACT:
                    for ctx in CTX:
                        fr = q.QueryFrame(raw_query="r", enriched_query="e", obj=obj, action=act, constraints=ctx, domain=dom)
                        p, r = both(q.decide_policy, fr)
                        check("B1 decide_policy", p == r, f"{dom!r} {obj!r} {act!r} {ctx!r}: {p} vs {r}")
                        n += 1
        for dom, obj in ((None, "x"), (5, "x"), ("general", 5), ("general", ["x"]), (b"x", None), ("Медицина", 5), ("\ud800", "x"), ("general", "\ud800")):
            fr = q.QueryFrame(raw_query="r", enriched_query="e", obj=obj, domain=dom, action="a", constraints={"k": 1})
            p, r = both(q.decide_policy, fr)
            check("B2 decide_policy типы/суррогаты", p == r, f"{dom!r} {obj!r}: {p} vs {r}")
            n += 1

        # ---- C. _auto_cq ------------------------------------------------------------------------------------------------------------
        keys = list(q._MISSING_TO_QUESTION)
        MISS0 = []
        for k in keys:
            MISS0 += [k, k.upper(), k.capitalize(), "не указан " + k, k + "а", "нет: " + k + "!", k.title()]
        MISS0 += ["", " ", "непонятно", "ОБЪЕКТ и действие", "действие и объект", "бюджет, место", "Уточнить сезон/цель", "İ", "K", "Ǆ", "ﬃ", "объект\n", "🌍", "Ɤ", "для  кого"]
        ACTS = [None, "", "купить", "сделать", "{action}", "{", "}", "{0}", "% s", "тест {x}", "действие 🌍", " ", "İ", "a\nb"]
        for m0 in MISS0:
            for act in ACTS:
                for extra in ([], ["второй"]):
                    fr = q.QueryFrame(raw_query="r", enriched_query="e", action=act, missing=[m0] + extra)
                    p, r = both(q._auto_cq, fr)
                    check("C1 _auto_cq", p == r, f"{m0!r} {act!r}: {p} vs {r}")
                    n += 1
        for act in ACTS + [0, 5, ["a"]]:
            for miss in ([], None, 0, ""):
                fr = q.QueryFrame(raw_query="r", enriched_query="e", action=act, missing=miss)
                p, r = both(q._auto_cq, fr)
                check("C2 _auto_cq без missing", p == r, f"{act!r} {miss!r}: {p} vs {r}")
                n += 1
        for miss in ([5], [None], [b"x"], ["\ud800"], ("объект",), "объект", [["объект"]], [""], [" "]):
            for act in (None, "a", 5, "\ud800"):
                fr = q.QueryFrame(raw_query="r", enriched_query="e", action=act, missing=miss)
                p, r = both(q._auto_cq, fr)
                check("C3 _auto_cq типы/суррогаты", p == r, f"{miss!r} {act!r}: {p} vs {r}")
                n += 1
        # случайные сочетания
        pool = keys + ["не", "указано", "И", "ДЛЯ", "кого", " ", "İ", "ſ", "K", "🌍", "x"]
        for _ in range(4000):
            m0 = " ".join(rnd.choice(pool) for _ in range(rnd.randrange(0, 5)))
            act = rnd.choice(ACTS)
            fr = q.QueryFrame(raw_query="r", enriched_query="e", action=act, missing=[m0] if rnd.random() < 0.9 else [])
            p, r = both(q._auto_cq, fr)
            check("C4 _auto_cq фаззинг", p == r, f"{m0!r} {act!r}: {p} vs {r}")
            n += 1

        # ---- D. таблицы изменены / переключатель --------------------------------------------------------------------------------------
        rst()
        q._SAFE_DOMAINS_BACKUP = set(q._SAFE_DOMAINS)
        try:
            new_set = frozenset(q._SAFE_DOMAINS | {"кулинария"})
            old = q._SAFE_DOMAINS
            q._SAFE_DOMAINS = new_set
            check("D1 изменённые домены уважаются (Python-путь)", q._is_safe_domain("кулинария") is True)
        finally:
            q._SAFE_DOMAINS = old
        check("D2 после отката снова Rust и прежний ответ", q._is_safe_domain("кулинария") is False and q._rust_ok() is not None)
        old_m = dict(q._MISSING_TO_QUESTION)
        q._MISSING_TO_QUESTION["объект"] = "ИНОЕ {action}"
        try:
            fr = q.QueryFrame(raw_query="r", enriched_query="e", action="x", missing=["объект"])
            check("D3 изменённые шаблоны уважаются (Python-путь)", q._auto_cq(fr) == "ИНОЕ x")
        finally:
            q._MISSING_TO_QUESTION.clear()
            q._MISSING_TO_QUESTION.update(old_m)
        os.environ.pop("YANDI_ORCH_QUERY_FRAMER_ENGINE", None)
        q._rust_qf = None
        check("D4 по умолчанию выключен", q._get_rust_qf() is None)
        os.environ["YANDI_ORCH_QUERY_FRAMER_ENGINE"] = "rust"
        q._rust_qf = None
        check("D5 включается переменной", q._get_rust_qf() is not None)
        os.environ.pop("YANDI_ORCH_QUERY_FRAMER_ENGINE", None)
        q._rust_qf = None
        check("D6 выключение возвращает Python", q._get_rust_qf() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_ORCH_QUERY_FRAMER_ENGINE", None)
        else:
            os.environ["YANDI_ORCH_QUERY_FRAMER_ENGINE"] = saved
        q._rust_qf = None
    src = (ROOT / "agent" / "orch_query_framer.py").read_text(encoding="utf-8")
    check("D7 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(сравнений: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
