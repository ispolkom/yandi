"""
agent/source_clustering.py — Epistemic Core v1 Phase 6: production wiring
for source-independence cluster metadata.

Reuses the exact similarity math validated offline in Phase 5
(agent/source_independence_prototype.py) — title_similarity,
content_fingerprint_similarity, and their thresholds — rather than
reimplementing it. This module adds only the orchestration needed to
turn pairwise similarity into a cluster_id per evidence item across a
whole evidence pool (union-find), and to persist it as metadata on the
evidence dict.

METADATA ONLY, per the plan: nothing computed here is read by
claims/status.py's support_count tally or verification_status
assignment this phase — that is Phase 7's separate, deliberate step.

FAILS OPEN: an evidence item that isn't confidently similar to anything
else stays in its own singleton cluster. Clustering only happens on a
POSITIVE similarity signal (title_sim >= threshold or content_sim >=
threshold) — there is no code path that merges two items due to
uncertainty, missing data, or a comparison error. A comparison that
raises an exception is treated as "not similar" (caught, never re-raised,
never merged) — a confidently-wrong merge is categorically worse than a
missed cluster per the audit's own emphasis, so failures bias toward
NOT merging, never toward merging.

Rust-перенос (2026-09-24): rustlib/yandi_rs/src/source_clustering.rs — ядро assign_source_clusters
(попарное сравнение + union-find; канонизация/шинглы считаются один раз на источник, а не на
каждую пару) вместе с difflib-подобным title_similarity и Жаккаром по шинглам. Доказан на
совпадение (в т.ч. дифференциальным фаззингом против настоящего difflib и полной проверкой
Python-`\w` по всем кодовым точкам) тестом agent/source_clustering_rust_parity_test.py.
По умолчанию ВЫКЛЮЧЕН; включается переменной окружения YANDI_SOURCE_CLUSTERING_ENGINE=rust ПОСЛЕ
сборки rustlib/yandi_rs (`maturin develop`, см. rustlib/README.md). Не собран, либо вход нестроковый /
с одиночными суррогатами (PyO3 не принимает) — этот вызов выполняется прежним Python-путём.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from agent.source_independence_prototype import (
    SourceCandidate,
    title_similarity,
    content_fingerprint_similarity,
    TITLE_SIM_THRESHOLD,
    CONTENT_FINGERPRINT_THRESHOLD,
)


_logger = logging.getLogger("yandi.source_clustering")   # НЕ `log`: так называется параметр функций ниже

_rust_sc = None          # None = ещё не пробовали; False = не запрошено/не собрано; модуль = подключён


def _get_rust_sc():
    global _rust_sc
    if _rust_sc is None:
        if os.environ.get("YANDI_SOURCE_CLUSTERING_ENGINE") == "rust":
            try:
                import yandi_rs.source_clustering as _rs
                _rust_sc = _rs
                _logger.warning("YANDI_SOURCE_CLUSTERING_ENGINE=rust: используется Rust-реализация source_clustering (rustlib/yandi_rs)")
            except ImportError as e:
                _logger.warning("YANDI_SOURCE_CLUSTERING_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python", e)
                _rust_sc = False
        else:
            _rust_sc = False
    return _rust_sc or None


class _UnionFind:
    def __init__(self, keys):
        self._parent = {k: k for k in keys}

    def find(self, k):
        while self._parent[k] != k:
            self._parent[k] = self._parent[self._parent[k]]
            k = self._parent[k]
        return k

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _similar(a: SourceCandidate, b: SourceCandidate, log=None) -> bool:
    try:
        t_sim = title_similarity(a.title, b.title)
        c_sim = content_fingerprint_similarity(a.content_excerpt, b.content_excerpt)
        return t_sim >= TITLE_SIM_THRESHOLD or c_sim >= CONTENT_FINGERPRINT_THRESHOLD
    except Exception as e:
        # FAILS OPEN: a comparison error must never cause a merge.
        if log:
            log(f"[Source Clustering] comparison error, treating as not-similar: {e}")
        return False


def assign_source_clusters(
    evidence_data: List[Dict[str, Any]],
    log: Optional[Any] = None,
    verbose: bool = False,
) -> int:
    """
    Mutates each item in evidence_data in place: sets
    ev["source_cluster_id"]. Every item with an evidence_id gets a
    cluster_id — its own singleton if nothing else in the pool is
    confidently similar to it. Computed fresh from the CURRENT
    evidence_data each call (safe to call again after evidence_data
    grows, e.g. after PASS2 retrieval adds items — recomputes over the
    full current pool rather than only the new items).

    cluster_id is derived from the cluster's root evidence_id
    (f"sc_{root_evidence_id}"), not a fresh random id — deterministic
    within a call, and stable across repeated calls in the same request
    as long as the root item is still present in evidence_data.

    Returns the count of evidence items that ended up sharing a cluster
    with at least one other item (a coarse "how much syndication was
    found" signal for logging) — not itself used for any decision.
    """
    candidates = {}
    for ev in evidence_data:
        ev_id = ev.get("evidence_id")
        if not ev_id:
            continue
        candidates[ev_id] = SourceCandidate(
            url=ev.get("source_uri", "") or "",
            title=ev.get("source_title", "") or "",
            content_excerpt=ev.get("content_excerpt", "") or "",
        )

    ids = list(candidates.keys())

    root_of = None
    rs = _get_rust_sc()
    if rs is not None:
        try:
            idx_roots = rs.cluster_roots(
                [candidates[i].title for i in ids],
                [candidates[i].content_excerpt for i in ids],
            )
            root_of = {ids[k]: ids[r] for k, r in enumerate(idx_roots)}
        except (TypeError, UnicodeError) as e:
            # нестроковое поле / одиночный суррогат — PyO3 такое не принимает; исходный Python-путь
            # для этого вызова (он "проваливает открыто" пару за парой, как и раньше)
            _logger.warning("source_clustering: Rust-путь не принял вход (%s) — этот вызов на Python", e)

    if root_of is None:
        uf = _UnionFind(ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                id_a, id_b = ids[i], ids[j]
                if uf.find(id_a) == uf.find(id_b):
                    continue
                if _similar(candidates[id_a], candidates[id_b], log=log if verbose else None):
                    uf.union(id_a, id_b)
        root_of = {i: uf.find(i) for i in ids}

    for ev in evidence_data:
        ev_id = ev.get("evidence_id")
        if ev_id not in candidates:
            continue
        ev["source_cluster_id"] = f"sc_{root_of[ev_id]}"

    cluster_sizes: Dict[str, int] = {}
    for ev in evidence_data:
        cid = ev.get("source_cluster_id")
        if cid:
            cluster_sizes[cid] = cluster_sizes.get(cid, 0) + 1

    non_singleton = sum(
        1
        for ev in evidence_data
        if cluster_sizes.get(ev.get("source_cluster_id"), 0) > 1
    )

    if verbose and log:
        log(
            f"[Source Clustering] "
            f"evidence={len(evidence_data)} "
            f"clusters={len(cluster_sizes)} "
            f"non_singleton_members={non_singleton}"
        )

    return non_singleton
