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

    # ================= извлечение личных фактов =================
    import pet.fact_extraction as fx

    def run_fx_py(message, responses, known):
        queue = list(responses)
        prompts = []

        def llm(msgs):
            prompts.append(msgs)
            r = queue.pop(0) if queue else ""
            if isinstance(r, str) and r.startswith("__raise__:"):
                raise type(r.split(":", 1)[1], (Exception,), {})()
            return r

        try:
            res = fx.extract_personal_facts(message, llm, known)
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}
        d = dataclasses.asdict(res)
        d["facts"] = [{"fact_class": f["fact_class"], "statement": f["statement"], "polarity": f["polarity"], "temporality": f["temporality"], "evidence": f["evidence"], "start": f["start"], "end": f["end"],
                       "relation": f["relation"], "target_fact_id": f["target_fact_id"]} for f in d["facts"]]
        return {"result": d, "prompts": prompts}

    def run_fx_rs(message, responses, known):
        r = json.loads(yandi_core.call(h, "fe_extract", json.dumps({"message": message, "responses": responses, "known": known}, ensure_ascii=False)))
        return r.get("ok") or {"error": r.get("error")}

    def fcase(label, message, responses, known=None):
        nonlocal n
        n += 1
        known = known or []
        p, r = run_fx_py(message, responses, known), run_fx_rs(message, responses, known)
        ok = ("error" in p and "error" in r) if ("error" in p or "error" in r) else norm(p) == norm(r)
        check(f"факты: {label}", ok, f"\n msg={message!r} resp={responses!r} known={known}\n py={norm(p)[:800]}\n rs={norm(r)[:800]}")

    fext = lambda facts, mq="none": json.dumps({"facts": facts, "memory_query": mq}, ensure_ascii=False)  # noqa: E731
    F1 = lambda span, statement="У пользователя есть кошка по имени Мурка", cls="possession", pol="affirmed", tm="current", stab="stable", **kw: {"span": span, "class": cls, "statement": statement, "polarity": pol, "time": tm, "stability": stab, **kw}  # noqa: E731
    blind = lambda frame="current", secret=False, pol="affirmed", stab="stable": json.dumps({"frame": frame, "secret": secret, "polarity": pol, "stability": stab})  # noqa: E731
    sup = lambda supported=True, adds=False: json.dumps({"supported": supported, "adds": adds})  # noqa: E731
    known2 = [{"fact_id": "pf_1", "statement": "У меня есть собака"}, {"fact_id": "pf_2", "statement": "Живу в Риге \"на окраине\"\nтихо"}]
    M = "У меня есть кошка Мурка, живу в Вильнюсе."

    fcase("пусто", "", [])
    fcase("длинное", " ".join(["слово"] * 121), [])
    fcase("сбой", M, ["__raise__:TimeoutError"])
    fcase("не JSON", M, ["нет"])
    fcase("нет facts", M, ['{"memory_query": "general"}'])
    fcase("факт принят", M, [fext([F1([2, 4])], "specific"), blind(), sup()])
    fcase("memory_query неизвестный", M, [fext([], "bogus")])
    fcase("memory_query нет", M, [fext([])])
    fcase("memory_query не строка", M, [fext([], 5)])
    fcase("знаки препинания вне доказательства", M, [fext([F1([5, 6], "Пользователь живёт в городе Вильнюс", cls="location")]), blind(), sup()])
    fcase("слишком много фактов", M, [fext([F1([2, 3])] * 5)])
    fcase("секрет по классу", M, [fext([F1([2, 4], cls="secret")])])
    fcase("неизвестный класс", M, [fext([F1([2, 4], cls="hobby")])])
    fcase("утверждение короткое", M, [fext([F1([2, 4], "Кошка")])])
    fcase("утверждение с переводом строки", M, [fext([F1([2, 4], "У пользователя есть\nкошка Мурка")])])
    fcase("утверждение 200 символов", M, [fext([F1([2, 4], "к" * 200)]), blind(), sup()])
    fcase("утверждение 201 символ", M, [fext([F1([2, 4], "к" * 201)])])
    fcase("утверждение 8 символов", M, [fext([F1([2, 4], "кошка ес")]), blind(), sup()])
    fcase("утверждение 7 символов", M, [fext([F1([2, 4], "кошка е")])])
    fcase("полярность неизвестна", M, [fext([F1([2, 4], pol="maybe")])])
    fcase("время неизвестно", M, [fext([F1([2, 4], tm="future")])])
    fcase("нестабильный", M, [fext([F1([2, 4], stab="temporary")])])
    fcase("relation неизвестен", M, [fext([F1([2, 4], relation="merge")])])
    fcase("relation null", M, [fext([F1([2, 4], relation=None)])])
    fcase("same с целью", M, [fext([F1([2, 4], relation="same", target=0)]), blind(), sup()], known2)
    fcase("same без цели → none", M, [fext([F1([2, 4], relation="same", target=9)]), blind(), sup()], known2)
    fcase("same цель булево", M, [fext([F1([2, 4], relation="same", target=True)]), blind(), sup()], known2)
    fcase("replaces без цели", M, [fext([F1([2, 4], relation="replaces", target=9)])], known2)
    fcase("replaces подтверждена связью", M, [fext([F1([2, 4], relation="replaces", target=1)]), blind(), sup(), '{"revises": true}'], known2)
    fcase("replaces подтверждена конфликтом", M, [fext([F1([2, 4], relation="replaces", target=1)]), blind(), sup(), '{"revises": false}', '{"conflict": true}'], known2)
    fcase("replaces не подтверждена", M, [fext([F1([2, 4], relation="replaces", target=1)]), blind(), sup(), '{"revises": false}', '{"conflict": false}'], known2)
    fcase("replaces: сбой проверки связи", M, [fext([F1([2, 4], relation="replaces", target=1)]), blind(), sup(), "__raise__:KeyError", '{"conflict": true}'], known2)
    fcase("replaces: revises не булево", M, [fext([F1([2, 4], relation="replaces", target=1)]), blind(), sup(), '{"revises": 1}', '{"conflict": "yes"}'], known2)
    fcase("слепая: не тот кадр", M, [fext([F1([2, 4])]), blind("past")])
    fcase("слепая: неизвестный кадр", M, [fext([F1([2, 4])]), blind("dream")])
    fcase("слепая: секрет", M, [fext([F1([2, 4])]), blind(secret=True)])
    fcase("слепая: секрет не булево", M, [fext([F1([2, 4])]), blind(secret=0)])
    fcase("слепая: полярность", M, [fext([F1([2, 4])]), blind(pol="negated")])
    fcase("слепая: устойчивость", M, [fext([F1([2, 4])]), blind(stab="temporary")])
    fcase("слепая: пустой объект", M, [fext([F1([2, 4])]), "{}"])
    fcase("слепая: сбой", M, [fext([F1([2, 4])]), "__raise__:OSError"])
    fcase("поддержка: не поддержано", M, [fext([F1([2, 4])]), blind(), sup(False)])
    fcase("поддержка: добавляет", M, [fext([F1([2, 4])]), blind(), sup(True, True)])
    fcase("поддержка: supported=1", M, [fext([F1([2, 4])]), blind(), '{"supported": 1, "adds": false}'])
    fcase("поддержка: сбой", M, [fext([F1([2, 4])]), blind(), "__raise__:ValueError"])
    fcase("секрет в доказательстве", "мой ключ sk-abcdefghijklmnopqrstuvwxyz0123456789 запомни", [fext([F1([2, 2], "Ключ пользователя такой-то")])])
    fcase("секрет в утверждении", M, [fext([F1([2, 4], "Ключ пользователя sk-abcdefghijklmnopqrstuvwxyz0123456789")])])
    fcase("пересечение", M, [fext([F1([2, 4]), F1([3, 5], "Пользователь живёт в городе Вильнюс")])])
    fcase("два факта", M, [fext([F1([2, 4]), F1([5, 6], "Пользователь живёт в городе Вильнюс", cls="location")]), blind(), sup(), blind(), sup()])
    fcase("дубль факта", "кошка Мурка и опять кошка Мурка", [fext([F1([0, 1]), F1([3, 4], "У ПОЛЬЗОВАТЕЛЯ ЕСТЬ КОШКА ПО ИМЕНИ МУРКА")]), blind(), sup(), blind(), sup()])
    fcase("известные факты: 31 штука", M, [fext([]) ], [{"fact_id": f"pf_{i}", "statement": f"факт номер {i}"} for i in range(31)])
    fcase("цель за пределами MAX_KNOWN", M, [fext([F1([2, 4], relation="same", target=30)]), blind(), sup()], [{"fact_id": f"pf_{i}", "statement": f"факт номер {i}"} for i in range(31)])
    fcase("юникод и эмодзи", "У меня 🌍 есть Ёжик… и кот!", [fext([F1([3, 5], "У пользователя есть ёжик и кот")]), blind(), sup()])
    fcase("доказательство из двух символов", "да ок...", [fext([F1([0, 0], "У пользователя есть что-то")])])

    Mlong = " ".join(["кошка"] * 40)
    fcase("31 слово отрезка", Mlong, [fext([F1([0, 30])]), blind(), sup()])
    fcase("30 слов отрезка", Mlong, [fext([F1([0, 29])]), blind(), sup()])
    fcase("отрезок из трёх чисел", M, [fext([F1([2, 4, 5])])])
    fcase("доказательство оканчивается многоточием", "У меня есть кошка Мурка…", [fext([F1([3, 4])]), blind(), sup()])
    fcase("соседние отрезки с общим словом", "У меня есть кошка Мурка и собака Шарик", [fext([F1([0, 2]), F1([2, 4], "Пользователь имеет кошку Мурку", cls="possession")]), blind(), sup(), blind(), sup()])
    fcase("соседние отрезки без общих слов", "У меня есть кошка Мурка и собака Шарик", [fext([F1([0, 2]), F1([3, 4], "Пользователь имеет кошку Мурку", cls="possession")]), blind(), sup(), blind(), sup()])

    fkinds = ["possession", "location", "secret", "hobby", None, 5]
    for i in range(250):
        m = rnd.choice(["У меня есть кошка Мурка", "Живу в Риге уже пять лет", "Раньше работал таксистом в Москве", "Мой пароль abc123def456ghi789jkl012mno345", "Как дела?", "Я люблю пельмени, а не суши.", "Моего брата зовут Иван"])
        nw = len(m.split())
        facts = []
        for _ in range(rnd.choice([0, 1, 1, 2, 3, 5])):
            a = rnd.randint(-1, nw)
            b = rnd.choice([a, a + 1, rnd.randint(-1, nw + 1)])
            f = F1(rnd.choice([[a, b], [a, b], [a], "x"]), rnd.choice(["У пользователя есть кошка Мурка", "Пользователь живёт в городе Рига", "короткое", "с\nпереводом строки внутри", "к" * 210, "Ключ abcdefghij1234567890KLMNOP"]),
                   cls=rnd.choice(fkinds), pol=rnd.choice(["affirmed", "negated", "x", None]), tm=rnd.choice(["current", "past", "future"]), stab=rnd.choice(["stable", "stable", "temporary"]),
                   relation=rnd.choice(["none", "same", "replaces", "bogus", None, "none"]), target=rnd.choice([0, 1, 5, None, True, "x"]))
            facts.append(f)
        resp = [rnd.choice([fext(facts, rnd.choice(["none", "general", "specific", "x"])), fext(facts), "мусор", "__raise__:OSError", '{"facts": 3}'])]
        for _ in range(8):
            resp.append(rnd.choice([blind(rnd.choice(["current", "past", "question", "x"]), rnd.choice([False, False, True, None]), rnd.choice(["affirmed", "negated"]), rnd.choice(["stable", "stable", "temporary"])), sup(rnd.choice([True, True, False]), rnd.choice([False, False, True])),
                                    '{"revises": true}', '{"conflict": true}', '{"revises": false}', '{"conflict": false}', "{}", "плохо", "__raise__:KeyError"]))
        fcase(f"случайные #{i}", m, resp, rnd.choice([[], known2, known2[:1]]))

    # ================= проверка наблюдаемого выполнения =================
    import pet.commitment_verification as cvm

    def cv_py(op, message, responses, cands=None, evidence="", spans=None, drop=False):
        queue = list(responses)
        prompts = []

        def llm(msgs):
            prompts.append(msgs)
            r = queue.pop(0) if queue else ""
            if isinstance(r, str) and r.startswith("__raise__:"):
                raise type(r.split(":", 1)[1], (Exception,), {})()
            return r

        try:
            if op == "classify":
                return {"out": cvm.classify_commitment(message, evidence, llm), "prompts": prompts}
            res = cvm.verify_direct_fulfilment(message, llm, cands or [])
            if drop:
                res = cvm.drop_if_reported(res, [tuple(x) for x in (spans or [])])
            d = dataclasses.asdict(res)
            d["verified"] = None if d["verified"] is None else {k: d["verified"][k] for k in ("commitment_id", "evidence", "start", "end")}
            return {"out": d, "prompts": prompts}
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def cv_rs(op, message, responses, cands=None, evidence="", spans=None, drop=False):
        a = {"op": op, "message": message, "responses": responses, "candidates": cands or [], "evidence": evidence, "event_spans": spans or [], "drop": drop}
        r = json.loads(yandi_core.call(h, "cv_run", json.dumps(a, ensure_ascii=False)))
        return r.get("ok") or {"error": r.get("error")}

    def ccase(label, op, message, responses, **kw):
        nonlocal n
        n += 1
        p, r = cv_py(op, message, responses, **kw), cv_rs(op, message, responses, **kw)
        ok = ("error" in p and "error" in r) if ("error" in p or "error" in r) else norm(p) == norm(r)
        check(f"проверка: {label}", ok, f"\n msg={message!r} resp={responses!r} kw={kw}\n py={norm(p)[:800]}\n rs={norm(r)[:800]}")

    dv = lambda span, target=0: json.dumps({"delivery": {"span": span, "target": target}})  # noqa: E731
    dl = lambda delivers=True, frame="current": json.dumps({"delivers": delivers, "frame": frame})  # noqa: E731
    C1 = [{"commitment_id": "c1", "evidence": "напишу кодовое слово"}]
    C2 = C1 + [{"commitment_id": "c2", "evidence": "пришлю число"}]
    MM = "Кодовое слово: облако."

    ccase("классификация: in_chat", "classify", MM, ['{"deliverable": "in_chat"}'], evidence="напишу кодовое слово")
    ccase("классификация: external", "classify", MM, ['{"deliverable": "external"}'], evidence="оплачу счёт")
    ccase("классификация: пусто", "classify", MM, ['{}'], evidence="x")
    ccase("классификация: без доказательства", "classify", MM, ['{"deliverable": "in_chat"}'], evidence="")
    ccase("классификация: сбой", "classify", MM, ["__raise__:OSError"], evidence="x")
    ccase("классификация: не JSON", "classify", MM, ["да"], evidence="x")
    ccase("классификация: deliverable не строка", "classify", MM, ['{"deliverable": ["in_chat"]}'], evidence="x")
    ccase("нет кандидатов", "verify", MM, [], cands=[])
    ccase("пустое сообщение", "verify", "  ", [], cands=C1)
    ccase("длинное сообщение", "verify", " ".join(["слово"] * 121), [], cands=C1)
    ccase("проверено", "verify", MM, [dv([2, 2]), dl()], cands=C1)
    ccase("нет delivery", "verify", MM, ['{"x": 1}'], cands=C1)
    ccase("delivery null", "verify", MM, ['{"delivery": null}'], cands=C1)
    ccase("delivery не объект", "verify", MM, ['{"delivery": [1, 2]}'], cands=C1)
    ccase("delivery строка", "verify", MM, ['{"delivery": "да"}'], cands=C1)
    ccase("span вне диапазона", "verify", MM, [dv([0, 9])], cands=C1)
    ccase("span не числа", "verify", MM, [dv(["a", 1])], cands=C1)
    ccase("span слишком длинный", "verify", " ".join(["слово"] * 40), [dv([0, 30])], cands=C1)
    ccase("span ровно 30", "verify", " ".join(["слово"] * 40), [dv([0, 29]), dl()], cands=C1)
    ccase("target вне диапазона", "verify", MM, [dv([2, 2], 3)], cands=C1)
    ccase("target булево", "verify", MM, ['{"delivery": {"span": [2, 2], "target": true}}'], cands=C1)
    ccase("target отрицательный", "verify", MM, [dv([2, 2], -1)], cands=C1)
    ccase("доказательство из знаков препинания", "verify", "Слово ...", [dv([1, 1])], cands=C1)
    ccase("в цитате", "verify", "Он сказал «кодовое слово облако» вчера", [dv([3, 4]), dl()], cands=C1)
    ccase("в незакрытой цитате", "verify", "Он сказал \"кодовое слово облако вчера", [dv([2, 3]), dl()], cands=C1)
    ccase("рядом с цитатой", "verify", "«Привет» кодовое слово облако", [dv([1, 3]), dl()], cands=C1)
    ccase("слепая: не доставка", "verify", MM, [dv([2, 2]), dl(False)], cands=C1)
    ccase("слепая: другой кадр", "verify", MM, [dv([2, 2]), dl(True, "quotation")], cands=C1)
    ccase("слепая: неизвестный кадр", "verify", MM, [dv([2, 2]), dl(True, "dream")], cands=C1)
    ccase("слепая: delivers не булево", "verify", MM, [dv([2, 2]), '{"delivers": 1, "frame": "current"}'], cands=C1)
    ccase("слепая: пусто", "verify", MM, [dv([2, 2]), "{}"], cands=C1)
    ccase("слепая: сбой", "verify", MM, [dv([2, 2]), "__raise__:KeyError"], cands=C1)
    ccase("два обещания: подходит названное", "verify", MM, [dv([2, 2], 1), dl(False), dl(True)], cands=C2)
    ccase("два обещания: подходят оба", "verify", MM, [dv([2, 2], 0), dl(True), dl(True)], cands=C2)
    ccase("два обещания: подходит другое", "verify", MM, [dv([2, 2], 0), dl(False), dl(True)], cands=C2)
    ccase("два обещания: не подходит ни одно", "verify", MM, [dv([2, 2], 0), dl(False), dl(False)], cands=C2)
    ccase("два обещания: сбой второго", "verify", MM, [dv([2, 2], 0), dl(True), "__raise__:OSError"], cands=C2)
    ccase("семь обещаний берутся последние пять", "verify", MM, [dv([2, 2], 4)] + [dl(True) if i == 4 else dl(False) for i in range(5)], cands=[{"commitment_id": f"c{i}", "evidence": f"обещание {i}"} for i in range(7)])
    ccase("отбрасывание: внутри заявления", "verify", MM, [dv([2, 2]), dl()], cands=C1, spans=[["fulfilment_claim", 0, 30]], drop=True)
    ccase("отбрасывание: внутри обещания", "verify", MM, [dv([2, 2]), dl()], cands=C1, spans=[["promise", 5, 12]], drop=True)
    ccase("отбрасывание: касается границей", "verify", MM, [dv([2, 2]), dl()], cands=C1, spans=[["promise", 0, 15]], drop=True)
    ccase("отбрасывание: касается границей справа", "verify", MM, [dv([2, 2]), dl()], cands=C1, spans=[["promise", 21, 30]], drop=True)
    ccase("многоточие в конце доставки", "verify", "Кодовое слово: облако…", [dv([2, 2]), dl()], cands=C1)
    ccase("отбрасывание: другой тип", "verify", MM, [dv([2, 2]), dl()], cands=C1, spans=[["insult", 0, 30]], drop=True)
    ccase("отбрасывание: без проверки", "verify", MM, ['{"delivery": null}'], cands=C1, spans=[["promise", 0, 30]], drop=True)
    for i in range(200):
        m = rnd.choice(["Кодовое слово: облако.", "Вот число 42, как обещал", "Он написал «привет мир» и ушёл", "Пароль был \"abc\" вчера", "Готово!", "Просто сообщение без доставки"])
        nw = len(m.split())
        d = rnd.choice([dv([rnd.randint(-1, nw), rnd.randint(-1, nw + 1)], rnd.choice([0, 1, 2, -1, None])), '{"delivery": null}', "{}", "мусор", "__raise__:OSError", '{"delivery": {"span": [0, 0], "target": 0}}'])
        rs_ = [d] + [rnd.choice([dl(rnd.choice([True, False]), rnd.choice(["current", "quotation", "report", "x"])), "{}", "плохо", "__raise__:KeyError"]) for _ in range(4)]
        ccase(f"случайная проверка #{i}", "verify", m, rs_, cands=rnd.choice([C1, C2, [], C2 + C2]), spans=rnd.choice([[], [["promise", 0, 5]], [["fulfilment_claim", 3, 12]], [["insult", 0, 40]]]), drop=rnd.choice([False, True]))

    print(f"\n(сценариев: {n}; успешных проверок: {_OK} из {_OK + len(FAILURES)})")
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
