"""
scripts/rust_bench.py — честный замер «Python против Rust» для всех перенесённых функций через ПУБЛИЧНЫЙ Python-API
(с накладными расходами вызова через границу PyO3 и обёртки), на реалистичных входах. Показывает, какие переключатели
YANDI_*_ENGINE=rust дают реальный выигрыш, а какие лишь «правильны», но по скорости не важны.

    python scripts/rust_bench.py            # таблица
    python scripts/rust_bench.py --md       # таблица в Markdown (для README)

Требует собранного нативного модуля (rustlib/yandi_rs, `maturin develop --release`).
"""
from __future__ import annotations

import copy
import os
import random
import re
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("YANDI_TEST_MODE", "1")

import logging  # noqa: E402

logging.disable(logging.CRITICAL)

try:
    import yandi_rs  # noqa: F401,E402
except ImportError:
    print("yandi_rs не собран — см. rustlib/README.md")
    sys.exit(0)

SRC = [p for p in (ROOT / "agent", ROOT / "pet")]
ENV_VARS = sorted({m for f in list((ROOT / "agent").rglob("*.py")) + list((ROOT / "pet").rglob("*.py"))
                   for m in re.findall(r"YANDI_[A-Z_]+_ENGINE", f.read_text(encoding="utf-8", errors="ignore"))
                   if "OLLAMA" not in m and "LLM" not in m})


def set_engine(on: bool) -> None:
    for v in ENV_VARS:
        if on:
            os.environ[v] = "rust"
        else:
            os.environ.pop(v, None)
    for name, mod in list(sys.modules.items()):
        if mod is None or not (name.startswith("agent") or name.startswith("pet")):
            continue
        for attr in list(vars(mod)):
            if attr.startswith("_rust_") and not callable(getattr(mod, attr, None)):
                setattr(mod, attr, None)


def measure(fn, budget=0.25):
    fn()
    t0 = time.perf_counter()
    n = 0
    while True:
        for _ in range(20):
            fn()
        n += 20
        dt = time.perf_counter() - t0
        if dt >= budget:
            return dt / n * 1e6                       # мкс/вызов


rng = random.Random(7)
CLAIM = "По имеющимся данным, разумная жизнь на Юпитере не была обнаружена, а температура у облаков составляет около -145°C согласно исследованию."
QUERY = "Есть ли разумная жизнь на Юпитере?"
LONG = " ".join([CLAIM] * 6)
RAW = "Ответ модели, который довольно длинный и содержит рассуждения.\n#YANDI_STATE: " + '{"is_insult": false, "is_apology": false, "severity": 0.4, "sincerity": 0.6}'


