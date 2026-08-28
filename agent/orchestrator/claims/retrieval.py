"""
Claim Resolution Gate + second (claim-specific) retrieval pass — extracted
from agent/orchestrator_v2.py [8] ("CLAIM RESOLUTION GATE + SECOND
RETRIEVAL PASS" / "CLAIM-SPECIFIC RETRIEVAL — SECOND PASS" blocks).

Structural extraction only: no gate semantics, retrieval triggering
conditions, mapper re-run logic, or log markers changed. Thin orchestration
around agent.claim_evidence_retriever.retrieve_for_claims,
agent.claim_evidence_mapper.map_claims_to_evidence,
agent.evidence_pool.merge_evidence, and
agent.orchestrator.claims.mapping.run_claim_evidence_batch (PASS2) — this
module does not reimplement any of their logic.

Первый Mapper + Claim↔Evidence NLI (PASS1) уже выполнены до входа сюда.

Теперь можно отличить:
    semantic candidate link
от:
    epistemically effective evidence.

Claim считается resolved только если существует хотя бы одно
DIRECT + ELIGIBLE evidence с отношением: supports | contradicts.
uncertain / unrelated / context / secondary не останавливают
claim-specific retrieval.
"""

import time

from agent.claim_evidence_mapper import map_claims_to_evidence
from agent.claim_evidence_retriever import retrieve_for_claims
from agent.evidence_pool import merge_evidence
from agent.orchestrator.claims.mapping import run_claim_evidence_batch
from agent.source_clustering import assign_source_clusters


def _claim_has_effective_evidence(claim):
    """
    P5 (verification memory, P4 §10): a relation derived from historical
    memory evidence (rel["from_memory"] is True) must NOT, by itself,
    make a claim look "already resolved" and skip PASS2 — LOCAL MEMORY
    is one comparison channel, not a reason to stop looking for NEW
    evidence this cycle. A fresh (non-memory) direct+eligible supports/
    contradicts relation still resolves the claim exactly as before —
    this is the one added condition, not a rewritten gate.
    """
    for rel in claim.get("evidence_relations", []) or []:
        if (
            rel.get("evidence_role") == "direct"
            and rel.get("evidence_eligible") is True
            and rel.get("relation") in {
                "supports",
                "contradicts",
            }
            and not rel.get("from_memory")
        ):
            return True

    return False


