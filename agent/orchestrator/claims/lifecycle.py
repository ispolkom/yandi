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

import time
import uuid

from agent.evidence_pool import build_canonical_evidence_pool, merge_evidence
from agent.claim_identity import compute_claim_content_hash
from agent.source_clustering import assign_source_clusters
from agent.claim_family_registry import get_claim_family_registry


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

    # Epistemic Core v1 Phase 6: source-independence cluster metadata.
    # Computed here, not read by any status/trust logic this phase — see
    # agent/source_clustering.py's module docstring.
    assign_source_clusters(evidence_data, log=log, verbose=verbose)

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

        # Epistemic Core v1 Phase 2: deterministic content identity,
        # alongside (not instead of) the random occurrence claim_id above.
        # See agent/claim_identity.py for the exact canonicalization
        # policy and why this is NOT semantic/paraphrase identity.
        if "content_hash" not in claim:
            claim["content_hash"] = compute_claim_content_hash(
                claim.get("claim_text", "")
            )

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


def assign_claim_family_identity(
    claims_data,
    epistemic_result,
    is_subjective_answer,
    cost,
    log,
    verbose,
):
    """
    P7 (Этап 4C, Finding A/B fix): Epistemic Core v1 Phase 10 cross-
    request claim linking (agent.claim_family_registry.ClaimFamilyRegistry
    .find_or_link_claim), extracted verbatim from the block that used to
    live inside update_beliefs_link_answer_and_personality_cycle() —
    same registry call, same domain derivation, same try/except, same
    cost bucket name. Two changes, both scoped exactly to this INSPECT's
    findings:

    Finding A (ordering): this function must be called BEFORE
    finalize_claim_trace_and_grounding() (which does trace.add_claim_raw
    -> persist_verification_evidence -> index_trace), not after — so
    claim["semantic_family_id"] actually exists by the time the claim is
    persisted. No belief/personality/linker logic moved — those stay
    exactly where they were, in update_beliefs_link_answer_and_
    personality_cycle(), unchanged, still running after trace
    finalization same as before.

    Finding B (coverage): the [:3] cap was IDENTITY-scoped ("claims 4+
    never get a family_id at all"), not a belief-update-cost decision of
    its own — it was borrowed from the belief-update loop's cap, which
    exists for a DIFFERENT reason (bounded LLM/network cost for belief
    writes) and stays untouched at its own call site. Identity coverage
    must not depend on a claim's position in the list, so this function
    processes ALL claims_data, not claims_data[:3].

    Mutates claims_data in place (claim["semantic_family_id"]) and
    cost["claim_family_link_ms"]. Returns nothing.

    P9 (Этап 4D-2 §5, intra-request mutation preserved): claims are
    still processed ONE AT A TIME, sequentially — each call sees the
    registry state left by the PREVIOUS claim in this same request, so
    claim #5 can still land in a family claim #1 just created. NOT
    batched across claims_data — only each individual find_or_link_claim
    call's OWN embedding step is now batched internally (against that
    claim's candidate families), per Этап 4D-2's scope.
    """
    if not claims_data:
        return

    _t0_family_link = time.time()
    _stats = {}

    try:
        registry = get_claim_family_registry()
        domain = (
            epistemic_result.domain
            if not is_subjective_answer
            else "subjective"
        )
        for claim in claims_data:
            claim_text = (claim.get("claim_text") or "").strip()
            claim_id = claim.get("claim_id")
            if not claim_text or not claim_id:
                continue
            family_id = registry.find_or_link_claim(
                claim_text, claim_id, domain, log=log, verbose=verbose, stats=_stats,
            )
            claim["semantic_family_id"] = family_id
    except Exception as e:
        if verbose:
            log(f"[Claim Family] Ошибка линковки семей: {e}")

    cost["claim_family_link_ms"] = (time.time() - _t0_family_link) * 1000

    # P9 (Этап 4D-2 §10): minimal observability — the whole point of
    # this patch is to stop having this cost hide inside [PROFILE]'s
    # "unaccounted" bucket (Этап 4C found 157s/10 claims there).
    print(
        f"[FamilyIdentity] claims={_stats.get('claims', 0)} "
        f"exact_hits={_stats.get('exact_hits', 0)} "
        f"embed_batches={_stats.get('embed_batches', 0)} "
        f"embedded_texts={_stats.get('embedded_texts', 0)} "
        f"prefilter_candidates={_stats.get('prefilter_candidates', 0)} "
        f"llm_judges={_stats.get('llm_judges', 0)} "
        f"linked={_stats.get('linked', 0)} "
        f"created={_stats.get('created', 0)} "
        f"wall={cost['claim_family_link_ms'] / 1000:.2f}s"
    )