def build_entries():
    import agent.boundaries as bd
    import agent.claim_answer_linker as cal
    import agent.claim_evidence_retriever as cer
    import agent.claim_graph as cgm
    import agent.claim_identity as ci
    import agent.claim_semantic_identity_hardening as hg
    import agent.entity_resolver as entm
    import agent.epistemic_router as er
    import agent.intent_router as ir
    import agent.message_intensity as mi
    import agent.object_resolver as objm
    import agent.orch_risk as orisk
    import agent.orchestrator.epistemic.canonical_trust as ctm
    import agent.orchestrator.epistemic.trust_gate as tg
    import agent.personal_boundary as pb
    import agent.scene_builder as sbm
    import agent.source_clustering as sc
    import agent.source_quality as sq
    import agent.target_router as tr
    import pet.local_guard as lg
    from agent.claim_validator import ClaimValidator
    from agent.criticism_detector import CriticismDetector

    import agent.db.sql.crypto as cr
    import agent.orch_query_framer as qf
    import agent.orch_tag_tree as tt
    import agent.policy as pol
    import agent.relationship_memory as rm
    import agent.tools.tool_shell as tsh
    import pet.commitment_verification as cv
    import pet.core_lifecycle as cl
    import pet.event_extraction as ee
    import pet.fact_extraction as fe
    import pet.ui_settings as us

    validator, detector, linker = ClaimValidator(), CriticismDetector(), cal.ClaimAnswerLinker()
    boundary, scene, objr, entr, graph = pb.PersonalBoundary(), sbm.SceneBuilder(), objm.ObjectResolver(), entm.EntityResolver(), cgm.ClaimGraph()
    claims = [{"claim_id": f"c{i}", "claim_text": CLAIM[i * 7:] + " " + str(i)} for i in range(6)]

    def mk_pool(n):
        words = ["альфа", "бета", "гамма", "delta", "мир", "война", "юпитер", "планета", "учёные", "нашли", "новую", "сообщает", "агентство", "science"]
        r = random.Random(1)
        return [{"evidence_id": f"e{i}", "source_title": " ".join(r.choice(words) for _ in range(8)),
                 "content_excerpt": " ".join(r.choice(words) for _ in range(90))} for i in range(n)]

    pool30, pool60 = mk_pool(30), mk_pool(60)
    er_ns = types.SimpleNamespace(domain="scientific", testability="empirical", max_trust_cap="SUPPORTED", is_science_as_model=False,
                                  trust_score=0.8, need_clarification=False, needs_frame_split=False)

    class Trace:
        def add_learning_rule(self, *a):
            pass

    def trust_call():
        return tg.apply_epistemic_trust_adjustment(
            False, "STRONGLY_SUPPORTED", er_ns, {"t": 1}, 0.9, 0.9, None, Trace(), True, [1, 2],
            types.SimpleNamespace(confidence=0.9), 0.7, False, False, ["a"], None, types.SimpleNamespace(intent="q"))

    ev = [{"evidence_id": f"ev{k}", "content_excerpt": LONG, "relevance_to_query": 0.5, "source_uri": "https://en.wikipedia.org/x", "source_type": "web"} for k in range(4)]
    texts30 = [f"{CLAIM} вариант {i} " + " ".join(rng.choice(["Марс", "Земля", "не", "является", "планета", "спутник"]) for _ in range(6)) for i in range(30)]

    def build_graph30():
        g = cgm.ClaimGraph()
        g.claims = [cgm.Claim(claim_id=f"c{i}", text=t) for i, t in enumerate(texts30)]
        g._build_graph()

    words120 = [(f"w{i}", i * 3, i * 3 + 2) for i in range(120)]
    cand = {"type": "insult", "span": [2, 5], "severity": 0.7}
    fact = {"span": [0, 3], "class": "preference", "statement": "любит чёрный кофе по утрам", "polarity": "affirmed", "time": "current", "stability": "stable",
            "relation": "replaces", "target": 1}
    known = [{"fact_id": f"f{i}", "statement": f"s{i}"} for i in range(10)]
    frame = qf.QueryFrame(raw_query="q", enriched_query="q", domain="general", obj="двигатель", action="починить", constraints={"a": 1}, missing=["бюджет"])
    settings = {"voice": "remote", "advisors": ["local", "api"], "local": {"path": "/m/model.gguf"}, "remote": {"address": "http://192.168.1.5:8080", "model": "qwen-14b"},
                "api": {"service": "openai", "model": "gpt-x"}}
    enc_key = bytes(range(32))
    enc_kw = dict(entity_type="question", entity_id=42, field_name="text")
    blob = cr.encrypt_field(enc_key, CLAIM, **enc_kw)
    unlock_key = "A" * 43 + "="
    return [
        ("policy.scan_text (секреты)", lambda: pol.SecretScanner().scan_text("token sk-abcdefghijklmnopqrstuv and " + CLAIM)),
        ("tool_shell._allowed", lambda: tsh._allowed("ls -la agent/")),
        ("crypto.encrypt_field+decrypt_field (AES-GCM)", lambda: cr.decrypt_field(enc_key, cr.encrypt_field(enc_key, CLAIM, **enc_kw), **enc_kw)),
        ("crypto.blind_index", lambda: cr.blind_index(enc_key, "question", QUERY)),
        ("event_extraction.segment_words (120 слов)", lambda: ee.segment_words(" ".join(w for w, _, _ in words120))),
        ("event_extraction._validate_candidate", lambda: ee._validate_candidate(cand, words120)),
        ("fact_extraction._validate", lambda: fe._validate(fact, words120, known)),
        ("fact_extraction.looks_secret", lambda: fe.looks_secret(LONG)),
        ("commitment_verification.inside_quotation", lambda: cv.inside_quotation("он сказал «я всё сделал» и ушёл " * 5, 90)),
        ("orch_tag_tree.lsh_entropy (30 запросов)", lambda: tt.lsh_entropy(texts30, 32)),
        ("orch_tag_tree._tokenize", lambda: tt._tokenize(CLAIM)),
        ("relationship_memory._stems", lambda: rm._stems("Извини, я назвал тебя дураком и не хотел обидеть " * 3, drop_non_content=True)),
        ("orch_query_framer.decide_policy", lambda: qf.decide_policy(frame)),
        ("orch_query_framer._auto_cq", lambda: qf._auto_cq(frame)),
        ("ui_settings.validate", lambda: us.validate(settings)),
        ("core_lifecycle._decode_key", lambda: cl._decode_key(unlock_key)),
        ("core_lifecycle._validate (unlock)", lambda: cl._validate("unlock", {"key": unlock_key, "context": "yandi/core/v1"})),
        ("claim_identity.canonicalize_claim_text", lambda: ci.canonicalize_claim_text(CLAIM)),
        ("claim_identity.extract_subject_anchors", lambda: ci.extract_subject_anchors(CLAIM)),
        ("claim_validator.validate", lambda: validator.validate(CLAIM)),
        ("hardening_guard", lambda: hg.hardening_guard(CLAIM, CLAIM.replace("не была", "была"))),
        ("criticism_detector.analyze", lambda: detector.analyze("Ты дура, но стоит перепроверить расчёты", {"trust": 40, "history_insults": 1})),
        ("boundaries.detect_toxicity", lambda: bd.detect_toxicity("ты тупой идиот и вообще дурак")),
        ("claim_answer_linker.link", lambda: linker.link_answer_to_claims(LONG, claims)),
        ("claim_evidence_retriever.classify_role", lambda: cer._classify_claim_role(CLAIM, QUERY)),
        ("source_quality.evaluate", lambda: sq.evaluate_source_quality("https://en.wikipedia.org/wiki/Jupiter", "Jupiter", LONG, "web")),
        ("epistemic_router.detect_domain", lambda: er._detect_domain(QUERY.lower())),
        ("message_intensity.parse_self_report", lambda: mi.parse_self_report(RAW)),
        ("orch_risk.assess_risk", lambda: orisk.assess_risk("Как лечить кашель и стоит ли инвестировать в биткоин?")),
        ("intent_router.detect_intent", lambda: ir.detect_intent("Как работает DHT в P2P-сети? Расскажи подробнее")),
        ("target_router.detect_target", lambda: tr.detect_target("Расскажи о себе, что ты чувствуешь?")),
        ("personal_boundary.analyze", lambda: boundary.analyze("Извини, но я просто хотел спросить, ты меня любишь?")),
        ("scene_builder.build", lambda: scene.build("Пойдёшь за меня замуж? Я тебя люблю!", {"is_dialog": True})),
        ("object_resolver.resolve", lambda: objr.resolve("Твоё мнение о песне Guns N Roses")),
        ("entity_resolver.resolve", lambda: entr.resolve("Легенда Форнема сектор X3 фильм")),
        ("claim_graph.extract_claims (4 evidence)", lambda: cgm.ClaimGraph().extract_claims(copy.deepcopy(ev))),
        ("claim_graph._build_graph (30 утверждений)", build_graph30),
        ("trust_gate.apply_epistemic_trust_adjustment", trust_call),
        ("trust_gate._calculate_delta_factors", lambda: tg._calculate_delta_factors("VERIFIED", 0.8, True, 2, 3)),
        ("canonical_trust.compute", lambda: ctm.compute_canonical_trust("STRONGLY_SUPPORTED", "SUPPORTED", None, False)),
        ("local_guard.is_allowed_request", lambda: lg.is_allowed_request({"host": "127.0.0.1:5000", "origin": "http://localhost:5000", "sec-fetch-site": "same-origin"}, "/api/x")),
        ("source_clustering.assign (30 источников)", lambda: sc.assign_source_clusters(copy.deepcopy(pool30))),
        ("source_clustering.assign (60 источников)", lambda: sc.assign_source_clusters(copy.deepcopy(pool60))),
    ]


def main() -> int:
    md = "--md" in sys.argv
    set_engine(False)
    entries = build_entries()
    rows = []
    for label, fn in entries:
        set_engine(False)
        py = measure(fn)
        set_engine(True)
        try:
            rs = measure(fn)
        except Exception as e:                            # noqa: BLE001
            rows.append((label, py, float("nan"), f"ошибка: {type(e).__name__}"))
            continue
        rows.append((label, py, rs, ""))
    set_engine(False)
    if md:
        print("| Функция | Python, мкс | Rust, мкс | Ускорение |\n|---|---:|---:|---:|")
    else:
        print(f"{'Функция':<52}{'Python, мкс':>13}{'Rust, мкс':>12}{'Ускорение':>12}")
    for label, py, rs, err in rows:
        ratio = py / rs if rs == rs and rs > 0 else float("nan")
        note = err or (f"×{ratio:.1f}" if ratio >= 1.05 else ("≈ без разницы" if ratio > 0.95 else f"медленнее ×{1 / ratio:.1f}"))
        if md:
            print(f"| `{label}` | {py:,.1f} | {rs:,.1f} | {note} |")
        else:
            print(f"{label:<52}{py:>13,.1f}{rs:>12,.1f}{note:>12}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
