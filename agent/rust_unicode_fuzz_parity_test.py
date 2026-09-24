"""
agent/rust_unicode_fuzz_parity_test.py — массовый дифференциальный фаззинг ВСЕХ перенесённых на Rust модулей
случайным Unicode: «трудные» символы (комбинирующие знаки, надстрочные/дробные цифры, цифры Unicode,
полноширинные, особые регистры İ ı ß ẞ ſ K Å Ω ǅ Σ ς, нулевой ширины, NBSP, U+2028, управляющие и разделители
U+001C..1F, эмодзи/астральные) вперемешку с реальными ключевыми словами самих модулей (чтобы паттерны
срабатывали) и разделителями. Python-по-умолчанию против Rust-функции напрямую.

Это ОБНАРУЖЕНИЕ расхождений, а не проверка одной функции: всё, что здесь найдено, — реальные различия между
Python и Rust на входах, которых целевые parity-тесты не пробовали. Известные остаточные ограничения (см.
rustlib/README.md) сведены в KNOWN и печатаются отдельно.

Run: python -m agent.rust_unicode_fuzz_parity_test [N]     (N — число строк на модуль, по умолчанию 1500)
"""
from __future__ import annotations

import ast
import dataclasses
import os
import random
import sys
from collections import Counter
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TRICKY = [
    "́", "̀", "̈", "ͅ", "҃", "​", "‌", "‍", "⁠", "﻿", " ", " ", "　", " ",
    " ", "\x85", "\x00", "\x1c", "\x1d", "\x1e", "\x1f", "\x7f", "\x0b", "\x0c", "²", "³", "¹", "½", "Ⅷ", "ⅷ", "٣", "١", "१", "３", "Ａ", "ａ",
    "İ", "ı", "ß", "ẞ", "ſ", "K", "Å", "Ω", "ǅ", "ǆ", "Σ", "ς", "σ", "ё", "Ё", "й", "Й", "é", "é", "😀", "🇷🇺", "\U0001d400", "\U000e0001",
    "_", "-", "'", "’", "«", "»", "—", "…", ".", ",", "!", "?", ":", ";", "(", ")", "[", "]", "{", "}", "\"", "\\", "/", "#", "%", "$", "@", "&", "*",
    "\t", "\n", "\r", " ", "  ",
]


