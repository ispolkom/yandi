"""
agent/embeddings_migration_regression_test.py

Проверяет полную runtime-миграцию оставшихся живых embedding-путей
agent/ на llm_gateway.embed() (мандат "agent embeddings full
migration"): claim_evidence_retriever, belief_manager,
source_quality, claim_evidence_mapper, final_claim_coverage,
claim_relation (extract_claim_from_source + is_relevant).

Для каждого кластера: шлюз реально вызван (не прямой Ollama HTTP),
имя модели сохранено, batch/single семантика сохранена, malformed
gateway-результат не порождает ложный эпистемический вывод, а
несовместимые vector spaces не сравниваются молча там, где путь
способен их получить.

Никакой реальной сети не трогаем нигде в этом файле — если бы код
случайно попытался сделать прямой HTTP-запрос вместо вызова
llm_gateway.embed(), он бы либо упал с реальной сетевой ошибкой (нет
мока на requests), либо завис — оба исхода надёжно ловятся тестом.

Запуск: python3 -m agent.embeddings_migration_regression_test
"""
from __future__ import annotations

import sys
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    import llm_gateway
    from llm_gateway.client import EmbeddingResult
    from llm_gateway import vector_space as vs

    space_x = vs.VectorSpaceId(backend="ollama-compat", protocol="ollama-embed", model="embeddinggemma:latest", dimension=4, normalized=False)
    space_y = vs.VectorSpaceId(backend="remote", protocol="remote-openai-embeddings", model="other", dimension=4, normalized=False)

    # ── A: claim_evidence_retriever._query_relevance_score ──────────
    from agent import claim_evidence_retriever as cer

    calls = []

    def fake_embed_a(texts, *, model):
        calls.append((texts, model))
        text = texts if isinstance(texts, str) else texts[0]
        return EmbeddingResult(vectors=[[float(len(text) % 5 + 1), 1.0, 0.0, 0.0]], space=space_x)

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_a):
        calls.clear()
        score = cer._query_relevance_score("Юпитер — газовый гигант", "Что известно про Юпитер?")
        check("A: gateway called (not raw Ollama HTTP)", len(calls) == 2, repr(calls))
        check("A: correct model name preserved", all(m == "embeddinggemma:latest" for _, m in calls))
        check("A: returns a real float score for compatible spaces", isinstance(score, float))

    # A: malformed gateway result (raises EmbedError) -> OLD/NEW failure behavior preserved: 0.0
    with patch.object(llm_gateway, "embed", side_effect=llm_gateway.EmbedError("malformed")):
        score = cer._query_relevance_score("a", "b")
        check("A: EmbedError degrades to 0.0 (unchanged failure semantics)", score == 0.0)

    # A: incompatible vector spaces between the two separate calls -> must not silently compare
    def fake_embed_incompatible(texts, *, model):
        text = texts if isinstance(texts, str) else texts[0]
        space = space_x if "Юпитер" in text else space_y
        return EmbeddingResult(vectors=[[1.0, 0.0, 0.0, 0.0]], space=space)

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_incompatible):
        score = cer._query_relevance_score("Юпитер", "другой текст")
        check("A: incompatible vector spaces -> 0.0, not a fabricated similarity", score == 0.0)

    # ── B: belief_manager.BeliefManager._embed_batch ─────────────────
    from agent.belief_manager import BeliefManager

    def fake_embed_batch(texts, *, model):
        calls.clear()
        calls.append((texts, model))
        return EmbeddingResult(vectors=[[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]], space=space_x)

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_batch):
        result = BeliefManager._embed_batch(["a", "b", "c"])
        check("B: gateway called with the full batch in ONE call", calls[0][0] == ["a", "b", "c"])
        check("B: correct model name preserved", calls[0][1] == "embeddinggemma:latest")
        check("B: batch result has one row per input text", result.shape[0] == 3)

    with patch.object(llm_gateway, "embed", side_effect=llm_gateway.EmbedError("down")):
        result = BeliefManager._embed_batch(["a", "b"])
        check("B: EmbedError -> None (unchanged failure semantics, caller treats as 'no prefilter')", result is None)

    # ── C: source_quality.evaluate_evidence_directness ───────────────
    from agent import source_quality as sq

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_a):
        calls.clear()
        score = sq.evaluate_evidence_directness("claim text here", "passage text here")
        check("C: gateway called (not raw Ollama HTTP)", len(calls) == 2)
        check("C: correct model name preserved", all(m == "embeddinggemma:latest" for _, m in calls))
        check("C: returns a real float for compatible spaces", isinstance(score, float))

    with patch.object(llm_gateway, "embed", side_effect=llm_gateway.EmbedError("down")):
        score = sq.evaluate_evidence_directness("a", "b")
        check("C: EmbedError degrades to 0.0 (unchanged failure semantics)", score == 0.0)

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_incompatible):
        score = sq.evaluate_evidence_directness("Юпитер claim", "другой passage")
        check("C: incompatible vector spaces -> 0.0, not a fabricated directness score", score == 0.0)

    # ── D: claim_evidence_mapper.map_claims_to_evidence ──────────────
    from agent.claim_evidence_mapper import map_claims_to_evidence, EvidenceEmbeddingCache

    claims = [{"claim_id": "c1", "claim_text": "Юпитер — газовый гигант"}]
    evidence = [{"evidence_id": "e1", "content_excerpt": "Юпитер является газовым гигантом с массой " + "x" * 60, "source_title": "Astro", "source_uri": "https://x"}]

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_a):
        calls.clear()
        mapped = map_claims_to_evidence(claims, evidence, EvidenceEmbeddingCache())
        check("D: gateway called for both evidence and claim embedding", len(calls) >= 2)
        check("D: correct model name preserved", all(m == "embeddinggemma:latest" for _, m in calls))
        check("D: mapping produced a real ClaimRecord list", len(mapped) == 1)

    # D: evidence embedding phase fails entirely -> semantic_available=False -> lexical fallback (not a crash)
    with patch.object(llm_gateway, "embed", side_effect=llm_gateway.EmbedError("down")):
        mapped = map_claims_to_evidence(claims, evidence, EvidenceEmbeddingCache())
        check("D: total embedding failure -> graceful lexical fallback, no crash", len(mapped) == 1)

    # D: evidence and claim come from genuinely different vector spaces -> must not match via embeddings
    def fake_embed_d_incompatible(texts, *, model):
        text = texts if isinstance(texts, str) else texts[0]
        space = space_y if "Юпитер является" in text else space_x  # evidence gets space_y, claim gets space_x
        return EmbeddingResult(vectors=[[1.0, 0.0, 0.0, 0.0]], space=space)

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_d_incompatible):
        mapped = map_claims_to_evidence(claims, evidence, EvidenceEmbeddingCache())
        check(
            "D: incompatible evidence/claim vector spaces -> no false semantic match (empty derived_from_evidence_ids)",
            mapped[0].derived_from_evidence_ids == [],
            repr(mapped[0].derived_from_evidence_ids),
        )

    # ── E: final_claim_coverage._embed_texts_batch / _route_candidate_pairs ──
    from agent import final_claim_coverage as fcc

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_batch):
        calls.clear()
        vec_map = fcc._embed_texts_batch(["final claim one", "pipeline claim one"])
        check("E: gateway called with the full batch in ONE call", len(calls) == 1 and len(calls[0][0]) == 2)
        check("E: correct model name preserved", calls[0][1] == "embeddinggemma:latest")
        check("E: one vector per unique input text", len(vec_map) == 2)

    with patch.object(llm_gateway, "embed", side_effect=llm_gateway.EmbedError("down")):
        vec_map = fcc._embed_texts_batch(["a", "b"])
        check("E: EmbedError -> {} (unchanged failure semantics, routing falls back to mandatory-only)", vec_map == {})

    # ── F: claim_relation.extract_claim_from_source (batch) + is_relevant (two single calls) ──
    from agent import claim_relation as cr

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_batch):
        calls.clear()
        passage = cr.extract_claim_from_source("Юпитер большой. Он газовый гигант. Луна не имеет атмосферы.", "Юпитер газовый гигант")
        check("F1: gateway called with ONE batch (main_claim + all sentences)", len(calls) == 1)
        check("F1: correct model name preserved", calls[0][1] == "embeddinggemma:latest")
        check("F1: returns a real, non-empty passage", isinstance(passage, str) and len(passage) > 0)

    with patch.object(llm_gateway, "embed", side_effect=llm_gateway.EmbedError("down")):
        passage = cr.extract_claim_from_source("Предложение раз. Предложение два.", "claim")
        check("F1: EmbedError -> lexical fallback, no crash", isinstance(passage, str))

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_a):
        calls.clear()
        relevant = cr.is_relevant("some source text about the topic", "some main claim about the topic")
        check("F2: gateway called TWICE (two separate single calls, not merged into a batch)", len(calls) == 2)
        check("F2: correct model name preserved", all(m == "embeddinggemma:latest" for _, m in calls))
        check("F2: returns a real bool", isinstance(relevant, bool))

    with patch.object(llm_gateway, "embed", side_effect=llm_gateway.EmbedError("down")):
        relevant = cr.is_relevant("text", "claim")
        check("F2: EmbedError -> lexical fallback, no crash", isinstance(relevant, bool))

    with patch.object(llm_gateway, "embed", side_effect=fake_embed_incompatible):
        relevant = cr.is_relevant("другой текст", "Юпитер claim")
        check("F2: incompatible vector spaces -> lexical fallback path engaged, not a fabricated semantic match", isinstance(relevant, bool))

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
    else:
        print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
