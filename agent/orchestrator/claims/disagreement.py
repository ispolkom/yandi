"""
Claim <-> claim disagreement — extracted from agent/orchestrator_v2.py [8]
("---- YANDI V6: DISAGREEMENT ----" block).

Structural extraction only: no prefilter thresholds, embedding model,
batch size, NLI semantics, or log markers changed. Thin orchestration
around agent.claim_relation.infer_claim_relations_batch and the
disagreement_engine (V6) singleton's .challenge() — this module does not
reimplement either.

Полный граф имеет N * (N - 1) / 2 пар. Раньше все пары отправлялись в LLM
NLI. Теперь: claims -> embeddings (один раз на claim) -> cosine similarity
всех пар -> мягкий semantic prefilter -> batch LLM NLI только для
candidate pairs.

ВАЖНО: embedding НЕ определяет supports/contradicts. Он используется
только как дешёвый retrieval gate: "есть ли вообще смысл отдавать эту
пару дорогому NLI?" Финальное логическое отношение по-прежнему
определяет только LLM NLI.

Uses its own raw HTTP session directly against the local embed endpoint
(http://127.0.0.1:11434/api/embed) rather than a shared helper — matches
the original inline code exactly, not something this move should "fix".
"""

from agent.claim_relation import infer_claim_relations_batch


