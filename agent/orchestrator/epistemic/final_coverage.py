"""
Final Claim Coverage — extracted from agent/orchestrator_v2.py [8]
(FINAL CLAIM COVERAGE block, structural extraction, no behavior change).

Claim lifecycle up to this point validates claims extracted during
synthesis. But the final answer may contain additional factual statements
that never entered the lifecycle at all.

This gate answers only: "what fraction of the final answer's factual claims
did YANDI actually check?" Coverage does not mean truth/support and must
never raise Trust.

Thin wrapper around agent.final_claim_coverage.evaluate_final_claim_coverage
— the real coverage-analysis logic lives there; this module only owns the
orchestration (timing, logging, trace observations, exception fallback)
that orchestrator_v2.py previously inlined.
"""

import time

from agent.final_claim_coverage import evaluate_final_claim_coverage


def evaluate_and_record_final_coverage(synthesis_result, claims_data, query_to_use, cost, trace, log, verbose):
    """
    Returns (final_claim_coverage_score, final_claims_count,
    final_claims_covered, final_claims_uncovered).

    On evaluator exception: returns (1.0, 0, 0, []) — matches the original
    inline behavior, where only the score was reset on error and the count/
    covered/uncovered locals were left at their pre-pipeline-init defaults
    (0, 0, []), which nothing else in the pipeline sets before this point.
    """
    try:
        _t0_final_coverage = time.time()

        final_coverage = evaluate_final_claim_coverage(
            synthesis_result.answer,
            claims_data,
            query=query_to_use,
        )

        cost["final_coverage_ms"] = (
            (time.time() - _t0_final_coverage) * 1000
        )

        final_claim_coverage_score = (
            final_coverage.coverage_score
        )

        final_claims_count = (
            final_coverage.factual_count
        )

        final_claims_covered = (
            final_coverage.covered_count
        )

        final_claims_uncovered = list(
            final_coverage.uncovered_claims
        )

        log(
            "[Final Claim Coverage] "
            f"factual={final_claims_count} "
            f"covered={final_claims_covered} "
            f"uncovered={len(final_claims_uncovered)} "
            f"coverage={final_claim_coverage_score:.2f} "
            f"status={final_coverage.coverage_status}"
        )

        # P0-C (YANDI_FINAL_EPISTEMIC_AUDIT_AND_FIX.md): переиспользует
        # уже вычисленные covered/uncovered — никакой новой extraction
        # machinery. "novel" = factual claims финального ответа, не
        # найденные нигде в claim lifecycle (uncovered); "speculative"
        # = extracted claims (любого типа), которые сам extractor
        # опознал как гипотезу/возможность, а не факт.
        _leakage_speculative = sum(
            1
            for c in final_coverage.final_claims
            if c.get("claim_type") == "speculative"
        )

        log(
            "[Final Claim Leakage] "
            f"extracted={len(final_coverage.final_claims)} "
            f"known={final_claims_covered} "
            f"novel={len(final_claims_uncovered)} "
            f"speculative={_leakage_speculative}"
        )

        if verbose and final_claims_uncovered:
            for uncovered in final_claims_uncovered[:8]:
                log(
                    "[Final Claim Coverage] UNCOVERED: "
                    f"{uncovered.get('claim_text', '')[:180]}"
                )

        trace.add_observation(
            "final_claim_coverage_score",
            final_claim_coverage_score,
        )

        trace.add_observation(
            "final_claims_count",
            final_claims_count,
        )

        trace.add_observation(
            "final_claims_covered",
            final_claims_covered,
        )

        trace.add_observation(
            "final_claims_uncovered",
            len(final_claims_uncovered),
        )

        return (
            final_claim_coverage_score,
            final_claims_count,
            final_claims_covered,
            final_claims_uncovered,
        )

    except Exception as e:
        # Ошибка coverage-анализатора НЕ должна поднимать Trust.
        #
        # Но и не превращаем технический сбой автоматически
        # в epistemic failure существующего pipeline.
        if verbose:
            log(
                f"[Final Claim Coverage] error={e}"
            )

        return (1.0, 0, 0, [])
