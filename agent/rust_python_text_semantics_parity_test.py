"""
agent/rust_python_text_semantics_parity_test.py — сквозная проверка «питоновской» текстовой семантики
во ВСЕХ перенесённых на Rust модулях (rustlib/yandi_rs/src/py_text.rs).

    ЭТО НЕ ТЕСТ ОДНОГО МОДУЛЯ. Это страховка от ЦЕЛОГО КЛАССА ошибок переноса: стандартные средства
    Rust (trim / split_whitespace / `\\s` в крейте regex / parse::<f64>) ведут себя иначе, чем Python
    (str.strip / str.split / `\\s` в re / float), на узком наборе символов и форматов:
      * Python-пробелы включают U+001C..U+001F (разделители FS/GS/RS/US) — Rust White_Space нет;
      * Python float() принимает `1_000` и любые десятичные цифры Unicode — Rust только ASCII без `_`.
    Найдено при подготовке 16-го среза на ЖИВОМ коде: canonicalize_claim_text("a\\x1cb") давал разные
    ответы в Python и Rust (срез 3, все его прежние проверки были зелёными — они не пробовали эти символы).

Проверяется: (A) подпорка py_text по ВСЕМ кодовым точкам и фаззингом float; (B) КАЖДЫЙ ранее
перенесённый модуль на строках с разделителями U+001C..U+001F во всех позициях (внутри, по краям,
вперемешку с обычными пробелами) — Python-по-умолчанию против Rust-функции напрямую.

Требует собранного нативного модуля — если не установлен, SKIP.

Run: python -m agent.rust_python_text_semantics_parity_test
"""
from __future__ import annotations

import dataclasses
import os
import random
import sys
from enum import Enum
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


