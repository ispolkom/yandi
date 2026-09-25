"""Распознавание событий отношений на Rust (rustlib/yandi_core/src/event_extraction.rs) против pet/event_extraction.py: тексты промптов, порядок вызовов модели, отбор кандидатов,
слепая проверка, правило «обещание — только единственный кандидат», перенос в IntensityResult. Модель заменена сценарием ответов (детерминированно, одинаково для обеих сторон).
Не требует MySQL."""
from __future__ import annotations

import dataclasses
import json
import random
import sys
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


def main() -> int:
    try:
        import yandi_core
    except ImportError as e:
        print(f"SKIP: yandi_core не собран ({e})")
        return 0
    import pet.event_extraction as ee

    h = yandi_core.open_memory()
    rnd = random.Random(20260926)

    def run_py(message, responses):
        queue = list(responses)
        prompts = []

        def llm(msgs):
            prompts.append(msgs)
            r = queue.pop(0) if queue else ""
            if isinstance(r, str) and r.startswith("__raise__:"):
                raise type(r.split(":", 1)[1], (Exception,), {})()
            return r

        try:
            res = ee.extract_relational_events(message, llm)
            inten = ee.to_intensity(res)
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}
        d = dataclasses.asdict(res)
        d["events"] = [{"type": e["type"], "evidence": e["evidence"], "start": e["start"], "end": e["end"], "severity": e["severity"], "sincerity": e["sincerity"]} for e in d["events"]]
        d["judged"] = [list(j) for j in d["judged"]]
        i = dataclasses.asdict(inten)
        i["spans"] = [list(s) for s in i["spans"]]
        return {"result": d, "intensity": i, "prompts": prompts}

    def run_rs(message, responses):
        r = json.loads(yandi_core.call(h, "ee_extract", json.dumps({"message": message, "responses": responses}, ensure_ascii=False)))
        return r.get("ok") or {"error": r.get("error")}

    def norm(v):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)

    n = 0

    def case(label, message, responses):
        nonlocal n
        n += 1
        p, r = run_py(message, responses), run_rs(message, responses)
        if "error" in p or "error" in r:
            ok = "error" in p and "error" in r
        else:
            ok = norm(p) == norm(r)
        check(label, ok, f"\n msg={message!r} resp={responses!r}\n py={norm(p)[:700]}\n rs={norm(r)[:700]}")

    ext = lambda evs: json.dumps({"events": evs}, ensure_ascii=False)  # noqa: E731
    chk = lambda act, frame="current": json.dumps({"act": act, "frame": frame})  # noqa: E731

    # ---- целевые сценарии ----
    case("пустое сообщение", "", [])
    case("только пробелы", "  \n\t ", [])
    case("слишком длинное", " ".join(["слово"] * 121), [ext([])])
    case("ровно на пределе слов", " ".join(["слово"] * 120), [ext([])])
    case("нет ответа модели", "Прости меня", [])
    case("сбой транспорта", "Прости меня", ["__raise__:TimeoutError"])
    case("не JSON", "Прости меня", ["привет"])
    case("JSON не объект", "Прости меня", ["[1, 2]"])
    case("нет events", "Прости меня", ['{"x": 1}'])
    case("events не список", "Прости меня", ['{"events": 5}'])
    case("пустой список", "Привет как дела", [ext([])])
    case("извинение принято", "Прости меня, я был неправ", [ext([{"span": [0, 4], "type": "apology", "sincerity": 0.8}]), chk("apology")])
    case("извинение: слепая проверка не согласна", "Прости меня, я был неправ", [ext([{"span": [0, 4], "type": "apology", "sincerity": 0.8}]), chk("insult")])
    case("извинение: цитата", "Он сказал: прости меня", [ext([{"span": [2, 3], "type": "apology", "sincerity": 0.8}]), chk("apology", "quotation")])
    case("оскорбление принято", "Ты тупая железка", [ext([{"span": [1, 2], "type": "insult", "severity": 0.7}]), chk("insult")])
    case("оскорбление без тяжести", "Ты тупая железка", [ext([{"span": [1, 2], "type": "insult"}])])
    case("оскорбление: тяжесть вне 0..1", "Ты тупая железка", [ext([{"span": [1, 2], "type": "insult", "severity": 1.5}])])
    case("оскорбление: тяжесть булево", "Ты тупая железка", [ext([{"span": [1, 2], "type": "insult", "severity": True}])])
    case("оскорбление: тяжесть строка", "Ты тупая железка", [ext([{"span": [1, 2], "type": "insult", "severity": "0.5"}])])
    case("оскорбление: тяжесть целая 1", "Ты тупая железка", [ext([{"span": [1, 2], "type": "insult", "severity": 1}]), chk("insult")])
    case("извинение без искренности", "Прости", [ext([{"span": [0, 0], "type": "apology"}])])
    case("отрезок из булевых", "Прости меня", [ext([{"span": [True, 1], "type": "apology", "sincerity": 0.5}])])
    case("отрезок из float", "Прости меня", [ext([{"span": [0.0, 1.0], "type": "apology", "sincerity": 0.5}])])
    case("отрезок из трёх", "Прости меня", [ext([{"span": [0, 1, 2], "type": "apology", "sincerity": 0.5}])])
    case("отрезок вне диапазона", "Прости меня", [ext([{"span": [0, 5], "type": "apology", "sincerity": 0.5}])])
    case("отрезок обратный", "Прости меня", [ext([{"span": [1, 0], "type": "apology", "sincerity": 0.5}])])
    case("отрезок отрицательный", "Прости меня", [ext([{"span": [-1, 1], "type": "apology", "sincerity": 0.5}])])
    case("слишком длинный отрезок", " ".join(["прости"] * 40), [ext([{"span": [0, 34], "type": "apology", "sincerity": 0.5}])])
    case("ровно допустимый отрезок", " ".join(["прости"] * 40), [ext([{"span": [0, 29], "type": "apology", "sincerity": 0.5}]), chk("apology")])
    case("неизвестный тип", "Прости меня", [ext([{"span": [0, 1], "type": "flirt"}])])
    case("тип не строка", "Прости меня", [ext([{"span": [0, 1], "type": ["apology"]}])])
    case("кандидат не объект", "Прости меня", [ext(["x", 5, None])])
    case("слишком много событий", "Прости меня, ты тупая", [ext([{"span": [0, 0], "type": "apology", "sincerity": 0.5}] * 4)])
    case("пересекающиеся отрезки", "Прости меня, ты тупая", [ext([{"span": [0, 2], "type": "apology", "sincerity": 0.5}, {"span": [2, 3], "type": "insult", "severity": 0.5}])])
    case("два события подряд", "Прости меня, но ты тупая железка", [ext([{"span": [0, 1], "type": "apology", "sincerity": 0.5}, {"span": [3, 5], "type": "insult", "severity": 0.5}]), chk("apology"), chk("insult")])
    case("слишком короткое доказательство", "я ок", [ext([{"span": [0, 0], "type": "apology", "sincerity": 0.5}])])
    case("обещание единственное", "Завтра обязательно принесу чертёж", [ext([{"span": [0, 3], "type": "promise"}]), chk("promise")])
    case("обещание вместе с извинением отбрасывается", "Прости, завтра принесу чертёж", [ext([{"span": [0, 0], "type": "apology", "sincerity": 0.9}, {"span": [1, 3], "type": "promise"}]), chk("apology"), chk("promise")])
    case("заявление о выполнении", "Всё готово, принёс", [ext([{"span": [0, 3], "type": "fulfilment_claim"}]), chk("fulfilment_claim")])
    case("обещание и заявление вместе", "Обещаю и уже сделал", [ext([{"span": [0, 1], "type": "promise"}, {"span": [2, 4], "type": "fulfilment_claim"}]), chk("promise"), chk("fulfilment_claim")])
    case("вердикт: пустой объект", "Прости меня", [ext([{"span": [0, 1], "type": "apology", "sincerity": 0.5}]), "{}"])
    case("вердикт: не JSON", "Прости меня", [ext([{"span": [0, 1], "type": "apology", "sincerity": 0.5}]), "нет"])
    case("вердикт: сбой", "Прости меня", [ext([{"span": [0, 1], "type": "apology", "sincerity": 0.5}]), "__raise__:ValueError"])
    case("вердикт: act не строка", "Прости меня", [ext([{"span": [0, 1], "type": "apology", "sincerity": 0.5}]), '{"act": ["apology"], "frame": "current"}'])
    case("вердикт: без frame", "Прости меня", [ext([{"span": [0, 1], "type": "apology", "sincerity": 0.5}]), '{"act": "apology"}'])
    case("блок кода вокруг JSON", "Прости меня", ["```json\n" + ext([{"span": [0, 1], "type": "apology", "sincerity": 0.5}]) + "\n```", "```\n" + chk("apology") + "\n```"])
    case("текст вокруг JSON", "Прости меня", ["Вот: " + ext([])])
    case("NaN в ответе", "Прости меня", ['{"events": [{"span": [0, 1], "type": "apology", "sincerity": NaN}]}'])
    case("большое целое", "Прости меня", ['{"events": [{"span": [0, 99999999999999999999], "type": "apology", "sincerity": 0.5}]}'])
    case("дубль ключей", "Прости меня", ['{"events": [], "events": [{"span": [0, 1], "type": "apology", "sincerity": 0.5}]}', chk("apology")])
    case("юникод и эмодзи в сообщении", "Прости 🌍 меня, «дорогая» — Ёжик", [ext([{"span": [0, 2], "type": "apology", "sincerity": 0.5}]), chk("apology")])
    case("многострочное сообщение", "Первая строка\nПрости меня\n\nТретья", [ext([{"span": [2, 3], "type": "apology", "sincerity": 0.5}]), chk("apology")])
    case("неразрывный пробел", "Прости меня тебя", [ext([{"span": [0, 1], "type": "apology", "sincerity": 0.5}]), chk("apology")])

    case("ровно 31 слово отрезка", " ".join(["прости"] * 40), [ext([{"span": [0, 30], "type": "apology", "sincerity": 0.5}])])
    case("доказательство из двух символов", "да ок", [ext([{"span": [0, 0], "type": "apology", "sincerity": 0.5}])])
    case("доказательство из трёх символов", "нет ок", [ext([{"span": [0, 0], "type": "apology", "sincerity": 0.5}]), chk("apology")])
    # to_intensity напрямую: события могли бы прийти иначе, чем из extract (защитные ветви)
    E = lambda t, ev="доказательство", s=0, e=5, sev=0.0, sin=0.0: {"type": t, "evidence": ev, "start": s, "end": e, "severity": sev, "sincerity": sin}  # noqa: E731
    for label, evs in (("пусто", []), ("обещание", [E("promise")]), ("заявление", [E("fulfilment_claim")]), ("обещание+заявление", [E("promise"), E("fulfilment_claim")]),
                       ("два обещания", [E("promise"), E("promise", "другое", 6, 9)]), ("обещание+извинение", [E("promise"), E("apology", sin=0.7)]), ("извинение+оскорбление", [E("apology", sin=0.7), E("insult", sev=0.4)]),
                       ("оскорбление", [E("insult", sev=0.9)]), ("оскорбление+заявление", [E("insult", sev=0.9), E("fulfilment_claim")])):
        n += 1
        pe = [ee.ExtractedEvent(e["type"], e["evidence"], e["start"], e["end"], e["severity"], e["sincerity"]) for e in evs]
        res = ee.ExtractionResult(events=pe)
        pi = dataclasses.asdict(ee.to_intensity(res))
        pi["spans"] = [list(s_) for s_ in pi["spans"]]
        ri = json.loads(yandi_core.call(h, "ee_to_intensity", json.dumps({"events": evs}, ensure_ascii=False)))["ok"]
        check(f"to_intensity: {label}", norm(pi) == norm(ri), f"\n py={norm(pi)}\n rs={norm(ri)}")

    # ---- случайные сценарии ----
    msgs = ["Прости меня пожалуйста", "Ты полная дура и тупица", "Завтра приду и всё исправлю", "Уже сделал, как договаривались", "Обычное сообщение без событий про погоду",
            "Он назвал меня дураком, представляешь", "Извини, что вчера обозвал тебя, я был не прав, обещаю больше не буду", "Ок", "🌍 привет 🌍", "Я не оскорбляю тебя"]
    kinds = ["apology", "insult", "promise", "fulfilment_claim", "flirt", None, 5]
    for i in range(300):
        m = rnd.choice(msgs)
        nw = len(m.split())
        events = []
        for _ in range(rnd.choice([0, 1, 1, 1, 2, 3, 4])):
            a = rnd.randint(-1, nw)
            b = rnd.choice([a, a + 1, a + 2, rnd.randint(-1, nw + 1)])
            ev = {"span": rnd.choice([[a, b], [a, b], [a, b], [a], "x", [a, b, 0]]), "type": rnd.choice(kinds)}
            if rnd.random() < 0.7:
                ev["severity"] = rnd.choice([0.0, 0.5, 1, 1.5, -0.1, True, "0.5", None])
            if rnd.random() < 0.7:
                ev["sincerity"] = rnd.choice([0.0, 0.5, 1, 1.5, -0.1, True, "0.5", None])
            events.append(ev)
        first = rnd.choice([ext(events), ext(events), "```json\n" + ext(events) + "\n```", "мусор", '{"events": "x"}', "__raise__:OSError"])
        resp = [first]
        for _ in range(4):
            resp.append(rnd.choice([chk(rnd.choice(["apology", "insult", "promise", "fulfilment_claim", "none"]), rnd.choice(["current", "current", "quotation", "hypothetical", "negated"])),
                                    "{}", "плохо", '{"act": "apology"}', "__raise__:KeyError", "[]"]))
        case(f"случайный #{i}", m, resp)

    print(f"\n(сценариев: {n}; успешных проверок: {_OK} из {_OK + len(FAILURES)})")
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
