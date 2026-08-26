"""
Claim <-> evidence relation NLI batch — extracted from
agent/orchestrator_v2.py's nested closure `_run_claim_evidence_batch`
(defined inside process(), called at two sites: PASS1 after evidence
mapping, PASS2 after the second retrieval pass).

De-closured as a standalone, explicit-dependency function. Structural
extraction only — no mapper/NLI semantics, no PASS1/PASS2 order, no
evidence ownership, no log markers, no batch_size, no concurrency changed.

Free-variable audit performed before this move (documented in the
extraction commit): the closure captured exactly two names from
process()'s enclosing scope — `verbose` and `log` — both now explicit
parameters. Everything else it used (`time`, `evaluate_evidence_directness`,
`classify_claim_evidence_batch`) resolved via orchestrator_v2.py's
module-level imports and is re-imported here identically; these are not
closure captures in any meaningful sense (same object, same names, would
resolve the same way regardless of which module the function lives in).

Mutates each claim in `claims` in place: writes claim["evidence_relations"].
Never mutates `evidence`. Returns the total relation count written.
"""

import time

from agent.claim_relation import classify_claim_evidence_batch
from agent.claim_evidence_mapper import map_claims_to_evidence, get_claim_grounding_score
from agent.source_quality import evaluate_evidence_directness


def run_claim_evidence_mapping_pass1(claims_data, evidence_data, log, verbose):
    """
    Evidence mapping PASS1 — extracted from agent/orchestrator_v2.py [8]
    ("EVIDENCE MAPPING" block, immediately before the PASS1 NLI batch
    call). Thin wrapper around agent.claim_evidence_mapper.map_claims_to_evidence
    / get_claim_grounding_score — does not reimplement mapper logic.

    Mapper видит initial evidence + claim-specific evidence, но сам решает
    semantic candidate links. map_claims_to_evidence() — единственный
    компонент, который имеет право назначать derived_from_evidence_ids;
    важно вернуть результат mapping обратно в claims_data, иначе trace
    видел бы правильные связи, а Validator/BeliefManager/Linker продолжали
    бы работать со старой версией claims.

    Mutates each claim in claims_data in place (derived_from_evidence_ids,
    verification_status). Returns semantic_grounding_score.
    """
    mapped_claims = map_claims_to_evidence(
        claims_data,
        evidence_data,
    )

    # ------------------------------------------------------------
    # CLAIM <-> EVIDENCE SINGLE SOURCE OF TRUTH
    # ------------------------------------------------------------
    #
    # map_claims_to_evidence() — единственный компонент,
    # который имеет право назначать derived_from_evidence_ids.
    #
    # Важно вернуть результат mapping обратно в claims_data.
    # Иначе trace видел бы правильные связи, а Validator,
    # BeliefManager и Linker продолжали бы работать со старой
    # версией claims.
    # ------------------------------------------------------------

    mapped_by_id = {
        mc.claim_id: mc
        for mc in mapped_claims
        if getattr(mc, "claim_id", None)
    }

    for claim in claims_data:
        claim_id = claim.get("claim_id")
        mapped = mapped_by_id.get(claim_id)

        if mapped is None:
            # Если mapper не смог обработать claim,
            # связь не выдумываем.
            claim["derived_from_evidence_ids"] = []
            claim["verification_status"] = "candidate"
            continue

        claim["derived_from_evidence_ids"] = list(
            mapped.derived_from_evidence_ids or []
        )

        # candidate означает:
        # evidence тематически привязан, но истинность claim
        # ещё НЕ установлена.
        claim["verification_status"] = (
            mapped.verification_status or "candidate"
        )

    # ВАЖНО:
    # mapped_claims здесь ещё имеют промежуточный status=candidate.
    # В trace они будут записаны ПОСЛЕ Claim Evidence NLI
    # и вычисления окончательного epistemic status.

    semantic_grounding_score = get_claim_grounding_score(
        mapped_claims
    )

    mapped_with_evidence = sum(
        1
        for mc in mapped_claims
        if getattr(mc, "derived_from_evidence_ids", None)
    )

    total_candidate_links = sum(
        len(getattr(mc, "derived_from_evidence_ids", None) or [])
        for mc in mapped_claims
    )

    log(
        f"[Evidence Mapper] "
        f"claims={len(claims_data)}, "
        f"processed={len(mapped_claims)}, "
        f"linked_claims={mapped_with_evidence}, "
        f"candidate_links={total_candidate_links}, "
        f"semantic_grounding={semantic_grounding_score:.2f}"
    )

    return semantic_grounding_score


