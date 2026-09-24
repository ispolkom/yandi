"""
agent/claim_graph_rust_parity_test.py — доказательство, что rustlib/yandi_rs/src/claim_graph.rs даёт ПОСТРОЧНО ТЕ ЖЕ
ответы, что и agent/claim_graph.py::ClaimGraph (текстовое ядро, _build_graph и весь extract_claims).

    ЭТО НЕ ТЕСТ «RUST РАБОТАЕТ». ЭТО ТЕСТ «RUST РАБОТАЕТ ТАК ЖЕ, КАК PYTHON, СЕЙЧАС».

Двадцать второй шаг переноса Python -> Rust (2026-09-24). Методология — rustlib/README.md («трудные входы»,
«оракул-фаззинг»). Есть чужой тест для теневого графа, но не для самого ClaimGraph. Проверяется: каждая функция
по отдельности на случайной смеси ключевых слов модуля и Unicode (U+001C..1F, İ/ı/K/ſ, нулевой ширины, цифры Unicode,
`**`/`*`/`[...]`, нумерация «1. »), границы длин 20/350/100/50 в СИМВОЛАХ, `not`-подстрока («неделя»), регистр
«является»/«не является» (в исходном тексте, не lower), пересечение слов > 3, NaN/inf/bool/огромные числа в
relevance_to_query (питоновские max/min), порядок рёбер в _build_graph и весь extract_claims (детерминированные id).

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.claim_graph_rust_parity_test
"""
from __future__ import annotations

