"""
agent/object_resolver_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/object_resolver.rs даёт
ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/object_resolver.py::ObjectResolver.resolve.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Двадцатый шаг переноса Python -> Rust (2026-09-24). Методология — rustlib/README.md (правила «трудные входы» и
«оракул-фаззинг»). Своего теста не было. Проверяется: КАЖДЫЙ паттерн в 9 формах (регистр, U+001C..1F,
İ/ı/K/ſ-варианты IGNORECASE, префикс/суффикс), ничьи и приоритет самопознания (+0.2), отсечение 1.0, порядок
типов, фаззинг фраз из фрагментов паттернов + случайный Unicode, изменённые паттерны экземпляра.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.object_resolver_rust_parity_test
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
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    try:
        import yandi_rs.object_resolver as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.object_resolver").setLevel(logging.ERROR)
    import agent.object_resolver as orm

    os.environ.pop("YANDI_OBJECT_RESOLVER_ENGINE", None)
    orm._rust_or = None
    py = orm.ObjectResolver()
    rng = random.Random(20260924)

    pats = [p for d in py.patterns.values() for p in d["patterns"]]
    tricky = ["́", "​", "\x1c", " ", "İ", "ı", "K", "ſ", "ß", "ǅ", "Σ", "ς", "😀", "\n", "\t", "²", "٣"]
    queries = ["", " ", "\x1c", "?", "привет", "xyz", "Песня", "ПЕСНЯ", "İ", "ı", "K", "ſ"]
    for p in pats:
        for q in (p, p.upper(), p.capitalize(), "вот " + p + " тут", p + "!", "\x1c" + p + "\x1f", p.replace("i", "İ").replace("k", "K"),
                  p.replace("i", "ı").replace("s", "ſ"), p + rng.choice(tricky), rng.choice(tricky) + p, p[: len(p) // 2] + rng.choice(tricky) + p[len(p) // 2:]):
            queries.append(q)
    frags = pats + ["и", "не", "а", "?", "очень", "ты", "мне"]
    for _ in range(6000):
        queries.append(" ".join(rng.choice(frags) for _ in range(rng.randint(1, 6))))
    for _ in range(2000):
        queries.append("".join(rng.choice(tricky + [rng.choice(pats)]) for _ in range(rng.randint(1, 6))))
    # перегруженные: много совпадений из разных типов (ничьи и приоритет)
    for _ in range(3000):
        queries.append(" ".join(rng.sample(pats, rng.randint(2, 6))))

    for q in queries:
        p_, r_ = py.resolve(q), dict(rs.resolve(q))
        check(f"resolve {q[:50]!r}", p_ == r_, f"python={p_} rust={r_}")

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_OBJECT_RESOLVER_ENGINE")
    try:
        os.environ.pop("YANDI_OBJECT_RESOLVER_ENGINE", None)
        orm._rust_or = None
        check("G0 по умолчанию активен Python-движок", orm._get_rust_or() is None)
        os.environ["YANDI_OBJECT_RESOLVER_ENGINE"] = "rust"
        orm._rust_or = None
        check("G1 переменная окружения переключает на Rust", orm._get_rust_or() is rs)
        for q in queries[:300]:
            check(f"G2 переключённый resolve {q[:30]!r}", py.resolve(q) == dict(rs.resolve(q)))
        custom = orm.ObjectResolver()
        custom.patterns["song"]["patterns"] = ["мой_особый_паттерн"]
        r = custom.resolve("текст мой_особый_паттерн")
        check("G2b изменённые паттерны экземпляра уважаются (Python-путь)", r["matched_pattern"] == "мой_особый_паттерн", f"{r}")
        check("G2c нестрока не идёт в Rust", _raises(lambda: py.resolve(None)) == _raises(lambda: _py_only(orm, py, None)))
        os.environ.pop("YANDI_OBJECT_RESOLVER_ENGINE", None)
        orm._rust_or = None
        check("G3 выключение возвращает Python (кэш не залипает)", orm._get_rust_or() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_OBJECT_RESOLVER_ENGINE", None)
        else:
            os.environ["YANDI_OBJECT_RESOLVER_ENGINE"] = saved
        orm._rust_or = None

    src = (ROOT / "agent" / "object_resolver.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(успешных проверок: {_OK})")
    return 1 if FAILURES else 0


def _raises(fn):
    try:
        fn()
        return "ok"
    except Exception as e:                                    # noqa: BLE001
        return type(e).__name__


def _py_only(orm, py, q):
    saved = orm._rust_or
    orm._rust_or = False
    try:
        return py.resolve(q)
    finally:
        orm._rust_or = saved


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:8]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