def run_claim_evidence_batch(claims, evidence, batch_label, log, verbose):
    evidence_by_id_local = {
        ev.get("evidence_id"): ev
        for ev in (evidence or [])
        if ev.get("evidence_id")
    }

    jobs = []

    for claim in claims:
        claim_text = (
            claim.get("claim_text") or ""
        ).strip()

        if not claim_text:
            claim["evidence_relations"] = []
            continue

        linked_ids = list(
            claim.get(
                "derived_from_evidence_ids",
                [],
            ) or []
        )

        candidate_sources = []

        for ev_id in linked_ids:
            ev = evidence_by_id_local.get(ev_id)

            if not ev:
                continue

            ev_text = (
                ev.get("content_excerpt") or ""
            ).strip()

            if not ev_text:
                continue

            # P0-F: directness — НЕЗАВИСИМЫЙ от source authority
            # сигнал "насколько конкретно ЭТОТ passage отвечает
            # ЭТОМУ claim". Считается здесь (а не в
            # source_quality.py), потому что evaluate_source_quality()
            # вызывается ДО того, как claim, к которому evidence
            # привяжется, вообще известен (PASS1 evidence общий
            # для многих claims) — сама evidence-запись не может
            # нести per-claim directness, только per-pair.
            directness = evaluate_evidence_directness(
                claim_text,
                ev_text,
            )

            if verbose:
                _reason_bits = []
                if ev.get("evidence_role") == "direct" and ev.get("evidence_eligible") is True:
                    _reason_bits.append("authority_eligible")
                if directness >= 0.60:
                    _reason_bits.append("directness_strong")
                if not _reason_bits:
                    _reason_bits.append("neither_path_qualifies")

                log(
                    "[Evidence Eligibility] "
                    f"claim={claim.get('claim_id', '')} "
                    f"ev={ev_id} "
                    f"source_class={ev.get('source_class', 'unknown')} "
                    f"quality={ev.get('quality_score', 0.0):.3f} "
                    f"directness={directness:.3f} "
                    f"role={ev.get('evidence_role', 'context')} "
                    f"eligible={ev.get('evidence_eligible', False)} "
                    f"reason={'+'.join(_reason_bits)}"
                )

            candidate_sources.append({
                "evidence_id": ev_id,
                "type": ev.get(
                    "source_type",
                    "evidence",
                ),
                "text": ev_text,
                "url": ev.get(
                    "source_uri",
                    "",
                ),
                "source_class": ev.get(
                    "source_class",
                    "unknown",
                ),
                "quality_score": ev.get(
                    "quality_score",
                    0.0,
                ),
                "evidence_eligible": ev.get(
                    "evidence_eligible",
                    False,
                ),
                "evidence_role": ev.get(
                    "evidence_role",
                    "context",
                ),
                # P0-E: registry evidence — прошлые UNVERIFIED
                # ответы самой модели (см. отчёт), не внешняя
                # provenance. Directness-путь ниже обязан их
                # исключать явно, иначе получится circular
                # self-validation.
                "retrieval_origin": ev.get(
                    "retrieval_origin",
                    "",
                ),
                "directness": directness,
                "relevance": "relevant",
            })

        jobs.append({
            "claim_id": claim.get(
                "claim_id",
                "",
            ),
            "claim_text": claim_text,
            "sources": candidate_sources,
        })

    started = time.time()

    classified = classify_claim_evidence_batch(
        jobs,
        batch_size=8,
    )

    elapsed = time.time() - started

    relation_count = 0

    for claim in claims:
        claim_id = claim.get(
            "claim_id",
            "",
        )

        grouped = classified.get(
            claim_id,
            {},
        )

        evidence_relations = []

        for relation in (
            "supports",
            "contradicts",
            "uncertain",
            "unrelated",
        ):
            for source in (
                grouped.get(
                    relation,
                    [],
                ) or []
            ):
                ev_id = source.get(
                    "evidence_id"
                )

                if not ev_id:
                    continue

                evidence_relations.append({
                    "evidence_id": ev_id,
                    "relation": relation,
                    "method": source.get(
                        "relation_method",
                        "unknown",
                    ),
                    "source_claim": source.get(
                        "source_claim",
                        "",
                    ),
                    "error": source.get(
                        "relation_error",
                    ),
                    "source_class": source.get(
                        "source_class",
                        "unknown",
                    ),
                    "quality_score": source.get(
                        "quality_score",
                        0.0,
                    ),
                    "evidence_eligible": source.get(
                        "evidence_eligible",
                        False,
                    ),
                    "evidence_role": source.get(
                        "evidence_role",
                        "context",
                    ),
                    "retrieval_origin": source.get(
                        "retrieval_origin",
                        "",
                    ),
                    "directness": source.get(
                        "directness",
                        0.0,
                    ),
                })

                relation_count += 1

        claim["evidence_relations"] = (
            evidence_relations
        )

    if verbose:
        pair_count = sum(
            len(job.get("sources", []) or [])
            for job in jobs
        )

        generation_calls = (
            (pair_count + 7) // 8
            if pair_count
            else 0
        )

        log(
            f"[Claim Evidence Batch {batch_label}] "
            f"claims={len(jobs)} "
            f"pairs={pair_count} "
            f"relations={relation_count} "
            f"generation_calls<={generation_calls} "
            f"time={elapsed:.2f}s"
        )

    return relation_count