import os
import random
import sys
import types
from pathlib import Path
from unittest import mock

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
        import yandi_rs.claim_graph as rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен в этом интерпретаторе ({e}) — см. rustlib/README.md")
        return 0

    import logging
    logging.getLogger("yandi.claim_graph").setLevel(logging.ERROR)
    import agent.claim_graph as cgm

    os.environ.pop("YANDI_CLAIM_GRAPH_ENGINE", None)
    cgm._rust_cg = None
    py = cgm.ClaimGraph()
    rng = random.Random(20260924)

    kw = ["является", "Является", "ЯВЛЯЕТСЯ", "не является", "Не является", "определяется", "называется", "представляет собой", "состоит из", "включает",
          "содержит", "возник", "произошёл", "создан", "относится к", "связан с", "может быть", "существует", "известно", "установлено", "не установлено",
          "вопрос пользователя", "запрос  пользователя", "пользователь спрашивает", "требует философских", "отвечая на вопрос", "данный документ",
          "эта статья", "источник содержит", "содержание источников", "проанализировав", "результат анализа", "сырые данные", "извлечённые факты",
          "матрица эпистемического", "вероятно", "возможно", "предположительно", "гипотеза", "неизвестно", "остаётся неясным", "предмет дискуссии",
          "это", "означает", "согласно", "по данным", "по  данным", "исследование", "показывает", "факт", "доказано", "не", "нет", "нельзя", "невозможно",
          "неделя", "Юпитер", "планета", "газовый гигант", "12", "3,5", "٣٤", "²", "[1]", "[источник]", "**жирный**", "*курсив*", "1. ", "12. ", "٣. ",
          "температура", "около 145 градусов", "Солнечной системы", "крупнейшей", "и", "в", "на", "с", "по"]
    tricky = ["́", "​", "\x1c", "\x1f", " ", "　", "\t", "\n", " ", "\x85", "İ", "ı", "K", "ſ", "ß", "Σ", "😀", "٣", "²"]
    seps = [". ", "! ", "? ", ".\n", ". \t", ".  ", ". \x1c", ".", " ", "  "]

    def rand_text():
        parts = []
        for _ in range(rng.randint(1, 12)):
            w = rng.choice(kw) if rng.random() < 0.75 else rng.choice(tricky)
            if rng.random() < 0.2:
                w = w.upper() if rng.random() < 0.5 else w.capitalize()
            if rng.random() < 0.1:
                i = rng.randint(0, len(w))
                w = w[:i] + rng.choice(tricky) + w[i:]
            parts.append(w)
        out = ""
        for p in parts:
            out += p + rng.choice([" ", " ", " ", "  ", "\x1c", ", ", rng.choice(seps)])
        return out

    texts = ["", " ", "коротко", "а" * 19, "а" * 20, "а" * 21, "а" * 349, "а" * 350, "а" * 351, "я" * 100, "я" * 101,
             "Юпитер является крупнейшей планетой Солнечной системы", "Вопрос пользователя требует философских размышлений о жизни",
             "1. **Жирный** и *курсив* [1] текст про Юпитер который является газовым гигантом"] + [rand_text() for _ in range(12000)]
    texts += ["".join(rng.choice(kw + tricky) for _ in range(rng.randint(1, 8))) for _ in range(3000)]
    # Границы длины 20/350 при наличии маркера «утверждения о мире» (иначе граница ненаблюдаема) и с кириллицей (2 байта/символ)
    for marker in ("является", "12", "٣٤", "Вопрос пользователя"):
        for L in (18, 19, 20, 21, 22, 348, 349, 350, 351, 352):
            texts.append((marker + "я" * L)[:L].ljust(L, "я") if len(marker) < L else marker[:L])
            texts.append(("я" * (L - len(marker)) + marker) if L > len(marker) else marker[:L])

    def cmp(label, p_fn, r_fn, *args):
        try:
            p = p_fn(*args)
        except Exception as e:                            # noqa: BLE001
            p = f"EXC:{type(e).__name__}"
        try:
            r = r_fn(*args)
        except Exception as e:                            # noqa: BLE001
            r = f"EXC:{type(e).__name__}"
        if isinstance(p, float) and p != p:
            p = "NaN"
        if isinstance(r, float) and r != r:
            r = "NaN"
        check(f"{label} {[str(a)[:40] for a in args]}", p == r, f"python={str(p)[:200]} rust={str(r)[:200]}")

    for t in texts:
        cmp("split", py._split_into_sentences, lambda x: list(rs.split_into_sentences(x)), t)
        cmp("clean", py._clean_sentence, rs.clean_sentence, t)
        cmp("is_world_claim", py._is_world_claim, rs.is_world_claim, t)
        cmp("claim_type", py._determine_claim_type, rs.determine_claim_type, t)
    def rs_conf(x, r):
        try:
            f = float(r)
        except OverflowError:                            # огромное целое: и Python-версия (r * 0.15) падает так же
            return "EXC:OverflowError"
        return rs.calculate_confidence(x, f)

    for t in texts[:3000]:
        rel = rng.choice([0.5, 0, 1, -1, 0.0, 0.99, 100, float("nan"), float("inf"), float("-inf"), True, False, 10 ** 400, 2.5, -0.0])
        cmp("confidence", lambda x, r: py._calculate_confidence(x, {"relevance_to_query": r}), rs_conf, t, rel)
    for uri in ["https://en.wikipedia.org/x", "https://sciencenews.org", "nature.com/a", "http://news.example.com", "myblog.example", "local", "", "WIKIPEDIA",
                "x-science-y", "natur", "news blog", "http://a.b/c"]:
        for st in ["local_registry", "web", "", "LOCAL", "remote"]:
            cmp("source_reliability", lambda u, s: py._get_source_reliability({"source_uri": u, "source_type": s}), rs.source_reliability, uri, st)
    for a in texts[:2500]:
        b = rng.choice(texts)
        cmp("is_contradiction", py._is_contradiction, rs.is_contradiction, a, b)
        cmp("is_support", py._is_support, rs.is_support, a, b)
    # «является»/«не является» в разных регистрах и без отрицаний
    for a, b in [("Это является так", "Это не является так"), ("Это Является так", "Это не является так"), ("ЭТО ЯВЛЯЕТСЯ", "не является"),
                 ("день длинный", "ночь короткая"), ("неделя", "день"), ("НЕТ", "да"), ("нет", "нельзя"), ("невозможно", "нет")]:
        cmp("contradiction(regs)", py._is_contradiction, rs.is_contradiction, a, b)

    # ── _build_graph: порядок рёбер ──
    def mk(texts_, engine):
        os.environ.pop("YANDI_CLAIM_GRAPH_ENGINE", None)
        if engine:
            os.environ["YANDI_CLAIM_GRAPH_ENGINE"] = "rust"
        cgm._rust_cg = None
        g = cgm.ClaimGraph()
        g.claims = [cgm.Claim(claim_id=f"c{i}", text=t) for i, t in enumerate(texts_)]
        g._build_graph()
        out = [(c.claim_id, list(c.supports), list(c.contradicts), list(c.depends_on)) for c in g.claims]
        os.environ.pop("YANDI_CLAIM_GRAPH_ENGINE", None)
        cgm._rust_cg = None
        return out

    for _ in range(600):
        n = rng.randint(0, 9)
        ts = [rand_text() for _ in range(n)]
        if n >= 2 and rng.random() < 0.6:                       # много общих слов -> поддержка
            base = " ".join(rng.choice(kw) for _ in range(6))
            ts = [base + " " + rand_text() for _ in ts]
        check(f"_build_graph n={n}", mk(ts, False) == mk(ts, True))

    # ── extract_claims целиком (детерминированные id, без времени) ──
    def extract(evidence, engine):
        os.environ.pop("YANDI_CLAIM_GRAPH_ENGINE", None)
        if engine:
            os.environ["YANDI_CLAIM_GRAPH_ENGINE"] = "rust"
        cgm._rust_cg = None
        counter = iter(range(10 ** 6))
        fake = mock.Mock(side_effect=lambda: types.SimpleNamespace(hex=f"{next(counter):08x}" + "0" * 24))
        with mock.patch.object(cgm.uuid, "uuid4", fake):
            g = cgm.ClaimGraph()
            claims = g.extract_claims(evidence)
        out = [(c.claim_id, c.text, c.claim_type, c.confidence, c.source_reliability, list(c.evidence_for), list(c.supports),
                list(c.contradicts), list(c.depends_on), c.verification_status) for c in claims]
        os.environ.pop("YANDI_CLAIM_GRAPH_ENGINE", None)
        cgm._rust_cg = None
        return out

    for _ in range(300):
        evs = []
        for k in range(rng.randint(0, 5)):
            content = " ".join(rand_text() for _ in range(rng.randint(1, 4)))
            evs.append({"evidence_id": f"ev{k}", "content_excerpt": content, "relevance_to_query": rng.choice([0.5, 0.2, 1.0, 0, 0.9]),
                        "source_uri": rng.choice(["https://en.wikipedia.org/x", "news.example", "myblog.com", "", "https://a.b"]),
                        "source_type": rng.choice(["web", "local_registry", ""])})
        check("extract_claims", extract(evs, False) == extract(evs, True))
    check("extract_claims (пусто)", extract([], False) == extract([], True) == [])

    # ── G: переключатель ──
    saved = os.environ.get("YANDI_CLAIM_GRAPH_ENGINE")
    try:
        os.environ.pop("YANDI_CLAIM_GRAPH_ENGINE", None)
        cgm._rust_cg = None
        check("G0 по умолчанию активен Python-движок", cgm._get_rust_cg() is None)
        os.environ["YANDI_CLAIM_GRAPH_ENGINE"] = "rust"
        cgm._rust_cg = None
        check("G1 переменная окружения переключает на Rust", cgm._get_rust_cg() is rs)
        os.environ.pop("YANDI_CLAIM_GRAPH_ENGINE", None)
        cgm._rust_cg = None
        check("G3 выключение возвращает Python (кэш не залипает)", cgm._get_rust_cg() is None)
    finally:
        if saved is None:
            os.environ.pop("YANDI_CLAIM_GRAPH_ENGINE", None)
        else:
            os.environ["YANDI_CLAIM_GRAPH_ENGINE"] = saved
        cgm._rust_cg = None

    src = (ROOT / "agent" / "claim_graph.py").read_text(encoding="utf-8")
    check("G4 сбой импорта yandi_rs пойман (ImportError)", "except ImportError as e:" in src)
    print(f"\n(успешных проверок: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:6]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
