"""
agent/entity_resolver_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/entity_resolver.rs даёт
ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/entity_resolver.py::EntityResolver.resolve.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Двадцать первый шаг переноса Python -> Rust (2026-09-24). Своего теста не было. Особенности:
  * в оригинале словари — `set` строк: при совпадении нескольких игр поле `game` = последняя в порядке обхода set
    (хеш-рандомизация), порядок `categories` тоже не фиксирован — тест допускает ЛЮБОЙ из возможных Python-исходов
    (`game` ∈ множеству совпавших игр в верхнем регистре, `categories` — как мультимножество);
  * `w[0].isupper()` — Python-семантика Unicode по первому символу (Lt, Other_Uppercase, İ, ẞ…);
  * пустой/пробельный запрос: `0 >= 0.0` истинно — is_proper_name=True (как в Python);
  * порядок прибавления float (0.5, +0.2, 0.1×k, 0.3×k, 0.1×k, +0.3) и порог `> 0.4`, `min(1.0, …)`,
    перезапись type "proper_name" поверх "game_location".

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.entity_resolver_rust_parity_test
"""
from __future__ import annotations

import os
import random
import sys
from collections import Counter
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
        import yandi_rs.entity_resolver as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.entity_resolver").setLevel(logging.ERROR)
    import agent.entity_resolver as erm

    os.environ.pop("YANDI_ENTITY_RESOLVER_ENGINE", None)
    erm._rust_er = None
    py = erm.EntityResolver()
    rng = random.Random(20260924)

    def comparable(d):
        d = dict(d)
        d["categories"] = sorted(d["categories"])
        return d

    def eq(q):
        p_, r_ = py.resolve(q), dict(rs.resolve(q))
        allowed_games = {g.upper() for g in erm.KNOWN_GAMES if g in q.strip().lower()} or {None}
        game_ok = p_["game"] in allowed_games and r_["game"] in allowed_games
        p2, r2 = comparable(p_), comparable(r_)
        p2.pop("game"), r2.pop("game")
        return game_ok and p2 == r2, p_, r_

    words = ["Легенда", "Форнема", "сектор", "x3", "X3", "x3 terran conflict", "x4", "X4 Foundations", "фильм", "книга", "игра", "песня", "сериал", "аниме",
             "звездная система", "корабль", "станция", "гонка", "фракция", "Москва", "ёлка", "Ёлка", "İstanbul", "ǅ", "ẞ", "Ⅷ", "ⅷ", "1", "ты", "мне",
             "и", "не", "а", "Ω", "ω", "А", "а", "Я", "я", "Éa", "éa", "ＡＢＣ", "ａｂｃ", "😀", "́a", "٣"]
    tricky = ["́", "​", "\x1c", "\x1f", " ", "　", "\t", "\n", " ", "\x85"]
    queries = ["", " ", "\x1c", "\t\n", "?", "x3", "X3", "x3 terran conflict", "Легенда Форнема", "Сектор X3", "фильм про x3", "Один", "один"]
    for _ in range(9000):
        parts = [rng.choice(words) for _ in range(rng.randint(1, 6))]
        seps = [rng.choice([" ", " ", " ", "  ", "\t", "\x1c", " ", "\n", ", "]) for _ in parts]
        q = "".join(p + s for p, s in zip(parts, seps))
        if rng.random() < 0.25:
            q = rng.choice(tricky) + q
        queries.append(q)
    for _ in range(3000):
        queries.append("".join(rng.choice(words + tricky) + rng.choice([" ", ""]) for _ in range(rng.randint(1, 5))))
    # несколько игр сразу и много терминов/медиа (суммы float, перезапись type)
    for _ in range(2000):
        queries.append(" ".join(rng.sample(erm.KNOWN_GAMES + erm.KNOWN_GAME_TERMS + erm.KNOWN_MEDIA + ["Имя", "Фамилия", "Город"], rng.randint(2, 8))))

    for q in queries:
        ok, p_, r_ = eq(q)
        check(f"resolve {q[:50]!r}", ok, f"python={p_} rust={r_}")

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_ENTITY_RESOLVER_ENGINE")
    try:
        os.environ.pop("YANDI_ENTITY_RESOLVER_ENGINE", None)
        erm._rust_er = None
        check("G0 по умолчанию активен Python-движок", erm._get_rust_er() is None)
        os.environ["YANDI_ENTITY_RESOLVER_ENGINE"] = "rust"
        erm._rust_er = None
        check("G1 переменная окружения переключает на Rust", erm._get_rust_er() is rs)
        for q in queries[:300]:
            a = comparable(py.resolve(q))
            b = comparable(dict(rs.resolve(q)))
            games = {g.upper() for g in erm.KNOWN_GAMES if g in q.strip().lower()} or {None}
            check(f"G2 переключённый resolve {q[:30]!r}", a["game"] in games and b["game"] in games and {**a, "game": 0} == {**b, "game": 0})
        custom = erm.EntityResolver()
        custom.known_media = {"мой_особый_термин"}
        r = custom.resolve("текст мой_особый_термин")
        check("G2b изменённые словари экземпляра уважаются (Python-путь)", "мой_особый_термин" in r["categories"], f"{r}")
        os.environ.pop("YANDI_ENTITY_RESOLVER_ENGINE", None)
        erm._rust_er = None
        check("G3 выключение возвращает Python (кэш не залипает)", erm._get_rust_er() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_ENTITY_RESOLVER_ENGINE", None)
        else:
            os.environ["YANDI_ENTITY_RESOLVER_ENGINE"] = saved
        erm._rust_er = None

    src = (ROOT / "agent" / "entity_resolver.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:8]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
