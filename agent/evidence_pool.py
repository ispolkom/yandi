"""
agent/evidence_pool.py

Canonical Evidence Pool для YANDI.

Назначение:
- собрать уже найденные pipeline источники в единый evidence pool;
- не назначать claim -> evidence;
- не назначать supports / contradicts;
- не повышать Trust;
- сохранить Source Quality metadata;
- не заставлять claim-specific retriever повторно искать то,
  что YANDI уже получила на основном retrieval pass.

Источники:
    web_result
    search_result (local registry)
    refutation snippets
    позже claim-specific retrieval

Логическое отношение source -> claim определяет только NLI.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from agent.source_quality import evaluate_source_quality


def _value(obj: Any, *names: str, default: Any = "") -> Any:
    """
    Безопасно получить поле и из объекта, и из dict.
    """
    if obj is None:
        return default

    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)

        if value not in (None, ""):
            return value

    return default


def _make_evidence(
    *,
    source_type: str,
    url: str,
    title: str,
    text: str,
    retrieval_origin: str,
    route_side: str = "",
) -> Optional[Dict[str, Any]]:

    text = (text or "").strip()
    url = (url or "").strip()
    title = (title or "").strip()

    if len(text) < 20:
        return None

    quality = evaluate_source_quality(
        url=url,
        title=title,
        text=text,
        source_type=source_type,
    )

    return {
        "evidence_id": f"ev_{uuid.uuid4().hex[:8]}",
        "source_type": source_type,
        "source_uri": url,
        "source_title": title,
        "content_excerpt": text[:700],

        # ВАЖНО:
        # relevance_to_query здесь не означает relation с claim.
        "relevance_to_query": 0.5,

        "quality_score": quality.quality_score,
        "source_class": quality.source_class,
        "evidence_eligible": quality.evidence_eligible,
        "evidence_role": quality.evidence_role,

        "authority": quality.authority,
        "traceability": quality.traceability,
        "primaryness": quality.primaryness,
        "quality_reasons": list(quality.reasons),

        "retrieval_origin": retrieval_origin,
        # P6 (Этап 4 §9, Finding 2 fix): "main"/"counter" for stage 6
        # (from WebSnippet.origin, set by scrape_budgeted_side) —
        # separate from retrieval_origin (the stage label), same
        # reasoning as claim_evidence_retriever.py's PASS2 evidence.
        "route_side": route_side or "",

        "is_meta_pipeline_output": False,
        "is_subject_matter_evidence": True,
        "rejection_reason": None,
    }


def _dedupe(
    evidence: List[Dict[str, Any]],
    *,
    label: str = "dedupe",
) -> List[Dict[str, Any]]:
    """
    Identity для dedup зависит от типа evidence.

    A. GLOBAL/SHARED evidence (нет claim-specific ownership):
       identity = URL (для local evidence без URL —
       source_type + title + excerpt prefix).

       Это исходная семантика: такое evidence потенциально
       относится к нескольким claims, поэтому один и тот же
       URL считается одной записью.

    B. CLAIM-OWNED evidence (retrieval_origin == "claim_specific"
       и retrieval_claim_id непусто, см. claim_evidence_retriever.py
       и ownership-gate в claim_evidence_mapper.py):
       identity = (retrieval_claim_id, URL) — или
       (retrieval_claim_id, source_type, title, excerpt prefix)
       без URL.

       ВАЖНО:
       claim-owned evidence с тем же URL, что и global evidence
       (или evidence другого claim), НЕ считается дубликатом.
       PASS2-запись несёт дополнительную provenance/ownership
       semantics и не должна исчезать только из-за совпадения URL —
       иначе Mapper теряет evidence, которое сам корректно
       фильтрует по retrieval_claim_id.

       Две записи одного и того же claim с одним и тем же URL
       по-прежнему считаются дубликатом и схлопываются.
    """

    result = []
    seen = set()

    global_duplicates = 0
    claim_owned_duplicates = 0
    removed_details = []

    for ev in evidence:
        url = (ev.get("source_uri") or "").strip().lower()

        is_claim_owned = (
            ev.get("retrieval_origin") == "claim_specific"
            and bool((ev.get("retrieval_claim_id") or "").strip())
        )

        if is_claim_owned:
            owner = (ev.get("retrieval_claim_id") or "").strip()

            if url:
                key = ("claim_url", owner, url)
            else:
                key = (
                    "claim_content",
                    owner,
                    ev.get("source_type", ""),
                    (ev.get("source_title") or "")[:100].lower(),
                    (ev.get("content_excerpt") or "")[:200].lower(),
                )
        else:
            if url:
                key = ("url", url)
            else:
                key = (
                    "content",
                    ev.get("source_type", ""),
                    (ev.get("source_title") or "")[:100].lower(),
                    (ev.get("content_excerpt") or "")[:200].lower(),
                )

        if key in seen:
            if is_claim_owned:
                claim_owned_duplicates += 1
            else:
                global_duplicates += 1

            removed_details.append(
                f"{'claim_owned' if is_claim_owned else 'global'}:"
                f"owner={(ev.get('retrieval_claim_id') or '-')}:"
                f"url={url[:70] or '<no-url>'}"
            )

            continue

        seen.add(key)
        result.append(ev)

    removed = len(evidence) - len(result)

    print(
        f"[Evidence Dedup] label={label} "
        f"before={len(evidence)} after={len(result)} removed={removed} "
        f"global_dup={global_duplicates} "
        f"claim_owned_dup={claim_owned_duplicates}"
    )

    for line in removed_details:
        print(f"[Evidence Dedup]   removed: {line}")

    return result


def build_canonical_evidence_pool(
    *,
    search_result: Any = None,
    web_result: Any = None,
    refutation_snippets: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Собрать все УЖЕ полученные pipeline sources.

    Никакого нового поиска здесь нет.
    """

    evidence: List[Dict[str, Any]] = []

    # ========================================================
    # 1. MAIN WEB SEARCH
    # ========================================================

    snippets = _value(
        web_result,
        "snippets",
        default=[],
    ) or []

    for snippet in snippets:
        text = _value(
            snippet,
            "text",
            "content",
            default="",
        )

        ev = _make_evidence(
            source_type="web",
            url=_value(
                snippet,
                "url",
                default="",
            ),
            title=_value(
                snippet,
                "title",
                default="",
            ),
            text=text,
            retrieval_origin="initial_web",
            route_side=_value(snippet, "origin", default="") or "",
        )

        if ev:
            evidence.append(ev)

    # ========================================================
    # 2. LOCAL REGISTRY
    # ========================================================

    docs = _value(
        search_result,
        "docs",
        default=[],
    ) or []

    for doc in docs:
        text = _value(
            doc,
            "content",
            "text",
            default="",
        )

        ev = _make_evidence(
            source_type="local",
            url=_value(
                doc,
                "url",
                "source_uri",
                default="",
            ),
            title=_value(
                doc,
                "title",
                "name",
                default="local registry",
            ),
            text=text,
            retrieval_origin="local_registry",
        )

        if ev:
            evidence.append(ev)

    # ========================================================
    # 3. REFUTATION PASS
    # ========================================================

    for snippet in (refutation_snippets or []):
        text = _value(
            snippet,
            "text",
            "content",
            default="",
        )

        ev = _make_evidence(
            source_type="web",
            url=_value(
                snippet,
                "url",
                default="",
            ),
            title=_value(
                snippet,
                "title",
                default="",
            ),
            text=text,
            retrieval_origin="refutation",
            route_side=_value(snippet, "origin", default="") or "",
        )

        if ev:
            evidence.append(ev)

    return _dedupe(evidence, label="initial_pool")


def merge_evidence(
    base: List[Dict[str, Any]],
    extra: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Добавить evidence в canonical pool без дублей.

    Dedup identity зависит от типа evidence — см. _dedupe().
    Claim-owned evidence (claim-specific retrieval) не схлопывается
    с global evidence или evidence другого claim только из-за
    совпадения URL.
    """
    return _dedupe(
        list(base or []) + list(extra or []),
        label="merge",
    )


if __name__ == "__main__":
    print("Canonical Evidence Pool module OK")