def apply_claim_resolution_and_second_retrieval(
    claims_data,
    evidence_data,
    enable_web,
    is_subjective_answer,
    skip_rag,
    request_fetch_cache,
    cost,
    log,
    verbose,
):
    """
    Mutates claims_data items in place (derived_from_evidence_ids,
    evidence_relations via PASS2 NLI) and writes cost["claim_retrieval_ms"]
    / cost["claim_pass2_mapping_nli_ms"] when the retrieval pass actually
    runs.

    Returns evidence_data — reassigned (via merge_evidence) only if the
    retrieval pass ran; returned unchanged otherwise (gate not triggered,
    or an exception occurred before the reassignment — matching the
    original inline try/except exactly).
    """
    retrieval_claims = [
        claim
        for claim in claims_data
        if claim.get("verification_status") != "rejected"
        and not _claim_has_effective_evidence(claim)
    ]

    # Epistemic Core v1 Phase 3: search-outcome disambiguation. Does NOT
    # change verification_status or its vocabulary — these are companion
    # fields answering a different question ("was a search even
    # attempted for this claim, and did it error"), not "what did the
    # search find". A claim already resolved via PASS1 (not in
    # retrieval_claims) is left at None/None — PASS2 simply isn't
    # applicable to it, this function has no opinion on PASS1's own
    # (query-wide, not per-claim) search attempt.
    _retrieval_claim_ids = {
        c.get("claim_id") for c in retrieval_claims if c.get("claim_id")
    }
    for claim in claims_data:
        if claim.get("claim_id") in _retrieval_claim_ids:
            # Needs PASS2. Defaults to "gate blocked it" (False); flipped
            # to True below only if the retrieval call actually runs.
            claim["evidence_search_attempted"] = False
            claim["evidence_search_error"] = None
        else:
            claim["evidence_search_attempted"] = None
            claim["evidence_search_error"] = None

    if verbose:
        resolved_count = (
            len(claims_data) - len(retrieval_claims)
        )

        log(
            f"[Claim Resolution Gate] "
            f"claims={len(claims_data)} "
            f"resolved={resolved_count} "
            f"need_retrieval={len(retrieval_claims)}"
        )

    # ============================================================
    # CLAIM-SPECIFIC RETRIEVAL — SECOND PASS
    # ============================================================

    if (
        enable_web
        and retrieval_claims
        and not skip_rag
        and not is_subjective_answer
    ):
        try:
            # P1.2 (YANDI_FULL_PIPELINE_AUDIT.md, §26/§33):
            # эта фаза раньше не попадала в [PROFILE] вообще,
            # хотя в реальном прогоне занимала 47% total latency
            # ([Claim Retrieval Timing] wall=240.74s из 509.74s).
            _claim_retrieval_t0 = time.time()

            # Phase 3: retrieve_for_claims() is one batch call for all of
            # retrieval_claims — mark them attempted up front, since
            # reaching this line means the HTTP/search attempt is really
            # being made for all of them (not per-claim granularity;
            # matches what the code actually does, not an invented finer
            # signal).
            for claim in retrieval_claims:
                claim["evidence_search_attempted"] = True

            claim_retrieved_evidence = retrieve_for_claims(
                retrieval_claims,
                fetch_cache=request_fetch_cache,
            )

            cost["claim_retrieval_ms"] = (
                (time.time() - _claim_retrieval_t0) * 1000
            )

            evidence_before = len(evidence_data)

            evidence_data = merge_evidence(
                evidence_data,
                claim_retrieved_evidence,
            )

            added_count = (
                len(evidence_data) - evidence_before
            )

            # Epistemic Core v1 Phase 6: recompute cluster metadata over
            # the now-larger pool. Metadata only — see
            # agent/source_clustering.py's module docstring.
            if added_count > 0:
                assign_source_clusters(evidence_data, log=log, verbose=verbose)

            if verbose:
                log(
                    f"[Claim Retrieval Pass 2] "
                    f"requested={len(retrieval_claims)} "
                    f"returned={len(claim_retrieved_evidence)} "
                    f"added={added_count} "
                    f"evidence_total={len(evidence_data)}"
                )

            # ====================================================
            # SECOND MAPPER + NLI PASS
            # ====================================================
            #
            # Выполняем только если retrieval действительно
            # расширил canonical evidence pool.
            #
            # Mapper снова остаётся единственным владельцем
            # derived_from_evidence_ids.
            # ----------------------------------------------------

            if added_count > 0:

                _t0_pass2_mapping_nli = time.time()

                # P1-A (YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md §3,
                # §19 item 1): PASS2 mapping/NLI must only touch the
                # claims that actually triggered this retrieval pass
                # (retrieval_claims) — claims already resolved at PASS1
                # (effective direct+eligible supports/contradicts
                # evidence) had no reason to be re-mapped or re-scored.
                # Re-running NLI on an already-resolved claim is pure
                # waste (nothing new was fetched for it) AND a proven
                # correctness hazard: live_run.log showed cl_afff1e70,
                # resolved PASS1 with relation=supports, silently
                # flip to relation=uncertain against the SAME evidence
                # on this redundant re-run — an already-resolved
                # claim's relation must never change without a new
                # reason (new evidence actually retrieved for it).
                # Scoping to retrieval_claims here (not claims_data)
                # is the actual fix — resolved claims are simply never
                # passed into either function again, so their
                # derived_from_evidence_ids / evidence_relations /
                # verification_status / source_cluster references from
                # PASS1 are left completely untouched.
                mapped_claims = map_claims_to_evidence(
                    retrieval_claims,
                    evidence_data,
                )

                mapped_by_id = {
                    mc.claim_id: mc
                    for mc in mapped_claims
                    if getattr(mc, "claim_id", None)
                }

                for claim in retrieval_claims:
                    claim_id = claim.get("claim_id")
                    mapped = mapped_by_id.get(claim_id)

                    if mapped is None:
                        claim["derived_from_evidence_ids"] = []
                        continue

                    claim["derived_from_evidence_ids"] = list(
                        mapped.derived_from_evidence_ids or []
                    )

                # -----------------------------------------------
                # CLAIM <-> EVIDENCE NLI — PASS 2
                # -----------------------------------------------
                #
                # После второго retrieval Mapper уже обновил
                # derived_from_evidence_ids — только для
                # retrieval_claims (см. комментарий выше).
                #
                # Повторяем NLI через тот же batch helper, тоже
                # только для retrieval_claims.
                claim_relation_count_pass2 = run_claim_evidence_batch(
                    retrieval_claims,
                    evidence_data,
                    "PASS2",
                    log,
                    verbose,
                )

                cost["claim_pass2_mapping_nli_ms"] = (
                    (time.time() - _t0_pass2_mapping_nli) * 1000
                )

                # ====================================================
                # PASS 2 TRACE
                # ====================================================
                #
                # Диагностика остаётся отдельно от NLI execution.
                # Здесь ничего не классифицируется повторно.
                if verbose:
                    evidence_by_id = {
                        ev.get("evidence_id"): ev
                        for ev in (evidence_data or [])
                        if ev.get("evidence_id")
                    }

                    for claim in claims_data:
                        claim_text = (
                            claim.get("claim_text") or ""
                        ).strip()

                        linked_ids = list(
                            claim.get(
                                "derived_from_evidence_ids",
                                [],
                            ) or []
                        )

                        evidence_relations = list(
                            claim.get(
                                "evidence_relations",
                                [],
                            ) or []
                        )

                        log(
                            f"[Pass2 Trace] "
                            f"claim={claim.get('claim_id', 'unknown')} "
                            f"linked={len(linked_ids)} "
                            f"relations={len(evidence_relations)} "
                            f"text={claim_text[:140]}"
                        )

                        relation_by_evidence = {
                            rel.get("evidence_id"): rel
                            for rel in evidence_relations
                            if rel.get("evidence_id")
                        }

                        for ev_id in linked_ids:
                            ev = evidence_by_id.get(ev_id)

                            if not ev:
                                log(
                                    f"[Pass2 Trace] "
                                    f"  ev={ev_id} MISSING"
                                )
                                continue

                            rel = relation_by_evidence.get(
                                ev_id,
                                {},
                            )

                            log(
                                f"[Pass2 Trace] "
                                f"  ev={ev_id} "
                                f"role={ev.get('evidence_role', 'context')} "
                                f"eligible={ev.get('evidence_eligible', False)} "
                                f"quality={ev.get('quality_score', 0.0):.3f} "
                                f"relation={rel.get('relation', 'NO_RELATION')} "
                                f"method={rel.get('method', 'unknown')} "
                                f"class={ev.get('source_class', 'unknown')} "
                                f"owner={ev.get('retrieval_claim_id', '')} "
                                f"url={ev.get('source_uri', '')[:180]}"
                            )

                            log(
                                f"[Pass2 Trace] "
                                f"    source_claim="
                                f"{rel.get('source_claim', '')[:350]}"
                            )

                            log(
                                f"[Pass2 Trace] "
                                f"    excerpt="
                                f"{(ev.get('content_excerpt') or '')[:500]}"
                            )

                if verbose:
                    log(
                        f"[Claim Evidence NLI Pass 2] "
                        f"relations classified="
                        f"{claim_relation_count_pass2}"
                    )

        except Exception as e:
            # Phase 3: ERROR != NOT FOUND. The batch call itself failed
            # (network/timeout/etc) — every claim that was part of this
            # attempt gets that recorded, distinct from "attempted,
            # found nothing" (evidence_search_error is None in that case).
            for claim in retrieval_claims:
                claim["evidence_search_error"] = str(e)

            if verbose:
                log(
                    f"[Claim Retrieval Pass 2] error={e}"
                )

    return evidence_data