def norm(x):
    """Приводит результат к сравнимому виду (dataclass/Enum/tuple -> простые типы; NaN -> строка)."""
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return norm(dataclasses.asdict(x))
    if isinstance(x, Enum):
        return x.value
    if isinstance(x, dict):
        return {k: norm(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [norm(v) for v in x]
    if isinstance(x, float) and x != x:
        return "NaN"
    return x


SEPS = ["\x1c", "\x1d", "\x1e", "\x1f"]


def variants(text: str) -> list[str]:
    """text с разделителями U+001C..U+001F: вместо пробелов, по краям, вперемешку с обычным пробелом."""
    out = [text]
    for sep in SEPS:
        out.append(text.replace(" ", sep))
        out.append(sep + text + sep)
        out.append(text.replace(" ", " " + sep))
        out.append(text.replace(" ", sep + " "))
        out.append(text + sep + sep)
    return out


def main() -> int:
    try:
        import yandi_rs.py_text as pt
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран/не установлен ({e}) — см. rustlib/README.md")
        return 0

    for k in list(os.environ):
        if k.startswith("YANDI_") and k.endswith("_ENGINE"):
            del os.environ[k]          # Python-версии — по умолчанию

    # ── A1: is_space по ВСЕМ кодовым точкам ──
    bad = [cp for cp in range(0x110000) if chr(cp).isspace() != pt.is_space(cp)]
    check("A1 is_space совпадает с str.isspace() на всех кодовых точках", not bad, f"{[hex(c) for c in bad[:8]]}")

    # ── A1b: регистр и нормализация Unicode по ВСЕМ кодовым точкам (версия Unicode у Rust новее, чем у Python 3.11) ──
    import unicodedata
    bad_l, bad_c, bad_n = [], [], []
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        for t in (ch, "aΣ" + ch, ch + "Σa", "1" + ch + "Σ" + ch + "1"):
            if pt.lower(t) != t.lower():
                bad_l.append(cp)
        if pt.casefold(ch) != ch.casefold():
            bad_c.append(cp)
        for t in (ch, "a" + ch + "\u0301", ch + ch, "\u0301" + ch + "\u0323", "\U000113b8" + ch, ch + "\U000113b8", "\u1161" + ch + "\u11a8"):
            if pt.nfc(t) != unicodedata.normalize("NFC", t) or pt.nfkc(t) != unicodedata.normalize("NFKC", t):
                bad_n.append(cp)
    check("A1b py_lower совпадает с str.lower() на всех кодовых точках (включая финальную сигму)", not bad_l, f"{[hex(c) for c in bad_l[:8]]}")
    check("A1c py_casefold совпадает с str.casefold() на всех кодовых точках", not bad_c, f"{[hex(c) for c in bad_c[:8]]}")
    check("A1d py_nfc/py_nfkc совпадают с unicodedata.normalize на всех кодовых точках", not bad_n, f"{[hex(c) for c in bad_n[:8]]}")
    rng0 = random.Random(7)
    pool0 = ["Σ", "σ", "ς", "a", "A", "1", " ", "\u0301", "\u00b7", "'", ".", "Ω", "я", "Я", "İ", "\u02b0", "\u2019", "ǅ", "\U0001d400", "\u200b", "-",
             "e", "\u0323", "\u1100", "\u1161", "\u11a8", "\uac00", "\U000113c2", "\U000113b8", "\ufb01", "\u212b", "\u0344", "\ua7cb", "\u1c89"]
    bad_f = []
    for _ in range(150000):
        t = "".join(rng0.choice(pool0) for _ in range(rng0.randrange(1, 9)))
        if (pt.lower(t) != t.lower() or pt.nfc(t) != unicodedata.normalize("NFC", t) or pt.nfkc(t) != unicodedata.normalize("NFKC", t)
                or pt.casefold(t) != t.casefold()):
            bad_f.append(t)
    check("A1e фаззинг lower/casefold/nfc/nfkc на смесях (сигма, комбинирующие, хангыль, символы новее Unicode 14)", not bad_f, f"{bad_f[:3]!r}")

    # ── A2: strip / split ──
    rng = random.Random(20260924)
    pool = [" ", "\t", "\n", "\x0b", "\x1c", "\x1d", "\x1e", "\x1f", "\x85", "\xa0", "　", "​", "﻿",
            "a", "б", "1", "_", " "]
    for _ in range(3000):
        s = "".join(rng.choice(pool) for _ in range(rng.randint(0, 14)))
        check(f"A2 strip {s!r}", s.strip() == pt.strip(s))
        check(f"A2 split {s!r}", s.split() == pt.split(s))

    # ── A3: float() ──
    fixed = ["0.5", " 0.5 ", "1_000", "1__0", "_1", "1_", "1_0.0_1e1_0", "١٢٣", "१२३.५", "３.１４", "\x1c0.25\x1f",
             "", " ", "abc", "nan", "-inf", "+Infinity", "iNf", "1e5", "1E-5", ".5", "5.", "+.5e+3", "1e", "e5",
             "0x10", "1 0", "1_e5", "1e_5", "1.5_5", "1._5", "in_f", "--1", "+-1", "١_٢", "1 ", "  7  "]
    alphabet = list("0123456789_.eE+- ") + ["\x1c", "١", "٣", "i", "n", "f", "a", "N"]
    fuzz = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 9))) for _ in range(6000)]
    for s in fixed + fuzz:
        try:
            py_v = float(s)
        except ValueError:
            py_v = None
        rs_v = pt.float(s)
        same = (py_v is None and rs_v is None) or (py_v is not None and rs_v is not None and
                                                   (py_v == rs_v or (py_v != py_v and rs_v != rs_v)))
        check(f"A3 float({s!r})", same, f"python={py_v!r} rust={rs_v!r}")

    # ── A4: repr(str) по ВСЕМ кодовым точкам (кроме суррогатов, их в Rust-строке быть не может) ──
    bad = [cp for cp in range(0x110000) if not (0xD800 <= cp <= 0xDFFF) and repr(chr(cp)) != pt.repr_str(chr(cp))]
    check("A4 repr_str совпадает с repr() на всех кодовых точках", not bad, f"{[hex(c) for c in bad[:8]]}")
    for s in ["it's", 'say "hi"', "it's \"both\"", "\\", "a\nb\tc\r", "\x00\x1f\x7f\x80\xa0\xad", "\u200b\u2028\ufeff", "😀", "\U000e0001"]:
        check(f"A4 repr {s!r}", repr(s) == pt.repr_str(s))

    # ── A5: json.loads — значения И точные тексты ошибок (дифференциальный фаззинг) ──
    import json

    def jnorm(x):
        if isinstance(x, str):
            # ЗАДОКУМЕНТИРОВАННОЕ отличие: одиночный суррогат Python-строка хранит, Rust String — нет (-> U+FFFD)
            return "".join("\ufffd" if 0xD800 <= ord(c) <= 0xDFFF else c for c in x)
        if isinstance(x, bool) or x is None:
            return x
        if isinstance(x, (int, float)):
            f = float(x) if not isinstance(x, float) or x == x else x
            return "NaN" if f != f else (0.0 if f == 0 else f)
        if isinstance(x, list):
            return [jnorm(v) for v in x]
        if isinstance(x, dict):
            return {k: jnorm(v) for k, v in x.items()}
        raise TypeError(type(x))

    def py_loads(s):
        try:
            return True, jnorm(json.loads(s))
        except json.JSONDecodeError as e:
            return False, str(e)
        except RecursionError:
            return None, "recursion"

    def rs_loads(s):
        ok, v = pt.json_loads(s)
        return (ok, jnorm(v) if ok else v)

    def rand_json(depth=0):
        r = rng.random()
        if depth > 3 or r < 0.35:
            return rng.choice([None, True, False, 0, -0, 1, -1, 12345, 0.5, -2.5e10, 1e300, "", "a", "привет", "q\"uo\\te", "\n\t", "\u00e9😀"])
        if r < 0.65:
            return [rand_json(depth + 1) for _ in range(rng.randint(0, 4))]
        return {rng.choice(["a", "b", "severity", "ключ", ""]): rand_json(depth + 1) for _ in range(rng.randint(0, 4))}

    corpus = []
    for _ in range(1500):
        doc = rand_json()
        for kw in ({}, {"ensure_ascii": False}, {"indent": 2}, {"separators": (",", ":")}):
            corpus.append(json.dumps(doc, **kw))
        corpus.append(json.dumps(doc, allow_nan=True))
    special = ['NaN', 'Infinity', '-Infinity', '[NaN, Infinity, -Infinity]', '{"a": NaN}', '1e999', '-1e999', '1E5', '1e-5', '-0', '-0.0',
               '0e0', '01', '1.', '.5', '1.e5', '-', '--1', '+1', '1e', '1e+', '"\\ud800"', '"\\ud83d\\ude00"', '"\\ud83dx"', '"\\ude00"',
               '"\\u12"', '"\\u12x4"', '"\\x"', '"a\nb"', '"a\x01b"', '"abc', '"\\', '"\\u', '"\\u0041', '{"a"', '{"a":', '{"a":1', '{"a":1,', '{',
               '[', '[1', '[1,', '[1 2]', '{"a" 1}', "{'a':1}", '{"a":1,}', '[1,]', '', ' ', 'nul', 'tru', 'fals', 'nulll', 'true false', '{"a":1} x',
               '\ufeff{}', '{"a":1}\x1c', '\x1c{"a":1}', '{"a":\x1c1}', '{\n"a":\n1\n}', '{\n"a":\n1\n]', '[\n\n1,\n\n2 3]', '{"a":1,"a":2}',
               '[' * 600 + ']' * 600, '[' * 100000]
    corpus += special
    alphabet = list('{}[]:,"\\ \n\t\r0123456789.eE+-truefalsn NaInfity') + ["\x1c", "\ud7ff"[:0] or "x", "é"]
    mutated = []
    for base in rng.sample(corpus[:6000], 2500):
        s = list(base)
        for _ in range(rng.randint(1, 3)):
            op = rng.random()
            if s and op < 0.35:
                del s[rng.randrange(len(s))]
            elif op < 0.7:
                s.insert(rng.randint(0, len(s)), rng.choice(alphabet))
            elif s:
                s[rng.randrange(len(s))] = rng.choice(alphabet)
        mutated.append("".join(s))
    n_json = 0
    for s in corpus + mutated:
        n_json += 1
        p, r = py_loads(s), rs_loads(s)
        check_quiet_ok = (p == r)
        check(f"A5 json {s[:60]!r}", check_quiet_ok, f"python={str(p)[:200]} rust={str(r)[:200]}") if not check_quiet_ok else None
    global _OK
    _OK += n_json - sum(1 for f in FAILURES if f.startswith("A5"))

    # ── B: каждый перенесённый модуль на строках с U+001C..U+001F ──
    import agent.boundaries as bd
    import agent.claim_answer_linker as cal
    import agent.claim_evidence_retriever as cer
    import agent.claim_identity as ci
    import agent.claim_semantic_identity_hardening as hg
    import agent.message_intensity as mi
    import agent.source_clustering as sc
    import agent.source_independence_prototype as sip
    import agent.source_quality as sq
    import pet.local_guard as lg
    from agent.claim_validator import ClaimValidator
    from agent.criticism_detector import CriticismDetector
    import yandi_rs.boundaries as r_bd
    import yandi_rs.claim_answer_linker as r_cal
    import yandi_rs.claim_evidence_retriever as r_cer
    import yandi_rs.claim_identity as r_ci
    import yandi_rs.claim_semantic_identity_hardening as r_hg
    import yandi_rs.criticism_detector as r_cd
    import yandi_rs.claim_validator as r_cv
    import yandi_rs.local_guard as r_lg
    import yandi_rs.message_intensity as r_mi
    import yandi_rs.source_clustering as r_sc
    import yandi_rs.source_quality as r_sq

    claims = [
        "Разумная жизнь на Юпитере не была обнаружена.",
        "Нет доказательств существования жизни  ",
        "  - Юпитер — крупнейшая планета Солнечной системы.",
        "1. **Жирный** текст про Марс и его спутники",
        "По имеющимся данным, ответ на вопрос является неполным.",
        "Юпитер является крупнейшей планетой. Марс — четвёртая планета от Солнца! Земля третья",
        "Ни один аппарат не обнаружил признаков жизни на Юпитере",
        "НАТО и ЕС обсудили вопрос об Юпитере",
        "Температура на Юпитере не превышает -145°C",
        "Hello world. This is a test, isn't it?",
    ]
    queries = ["Есть ли разумная жизнь на Юпитере?", "Существует ли  бозон Хиггса?", "Расскажи о Юпитере"]
    n = 0

    def cmp(label, py_f, rs_f, *args):
        nonlocal n
        n += 1
        try:
            p = norm(py_f(*args))
        except Exception as e:                         # noqa: BLE001
            p = f"EXC:{type(e).__name__}"
        try:
            r = norm(rs_f(*args))
        except Exception as e:                         # noqa: BLE001
            r = f"EXC:{type(e).__name__}"
        check(f"B {label} {args!r}"[:200], p == r, f"python={p!r} rust={r!r}")

    validator = ClaimValidator()
    detector = CriticismDetector()
    for base in claims:
        for t in variants(base):
            cmp("claim_identity.canonicalize", ci.canonicalize_claim_text, r_ci.canonicalize_claim_text, t)
            cmp("claim_identity.hash", ci.compute_claim_content_hash, r_ci.compute_claim_content_hash, t)
            cmp("claim_identity.subject_anchors", ci.extract_subject_anchors, r_ci.extract_subject_anchors, t)
            cmp("claim_identity.content_anchors", ci.extract_content_anchors, r_ci.extract_content_anchors, t)
            cmp("claim_validator.normalize", ClaimValidator.normalize_claim_text, r_cv.normalize_claim_text, t)
            cmp("claim_validator.validate", validator.validate, lambda x: tuple(r_cv.validate(x)), t)
            cmp("hardening_guard", hg.hardening_guard, r_hg.hardening_guard, t, base)
            cmp("hardening_guard(rev)", hg.hardening_guard, r_hg.hardening_guard, base, t)
            cmp("criticism.analyze", lambda x: detector.analyze(x, {"trust": 40, "history_insults": 1}),
                lambda x: dict(r_cd.analyze(x, {"trust": 40, "history_insults": 1})), t)
            cmp("boundaries.toxicity", bd.detect_toxicity, lambda x: {**dict(r_bd.detect_toxicity(x)), "words": list(dict(r_bd.detect_toxicity(x))["words"])}, t)
            cmp("boundaries.apology", bd.is_apology, lambda x: tuple(r_bd.is_apology(x)), t)
            cmp("evidence.is_absence", cer._is_absence_claim, r_cer.is_absence_claim, t)
            for q in queries:
                for qv in variants(q):
                    cmp("evidence.classify_role", cer._classify_claim_role, lambda a, b: dict(r_cer.classify_claim_role(a, b)), t, qv)
                    break
            cmp("source_clustering.title", sip.title_similarity, r_sc.title_similarity, t, base)
            cmp("source_clustering.content", sip.content_fingerprint_similarity, r_sc.content_fingerprint_similarity, t, base)
            cmp("source_quality", sq.evaluate_source_quality, lambda u, ti, te, st: r_sq.evaluate_source_quality(u, ti, te, st),
                "https://example.com/x", t, t, "web")
    for q in queries:
        for qv in variants(q):
            cmp("evidence.is_existence_question", cer._is_existence_question, r_cer.is_existence_question, qv)
            cmp("evidence.extract_target", cer._extract_existence_target, r_cer.extract_existence_target, qv)

    # claim_answer_linker
    linker = cal.ClaimAnswerLinker()
    ans = "Юпитер является крупнейшей планетой Солнечной системы по объёму и массе. Марс — четвёртая планета."
    cl = [{"claim_id": "a", "claim_text": "Юпитер — крупнейшая планета Солнечной системы"},
          {"claim_id": "b", "claim_text": "Марс — четвёртая планета от Солнца"}]
    for av in variants(ans):
        cmp("claim_answer_linker", lambda x, c: tuple(linker.link_answer_to_claims(x, c)),
            lambda x, c: tuple(r_cal.link_answer_to_claims(x, c)), av, cl)
    for cv_ in variants(cl[0]["claim_text"]):
        cmp("claim_answer_linker(claim)", lambda x, c: tuple(linker.link_answer_to_claims(x, c)),
            lambda x, c: tuple(r_cal.link_answer_to_claims(x, c)), ans, [{"claim_id": "a", "claim_text": cv_}])

    # message_intensity: маркер и JSON с разделителями
    def mi_py_parse(x):
        v, r = mi.parse_self_report(x)
        d = dataclasses.asdict(r)
        d.pop("spans", None)          # как и в message_intensity_rust_parity_test: spans переносом не затрагивается
        return v, d
    raws = [
        'Ответ модели.\n#YANDI_STATE: {"severity": 0.4, "sincerity": 0.6}',
        'Ответ.\nYANDI STATE {"severity": "0.4", "sincerity": "0_5"}',
        'Ответ.\n## YANDI_STATE ##: {"severity": "١", "sincerity": " 0.5 "}',
        'Просто ответ без маркера',
        '{"severity": 0.3, "sincerity": 0.9} ответ',
    ]
    for raw in raws:
        for rv in variants(raw) + [raw.replace("YANDI_STATE", "YANDI\x1cSTATE"), raw.replace("YANDI STATE", "YANDI\x1fSTATE")]:
            cmp("message_intensity.parse", mi_py_parse, lambda x: (lambda v, r: (v, dict(r)))(*r_mi.parse_self_report(x)), rv)
            cmp("message_intensity.strip_markers", mi._strip_all_markers, r_mi.strip_all_markers, rv)

    # message_intensity сквозной фаззинг: комбинации значений полей (float()/bool() исходы, NaN, огромные числа...)
    pool_vals = ["0", "1", "0.5", "-3", "7", '"0.5"', '"abc"', '""', "null", "[]", "[1]", "{}", "true", "false", "NaN", "Infinity",
                 "-Infinity", "1e999", '"١٢"', '"1_0"', '"nan"', '"inf"', '"  0.3  "', '"\\u001c0.3"', '"1 2"', "1" + "0" * 400, "-0", "0.0",
                 '"it\'s"', '"\\n"', '"é\\u200b"']
    keys = ["is_insult", "is_apology", "severity", "sincerity"]
    m_cases = 0
    for _ in range(2500):
        fields = [f'"{k}": {rng.choice(pool_vals)}' for k in keys if rng.random() < 0.93]
        if rng.random() < 0.2:
            fields.append(f'"is_promise": {rng.choice(["true", "1", "null", "false"])}')
        rng.shuffle(fields)
        obj = "{" + ", ".join(fields) + "}"
        reply = rng.choice(["Ответ.", "", "  ", "О\x1cо"])
        for raw in (f"{reply}\n#YANDI_STATE: {obj}", f'{{"reply": {json.dumps(reply)}, "state": {obj}}}', f"{reply}\n###YANDI_STATE###\n{obj} хвост"):
            m_cases += 1
            cmp("message_intensity.fuzz", mi_py_parse, lambda x: (lambda v, r: (v, dict(r)))(*r_mi.parse_self_report(x)), raw)
    for _ in range(600):
        base = rng.choice(corpus[:6000])
        s = list(base)
        for _ in range(rng.randint(1, 3)):
            if s:
                s[rng.randrange(len(s))] = rng.choice(alphabet)
        payload = "".join(s)
        for raw in (f"Ответ.\n#YANDI_STATE: {payload}", payload, f"Привет\n#YANDI_STATE:\n{payload}\nдальше"):
            cmp("message_intensity.fuzz2", mi_py_parse, lambda x: (lambda v, r: (v, dict(r)))(*r_mi.parse_self_report(x)), raw)
    for st in ['{"is_insult": 1, "is_apology": 0, "severity": "0_5", "sincerity": 0.5}', '{"is_insult": 1}', "[]", "null", "5", '{"is_insult":1,"is_apology":1,"severity":NaN,"sincerity":-Infinity}']:
        pv = json.loads(st)
        cmp("intensity_from_state", lambda x: mi.intensity_from_state(x, error="e"), lambda x: dict(r_mi.intensity_from_state(x, "e")) | {"spans": ()}, pv)

    # local_guard: Host/Origin с разделителями по краям
    def hdr(host, origin=None):
        h = {"host": host}
        if origin is not None:
            h["origin"] = origin
        return h
    for host in ["127.0.0.1:5000", "localhost", "evil.com", "127.0.0.1"]:
        for sep in SEPS:
            for hv in (sep + host, host + sep, sep + host + sep):
                cmp("local_guard.is_local_request", lg.is_local_request, lambda h: tuple(r_lg.is_local_request(h)), hdr(hv))
                cmp("local_guard.is_allowed_request", lg.is_allowed_request, lambda h, p: tuple(r_lg.is_allowed_request(h, p)), hdr(hv), "/api/x")
                cmp("local_guard.host_name", lg._host_name, r_lg.host_name, hv)

    print(f"\n(сравнений модулей: {n}; успешных проверок всего: {_OK})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES[:8]}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
