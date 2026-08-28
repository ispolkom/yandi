"""
Pipeline wall-clock profile report — extracted from agent/orchestrator_v2.py
[10] (the `if verbose:` PROFILE block at the end of process()).

Pure read of the already-computed `cost{}` timing dict and
`_request_fetch_cache.summary()`; no pipeline state is mutated here, only
`log()` output is produced. Structural extraction only — log markers
([PROFILE], [PROFILE BOTTLENECK], [Search Work Audit]) preserved verbatim,
since existing diagnostic tooling (refutation_performance_regression_test.py
and friends) reads this exact shape.
"""


def report_pipeline_profile(cost, total, request_fetch_cache, log, verbose):
    if not verbose:
        return

    profile_items = []

    # Уже существующие timers + новые крупные wall-clock timers.
    profile_keys = [
        # G: personality/character/scene/target/entity/strategy/
        # criticism/boundary pre-processing — раньше НЕ измерялся
        # вообще (0 timing coverage, до "[0] Cache check").
        ("pre_pipeline_personality", "pre_pipeline_ms"),
        ("cache", "cache_ms"),
        ("risk", "risk_ms"),
        ("plan", "plan_ms"),
        ("intent", "intent_ms"),
        ("clarify", "clarify_ms"),
        ("enrich", "enrich_ms"),
        ("registry/web-initial", "registry_ms"),
        ("web", "web_ms"),
        ("refutation", "profile_refutation_ms"),
        ("hypothesis_graph", "profile_hypothesis_graph_ms"),
        ("local_wait", "profile_local_wait_ms"),
        ("blind_analysis", "profile_blind_analysis_ms"),
        ("source_classification", "profile_source_classification_ms"),
        ("synthesize", "synthesize_ms"),
        # P1.2: ранее отсутствовавшая, но реально доминирующая фаза
        # (см. YANDI_FULL_PIPELINE_AUDIT.md §26).
        ("claim_specific_retrieval", "claim_retrieval_ms"),
        # G (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md §G): раньше эти
        # фазы формировали unaccounted=275.87s (43.8% total).
        # claim_setup_ms now covers ONLY structural validation —
        # PASS1 mapping/NLI + PASS2 gate/retrieval/mapping/NLI moved
        # into the bounded async claim pipeline (see
        # claim_async_pipeline below); claim_retrieval_ms/
        # claim_pass2_mapping_nli_ms are no longer set by that
        # pipeline and this table simply omits them now (cost.get()
        # skips absent keys) rather than showing stale zeros.
        ("claim_setup_validator_mapper1_nli1", "claim_setup_ms"),
        ("claim_pass2_mapper_nli", "claim_pass2_mapping_nli_ms"),
        # Async claim pipeline (YANDI_AGENT_RETRIEVAL_PERFORMANCE_
        # AUDIT.md P2 follow-up) — bounded (MAX_CLAIM_WORKERS=3)
        # per-claim PASS1 map/NLI + PASS2 retrieval/map/NLI, replacing
        # the three legacy whole-batch steps above.
        ("claim_async_pipeline", "claim_async_pipeline_ms"),
        ("claim_claim_nli", "claim_claim_nli_ms"),
        ("final_claim_coverage", "final_coverage_ms"),
        # P0 (performance architecture pass): previously untracked
        # entirely — see [Belief Update Timing] instrumentation.
        ("belief_update", "belief_update_ms"),
    ]

    for label, key in profile_keys:
        value = cost.get(key)
        if isinstance(value, (int, float)):
            profile_items.append((label, float(value)))

    profile_items.sort(
        key=lambda item: item[1],
        reverse=True,
    )

    log("")
    log("=" * 72)
    log("YANDI PIPELINE WALL-CLOCK PROFILE")
    log("=" * 72)

    for label, ms in profile_items:
        pct = (
            (ms / cost["total_ms"]) * 100.0
            if cost.get("total_ms")
            else 0.0
        )

        log(
            f"[PROFILE] "
            f"{label:<24} "
            f"{ms / 1000:>8.2f}s "
            f"{pct:>6.1f}%"
        )

    if profile_items:
        bottleneck_label, bottleneck_ms = profile_items[0]

        log(
            f"[PROFILE BOTTLENECK] "
            f"{bottleneck_label} "
            f"{bottleneck_ms / 1000:.2f}s"
        )

    measured_ms = sum(
        ms
        for _, ms in profile_items
    )

    unaccounted_ms = max(
        0.0,
        cost["total_ms"] - measured_ms,
    )

    log(
        f"[PROFILE] "
        f"measured_sum={measured_ms / 1000:.2f}s "
        f"unaccounted={unaccounted_ms / 1000:.2f}s "
        f"total={total:.2f}s"
    )

    # Refutation performance audit: cumulative view of the ONE
    # request-scoped SharedFetchCache shared across main web
    # scrape(), refutation scrape() and claim-specific
    # retrieve_for_claims() — measures real cross-phase URL
    # overlap (saved = physical fetches avoided), not a guess.
    _fc_summary = request_fetch_cache.summary()
    log(
        f"[Search Work Audit] "
        f"requests={_fc_summary['requests']} "
        f"unique_urls={_fc_summary['unique']} "
        f"network_fetches={_fc_summary['network_fetches']} "
        f"saved={_fc_summary['saved']} "
        f"hit_ratio={_fc_summary['hit_ratio']:.2f}"
    )

    log("=" * 72)
