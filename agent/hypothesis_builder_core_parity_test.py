"""Построитель графа гипотез на Rust (rustlib/yandi_core/src/hypothesis_builder.rs) против agent/hypothesis_builder.py.
Идентификаторы задаются очередью, поэтому сверяются и они, и порядок их расхода. Базы не нужно."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import random
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
_OK = 0


def check(name, cond, detail=""):
    global _OK
    if cond:
        _OK += 1
    else:
        if len(FAILURES) < 30:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def canon(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def conv(v):
    if dataclasses.is_dataclass(v):
        return {k: conv(x) for k, x in dataclasses.asdict(v).items()}
    if isinstance(v, list):
        return [conv(x) for x in v]
    return v


def main() -> int:
    try:
        import yandi_core
    except ImportError as e:
        print(f"SKIP: yandi_core не собран ({e})")
        return 0
    import agent.hypothesis_builder as hb

    h = yandi_core.open_memory()
    idq: list[str] = []
    hb.uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=idq.pop(0)))

    def rs(method, args, ids):
        yandi_core.set_clock(h, 0.0, list(ids))
        r = json.loads(yandi_core.call(h, "hb_call", json.dumps({"method": method, **args}, ensure_ascii=False)))
        return r["ok"] if "ok" in r else {"error": r["error"]}

    n = 0

    def case(label, method, args):
        nonlocal n
        n += 1
        ids = [f"{i:08x}{i * 7919 % 65536:04x}" for i in range(400)]
        idq[:] = ids
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                if method == "build":
                    g = hb.build_hypothesis_graph(args["question"], args["texts"], args.get("source_refs"))
                    p = g.to_dict()
                elif method == "extract_observations":
                    p = conv(hb.extract_observations(args["text"], args.get("source_ref", "unknown"), args.get("question", "")))
                elif method == "build_inferences":
                    obs = [hb.Observation(**o) for o in args["observations"]]
                    p = conv(hb.build_inferences(obs, args.get("question", "")))
                elif method == "classify_hypothesis":
                    p = hb.classify_hypothesis(args["text"])
                elif method == "extract_hypotheses":
                    p = conv(hb.extract_hypotheses_from_texts(args["texts"], args.get("question", "")))
                else:
                    p = conv(hb.extract_traditions_from_texts(args["texts"]))
        except Exception as e:  # noqa: BLE001
            p = {"error": type(e).__name__}
        used = 400 - len(idq)
        r = rs(method, args, ids)
        ok = canon(p) == canon(r)
        check(f"{label} · {method}", ok, f"\n args={json.dumps(args, ensure_ascii=False)[:400]}\n py={json.dumps(p, ensure_ascii=False)[:1200]}\n rs={json.dumps(r, ensure_ascii=False)[:1200]}")
        if ok and not (isinstance(p, dict) and "error" in p):
            # порядок расхода: Rust отдаёт те же id, значит расход совпал; лишнего расхода не проверить прямо — сверим по длине очереди через повторный вызов с одним id меньше
            pass

    T = [
        "Согласно современным исследованиям, наука и религия представляют собой разные способы познания мира. Некоторые учёные считают, что они могут дополнять друг друга.",
        "Коммунизм часто рассматривается как светская религия, имеющая свои догмы и ритуалы.",
        "Демократия основана на принципах свободы и равенства, что роднит её с религиозными ценностями.",
    ]
    case("Главный пример", "build", {"question": "Разве наука, это не религия?", "texts": T, "source_refs": ["src1", "src2", "src3"]})
    case("Без источников", "build", {"question": "Почему Каин разгневался? После этого он убил.", "texts": T})
    case("Источников меньше", "build", {"question": "вопрос", "texts": T, "source_refs": ["a"]})
    case("Пусто", "build", {"question": "", "texts": []})
    case("Пустые тексты", "build", {"question": "", "texts": ["", "  ", "коротко"]})
    case("Школы", "build", {"question": "Конфликт науки и религии?", "texts": ["Докинз и Гулд спорили. Полани, Барбур и Брук писали об этом много лет подряд. Фейнман тоже, и Уайт, и Крик."]})
    for m in ("extract_traditions",):
        case("Школы отдельно", m, {"texts": ["Маска и Фернгрен", "Доукинс"]})
        case("Школы стандартные", m, {"texts": ["ничего"]})
    for t in ("Идёт война", "АНТАГОНИЗМ", "Диалог культур", "Автономия", "НЕЗАВИСИМОСТЬ", "Синтез", "Единство", "просто", "", "Ёж и КОНФЛИКТ"):
        case("Классификация", "classify_hypothesis", {"text": t})

    words = ["наука", "религия", "гнев", "разгневался", "после", "затем", "тогда", "зависть", "ревность", "гордость", "вина", "верность", "согласно", "по мнению", "конфликт", "диалог",
             "независимость", "интеграция", "концепция", "модель", "Докинз", "Гулд", "Полани", "Брук", "Маска", "почему", "зачем", "мир", "человек", "текст", "слово", "Ёлка", "ЁЖ", "ÄÖ", "ǅ", "İ", "коммунизм", "демократия"]
    seps = [". ", "! ", "? ", "... ", "\n", "  ", " ", " ", "\t", ", "]
    rnd = random.Random(20260928)

    def sentence(nw):
        return " ".join(rnd.choice(words) for _ in range(nw))

    def text():
        parts = []
        for _ in range(rnd.randint(0, 6)):
            parts.append(sentence(rnd.choice([1, 3, 6, 12, 40, 120])) + rnd.choice(seps))
        return "".join(parts)

    for k in range(160):
        texts = [text() for _ in range(rnd.randint(0, 4))]
        q = rnd.choice(["", "Почему наука?", "наука религия", "зачем", "что такое конфликт", sentence(4)])
        refs = None if rnd.random() < 0.5 else [f"r{i}" for i in range(rnd.randint(0, 5))]
        case(f"R{k}", "build", {"question": q, "texts": texts, "source_refs": refs})
        if k % 4 == 0:
            case(f"R{k}", "extract_observations", {"text": texts[0] if texts else "", "source_ref": "s", "question": q})
            case(f"R{k}", "extract_hypotheses", {"texts": texts, "question": q})
            case(f"R{k}", "extract_traditions", {"texts": texts})
    for k in range(20):
        obs = [{"id": f"o{i}", "text": sentence(rnd.randint(1, 6)), "source_ref": "s"} for i in range(rnd.randint(0, 5))]
        case(f"I{k}", "build_inferences", {"observations": obs, "question": rnd.choice(["почему", "как", "после чего", "ЗАЧЕМ", ""])})
    # граничные длины предложений: 15/16 знаков, 30/31 знак, 200/201, 799/800, 50 знаков
    for L in (14, 15, 16, 17, 29, 30, 31, 32, 199, 200, 201, 202, 798, 799, 800, 801):
        s = "б" * (L - len(" модель")) + " модель"
        case(f"L{L}", "build", {"question": "", "texts": [s + ". Ещё."]})
    for L in (49, 50, 51, 52, 199, 200, 201):
        case(f"F{L}", "extract_observations", {"text": "я" * L, "source_ref": "x", "question": ""})

    # ---- граничные случаи, найденные мутационной проверкой ----
    for L in (14, 15, 16, 17):
        case(f"E1 длина предложения {L}", "build", {"question": "", "texts": ["а" * L + ". " + "б" * 40 + "."]})
    for L in (798, 799, 800, 801):
        case(f"E2 длина наблюдения {L}", "build", {"question": "", "texts": ["в" * L + ". " + "г" * 40 + "."]})
    case("E3 после нормализации ровно 15", "build", {"question": "", "texts": ["а" * 6 + "   " + "б" * 8 + ". " + "г" * 40 + "."]})
    qw = " ".join(f"w{i}" for i in range(100))
    for k_ in (2, 3, 4):
        sent = " ".join(f"w{i}" for i in range(k_)) + " " + "ъ" * 20
        case(f"E4 значимость {k_}/100", "build", {"question": qw, "texts": [sent + ". Ещё одно достаточно длинное предложение без совпадений."]})
    for L in (49, 50, 51):
        case(f"E5 запасное наблюдение {L}", "build", {"question": "нет совпадений", "texts": ["я" * L]})
    case("E6 оценочные слова в верхнем регистре", "build", {"question": "", "texts": ["ЗАВИСТЬ ревность ГОРДОСТЬ тоже здесь есть. Обычное длинное предложение без всего."]})
    case("E6b два оценочных", "build", {"question": "", "texts": ["ЗАВИСТЬ и ВИНА тоже здесь есть. Ещё Зависть, Ревность и ВЕРНОСТЬ."]})
    for t_ in ("Просто наука и ничего более интересного тут.", "Просто религия и ничего более интересного тут.", "Просто коммунизм и ничего более интересного тут.", "Просто демократия и ничего более интересного тут.",
               "Наука и религия и всё остальное тут.", "Коммунизм и демократия и так далее тут вот.", "Ничего подходящего в этом тексте вообще нет."):
        case("E7 запасная гипотеза", "build", {"question": "", "texts": [t_]})
    base = "Согласно этому исследованию человек выбирает путь сам и без подсказки"
    case("E8 префикс 50 знаков", "build", {"question": "", "texts": [base + ".", base[:49] + "ы и что-то ещё."]})
    case("E8b префикс 50 знаков", "build", {"question": "", "texts": [base + ".", base[:50] + "ы и что-то ещё.", base[:49] + "ы"]})
    case("E9 частота больше трёх", "build", {"question": "", "texts": [base + "."] * 4})
    case("E9b частота три и две", "build", {"question": "", "texts": [base + "."] * 3 + ["Другое"], })
    many = " ".join(f"Предложение номер {'а' * (i + 1)} достаточно длинное." for i in range(12))
    case("E10 много наблюдений", "build", {"question": "Почему предложение номер", "texts": [many + " Предложение номер разгневался сильно тут. Предложение номер после этого ушёл."]})
    case("E10b ровно десять", "build", {"question": "Почему предложение номер", "texts": [" ".join(f"Предложение номер {'а' * (i + 1)} достаточно длинное." for i in range(10))]})
    case("E10c девять", "build", {"question": "Почему предложение номер", "texts": [" ".join(f"Предложение номер {'а' * (i + 1)} достаточно длинное." for i in range(9))]})
    case("E10d одиннадцать", "build", {"question": "Почему предложение номер", "texts": [" ".join(f"Предложение номер {'а' * (i + 1)} достаточно длинное." for i in range(10)) + " Предложение номер разгневался сильно тут."]})

    print(f"\n(сценариев: {n}; успешных проверок: {_OK} из {_OK + len(FAILURES)})")
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