def main() -> int:
    n_per = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    try:
        import yandi_rs
    except ImportError as e:
        print(f"SKIP: yandi_rs не собран ({e})")
        return 0
    for k in list(os.environ):
        if k.startswith("YANDI_") and k.endswith("_ENGINE"):
            del os.environ[k]
    import logging
    logging.disable(logging.CRITICAL)

    import agent.boundaries as bd
    import agent.claim_types as ct
    import agent.epistemic_router as er
    import agent.final_claim_coverage as fccm
    import agent.entity_resolver as entm
    import agent.object_resolver as objm
    import agent.claim_answer_linker as cal
    import agent.claim_evidence_retriever as cer
    import agent.claim_graph as cgm
    import agent.claim_identity as ci
    import agent.claim_semantic_identity_hardening as hg
    import agent.intent_router as ir
    import agent.message_intensity as mi
    import agent.orch_risk as orisk
    import agent.personal_boundary as pb
    import agent.scene_builder as sbm
    import agent.source_independence_prototype as sip
    import agent.source_quality as sq
    import agent.target_router as tr
    import pet.local_guard as lg
    from agent.claim_validator import ClaimValidator
    from agent.criticism_detector import CriticismDetector
    import yandi_rs.boundaries as r_bd
    import yandi_rs.claim_types as r_ct
    import yandi_rs.epistemic_router as r_er
    import yandi_rs.final_claim_coverage as r_fcc
    import yandi_rs.entity_resolver as r_ent
    import yandi_rs.object_resolver as r_obj
    import yandi_rs.claim_answer_linker as r_cal
    import yandi_rs.claim_evidence_retriever as r_cer
    import yandi_rs.claim_graph as r_cg
    import yandi_rs.claim_identity as r_ci
    import yandi_rs.claim_semantic_identity_hardening as r_hg
    import yandi_rs.claim_validator as r_cv
    import yandi_rs.criticism_detector as r_cd
    import yandi_rs.intent_router as r_ir
    import yandi_rs.local_guard as r_lg
    import yandi_rs.message_intensity as r_mi
    import yandi_rs.orch_risk as r_or
    import yandi_rs.personal_boundary as r_pb
    import yandi_rs.scene_builder as r_sb
    import yandi_rs.source_clustering as r_sc
    import yandi_rs.source_quality as r_sq
    import yandi_rs.target_router as r_tr

    rng = random.Random(int(os.environ.get("FUZZ_SEED", "20260924")))

    # ── реальные ключевые слова модулей (чтобы паттерны срабатывали) ──
    words = set()
    for d in ir.INTENT_PATTERNS.values():
        words |= {p.replace("?$", "") for p in d["patterns"]}
    pbo = pb.PersonalBoundary()
    for lst in (pbo.sincere_apology_patterns, pbo.formal_apology_patterns, pbo.provocation_patterns, pbo.deep_question_patterns):
        words |= {p.replace("[, ]*", ", ") for p in lst}
    words |= {p for p, _ in pbo.personal_patterns}
    sb = sbm.SceneBuilder()
    words |= set(sb.yandi_names + sb.ai_names + sb.ty_verb_forms + sb.group_reference + sb.coalition_markers)
    words |= {p[2:-2] for p in sb.self_reference}
    src = (ROOT / "agent" / "scene_builder.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "topic_patterns" for t in node.targets):
            for v in ast.literal_eval(node.value).values():
                words |= set(v)
    for v in sb.speech_act_patterns.values():
        words |= {p.replace("\\?", "?") for p in v}
    words |= {"ты", "тебе", "я", "меня", "мне", "не обнаружена", "не была обнаружена", "нет доказательств", "отсутствует", "Есть ли", "жизнь", "Юпитер",
              "Юпитере", "разумная", "телескоп", "зонд", "не найдено", "нет сомнений", "нет", "ни один", "аппарат", "обнаружен", "найден", "маловероятно",
              "юпитер", "жизни", "цифра", "5", "12", "3.5", "10%", "https://example.com/a", "example.com", "wikipedia.org", "gov.uk", "www.", "http://",
              "но", "потому что", "является", "равно", "это", "нет", "дура", "тупая", "идиот", "извини", "прости", "спасибо", "перепроверь", "стоит перепроверить",
              "YANDI_STATE", "#YANDI_STATE:", '{"is_insult": false, "is_apology": false, "severity": 0.5, "sincerity": 0.5}', "severity", "NaN",
              "[.!?]", "какие", "хоть", "телескопические наблюдения", "миссии", "спектр", "радар", "датчик", "сигнал"}
    words = sorted(words)

    def rand_text():
        parts = []
        for _ in range(rng.randint(1, 9)):
            r = rng.random()
            if r < 0.55:
                w = rng.choice(words)
                if rng.random() < 0.25:
                    w = w.upper() if rng.random() < 0.5 else w.capitalize()
                if rng.random() < 0.15:                      # вставить трудный символ внутрь слова
                    i = rng.randint(0, len(w))
                    w = w[:i] + rng.choice(TRICKY) + w[i:]
                parts.append(w)
            elif r < 0.85:
                parts.append("".join(rng.choice(TRICKY) for _ in range(rng.randint(1, 3))))
            else:
                parts.append("".join(chr(rng.choice([rng.randint(0x20, 0x7e), rng.randint(0x400, 0x4ff), rng.randint(0x300, 0x36f),
                                                     rng.randint(0x2000, 0x206f), rng.randint(0x1f300, 0x1f5ff), rng.randint(0x0, 0x1f)]))
                                     for _ in range(rng.randint(1, 4))))
        seps = [" ", " ", " ", "", "\t", "\n", " ", "\x1c", ", ", ". "]
        return "".join(p + rng.choice(seps) for p in parts)

    def norm(x):
        if dataclasses.is_dataclass(x) and not isinstance(x, type):
            return norm(dataclasses.asdict(x))
        if isinstance(x, Enum):
            return x.value
        if isinstance(x, dict):
            return {k: norm(v) for k, v in x.items() if k != "spans"}
        if isinstance(x, (list, tuple)):
            return [norm(v) for v in x]
        if isinstance(x, float) and x != x:
            return "NaN"
        return x

    fails: dict[str, list] = {}
    counts: Counter = Counter()

    def cmp(label, py_f, rs_f, *args):
        counts[label] += 1
        try:
            p = norm(py_f(*args))
        except Exception as e:                                # noqa: BLE001
            p = f"EXC:{type(e).__name__}"
        try:
            r = norm(rs_f(*args))
        except Exception as e:                                # noqa: BLE001
            r = f"EXC:{type(e).__name__}"
        if p != r:
            fails.setdefault(label, []).append((args, p, r))

    validator = ClaimValidator()
    detector = CriticismDetector()
    linker = cal.ClaimAnswerLinker()
    scene_norm = lambda sc: {k: (sorted(v) if k in ("participants", "mentioned", "coalition") else v) for k, v in dataclasses.asdict(sc).items()}  # noqa: E731
    bdry = pb.PersonalBoundary()
    sbld = sbm.SceneBuilder()
    objres = objm.ObjectResolver()
    cgraph = cgm.ClaimGraph()
    entres = entm.EntityResolver()

    def with_rust(mod_attr_owner, sentinel, env, fn):
        def run(*a):
            os.environ[env] = "rust"
            setattr(mod_attr_owner, sentinel, None)
            try:
                return fn(*a)
            finally:
                os.environ.pop(env, None)
                setattr(mod_attr_owner, sentinel, None)
        return run

    def py_only(owner, sentinel, fn):
        def run(*a):
            setattr(owner, sentinel, False)
            try:
                return fn(*a)
            finally:
                setattr(owner, sentinel, None)
        return run

    for _ in range(n_per):
        t, u = rand_text(), rand_text()
        q = "Есть ли " + rand_text() + rng.choice(["?", " на Юпитере?", ""])
        cmp("claim_identity.canonicalize", ci.canonicalize_claim_text, r_ci.canonicalize_claim_text, t)
        cmp("claim_identity.hash", ci.compute_claim_content_hash, r_ci.compute_claim_content_hash, t)
        cmp("claim_identity.subject_anchors", ci.extract_subject_anchors, r_ci.extract_subject_anchors, t)
        cmp("claim_identity.content_anchors", ci.extract_content_anchors, r_ci.extract_content_anchors, t)
        cmp("claim_validator.normalize", ClaimValidator.normalize_claim_text, r_cv.normalize_claim_text, t)
        cmp("claim_validator.validate", validator.validate, lambda x: tuple(r_cv.validate(x)), t)
        cmp("hardening_guard", hg.hardening_guard, r_hg.hardening_guard, t, u)
        cmp("criticism.analyze", lambda x: detector.analyze(x, {"trust": 40, "history_insults": 1}),
            lambda x: dict(r_cd.analyze(x, {"trust": 40, "history_insults": 1})), t)
        cmp("boundaries.toxicity", bd.detect_toxicity, lambda x: (lambda d: {**d, "words": list(d["words"])})(dict(r_bd.detect_toxicity(x))), t)
        cmp("boundaries.apology", bd.is_apology, lambda x: tuple(r_bd.is_apology(x)), t)
        cmp("evidence.is_absence", cer._is_absence_claim, r_cer.is_absence_claim, t)
        cmp("evidence.is_existence_question", cer._is_existence_question, r_cer.is_existence_question, q)
        cmp("evidence.extract_target", cer._extract_existence_target, r_cer.extract_existence_target, q)
        cmp("evidence.classify_role", cer._classify_claim_role, lambda a, b: dict(r_cer.classify_claim_role(a, b)), t, q)
        cmp("source_quality", sq.evaluate_source_quality, lambda a, b, c, d: r_sq.evaluate_source_quality(a, b, c, d),
            rng.choice(["https://example.com/x", "http://" + t.replace(" ", "")[:30] + ".org/a", "", "wikipedia.org", "gov.uk"]), t, u, "web")
        cmp("title_similarity", sip.title_similarity, r_sc.title_similarity, t, u)
        cmp("content_similarity", sip.content_fingerprint_similarity, r_sc.content_fingerprint_similarity, t, u)
        cmp("orch_risk", lambda x: (lambda r: (r.risk_level, r.mandatory_arbitrage, r.validator_model, r.nodes_required))(orisk.assess_risk(x)),
            lambda x: tuple(r_or.assess_risk(x)), t)
        cmp("intent_router", ir.detect_intent, lambda x: tuple(r_ir.detect_intent(x)) if x else ("unknown", 0.0, "empty"), t)
        cmp("target_router", tr.detect_target, lambda x: tuple(r_tr.detect_target(x)), t)
        cmp("personal_boundary", lambda x: dataclasses.asdict(bdry.analyze(x)), lambda x: dict(r_pb.analyze(x)), t)
        cmp("scene_builder", lambda x: scene_norm(sbld.build(x, {"is_dialog": True})),
            lambda x: scene_norm(sbm.SocialScene(**r_sb.build(x, True))), t)
        cmp("claim_answer_linker", lambda a, c: tuple(linker.link_answer_to_claims(a, c)), lambda a, c: tuple(r_cal.link_answer_to_claims(a, c)),
            t + ". " + u, [{"claim_id": "a", "claim_text": u}, {"claim_id": "b", "claim_text": t}])
        raw = rng.choice(["", "Ответ. ", u + "\n"]) + "#YANDI_STATE: " + rng.choice([
            '{"is_insult": false, "is_apology": false, "severity": 0.5, "sincerity": 0.5}', '{"severity": ' + t[:6].replace('"', "") + "}", "{" + u.replace('"', "'") + "}"])
        cmp("message_intensity", lambda x: (lambda v, r: (v, norm(r)))(*mi.parse_self_report(x)),
            lambda x: (lambda v, r: (v, dict(r)))(*r_mi.parse_self_report(x)), raw)
        host = rng.choice(["127.0.0.1:5000", "localhost", "evil.com", "[::1]:5000", "LOCALHOST", "127.0.0.1"]) + rng.choice(TRICKY + ["", "", ""])
        cmp("local_guard.host_name", lg._host_name, r_lg.host_name, host)
        headers = {}
        if rng.random() < 0.9:
            headers["host"] = host
        if rng.random() < 0.6:
            headers["origin"] = rng.choice(["http://localhost:5000", "http://127.0.0.1", "moz-extension://a1b2c3d4-e5f6-7890-abcd-ef1234567890", "null", "http://evil.com",
                                            "moz-extension://" + t[:20]]) + rng.choice(["", "", rng.choice(TRICKY)])
        if rng.random() < 0.6:
            headers["sec-fetch-site"] = rng.choice(["same-origin", "none", "cross-site", "same-site", "", "SAME-ORIGIN"]) + rng.choice(["", "", rng.choice(TRICKY)])
        path = rng.choice(["/", "/api/x", "/api/ext/x", "/api/ext/../x", "/api/ext//x", "/api/ext/a\\b", "/static/a.js", t[:12], "/api/ext/" + t[:10]])
        cmp("local_guard.is_allowed_request", lg.is_allowed_request, lambda h, p: tuple(r_lg.is_allowed_request(h, p)), headers, path)
        cmp("local_guard.is_local_request", lg.is_local_request, lambda h: tuple(r_lg.is_local_request(h)), headers)
        # epistemic_router — детекторы на строчных/исходных запросах
        ql = q.lower()
        cmp("epistemic.domain", er._detect_domain, lambda x: tuple(r_er.detect_domain(x)), ql)
        cmp("epistemic.hypothetical", er._detect_hypothetical, lambda x: bool(r_er.detect_hypothetical(x)), ql)
        cmp("epistemic.negative", er._detect_negative_claim, lambda x: bool(r_er.detect_negative_claim(x)), q)
        dom = er._detect_domain(ql)[0]
        cmp("epistemic.testability", er._detect_testability, lambda x, d: tuple(r_er.detect_testability(x, d)), ql, dom)
        cmp("claim_types.guess", lambda x: ct.guess_claim_type_by_text(x).value, r_ct.guess_claim_type_by_text, t)
        cmp("object_resolver", objres.resolve, lambda x: dict(r_obj.resolve(x)), t)
        cmp("fcc.content_words", lambda x: sorted(fccm._content_words(x)), r_fcc.content_words, t)
        cmp("fcc.has_negation", fccm._has_negation, r_fcc.has_negation, t)
        cmp("fcc.lexical_overlap", fccm._lexical_overlap, r_fcc.lexical_overlap, t, u)
        cmp("fcc.shares_number", fccm._shares_number, r_fcc.shares_number, t, u)
        cmp("fcc.is_near_duplicate", fccm._is_near_duplicate, r_fcc.is_near_duplicate, t, u)
        cmp("fcc.mandatory_reason", lambda a, b: fccm._mandatory_routing_reason(a, b, "CORE", None), lambda a, b: r_fcc.mandatory_routing_reason(a, b, "CORE", None), t, u)
        cmp("claim_graph.split", cgraph._split_into_sentences, lambda x: list(r_cg.split_into_sentences(x)), t + ". " + u)
        cmp("claim_graph.clean", cgraph._clean_sentence, r_cg.clean_sentence, t)
        cmp("claim_graph.is_world_claim", cgraph._is_world_claim, r_cg.is_world_claim, t)
        cmp("claim_graph.claim_type", cgraph._determine_claim_type, r_cg.determine_claim_type, t)
        cmp("claim_graph.confidence", lambda x: cgraph._calculate_confidence(x, {"relevance_to_query": 0.5}), lambda x: r_cg.calculate_confidence(x, 0.5), t)
        cmp("claim_graph.is_contradiction", cgraph._is_contradiction, r_cg.is_contradiction, t, u)
        cmp("claim_graph.is_support", cgraph._is_support, r_cg.is_support, t, u)
        # entity_resolver: game ∈ множеству совпавших игр (set-порядок у Python хеш-рандомизирован), categories — как мультимножество
        allowed = {g.upper() for g in entm.KNOWN_GAMES if g in t.strip().lower()} or {None}
        pe, re_ = entres.resolve(t), dict(r_ent.resolve(t))
        counts["entity_resolver"] += 1
        if not (pe["game"] in allowed and re_["game"] in allowed and sorted(pe["categories"]) == sorted(re_["categories"])
                and {k: v for k, v in pe.items() if k not in ("game", "categories")} == {k: v for k, v in re_.items() if k not in ("game", "categories")}):
            fails.setdefault("entity_resolver", []).append(((t,), pe, re_))

    # ── URL-подобные строки против настоящего urlparse(url).hostname (source_quality._hostname) ──
    url_atoms = ["http", "https", "HTTP", "ftp", "x-y", "1a", "a b", "", "a+b.c", "://", ":/", "//", "/", ":", "?", "#", "@", "[", "]", "%", "\t", "\n", "\r",
                 "example.com", "www.example.com", "WWW.Example.COM", "sub.wikipedia.org", "a.gov.uk", "127.0.0.1", "::1", "fe80::1%Eth0", "user", "user:pw",
                 "8080", "хост.рф", "пример", "Ａ", "ａ", "／", "℀", "㎑", "ｅｘａｍｐｌｅ", "İ", "ǅ", "\u200b", "\x00", " ", "\u00a0", "%41", "..", ".", "-", "_"]
    for _ in range(n_per * 2):
        url = "".join(rng.choice(url_atoms) for _ in range(rng.randint(1, 8)))
        if rng.random() < 0.5:
            url = rng.choice(["http://", "https://", "//", "ftp://", "HTTP://", "x:", "h:"]) + url
        cmp("source_quality.hostname(urlparse)", sq._hostname, r_sq.hostname, url)

    # Фиксированные пограничные URL (скобки / IPv6 / IPvFuture / userinfo): их случайный генератор почти не порождает
    edge_urls = ["http://[::1]", "http://[::1%]", "http://[::1%%x]", "http://[::1%eth0]:80/x", "http://[fe80::1%Eth0]/", "http://[1:2:3:4:5:6:7:8]",
                 "http://[1:2:3:4:5:6:7:8:9]", "http://[::]", "http://[:::]", "http://[1::2::3]", "http://[::ffff:1.2.3.4]", "http://[::1.2.3.256]",
                 "http://[::01.2.3.4]", "http://[::1.2.3]", "http://[1.2.3.4]", "http://[v1.x]", "http://[vx.y]", "http://[v1.]", "http://[v1.a\nb]",
                 "http://[V1.x]", "http://x[::1]", "http://[::1]x", "http://user@[::1]:80/", "http://user[@[::1]/", "http://[::1]:80:90/", "http://[]",
                 "http://[", "http://]", "http://[[::1]]", "http://[::1]]", "http://a@b@[::1]", "http://[0:0:0:0:0:0:0:1]", "http://[::12345]", "http://[::g]",
                 "http://[1:2:3:4:5:6:7::]", "http://[::2:3:4:5:6:7:8]", "http://[1::2:3:4:5:6:7:8]", "http://[:1:2:3:4:5:6:7]", "http://[1:2:3:4:5:6:7:]",
                 "http://[١::1]", "http://[::1/64]", "//[::1]/", "  http://example.com", "\x00\x1fhttp://example.com", "http://exa\tmple.com", "http:\n//example.com",
                 "http://user:pw@example.com:8080/p?q#f", "http://EXAMPLE.com:80", "http://a:b:c/x", "x-y:z://host/", "1http://host", "ht tp://host", "://host",
                 "http://℀/", "http://a℀b/", "http://ａ／ｂ/", "http://％41/", "http://ｅ＠ｘ/", "HTTP://WWW.Example.COM/"]
    for url in edge_urls:
        cmp("source_quality.hostname(edge)", sq._hostname, r_sq.hostname, url)

    total = sum(counts.values())
    print(f"сравнений: {total} ({n_per} строк на модуль, seed={os.environ.get('FUZZ_SEED', '20260924')})")
    if not fails:
        print("РЕЗУЛЬТАТ: расхождений нет")
        return 0
    print(f"РАСХОЖДЕНИЯ в {len(fails)} функциях из {len(counts)}:")
    for label, lst in sorted(fails.items(), key=lambda kv: -len(kv[1])):
        print(f"\n--- {label}: {len(lst)} из {counts[label]}")
        for args, p, r in lst[:3]:
            print(f"    args={[a if not isinstance(a, str) else a[:80] for a in args]!r}\n      python={str(p)[:160]}\n      rust  ={str(r)[:160]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
