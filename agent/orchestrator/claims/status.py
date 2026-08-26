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


def evaluate_claim_status_gate(claims_data, synthesis_result, log):
    """
    Extracted from agent/orchestrator_v2.py [9] ("---- [9] CLAIM STATUS
    GATE ----" block). Counts claims by verification_status and rewrites
    synthesis_result.answer/trust_level/confidence for 5 mutually
    exclusive cases (no claims; all rejected; only-contradicted; disputed
    present; verified==0).

    Эпистемические статусы:

    verified      — подтверждён более сильной процедурой проверки
    supported     — есть evidence SUPPORTS, но это ещё не verified
    disputed      — есть одновременно supports и contradicts
    contradicted  — есть contradicts и нет supports
    candidate     — ещё не прошёл evidence relation stage
    unverified    — поддержки/опровержения не найдено
    rejected      — структурно непригодный claim

    IMPORTANT — caller contract: the original inline code only ever
    assigned claims_accepted/total_claims/claims_rejected as process()
    locals when the outer guard (`not skip_rag and not is_subjective_answer
    and synthesis_result`) was true, and downstream code (the still-inline
    [10] V3 reflection block) checks `'claims_accepted' in locals()` to
    detect whether this gate ran at all. Moving that guard into this
    function would make the check always true after any call (Python
    would always assign the return values as locals), silently changing
    behavior. So the guard stays at the call site in orchestrator_v2.py;
    this function assumes it has already been evaluated true.

    Mutates synthesis_result in place. Returns (claims_accepted,
    total_claims, claims_rejected) for the caller to assign as its own
    locals.
    """
    claims_verified = len([
        c for c in claims_data
        if c.get("verification_status") == "verified"
    ])

    claims_supported = len([
        c for c in claims_data
        if c.get("verification_status") == "supported"
    ])

    claims_disputed = len([
        c for c in claims_data
        if c.get("verification_status") == "disputed"
    ])

    claims_contradicted = len([
        c for c in claims_data
        if c.get("verification_status") == "contradicted"
    ])

    claims_candidate = len([
        c for c in claims_data
        if c.get("verification_status") == "candidate"
    ])

    claims_rejected = len([
        c for c in claims_data
        if c.get("verification_status") == "rejected"
    ])

    claims_unverified = len([
        c for c in claims_data
        if c.get("verification_status") in (
            "unverified",
            "weak",
            None,
            "",
        )
    ])

    total_claims = len(claims_data)

    # Совместимость со старым reflection/data pipeline:
    # accepted означает ТОЛЬКО реально verified.
    claims_accepted = claims_verified

    log(
        f"[Claim Status Gate] "
        f"verified={claims_verified}, "
        f"supported={claims_supported}, "
        f"disputed={claims_disputed}, "
        f"contradicted={claims_contradicted}, "
        f"candidate={claims_candidate}, "
        f"unverified={claims_unverified}, "
        f"rejected={claims_rejected}, "
        f"total={total_claims}"
    )

    if total_claims == 0:
        log("[Claim Status Gate] Claims отсутствуют — статус UNVERIFIED")

        synthesis_result.answer = (
            "Я попыталась найти информацию.\n\n"
            "Но мне не удалось выделить достаточно проверяемых утверждений.\n"
            "Я не могу дать уверенный ответ на этот вопрос.\n\n"
            "Если дашь дополнительный контекст — я попробую ещё раз."
        )

        synthesis_result.trust_level = "UNVERIFIED"
        synthesis_result.confidence = 0.0

    elif claims_rejected == total_claims:
        log("[Claim Status Gate] Все claims структурно отклонены")

        synthesis_result.answer = (
            "Я попыталась сформировать ответ, но выделенные утверждения "
            "не прошли структурную проверку.\n\n"
            "Поэтому я не могу считать этот ответ надёжным."
        )

        synthesis_result.trust_level = "UNVERIFIED"
        synthesis_result.confidence = 0.0

    elif claims_contradicted > 0 and (
        claims_contradicted
        + claims_rejected
        + claims_unverified
        + claims_candidate
        == total_claims
    ):
        # Нет ни одного supported/verified claim,
        # зато есть явно contradicted.
        log(
            "[Claim Status Gate] "
            "Поддержанных claims нет, присутствуют опровергающие evidence"
        )

        synthesis_result.trust_level = "UNVERIFIED"
        synthesis_result.confidence = min(
            synthesis_result.confidence,
            0.25,
        )

        # P0-A (YANDI_FINAL_EPISTEMIC_AUDIT_AND_FIX.md): раньше этот
        # branch менял ТОЛЬКО trust_level/confidence — сам текст
        # synthesis_result.answer уже был сгенерирован compose_prompt
        # ДО того, как Claim Status вообще стал известен, и никогда
        # не пересматривался. Текст мог свободно утверждать то, что
        # найденные evidence прямо опровергают. Здесь — не переписываем
        # и не удаляем сгенерированный текст (в нём может быть полезный
        # объясняющий контекст), а явно маркируем его эпистемический
        # статус прямо в теле ответа, а не только в trust-бейдже.
        _contradiction_notice = (
            "⚠️ ВАЖНО: часть проверяемых утверждений в этом ответе "
            f"была ОПРОВЕРГНУТА найденными источниками "
            f"(contradicted={claims_contradicted} из {total_claims}), "
            "и ни одно утверждение не получило прямого подтверждения. "
            "Текст ниже остаётся гипотезой модели — не считай его "
            "установленным фактом.\n"
        )

        if not synthesis_result.answer.startswith("⚠️ ВАЖНО:"):
            synthesis_result.answer = (
                _contradiction_notice
                + "\n"
                + synthesis_result.answer
            )

    elif claims_disputed > 0:
        # Спор не означает ложность ответа, но требует сильного cap.
        log(
            f"[Claim Status Gate] "
            f"Обнаружены спорные claims: {claims_disputed}"
        )

        trust_rank = {
            "UNVERIFIED": 0,
            "WEAKLY_SUPPORTED": 1,
            "PARTIALLY_SUPPORTED": 2,
            "SUPPORTED": 3,
            "STRONGLY_SUPPORTED": 4,
            "VERIFIED": 5,
        }

        current = synthesis_result.trust_level

        if trust_rank.get(current, 0) > trust_rank["WEAKLY_SUPPORTED"]:
            synthesis_result.trust_level = "WEAKLY_SUPPORTED"

        synthesis_result.confidence = min(
            synthesis_result.confidence,
            0.45,
        )

    elif claims_verified == 0:
        # supported != verified.
        #
        # Даже если часть claims имеет supports evidence,
        # без отдельной сильной проверки Trust не должен
        # подниматься выше PARTIALLY_SUPPORTED.
        log(
            f"[Claim Status Gate] "
            f"verified=0, supported={claims_supported} — "
            f"ответ остаётся предварительным"
        )

        trust_rank = {
            "UNVERIFIED": 0,
            "WEAKLY_SUPPORTED": 1,
            "PARTIALLY_SUPPORTED": 2,
            "SUPPORTED": 3,
            "STRONGLY_SUPPORTED": 4,
            "VERIFIED": 5,
        }

        current = synthesis_result.trust_level

        if trust_rank.get(current, 0) > trust_rank["PARTIALLY_SUPPORTED"]:
            synthesis_result.trust_level = "PARTIALLY_SUPPORTED"

        # Если вообще нет supported claims — cap ещё ниже.
        if claims_supported == 0:
            if trust_rank.get(
                synthesis_result.trust_level,
                0,
            ) > trust_rank["WEAKLY_SUPPORTED"]:
                synthesis_result.trust_level = "WEAKLY_SUPPORTED"

            synthesis_result.confidence = min(
                synthesis_result.confidence,
                0.40,
            )

            # P0-A: точный сценарий из аудита — supported=0,
            # verified=0, unverified>0. Раньше синтезированный текст
            # (сгенерированный ДО того, как Claim Status стал
            # известен) оставался без изменений — только trust
            # badge менялся. Явно маркируем это в самом тексте.
            _unsupported_notice = (
                "⚠️ ВАЖНО: ни одно из "
                f"{total_claims} проверяемых утверждений не получило "
                "подтверждающих доказательств (supported=0, "
                "verified=0). Всё, что изложено ниже — "
                "неподтверждённая гипотеза модели, а не установленный "
                "факт. Система не получила достаточной evidence-базы "
                "для проверки.\n"
            )

            if not synthesis_result.answer.startswith("⚠️ ВАЖНО:"):
                synthesis_result.answer = (
                    _unsupported_notice
                    + "\n"
                    + synthesis_result.answer
                )
        else:
            synthesis_result.confidence = min(
                synthesis_result.confidence,
                0.60,
            )

    else:
        log(
            f"[Claim Status Gate] "
            f"Есть verified claims: "
            f"{claims_verified}/{total_claims}"
        )

    return claims_accepted, total_claims, claims_rejected
