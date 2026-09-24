"""
agent/scene_builder_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/scene_builder.rs даёт
ПОСТРОЧНО ТЕ ЖЕ значения, что и agent/scene_builder.py::SceneBuilder.build, на одних и тех же примерах.

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Девятнадцатый шаг переноса Python -> Rust (2026-09-24). Методология — rustlib/README.md (правило «трудные
входы»). Своего теста у модуля не было. Особенности:
  * participants/mentioned/coalition в оригинале — `list(set(...))`: порядок зависит от PYTHONHASHSEED, поэтому
    сравниваются КАК МНОЖЕСТВА (плюс проверка, что в Rust-списках нет дублей);
  * `text.isupper()` — Python-семантика Unicode: проверена по ВСЕМ кодовым точкам и на случайных строках,
    границы `len(text) > 10` в символах (кириллица 2 байта/символ);
  * `\\b`-слова (ты/тебе/тобой/твой/…/ваше) — соседи, где Python-`\\w` и regex-крейт расходятся (ударение
    U+0301, ², цифры Unicode, `_`, дефис);
  * каждый паттерн всех таблиц в 8 формах; порядок сложения float-счётчиков и ничьи (фаззинг 8000 фраз);
  * context: None / {} / is_dialog False/0/"yes"/None / не-dict (тот же исход, что у Python).

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.scene_builder_rust_parity_test
"""
from __future__ import annotations

