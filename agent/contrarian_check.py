"""
agent/contrarian_check.py — owner mandate: "янди должна не верить, искать
теорию заговора" (в смысле: активно проверять, есть ли у темы вопроса
известная альтернативная/конспирологическая версия — Антарктида, НЛО,
космические полёты, история, политика — и не подавать её пользователю
без проверки).

NOT for every question — "2+2*2=6" or a cooking recipe has no such
ecosystem; the LLM gate below decides per-question whether one plausibly
exists, same "latency/cost gate, does not change truth/confidence" shape
agent/claim_evidence_retriever.py's own retrieve_for_claims() already
uses for claim SELECTION.

THE ACTUAL CHECK reuses the EXACT SAME synthetic-claim path agent/
dependency_recheck.py already established (retrieve_for_claims() +
classify_relation(), see that module's own synthetic_claim construction)
— the alternative claim gets NO free pass, NO uncritical "both sides"
framing: it is checked with identical evidence rigor to any other claim
in this system. Verdict vocabulary matches dependency_recheck.py's own
outcome set (supported/contradicted/disputed/inconclusive/no_evidence)
rather than inventing a second, incompatible scale for the same concept.

HONESTY DISCIPLINE (schema mandate, agent/db/sql/schema.py's own
docstring: "MEMORY != TRUTH... outcomes are always candidate/reason/
verification_status, never a truth predicate"): this module's own output
is a claim + an evidence-based outcome, never an assertion that the
mainstream account or the alternative one is "the truth." "contradicted"
means the gathered evidence contradicts the alternative claim — it does
not mean this module has independently proven anything.

COST: on questions where the gate DOES trigger, this adds one Ollama
generate call (the gate + claim formulation) plus one retrieve_for_claims
batch — comparable in shape to agent/dependency_recheck.py's own per-
request bounded extra work, not free, but bounded to genuinely
controversy-shaped topics via the gate.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Optional

import requests as _requests

from agent.orch_config import (
    OLLAMA_BASE as OLLAMA, MODEL, TEMP_CONDUCTOR, MAX_TOKENS_CONDUCTOR, GENERATION_SEMAPHORE,
)
from agent.claim_evidence_retriever import retrieve_for_claims
from agent.claim_relation import ClaimRelation, classify_relation

TIMEOUT = 60

_session = _requests.Session()
_session.trust_env = False

_GATE_PROMPT = """Тема вопроса: "{query}"

Существует ли у ЭТОЙ ТЕМЫ известная, реально распространённая среди людей
альтернативная версия событий или конспирологическая теория? Не любая
случайная гипотеза, а то, что реально и widely обсуждается — например:
высадка человека на Луну, форма Земли, теракты 11 сентября, вакцины и
аутизм, НЛО и Розуэлл, экспедиции в Антарктиду, убийство Кеннеди и
подобное по масштабу распространённости.

Если ДА — сформулируй ОДНИМ предложением САМУЮ распространённую такую
альтернативную версию как конкретное проверяемое утверждение (не вопрос
и не описание темы, а утверждение, которое можно подтвердить или
опровергнуть источниками).

Если темы вопроса не касаются такой ситуации (обычный факт, техническая
процедура, математика, личное мнение, кулинария и т.п.) — альтернативы нет.

Верни ТОЛЬКО JSON, без пояснений:
{{"has_alternative": true/false, "alternative_claim": "..." или null}}
"""

_VERDICT_PHRASES = {
    "contradicted": "доступные источники противоречат этой версии",
    "no_evidence": "не нашла источников, которые бы её подтверждали",
    "inconclusive": "источники по ней не дают однозначного ответа",
    "supported": "нашла источники, которые её поддерживают — единого мнения по этой теме нет",
    "disputed": "по ней есть источники и за, и против — это реальный предмет спора",
}


def _call_ollama(prompt: str) -> str:
    _wait_started = time.time()
    with GENERATION_SEMAPHORE:
        _waited = time.time() - _wait_started
        if _waited > 0.05:
            print(f"[ContrarianCheck] generation queue wait={_waited:.2f}s")
        resp = _session.post(
            f"{OLLAMA}/api/generate",
            json={
                "model": MODEL, "prompt": prompt, "stream": False,
                "options": {"temperature": TEMP_CONDUCTOR, "num_predict": MAX_TOKENS_CONDUCTOR},
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {}


def _classify_outcome(evidence_for: list, evidence_against: list, evidence_count: int) -> str:
    """Identical decision shape to agent/dependency_recheck.py's own
    outcome computation — deliberately not reinvented (two incompatible
    outcome vocabularies for the same underlying concept is exactly the
    kind of drift this codebase has hit and fixed more than once)."""
    if evidence_count == 0:
        return "no_evidence"
    if not evidence_for and not evidence_against:
        return "inconclusive"
    if evidence_for and evidence_against:
        return "disputed"
    if evidence_for:
        return "supported"
    return "contradicted"


def check_for_alternative_theory(query: str) -> Optional[Dict[str, Any]]:
    """Fail-open at every step (same discipline as every other best-
    effort shadow/side-check in this pipeline): a broken gate call,
    malformed JSON, or a broken retrieval returns None — this feature
    degrading to "nothing to add" must never break or alter the main
    answer.

    Returns None when the gate doesn't trigger OR on any failure.
    Returns {"alternative_claim": str, "outcome": str, "evidence_for":
    [...ids], "evidence_against": [...ids]} when a real check ran."""
    if not query or not query.strip():
        return None
    try:
        raw = _call_ollama(_GATE_PROMPT.format(query=query.strip()[:500]))
        gate = _extract_json(raw)
    except Exception:
        return None

    if not gate.get("has_alternative"):
        return None
    alt_claim = (gate.get("alternative_claim") or "").strip()
    if not alt_claim:
        return None

    synthetic_claim = {
        "claim_id": f"contrarian_{abs(hash(alt_claim))}",
        "claim_text": alt_claim,
        "claim_type": "factual",
        "query_context": alt_claim,
    }
    try:
        evidence = retrieve_for_claims([synthetic_claim])
    except Exception:
        return None

    evidence_for: list = []
    evidence_against: list = []
    for ev in evidence:
        excerpt = (ev.get("content_excerpt") or "").strip()
        if not excerpt:
            continue
        relation = classify_relation(alt_claim, excerpt)
        ev_id = ev.get("evidence_id")
        if relation == ClaimRelation.SUPPORTS and ev_id:
            evidence_for.append(ev_id)
        elif relation == ClaimRelation.CONTRADICTS and ev_id:
            evidence_against.append(ev_id)

    outcome = _classify_outcome(evidence_for, evidence_against, len(evidence))
    return {
        "alternative_claim": alt_claim, "outcome": outcome,
        "evidence_for": evidence_for, "evidence_against": evidence_against,
    }


def format_alternative_note(result: Dict[str, Any]) -> str:
    """Deterministic text, never LLM-authored — same discipline as
    agent/claim_history_note.py's own format_history_note_block()."""
    phrase = _VERDICT_PHRASES.get(result["outcome"], "результат проверки неоднозначен")
    return (
        "\n\n---\n🔎 Также проверила распространённую альтернативную версию по этой теме: "
        f"«{result['alternative_claim']}». {phrase.capitalize()}."
    )
