"""
Claim epistemic status classification — extracted from
agent/orchestrator_v2.py [8] ("---- CLAIM EPISTEMIC STATUS ----" block).

Structural extraction only: no thresholds, evidence-eligibility rules, or
log markers changed.

Structural validator answers only: "is this a normal claim?"
NLI answers: "what does the related evidence say ABOUT this claim?"
So a claim moves from `candidate` to a more precise epistemic status after
NLI. `supported` != `verified` — one or more agreeing evidence pieces does
not by itself prove the claim true; `verified` is never assigned here.

Note: this block has no cost[] timer wrapping it in the original code (a
documented, known gap — see YANDI_ORCHESTRATOR_MODULARIZATION_MAP.md §6) and
that gap is preserved as-is, not silently fixed.
"""

# ============================================================
# P0-F (YANDI_EVIDENCE_ELIGIBILITY_AND_REGISTRY_AUDIT.md):
# ============================================================
#
# Раньше единственный путь в supports/contradicts был
# role=="direct" AND eligible==True — оба поля производные
# ИСКЛЮЧИТЕЛЬНО от домена источника (source_quality.py), без
# учёта того, насколько КОНКРЕТНО passage отвечает claim.
# Математически доказано: source_class="unknown" (любой домен
# вне узкого whitelist) НЕ может пересечь eligibility threshold
# ни при каком содержимом (max quality_score≈0.655 < 0.70).
#
# Добавлен ВТОРОЙ, независимый путь: directness — насколько
# конкретный passage семантически близок claim (per-pair
# embedding similarity, см. source_quality.py::
# evaluate_evidence_directness). Authority-путь остаётся
# первым и НЕ ослаблен — это дополнение, не замена, не
# понижение порога 0.70/0.55.
#
# HARD_BLOCKED_SOURCE_CLASSES зеркалит source_quality.py's
# blocked_classes — форумы/соцсети/блоги/спекулятивные/
# новостные/pipeline-generated источники остаются исключены
# ДАЖЕ при высокой directness (тема НЕ авторитетность).
HARD_BLOCKED_SOURCE_CLASSES = {
    "generated_pipeline",
    "social",
    "forum",
    "blog_opinion",
    "speculative",
    "news",
    "popular_article",
}

# Калибровка не произвольная: 0.60 — та же граница
# "семантически близко к supports", что уже используется в
# claim_relation.py::classify_relation() для эмбеддингового
# fallback (similarity>=0.60 -> SUPPORTS). Переиспользуем уже
# установленную в проекте отметку, не изобретаем новую.
DIRECTNESS_SUPPORT_THRESHOLD = 0.60


def _counts_toward_status(rel):
    """
    Возвращает (counted: bool, via: str|None).

    via == "authority" — старый путь (role=direct+eligible),
    via == "directness" — новый путь (P0-F), НЕ применяется
    к local registry (P0-E: registry — прошлые UNVERIFIED
    ответы модели, не внешняя provenance; допускать их через
    directness означало бы модель подтверждает себя же).
    """
    if (
        rel.get("evidence_role") == "direct"
        and rel.get("evidence_eligible") is True
    ):
        return True, "authority"

    if (
        rel.get("source_class") not in HARD_BLOCKED_SOURCE_CLASSES
        and rel.get("retrieval_origin") != "local_registry"
        and float(rel.get("directness", 0.0) or 0.0)
        >= DIRECTNESS_SUPPORT_THRESHOLD
    ):
        return True, "directness"

    return False, None


def classify_claim_epistemic_status(claims_data, log, verbose):
    """
    Mutates each claim in claims_data in place: verification_status,
    support_count, contradiction_count, secondary_relation_count,
    context_relation_count (and rel["counted_via"] on each direct-counted
    evidence relation).

    Returns the claim_status_counts dict (supported/disputed/contradicted/
    unverified/rejected tallies) — purely local to this block in the
    original code (never read afterward there), returned here in case a
    future caller wants it.
    """
    claim_status_counts = {
        "supported": 0,
        "disputed": 0,
        "contradicted": 0,
        "unverified": 0,
        "rejected": 0,
    }

    for claim in claims_data:
        current_status = claim.get(
            "verification_status",
            "candidate",
        )

        # Structural rejection имеет приоритет.
        if current_status == "rejected":
            claim_status_counts["rejected"] += 1
            continue

        relations = list(
            claim.get("evidence_relations", []) or []
        )

        # P0-F: authority ИЛИ доказанная directness (см. выше).
        # secondary/context/internal relations, не прошедшие ни
        # один из двух путей, сохраняются для диагностики, но не
        # превращают claim в supported/contradicted.
        direct_relations = []

        for rel in relations:
            counted, via = _counts_toward_status(rel)

            if counted:
                rel["counted_via"] = via
                direct_relations.append(rel)

                if verbose:
                    log(
                        "[Claim Support Decision] "
                        f"claim={claim.get('claim_id')} "
                        f"ev={rel.get('evidence_id')} "
                        f"relation={rel.get('relation')} "
                        f"via={via} "
                        f"directness={float(rel.get('directness', 0.0) or 0.0):.3f} "
                        f"counted=True"
                    )

        supports_count = sum(
            1
            for rel in direct_relations
            if rel.get("relation") == "supports"
        )

        contradicts_count = sum(
            1
            for rel in direct_relations
            if rel.get("relation") == "contradicts"
        )

        secondary_count = sum(
            1
            for rel in relations
            if rel.get("evidence_role") == "secondary"
            and rel.get("relation") in {
                "supports",
                "contradicts",
            }
        )

        context_count = sum(
            1
            for rel in relations
            if rel.get("evidence_role") == "context"
            and rel.get("relation") in {
                "supports",
                "contradicts",
            }
        )

        if supports_count > 0 and contradicts_count > 0:
            new_status = "disputed"

        elif supports_count > 0:
            new_status = "supported"

        elif contradicts_count > 0:
            new_status = "contradicted"

        else:
            # uncertain / unrelated / отсутствие evidence
            # не дают основания считать claim поддержанным.
            new_status = "unverified"

        claim["verification_status"] = new_status

        # Эти два счётчика означают только epistemically effective
        # DIRECT evidence.
        claim["support_count"] = supports_count
        claim["contradiction_count"] = contradicts_count

        # Диагностика неавторитетных/вторичных отношений.
        claim["secondary_relation_count"] = secondary_count
        claim["context_relation_count"] = context_count

        claim_status_counts[new_status] += 1

        if verbose:
            log(
                f"[Claim Status] "
                f"claim={claim.get('claim_id')} "
                f"{current_status}->{new_status} "
                f"supports={supports_count} "
                f"contradicts={contradicts_count} "
                f"secondary={secondary_count} "
                f"context={context_count}"
            )

    if verbose:
        log(
            "[Claim Status] "
            f"supported={claim_status_counts['supported']} "
            f"disputed={claim_status_counts['disputed']} "
            f"contradicted={claim_status_counts['contradicted']} "
            f"unverified={claim_status_counts['unverified']} "
            f"rejected={claim_status_counts['rejected']}"
        )

    return claim_status_counts