import dataclasses
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
        import yandi_rs.scene_builder as rs
        import yandi_rs.py_text as pt
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import agent.scene_builder as sbm

    os.environ.pop("YANDI_SCENE_BUILDER_ENGINE", None)
    sbm._rust_sb = None
    sb = sbm.SceneBuilder()
    rng = random.Random(20260924)

    # ── A: str.isupper() по ВСЕМ кодовым точкам и на случайных строках ──
    bad = [cp for cp in range(0x110000) if not (0xD800 <= cp <= 0xDFFF) and chr(cp).isupper() != pt.isupper(chr(cp))]
    check("A1 isupper совпадает на всех кодовых точках", not bad, f"{[hex(c) for c in bad[:8]]}")
    pool = ["A", "b", "Я", "я", "1", " ", "!", "ǅ", "ǈ", "Ⅷ", "ⅷ", "ẞ", "ß", "İ", "ª", "º", "Ⓐ", "ⓐ", "ʰ", "́", "_", "ς", "Σ", "Ω"]
    for _ in range(4000):
        s = "".join(rng.choice(pool) for _ in range(rng.randint(0, 8)))
        check(f"A2 isupper {s!r}", s.isupper() == pt.isupper(s))

    def scene_dict(sc):
        d = dataclasses.asdict(sc)
        for k in ("participants", "mentioned", "coalition"):
            d[k] = sorted(d[k])
        return d

    def py_scene(text, ctx=None):
        sbm._rust_sb = False
        return sb.build(text, ctx)

    def rs_scene(text, ctx=None):
        os.environ["YANDI_SCENE_BUILDER_ENGINE"] = "rust"
        sbm._rust_sb = None
        try:
            return sb.build(text, ctx)
        finally:
            os.environ.pop("YANDI_SCENE_BUILDER_ENGINE", None)
            sbm._rust_sb = None

    # ── тексты ──
    import logging
    logging.getLogger("yandi.scene_builder").setLevel(logging.ERROR)
    words = ["ты", "тебе", "тобой", "твой", "твоя", "твоё", "ваш", "ваша", "ваше"]
    ctxs = ["{}", "{} тут", "вот {}", "эй,{}!", "{}-{}", "{}_", "_{}", "{}2", "2{}", "{}́", "{}́ ", "{}²", "²{}", "п{}", "{}т",
            "{}٣", "{}​", "{}\x1c", "\x1c{}", "{}?", "ё{}", "{}ё", "́{}"]
    texts = ["", " ", "?", "!", "привет", "Ты", "yandi", "Янди", "YANDI", "you and i", "Опиши меня", "охарактеризуй меня",
             "Пойдёшь за меня замуж?", "Твоё видение песни Арктик и Асти", "Как работает DHT?", "Расскажи о себе", "Что такое любовь?",
             "мы с claude", "с нами gpt", "мы и deepseek", "вместе с gemini", "мы все вместе", "нас трое", "с нами", "нами", "мы",
             "хаха)", ":-)", "лол", "рофл", "прикол", "!!!", "ПРИВЕТ МИР!!", "ПРИВЕТ ВСЕМ", "ПРИВЕТ ВСЕ", "ПРИВЕТ ВСЕМ!", "1234567890123", "ǅǅǅǅǅǅǅǅǅǅǅ",
             "ⅧⅧⅧⅧⅧⅧⅧⅧⅧⅧⅧ", "ẞẞẞẞẞẞẞẞẞẞẞ", "İİİİİİİİİİİ", "ABCDEFGHIJK", "ABCDEFGHIJ", "ABCDEFGHIJKa", "AAAAAAAAAAAªa",
             "İ", "ſ", "K", "K"]
    for w in words:
        for c in ctxs:
            texts.append(c.replace("{}", w))
    # каждый паттерн всех таблиц (берём из самого Python-объекта и из локального topic_patterns)
    import ast
    src = (ROOT / "agent" / "scene_builder.py").read_text(encoding="utf-8")
    topic_patterns = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "topic_patterns" for t in node.targets):
            topic_patterns = ast.literal_eval(node.value)
    lits = []
    lits += sb.yandi_names + sb.ai_names + sb.ty_verb_forms + sb.group_reference + sb.coalition_markers
    lits += [p[2:-2] for p in sb.self_reference]
    for v in list(sb.speech_act_patterns.values()) + list(topic_patterns.values()):
        lits += [p.replace("\\?", "?") for p in v]
    lits = sorted(set(lits))
    for lit in lits:
        for q in (lit, lit.upper(), lit.capitalize(), "вот " + lit + " тут", lit + "!", "\x1c" + lit + "\x1f", lit.replace(" ", "\x1c"),
                  lit + " " + lit):
            texts.append(q)
    frags = lits + ["мы", "я", "он", "и", "не", "а", "?", "!", ")", ":-)", "...", "очень", "ТЫ", "ПРИВЕТ"]
    for _ in range(8000):
        texts.append(" ".join(rng.choice(frags) for _ in range(rng.randint(1, 7))))
    # случайные регистры (для isupper/pressure)
    for _ in range(1500):
        t = " ".join(rng.choice(frags) for _ in range(rng.randint(1, 5)))
        texts.append(t.upper() if rng.random() < 0.5 else t.capitalize())

    # «Перегруженные» категории: ничьи после отсечения min(1.0, score) видны только когда 2–3 категории сразу
    # набирают >=3 совпадений (тогда 1.0 == 1.0 и решает порядок, а не число совпадений).
    categories = [[p.replace("\\?", "?") for p in v] for v in list(sb.speech_act_patterns.values()) + list(topic_patterns.values())]
    categories = [c for c in categories if len(c) >= 3]
    for _ in range(4000):
        parts = []
        for cat in rng.sample(categories, rng.randint(2, 3)):
            k = rng.randint(3, len(cat))
            parts += rng.sample(cat, k)
        rng.shuffle(parts)
        texts.append(" ".join(parts))

    contexts = [None, {}, {"is_dialog": True}, {"is_dialog": False}, {"is_dialog": 0}, {"is_dialog": 1}, {"is_dialog": ""}, {"is_dialog": "yes"},
                {"is_dialog": None}, {"other": 1}]
    n = 0
    for t in texts:
        for ctx in contexts if rng.random() < 0.15 or len(t) < 12 else [None, {"is_dialog": False}]:
            n += 1
            p_, r_ = scene_dict(py_scene(t, ctx)), scene_dict(rs_scene(t, ctx))
            ok = p_ == r_
            check(f"build {t[:40]!r} ctx={ctx}", ok, f"python={p_} rust={r_}") if not ok else None
    global _OK
    _OK += n - sum(1 for f in FAILURES if f.startswith("build "))
    # без дублей в Rust-списках
    for t in texts[:500]:
        sc = rs_scene(t, None)
        check(f"нет дублей {t[:30]!r}", all(len(set(getattr(sc, k))) == len(getattr(sc, k)) for k in ("participants", "mentioned", "coalition")))
    check("возвращается настоящий SocialScene", isinstance(rs_scene("привет"), sbm.SocialScene))

    # не-dict context / не-str текст: тот же исход, что у Python (Rust не участвует)
    for text, ctx in (("привет", []), ("привет", "x"), (None, None), (123, None)):
        outs = []
        for eng in (False, "rust"):
            if eng == "rust":
                os.environ["YANDI_SCENE_BUILDER_ENGINE"] = "rust"
                sbm._rust_sb = None
            else:
                sbm._rust_sb = False
            try:
                outs.append(scene_dict(sb.build(text, ctx)))
            except Exception as e:                        # noqa: BLE001
                outs.append(f"EXC:{type(e).__name__}")
            os.environ.pop("YANDI_SCENE_BUILDER_ENGINE", None)
        check(f"вход {text!r}/{ctx!r} — тот же исход", outs[0] == outs[1], f"{outs}")

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_SCENE_BUILDER_ENGINE")
    try:
        os.environ.pop("YANDI_SCENE_BUILDER_ENGINE", None)
        sbm._rust_sb = None
        check("G0 по умолчанию активен Python-движок", sbm._get_rust_sb() is None)
        os.environ["YANDI_SCENE_BUILDER_ENGINE"] = "rust"
        sbm._rust_sb = None
        check("G1 переменная окружения переключает на Rust", sbm._get_rust_sb() is rs)
        custom = sbm.SceneBuilder()
        custom.yandi_names = ["мой_особый_псевдоним"]
        sbm._rust_sb = None
        r = custom.build("привет мой_особый_псевдоним", {"is_dialog": False})
        check("G2 изменённые паттерны экземпляра уважаются (Python-путь)", "мой_особый_псевдоним" in r.mentioned, f"{r.mentioned}")
        os.environ.pop("YANDI_SCENE_BUILDER_ENGINE", None)
        sbm._rust_sb = None
        check("G3 выключение возвращает Python (кэш не залипает)", sbm._get_rust_sb() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_SCENE_BUILDER_ENGINE", None)
        else:
            os.environ["YANDI_SCENE_BUILDER_ENGINE"] = saved
        sbm._rust_sb = None

    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)

    print(f"\n(сцен сравнено: {n}; успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:8]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