def apply_claim_claim_disagreement(
    claims_data,
    disagreement_engine,
    epistemic_result,
    is_subjective_answer,
    cost,
    log,
    verbose,
):
    """
    Side effects: disagreement_engine.challenge() calls, log() calls, and
    cost["claim_claim_nli_ms"] — none of that changed by this docstring
    update.

    Epistemic Core v1 Phase 8 (additive): now ALSO returns
    {"batch_results": batch_results, "pair_claims": pair_claims} — the
    exact already-computed infer_claim_relations_batch() output and the
    pair_id -> (claim_dict, claim_dict) lookup used above, so
    agent/claim_graph_shadow.py can build claim-graph edges from the SAME
    NLI results this function already paid for, without a second NLI
    pass. Returns None when the early gate below is hit or on any
    exception — callers that don't check the return value (all existing
    ones) are unaffected either way.
    """
    if not (disagreement_engine and claims_data and len(claims_data) > 1):
        return None

    try:
        import math
        import time as _time

        disagreement_started = _time.time()

        # ====================================================
        # CLAIM ↔ CLAIM SEMANTIC PREFILTER + BATCH NLI
        # ====================================================
        #
        # Полный граф имеет:
        #
        #     N * (N - 1) / 2
        #
        # пар.
        #
        # Раньше все пары отправлялись в LLM NLI.
        #
        # Теперь:
        #
        # claims
        #   ↓
        # embeddings — один раз на claim
        #   ↓
        # cosine similarity всех пар
        #   ↓
        # мягкий semantic prefilter
        #   ↓
        # batch LLM NLI только для candidate pairs
        #
        # ВАЖНО:
        #
        # embedding НЕ определяет supports/contradicts.
        #
        # Он используется только как дешёвый retrieval gate:
        # "есть ли вообще смысл отдавать эту пару дорогому NLI?"
        #
        # Финальное логическое отношение по-прежнему
        # определяет только LLM NLI.

        CLAIM_CONFLICT_SIM_THRESHOLD = 0.30
        CLAIM_CONFLICT_TOP_K = 3
        CLAIM_CONFLICT_BATCH_SIZE = 16

        active_claims = []

        for original_index, claim in enumerate(claims_data):
            claim_text = (
                claim.get("claim_text", "") or ""
            ).strip()

            if not claim_text:
                continue

            active_claims.append({
                "original_index": original_index,
                "claim": claim,
                "text": claim_text,
            })

        total_pairs = (
            len(active_claims)
            * (len(active_claims) - 1)
        ) // 2

        # ----------------------------------------------------
        # EMBEDDINGS
        # ----------------------------------------------------

        embedding_started = _time.time()

        semantic_available = False
        semantic_error = None
        claim_vectors = {}

        # P0 (performance architecture pass, unaccounted
        # investigation): this used to call /api/embed ONCE
        # PER CLAIM sequentially — the exact same N+1 pattern
        # already found and fixed in extract_claim_from_source()
        # (claim_relation.py) earlier this session. Ollama's
        # /api/embed accepts a list input and returns one
        # embedding per item in one call — batching here uses
        # the identical technique, same math (each vector still
        # individually L2-normalized), just fewer round-trips.
        try:
            import requests
            import numpy as np

            embed_session = requests.Session()
            embed_session.trust_env = False

            def _claim_embed_batch(values):
                resp = embed_session.post(
                    "http://127.0.0.1:11434/api/embed",
                    json={
                        "model": "embeddinggemma:latest",
                        "input": [v[:2000] for v in values],
                    },
                    timeout=30,
                )

                resp.raise_for_status()

                vecs = np.array(
                    resp.json()["embeddings"],
                    dtype=np.float32,
                )

                norms = np.linalg.norm(
                    vecs, axis=1, keepdims=True
                )
                norms[norms == 0] = 1.0

                return vecs / norms

            _texts = [item["text"] for item in active_claims]

            if _texts:
                _vecs = _claim_embed_batch(_texts)

                for idx in range(len(active_claims)):
                    claim_vectors[idx] = _vecs[idx]

            semantic_available = (
                len(claim_vectors)
                == len(active_claims)
            )

        except Exception as exc:
            semantic_error = (
                f"{type(exc).__name__}: {exc}"
            )

            semantic_available = False

        embedding_elapsed = (
            _time.time() - embedding_started
        )

        # ----------------------------------------------------
        # PAIRWISE COSINE
        # ----------------------------------------------------

        prefilter_started = _time.time()

        all_pair_scores = []
        neighbor_scores = {
            i: []
            for i in range(len(active_claims))
        }

        if semantic_available:
            for i in range(len(active_claims)):
                v1 = claim_vectors[i]

                for j in range(
                    i + 1,
                    len(active_claims),
                ):
                    v2 = claim_vectors[j]

                    similarity = float(
                        np.dot(v1, v2)
                    )

                    all_pair_scores.append({
                        "i": i,
                        "j": j,
                        "similarity": similarity,
                    })

                    neighbor_scores[i].append(
                        (similarity, j)
                    )

                    neighbor_scores[j].append(
                        (similarity, i)
                    )

            # Top-K semantic neighbors каждого claim.
            top_neighbors = {
                i: set()
                for i in range(len(active_claims))
            }

            for i, scores in neighbor_scores.items():
                scores.sort(
                    key=lambda item: item[0],
                    reverse=True,
                )

                for _, neighbor in scores[
                    :CLAIM_CONFLICT_TOP_K
                ]:
                    top_neighbors[i].add(
                        neighbor
                    )

            candidate_pair_keys = set()

            for item in all_pair_scores:
                i = item["i"]
                j = item["j"]
                similarity = item["similarity"]

                threshold_match = (
                    similarity
                    >= CLAIM_CONFLICT_SIM_THRESHOLD
                )

                top_k_match = (
                    j in top_neighbors[i]
                    or i in top_neighbors[j]
                )

                if (
                    threshold_match
                    or top_k_match
                ):
                    candidate_pair_keys.add(
                        (i, j)
                    )

        else:
            # ------------------------------------------------
            # FAIL-OPEN FOR CORRECTNESS
            # ------------------------------------------------
            #
            # Если embedding pipeline сломан, НЕ теряем
            # потенциальные конфликты.
            #
            # В таком случае возвращаемся к полному набору пар,
            # но всё равно используем batch NLI.
            candidate_pair_keys = {
                (i, j)
                for i in range(len(active_claims))
                for j in range(
                    i + 1,
                    len(active_claims),
                )
            }

        prefilter_elapsed = (
            _time.time() - prefilter_started
        )

        # ----------------------------------------------------
        # BUILD BATCH PAIRS
        # ----------------------------------------------------

        claim_pairs = []
        pair_claims = {}

        for i, j in sorted(candidate_pair_keys):
            item1 = active_claims[i]
            item2 = active_claims[j]

            c1 = item1["claim"]
            c2 = item2["claim"]

            text1 = item1["text"]
            text2 = item2["text"]

            if text1 == text2:
                continue

            original_i = item1["original_index"]
            original_j = item2["original_index"]

            pair_id = (
                f"{original_i}:{original_j}"
            )

            claim_pairs.append({
                "pair_id": pair_id,
                "main_claim": text1,
                "other_claim": text2,
            })

            pair_claims[pair_id] = (
                c1,
                c2,
            )

        candidate_count = len(claim_pairs)

        skipped_count = max(
            0,
            total_pairs - candidate_count,
        )

        if verbose:
            log(
                f"[Claim↔Claim Prefilter] "
                f"claims={len(active_claims)} "
                f"total_pairs={total_pairs} "
                f"candidates={candidate_count} "
                f"skipped={skipped_count} "
                f"threshold="
                f"{CLAIM_CONFLICT_SIM_THRESHOLD:.2f} "
                f"top_k={CLAIM_CONFLICT_TOP_K} "
                f"semantic="
                f"{'ok' if semantic_available else 'fallback'}"
            )

            if semantic_error:
                log(
                    f"[Claim↔Claim Prefilter] "
                    f"embedding_error="
                    f"{semantic_error[:180]}"
                )

        # ----------------------------------------------------
        # BATCH NLI
        # ----------------------------------------------------

        nli_started = _time.time()

        batch_results = (
            infer_claim_relations_batch(
                claim_pairs,
                batch_size=(
                    CLAIM_CONFLICT_BATCH_SIZE
                ),
            )
            if claim_pairs
            else []
        )

        nli_elapsed = (
            _time.time() - nli_started
        )

        llm_classified_count = sum(
            1
            for result in batch_results
            if result.get("method")
            == "llm_nli_batch"
        )

        fallback_count = sum(
            1
            for result in batch_results
            if result.get("method")
            in {
                "batch_fallback",
                "batch_missing",
            }
        )

        contradiction_count = 0

        for result in batch_results:
            pair_id = str(
                result.get("pair_id", "")
            )

            relation = result.get(
                "relation"
            )

            method = result.get(
                "method"
            )

            # Не печатаем сотни unrelated / uncertain.
            #
            # Детально логируем только реальные
            # потенциальные конфликты или batch failure.
            if verbose and (
                relation == "contradicts"
                or method
                in {
                    "batch_fallback",
                    "batch_missing",
                }
            ):
                log(
                    f"[Claim↔Claim Batch] "
                    f"pair={pair_id} "
                    f"relation={relation} "
                    f"method={method}"
                )

            # Только настоящий LLM batch result имеет право
            # породить disagreement.
            if not (
                method == "llm_nli_batch"
                and relation == "contradicts"
            ):
                continue

            pair = pair_claims.get(
                pair_id
            )

            if not pair:
                continue

            c1, c2 = pair

            contradiction_count += 1

            disagreement_engine.challenge(
                topic=(
                    epistemic_result.domain
                    if not is_subjective_answer
                    else "subjective"
                ),
                old_position=(
                    c1.get(
                        "claim_text",
                        "",
                    )[:100]
                ),
                challenge=(
                    "Конфликт с утверждением: "
                    + c2.get(
                        "claim_text",
                        "",
                    )[:100]
                ),
                analysis=(
                    "Два утверждения "
                    "противоречат друг другу"
                ),
                new_position=(
                    c2.get(
                        "claim_text",
                        "",
                    )[:100]
                    if c2.get(
                        "claim_confidence",
                        0,
                    )
                    > c1.get(
                        "claim_confidence",
                        0,
                    )
                    else c1.get(
                        "claim_text",
                        "",
                    )[:100]
                ),
                confidence_before=c1.get(
                    "claim_confidence",
                    0.5,
                ),
                confidence_after=c2.get(
                    "claim_confidence",
                    0.5,
                ),
            )

            if verbose:
                log(
                    "[V6] Зафиксирован спор "
                    "между claims"
                )

        generation_calls = (
            math.ceil(
                candidate_count
                / CLAIM_CONFLICT_BATCH_SIZE
            )
            if candidate_count
            else 0
        )

        disagreement_elapsed = (
            _time.time()
            - disagreement_started
        )

        # G: раньше этот блок был частью unaccounted latency,
        # хотя уже имел собственный [Claim↔Claim Timing] print.
        cost["claim_claim_nli_ms"] = (
            disagreement_elapsed * 1000
        )

        if verbose:
            log(
                f"[Claim↔Claim Batch Summary] "
                f"pairs={candidate_count} "
                f"classified="
                f"{llm_classified_count} "
                f"fallback={fallback_count} "
                f"contradicts="
                f"{contradiction_count} "
                f"generation_calls<="
                f"{generation_calls}"
            )

            log(
                f"[Claim↔Claim Timing] "
                f"embedding="
                f"{embedding_elapsed:.2f}s "
                f"prefilter="
                f"{prefilter_elapsed:.3f}s "
                f"nli="
                f"{nli_elapsed:.2f}s "
                f"total="
                f"{disagreement_elapsed:.2f}s"
            )

        return {
            "batch_results": batch_results,
            "pair_claims": pair_claims,
        }

    except Exception as e:
        if verbose:
            log(
                f"[V6] Ошибка batch спора: {e}"
            )
        return None
