"""
assistant/claim_evidence_mapper.py — Claim-Evidence Mapper.

Привязывает утверждения (claims) к источникам (evidence).
Заполняет derived_from_evidence_ids.

Задача: каждый claim должен знать, откуда он взят.
"""
from __future__ import annotations

import sys
import uuid
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

# Добавляем путь для импорта
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from agent.orch_schemas import ClaimRecord, EvidenceRecord


@dataclass
class MappedClaim:
    """Утверждение с привязкой к evidence."""
    claim_id: str
    claim_text: str
    evidence_ids: List[str]
    confidence: float = 0.5
    claim_type: str = "factual"


def map_claims_to_evidence(
    claims: List[Dict[str, Any]],
    evidence_records: List[Dict[str, Any]]
) -> List[ClaimRecord]:
    """
    Привязать claims к evidence на основе текстового совпадения.

    Args:
        claims: список claims (словари)
        evidence_records: список evidence записей

    Returns:
        List[ClaimRecord] с заполненными derived_from_evidence_ids
    """
    if not claims:
        return []

    # Извлекаем тексты evidence
    evidence_texts = []
    for ev in evidence_records:
        content = ev.get("content_excerpt", "")
        if content and len(content) > 50:
            title = (ev.get("source_title") or "").strip()

            # Mapper должен видеть не только первые 500 символов body.
            #
            # Заголовок часто содержит главный subject страницы,
            # а первые сотни символов web body могут быть navigation /
            # boilerplate.
            semantic_text = " ".join(
                part
                for part in (
                    title,
                    content[:1200],
                )
                if part
            )

            evidence_texts.append({
                "id": ev.get(
                    "evidence_id",
                    f"ev_{uuid.uuid4().hex[:8]}"
                ),
                "text": semantic_text,
                "source": ev.get("source_uri", ""),
                "title": title,

                # Claim-specific retrieval ownership.
                #
                # Это НЕ означает support.
                # Это только ограничивает candidate pool:
                # evidence, специально найденный для одного claim,
                # не должен автоматически гулять по другим claims.
                "retrieval_origin": ev.get(
                    "retrieval_origin",
                    "",
                ),
                "retrieval_claim_id": ev.get(
                    "retrieval_claim_id",
                    "",
                ),
            })

    if not evidence_texts:
        # Если нет evidence, возвращаем claims с пустыми привязками
        return [_to_claim_record(c, []) for c in claims]

    mapped_claims = []

    # ========================================================
    # SEMANTIC EMBEDDING CACHE
    # ========================================================
    #
    # Evidence не меняются от claim к claim.
    # Поэтому их embeddings должны вычисляться ровно один раз
    # на mapping pass, а не N_claims * N_evidence раз.
    semantic_available = False
    semantic_error = None
    evidence_vectors = {}

    try:
        import requests
        import numpy as np

        session = requests.Session()
        session.trust_env = False

        def _gemma_embed(value: str):
            resp = session.post(
                "http://127.0.0.1:11434/api/embed",
                json={
                    "model": "embeddinggemma:latest",
                    "input": value[:2000],
                },
                timeout=30,
            )
            resp.raise_for_status()

            vec = np.array(
                resp.json()["embeddings"][0],
                dtype=np.float32,
            )

            norm = np.linalg.norm(vec)
            return vec / norm if norm > 0 else vec

        # P1.2 (YANDI_FULL_PIPELINE_AUDIT.md, §26/§33): этот цикл
        # раньше не имел собственного timing вообще — только
        # print-only диагностика, без изменения логики/данных.
        import time as _time
        _embed_t0 = _time.time()

        for ev in evidence_texts:
            ev_text = ev.get("text", "")
            if not ev_text:
                continue

            evidence_vectors[ev["id"]] = _gemma_embed(ev_text)

        semantic_available = bool(evidence_vectors)

        print(
            f"[Evidence Mapper Timing] "
            f"evidence={len(evidence_texts)} "
            f"embedded={len(evidence_vectors)} "
            f"time={_time.time() - _embed_t0:.2f}s"
        )

    except Exception as exc:
        semantic_available = False
        semantic_error = str(exc)

        print(
            f"[Evidence Mapper] embedding unavailable: "
            f"{semantic_error}"
        )

    # Каждый accepted claim проходит mapping.
    for claim in claims:
        claim_text = claim.get("claim_text", "")
        if not claim_text:
            continue

        matched_ids = []

        # ----------------------------------------------------
        # PRIMARY: embeddings
        # ----------------------------------------------------
        if semantic_available:
            try:
                claim_vec = _gemma_embed(claim_text)

                all_scores = []

                for ev in evidence_texts:
                    ev_id = ev["id"]

                    # ================================================
                    # CLAIM-SPECIFIC OWNERSHIP GATE
                    # ================================================
                    #
                    # Evidence из initial retrieval остаётся общим.
                    #
                    # Evidence из claim-specific retrieval может быть
                    # candidate только для claim, ради которого оно
                    # было найдено.
                    #
                    # Ownership != support.
                    # После этого gate NLI всё равно независимо решает:
                    #
                    #   supports / contradicts / uncertain / unrelated
                    #
                    retrieval_origin = ev.get(
                        "retrieval_origin",
                        "",
                    )
                    retrieval_claim_id = ev.get(
                        "retrieval_claim_id",
                        "",
                    )

                    if (
                        retrieval_origin == "claim_specific"
                        and retrieval_claim_id
                        and retrieval_claim_id
                        != claim.get("claim_id")
                    ):
                        continue

                    ev_vec = evidence_vectors.get(ev_id)

                    if ev_vec is None:
                        continue

                    similarity = float(
                        np.dot(claim_vec, ev_vec)
                    )

                    all_scores.append({
                        "score": similarity,
                        "id": ev_id,
                        "title": ev.get("title", ""),
                        "source": ev.get("source", ""),
                    })

                all_scores.sort(
                    key=lambda item: item["score"],
                    reverse=True,
                )

                # Диагностика показывает реальный score distribution.
                # Threshold пока намеренно НЕ меняем.
                top_debug = all_scores[:3]

                if top_debug:
                    debug_text = " | ".join(
                        (
                            f"{item['score']:.3f}:"
                            f"{item['title'][:45] or item['source'][:45]}"
                        )
                        for item in top_debug
                    )

                    print(
                        f"[Mapper Score] "
                        f"claim={claim.get('claim_id', 'unknown')} "
                        f"top={debug_text}"
                    )

                # ====================================================
                # CANDIDATE SELECTION POLICY
                # ====================================================
                #
                # Mapper — retrieval/candidate generator, а НЕ NLI.
                #
                # Поэтому для лучшего кандидата используем более мягкий
                # порог, чтобы не терять потенциально правильный evidence
                # на границе embedding similarity.
                #
                # Второй кандидат допускается только при более сильной
                # семантической близости, чтобы не раздувать число NLI
                # сравнений.
                #
                # ВАЖНО:
                # candidate link != support.
                # Финальное отношение определяет только NLI.
                PRIMARY_CANDIDATE_THRESHOLD = 0.35
                SECONDARY_CANDIDATE_THRESHOLD = 0.45

                matched_ids = []

                if all_scores:
                    best = all_scores[0]

                    if (
                        best["score"]
                        >= PRIMARY_CANDIDATE_THRESHOLD
                    ):
                        matched_ids.append(
                            best["id"]
                        )

                    if len(all_scores) > 1:
                        second = all_scores[1]

                        if (
                            second["score"]
                            >= SECONDARY_CANDIDATE_THRESHOLD
                        ):
                            matched_ids.append(
                                second["id"]
                            )

            except Exception as exc:
                print(
                    f"[Evidence Mapper] claim embedding error "
                    f"claim={claim.get('claim_id', 'unknown')} "
                    f"error={exc}"
                )
                matched_ids = []

        # ----------------------------------------------------
        # FALLBACK: lexical
        # ----------------------------------------------------
        if not semantic_available:
            claim_words = {
                w
                for w in re.findall(
                    r"[а-яёa-z0-9]+",
                    claim_text.lower(),
                )
                if len(w) > 4
            }

            lexical_matches = []

            for ev in evidence_texts:

                # Claim-specific ownership действует и при lexical
                # fallback, иначе при отказе embedding pipeline
                # cross-contamination появится снова.
                retrieval_origin = ev.get(
                    "retrieval_origin",
                    "",
                )
                retrieval_claim_id = ev.get(
                    "retrieval_claim_id",
                    "",
                )

                if (
                    retrieval_origin == "claim_specific"
                    and retrieval_claim_id
                    and retrieval_claim_id
                    != claim.get("claim_id")
                ):
                    continue

                ev_text = ev.get("text", "")
                if not ev_text:
                    continue

                ev_words = set(
                    re.findall(
                        r"[а-яёa-z0-9]+",
                        ev_text.lower(),
                    )
                )

                common = claim_words & ev_words

                if len(common) >= 2:
                    lexical_matches.append(
                        (
                            len(common),
                            ev["id"],
                        )
                    )

            lexical_matches.sort(reverse=True)

            matched_ids = [
                ev_id
                for _, ev_id in lexical_matches[:2]
            ]

        mapped_claims.append(
            _to_claim_record(
                claim,
                matched_ids,
            )
        )

    return mapped_claims


