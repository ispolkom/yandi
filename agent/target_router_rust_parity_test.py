"""
agent/target_router_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/target_router.rs даёт
ПОСТРОЧНО ТЕ ЖЕ ответы, что и agent/target_router.py, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Семнадцатый шаг переноса Python -> Rust (2026-09-24). Методология — rustlib/README.md (правило «трудные
входы»). Своего теста у модуля не было. Ключевое: паттерны `\b..\b` (граница слова) — крейт regex понимает
`\w` иначе, чем Python, поэтому в Rust они написаны вручную; здесь проверяются именно те входы, где
определения расходятся (ударение U+0301, надстрочные цифры, `_`, цифры Unicode, дефис/апостроф рядом со
словом), плюс порядок сложения float-счётчиков (фаззинг фраз из ключевых слов), `^ты\s` с разделителями
U+001C..1F, регистр/пробелы, каждое ключевое слово по отдельности.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.target_router_rust_parity_test
"""
from __future__ import annotations

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
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    try:
        import yandi_rs.target_router as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.target_router as tr

    os.environ.pop("YANDI_TARGET_ROUTER_ENGINE", None)
    tr._rust_tr = None
    rng = random.Random(20260924)

    words = ["ты", "тебе", "твой", "твоя", "твоё", "я", "меня", "мне", "мой", "моя", "моё"]
    contexts = ["{}", "{} тут", "вот {}", "эй,{}!", "{}-{}", "{}_", "_{}", "{}2", "2{}", "{}́", "{}́ ", "{}²", "²{}", "{}·",
                "{}'", "'{}'", "п{}", "{}т", "{}٣", "{}​", "{} ", "{}\x1c", "\x1c{}", "{}　?", "?{}", "{}́?",
                "́{}", "ё{}", "{}ё", "{}ª", "{}Ⅰ", "{}①", "{}०"]
    queries = ["", " ", "?", "привет", "Ты", "ТЫ ТЫ", "ты?", "ты\tтут", "ты\x1cтут", "ты\x1fтут", "ты　тут", "ты тут", "ты​тут",
               "Пойдёшь за меня замуж?", "Ты не можешь различить отношение между полами и отношения между цифрой и биологией??",
               "Как работает DHT?", "Расскажи о себе", "Что такое любовь?", "Твоё мнение о песне", "Придумай историю про дракона",
               "Помоги мне выбрать ноутбук", "Кто ты?", "Опиши себя", "себе", "о себе", "Guns N' Roses", "x3 сектор", "Янди", "YANDI",
               "İ", "ſ", "K", "K"]
    for w in words:
        for c in contexts:
            queries.append(c.replace("{}", w))
    kw = ["песн", "song", "трек", "композиц", "музык", "фильм", "movie", "кино", "сериал", "книг", "book", "роман", "игр", "game", "x3", "сектор",
          "произведени", "картин", "арктик", "асти", "guns", "roses", "сколько", "когда", "где", "кто такой", "что такое", "определение", "факты",
          "статистика", "история", "биография", "википедия", "как работает", "почему происходит", "найди", "поищи", "найти", "поиск",
          "информация о", "данные по", "как установить", "как настроить", "инструкция", "скажи", "расскажи", "объясни", "покажи", "напиши",
          "как ты думаешь", "твоё мнение", "что ты чувствуешь", "ты считаешь", "пойдёшь", "выйдешь", "любишь", "ты бы", "ты могла", "ты хочешь",
          "расскажи о себе", "опиши себя", "представься", "кто ты", "что ты", "ты кто", "ты женщина", "ты девушка", "ты цифровая", "первая цифровая",
          "о себе", "янди", "yandi", "?"]
    for k in kw:
        for form in ("{}", "{}?", "Вот {} тут", "{}\x1c", "{}".upper()):
            queries.append(form.replace("{}", k) if "{}" in form else k.upper())
    # Фаззинг: смесь слов-адресатов и ключевых слов — порядок float-сложения счётчиков и пороги 0.3
    pool = words + kw + ["и", "в", "а", "не", "это", "!", "..."]
    for _ in range(6000):
        queries.append(" ".join(rng.choice(pool) for _ in range(rng.randint(1, 9))))

    for q in queries:
        py_r, rs_r = tr.detect_target(q), tuple(rs.detect_target(q))
        check(f"detect_target {q[:50]!r}", py_r == rs_r, f"python={py_r} rust={rs_r}")
    for t in ["ai", "user", "object", "knowledge", "unknown", "", "AI", "zzz", "ai "]:
        check(f"get_target_description {t!r}", tr.get_target_description(t) == rs.get_target_description(t))

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_TARGET_ROUTER_ENGINE")
    try:
        import logging
        logging.getLogger("yandi.target_router").setLevel(logging.ERROR)
        os.environ.pop("YANDI_TARGET_ROUTER_ENGINE", None)
        tr._rust_tr = None
        check("G0 по умолчанию активен Python-движок", tr._get_rust_tr() is None)
        os.environ["YANDI_TARGET_ROUTER_ENGINE"] = "rust"
        tr._rust_tr = None
        check("G1 переменная окружения переключает на Rust", tr._get_rust_tr() is rs)
        for q in queries[:300]:
            check(f"G2 переключённый detect_target {q[:30]!r}", tr.detect_target(q) == tuple(rs.detect_target(q)))
        os.environ.pop("YANDI_TARGET_ROUTER_ENGINE", None)
        tr._rust_tr = None
        check("G3 выключение возвращает Python (кэш не залипает)", tr._get_rust_tr() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_TARGET_ROUTER_ENGINE", None)
        else:
            os.environ["YANDI_TARGET_ROUTER_ENGINE"] = saved
        tr._rust_tr = None

    src = (ROOT / "agent" / "target_router.py").read_text(encoding="utf-8")
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
