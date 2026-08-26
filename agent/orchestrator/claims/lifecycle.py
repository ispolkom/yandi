"""
Claim & evidence lifecycle setup — extracted from agent/orchestrator_v2.py
[8] (the "CANONICAL EVIDENCE POOL" + claim-normalization + "CLAIM IDENTITY"
+ "CLAIM QUERY CONTEXT" blocks that run right after synthesize() succeeds,
before structural claim validation).

Structural extraction only: no evidence-ownership rules, claim-id scheme,
or log markers changed. Thin orchestration around
agent.evidence_pool.build_canonical_evidence_pool/merge_evidence — this
module does not reimplement evidence-pool logic.
"""

import uuid

from agent.evidence_pool import build_canonical_evidence_pool, merge_evidence


def setup_claim_and_evidence_lifecycle(
    reasoning_info,
    search_result,
    web_result,
    refutation_snippets,
    query_to_use,
    log,
    verbose,
):
    """
    Returns (trust_report_data, trust_reasons, coverage_report_data,
    claims_data, evidence_data, technical_errors).

    Only called on the synthesis-succeeded path (orchestrator_v2.py's
    caller keeps the `if synthesis_timed_out or synthesis_result is None:
    ... else: <this>` structure intact — on the timeout path these values
    are never computed, matching pre-extraction behavior exactly).
    """
    trust_report_data = reasoning_info.get("trust_report", {})
    trust_reasons = []
    coverage_report_data = reasoning_info.get("coverage_report", {})
    claims_data = reasoning_info.get("claims", [])

    # ====================================================
    # CANONICAL EVIDENCE POOL
    # ====================================================
    #
    # Evidence принадлежит Orchestrator, а не Synthesizer.
    #
    # Synthesizer может вернуть собственные evidence_records,
    # но основной web/local/refutation retrieval уже произошёл
    # раньше и не должен исчезать только потому, что Synthesizer
    # его не протащил.
    synthesizer_evidence = reasoning_info.get(
        "evidence_records",
        [],
    ) or []

    pipeline_evidence = build_canonical_evidence_pool(
        search_result=search_result,
        web_result=web_result,
        refutation_snippets=refutation_snippets,
    )

    evidence_data = merge_evidence(
        pipeline_evidence,
        synthesizer_evidence,
    )

    technical_errors = reasoning_info.get(
        "technical_errors",
        [],
    )

    if verbose:
        direct_count = sum(
            1
            for ev in evidence_data
            if (
                ev.get("evidence_role") == "direct"
                and
                ev.get("evidence_eligible") is True
            )
        )

        context_count = sum(
            1
            for ev in evidence_data
            if ev.get("evidence_role") == "context"
        )

        origins = {}

        for ev in evidence_data:
            origin = ev.get(
                "retrieval_origin",
                "unknown",
            )

            origins[origin] = (
                origins.get(origin, 0) + 1
            )

        log(
            f"[Evidence Pool] "
            f"total={len(evidence_data)} "
            f"direct={direct_count} "
            f"context={context_count} "
            f"origins={origins}"
        )

    if claims_data and evidence_data:
        # ---- NORMALIZE CLAIMS BEFORE EVIDENCE MAPPING ----
        # Synthesizer/claim extractor может вернуть claims как строки
        # или как словари. Mapper ожидает только словари.
        normalized_claims = []
        for claim in claims_data or []:
            if isinstance(claim, dict):
                if "claim_text" not in claim:
                    if "text" in claim:
                        claim["claim_text"] = claim["text"]
                    elif "claim" in claim:
                        claim["claim_text"] = claim["claim"]
                normalized_claims.append(claim)
            elif isinstance(claim, str):
                text = claim.strip()
                if text:
                    normalized_claims.append({
                        "claim_text": text,
                        "source": "synthesizer"
                    })
            else:
                log(f"[Claims] Пропущен claim неизвестного типа: {type(claim).__name__}")
        claims_data = normalized_claims
        log(f"[Claims] Normalized claims: {len(claims_data)}")

    # ============================================================
    # CLAIM IDENTITY
    # ============================================================
    #
    # Claim должен иметь стабильный ID ещё ДО Validator/Mapper.
    # Иначе rejected claim может потерять идентичность, поскольку
    # раньше claim_id иногда создавался только внутри Mapper.
    for claim in claims_data:
        if not claim.get("claim_id"):
            claim["claim_id"] = f"cl_{uuid.uuid4().hex[:8]}"

        if not claim.get("claim_type"):
            claim["claim_type"] = "factual"

        if "claim_confidence" not in claim:
            claim["claim_confidence"] = 0.5

        # ========================================================
        # CLAIM QUERY CONTEXT
        # ========================================================
        #
        # Atomic claim может потерять явный субъект:
        #
        #   query:
        #       "Есть ли разумная жизнь на Юпитере?"
        #
        #   claim:
        #       "Температура варьируется от -145°C..."
        #
        # query_context используется ТОЛЬКО retrieval-слоем
        # для восстановления предметного контекста.
        #
        # Сам claim_text не изменяется и позже именно он
        # проверяется через NLI.
        if not claim.get("query_context"):
            claim["query_context"] = query_to_use

    return (
        trust_report_data,
        trust_reasons,
        coverage_report_data,
        claims_data,
        evidence_data,
        technical_errors,
    )