def _to_claim_record(claim: Dict, evidence_ids: List[str]) -> ClaimRecord:
    """Преобразовать словарь в ClaimRecord."""
    return ClaimRecord(
        claim_id=claim.get("claim_id", f"cl_{uuid.uuid4().hex[:8]}"),
        claim_text=claim.get("claim_text", ""),
        derived_from_evidence_ids=evidence_ids,
        claim_type=claim.get("claim_type", "factual"),
        claim_confidence=claim.get("claim_confidence", 0.5),
        # Наличие evidence_ids означает только потенциальную привязку,
        # но не поддержку и не проверку истинности.
        verification_status="candidate",
    )


def get_claim_grounding_score(claims: List[ClaimRecord]) -> float:
    """
    Вычислить score привязки claims к evidence.

    Returns:
        0.0 - 1.0
    """
    if not claims:
        return 0.0

    grounded = sum(1 for c in claims if c.derived_from_evidence_ids)
    return grounded / len(claims)


if __name__ == "__main__":
    # Тест
    test_claims = [
        {"claim_id": "cl_001", "claim_text": "Жизнь начинается с оплодотворения."},
        {"claim_id": "cl_002", "claim_text": "Смерть мозга — критерий смерти человека."},
        {"claim_id": "cl_003", "claim_text": "Происхождение жизни изучается через абиогенез."},
    ]

    test_evidence = [
        {
            "evidence_id": "ev_001",
            "content_excerpt": "Оплодотворение — это процесс слияния половых клеток. Зигота получает уникальную генетическую информацию.",
        },
        {
            "evidence_id": "ev_002",
            "content_excerpt": "Смерть мозга определяется как необратимое прекращение всех функций головного мозга.",
        },
    ]

    result = map_claims_to_evidence(test_claims, test_evidence)
    for r in result:
        print(f"{r.claim_text[:50]}... → evidence: {r.derived_from_evidence_ids}")
    print(f"Grounding score: {get_claim_grounding_score(result)}")