def update_beliefs_link_answer_and_personality_cycle(
    claims_data,
    synthesis_result,
    epistemic_result,
    is_subjective_answer,
    belief_manager,
    claim_answer_linker,
    personality_core,
    cost,
    log,
    verbose,
):
    """
    Extracted from agent/orchestrator_v2.py [8] ("---- YANDI V6: BELIEFS
    ----" / "---- YANDI V6: LINKER ----" / "---- YANDI V6: PERSONALITY
    ----" blocks, which run right after Claim↔Claim NLI grounding scores
    are computed and right before "---- YANDI V6: DISAGREEMENT ----").

    Belief != истина. Источник истины для evidence_for/evidence_against:
    claim["evidence_relations"] (relation общего main_claim здесь больше
    НЕ используется). BeliefManager получает только DIRECT evidence,
    прошедшие Source Quality Gate — secondary/context/internal могут
    храниться в trace, но не имеют права напрямую двигать belief.

    Mutates cost["belief_update_ms"] in place. Returns supporting_ids
    (claim IDs the linker connected to synthesis_result.answer) — the one
    value read by later pipeline phases.
    """
    _t0_belief_update = time.time()

    if belief_manager and claims_data:
        try:
            belief_updates_count = 0

            for claim in claims_data[:3]:
                claim_text = (claim.get("claim_text") or "").strip()

                if not claim_text or len(claim_text) <= 20:
                    continue

                evidence_relations = list(
                    claim.get("evidence_relations", []) or []
                )

                # BeliefManager получает только DIRECT evidence,
                # прошедшие Source Quality Gate.
                #
                # secondary/context/internal могут храниться в trace,
                # но не имеют права напрямую двигать belief.
                belief_relations = [
                    rel
                    for rel in evidence_relations
                    if rel.get("evidence_role") == "direct"
                    and rel.get("evidence_eligible") is True
                ]

                evidence_for = [
                    rel.get("evidence_id")
                    for rel in belief_relations
                    if rel.get("relation") == "supports"
                    and rel.get("evidence_id")
                ]

                evidence_against = [
                    rel.get("evidence_id")
                    for rel in belief_relations
                    if rel.get("relation") == "contradicts"
                    and rel.get("evidence_id")
                ]

                # uncertain / unrelated / missing relation
                # не считаются ни поддержкой, ни опровержением.

                belief_confidence = min(
                    float(claim.get("claim_confidence", 0.5)),
                    0.5,
                )

                if evidence_against and not evidence_for:
                    belief_confidence = min(
                        belief_confidence,
                        0.35,
                    )

                belief_manager.add_belief(
                    topic=epistemic_result.domain
                    if not is_subjective_answer
                    else "subjective",
                    statement=claim_text[:200],
                    confidence=belief_confidence,
                    evidence_for=evidence_for,
                    evidence_against=evidence_against,
                    claim_ids=[claim.get("claim_id")],
                )

                belief_updates_count += 1

                if verbose:
                    log(
                        f"[Belief] candidate={claim.get('claim_id')} "
                        f"for={len(evidence_for)} "
                        f"against={len(evidence_against)} "
                        f"conf={belief_confidence:.2f}"
                    )

            if verbose:
                stats = belief_manager.get_stats()
                log(
                    f"[V6] Beliefs обработано: {belief_updates_count}, "
                    f"всего в памяти: {stats['total']}"
                )

        except Exception as e:
            belief_updates_count = 0
            if verbose:
                log(f"[V6] Ошибка добавления убеждений: {e}")

    cost["belief_update_ms"] = (time.time() - _t0_belief_update) * 1000

    if verbose:
        log(
            f"[Belief Update Timing] "
            f"candidates={min(len(claims_data), 3) if claims_data else 0} "
            f"total={cost['belief_update_ms'] / 1000:.2f}s"
        )

    # Epistemic Core v1 Phase 10 (cross-request claim linking) moved out
    # of this function — P7 (Этап 4C, Finding A): it used to run HERE,
    # AFTER finalize_claim_trace_and_grounding() had already called
    # trace.add_claim_raw()/persist_verification_evidence()/index_trace(),
    # so claim["semantic_family_id"] was never set in time to reach the
    # persisted Trace or claim_verification_index — confirmed on live
    # data (207/210 real claims had semantic_family_id=null; the only 3
    # that didn't came through the cache-hit path in orchestrator/
    # pipeline.py, a different call site entirely). See
    # assign_claim_family_identity() below, now called BEFORE
    # finalize_claim_trace_and_grounding() in orchestrator_v2.py.

    # ---- YANDI V6: LINKER ----
    supporting_ids = []
    if claim_answer_linker:
        try:
            _, supporting_ids = claim_answer_linker.link_answer_to_claims(
                answer=synthesis_result.answer,
                claims=claims_data,
            )
            if verbose and supporting_ids:
                log(f"[V6] Связано claims: {len(supporting_ids)}")
        except Exception as e:
            if verbose:
                log(f"[V6] Ошибка линковки: {e}")

    # ---- YANDI V6: PERSONALITY ----
    if personality_core:
        try:
            personality_core.increment_cycles()
            personality_core.increment_decisions()
            if verbose:
                summary = personality_core.get_summary()
                log(f"[V6] Личность: {summary['name']}, циклов {summary['cycles']}")
        except Exception as e:
            if verbose:
                log(f"[V6] Ошибка личности: {e}")

    return supporting_ids
