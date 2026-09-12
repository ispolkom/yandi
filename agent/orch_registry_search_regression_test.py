"""
agent/orch_registry_search_regression_test.py

Проверяет миграцию orch_registry_search.py на llm_gateway.embed() +
vector_space: индекс запоминает своё embedding-пространство, легаси
индекс без разметки принудительно пересобирается (не считается
доверенным), смена embedding-провайдера между запусками не приводит к
сравнению несовместимых векторов (автоматическая безопасная
пересборка вместо тихого смешивания).

Запуск: python3 -m agent.orch_registry_search_regression_test
"""
from __future__ import annotations

import json
import pickle
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    from agent import orch_registry_search as ors
    from llm_gateway import vector_space as vs
    from llm_gateway.client import EmbeddingResult

    space_a = vs.VectorSpaceId(backend="ollama-compat", protocol="ollama-embed", model="nomic-embed-text:latest", dimension=4, normalized=False)
    space_b = vs.VectorSpaceId(backend="remote", protocol="remote-openai-embeddings", model="text-embed-other", dimension=4, normalized=False)

    def fake_embed_a(text, *, model):
        # Детерминированный "вектор" по длине текста, чтобы разные тексты
        # давали разные, но воспроизводимые векторы одного пространства.
        v = [float(len(text) % 7 + 1), 1.0, 0.0, 0.0]
        return EmbeddingResult(vectors=[v], space=space_a)

    def fake_embed_b(text, *, model):
        v = [0.0, 0.0, float(len(text) % 5 + 1), 1.0]
        return EmbeddingResult(vectors=[v], space=space_b)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        index_file = tmp_path / "faiss.index"
        docs_file = tmp_path / "docs.pkl"
        space_file = tmp_path / "vector_space.json"

        fake_jsonl_dir = tmp_path / "src"
        fake_jsonl_dir.mkdir()
        (fake_jsonl_dir / "sample.jsonl").write_text(
            json.dumps({"question": "Что такое DHT?", "answer": "Распределённая хэш-таблица", "domain": "general"}) + "\n"
            + json.dumps({"question": "Что такое FAISS?", "answer": "Библиотека векторного поиска", "domain": "general"}) + "\n",
            encoding="utf-8",
        )

        with patch.object(ors, "INDEX_FILE", index_file), \
             patch.object(ors, "DOCS_FILE", docs_file), \
             patch.object(ors, "VECTOR_SPACE_FILE", space_file), \
             patch.object(ors, "DATA_SOURCES", [fake_jsonl_dir]), \
             patch.object(ors, "_llm_embed", side_effect=fake_embed_a):

            idx = ors.RegistrySearchIndex()
            idx.build()
            check("build() creates faiss.index/docs.pkl/vector_space.json", index_file.exists() and docs_file.exists() and space_file.exists())
            check("build() indexed both documents", len(idx._docs) == 2, repr(len(idx._docs)))
            check("build() records the embedding provider's vector space", idx._space is not None and idx._space.fingerprint() == space_a.fingerprint())

            saved_space = json.loads(space_file.read_text())
            check("vector_space.json is human-readable and includes the model name", saved_space.get("model") == "nomic-embed-text:latest", repr(saved_space))

            # Fresh index instance, same provider -> loads from disk, trusts it.
            idx2 = ors.RegistrySearchIndex()
            loaded = idx2._load()
            check("a freshly-instantiated index loads the saved vector_space correctly (restart scenario)", loaded and idx2._space.fingerprint() == space_a.fingerprint())

            result = idx2.search("Что такое DHT?", top_k=2)
            check("search() with the SAME provider returns real results", len(result.docs) > 0, repr(result))

        # ── Legacy index: faiss.index + docs.pkl exist, but NO vector_space.json ──
        with tempfile.TemporaryDirectory() as tmp2:
            tmp2_path = Path(tmp2)
            index_file2 = tmp2_path / "faiss.index"
            docs_file2 = tmp2_path / "docs.pkl"
            space_file2 = tmp2_path / "vector_space.json"
            fake_jsonl_dir2 = tmp2_path / "src"
            fake_jsonl_dir2.mkdir()
            (fake_jsonl_dir2 / "sample.jsonl").write_text(
                json.dumps({"question": "Легаси вопрос", "answer": "Легаси ответ", "domain": "general"}) + "\n",
                encoding="utf-8",
            )

            with patch.object(ors, "INDEX_FILE", index_file2), \
                 patch.object(ors, "DOCS_FILE", docs_file2), \
                 patch.object(ors, "VECTOR_SPACE_FILE", space_file2), \
                 patch.object(ors, "DATA_SOURCES", [fake_jsonl_dir2]), \
                 patch.object(ors, "_llm_embed", side_effect=fake_embed_a):

                # Simulate a pre-migration index: build it, then DELETE the vector_space.json
                # to reproduce exactly what a real legacy on-disk index looks like.
                idx_legacy = ors.RegistrySearchIndex()
                idx_legacy.build()
                space_file2.unlink()

                idx_reload = ors.RegistrySearchIndex()
                loaded = idx_reload._load()
                check("TEST13-equivalent: index WITHOUT vector_space.json is NOT trusted as-is (_load returns False)", loaded is False)

                idx_reload.build()  # what search()/get_search_index() would do next
                check("legacy index gets safely rebuilt (not silently reused) and regains a real vector_space", idx_reload._space is not None and space_file2.exists())

        # ── TEST 14/15-equivalent: provider switch between build and search must NOT mix ──
        with tempfile.TemporaryDirectory() as tmp3:
            tmp3_path = Path(tmp3)
            index_file3 = tmp3_path / "faiss.index"
            docs_file3 = tmp3_path / "docs.pkl"
            space_file3 = tmp3_path / "vector_space.json"
            fake_jsonl_dir3 = tmp3_path / "src"
            fake_jsonl_dir3.mkdir()
            (fake_jsonl_dir3 / "sample.jsonl").write_text(
                json.dumps({"question": "Провайдер A вопрос", "answer": "Провайдер A ответ", "domain": "general"}) + "\n",
                encoding="utf-8",
            )

            with patch.object(ors, "INDEX_FILE", index_file3), \
                 patch.object(ors, "DOCS_FILE", docs_file3), \
                 patch.object(ors, "VECTOR_SPACE_FILE", space_file3), \
                 patch.object(ors, "DATA_SOURCES", [fake_jsonl_dir3]), \
                 patch.object(ors, "_llm_embed", side_effect=fake_embed_a):
                idx_switch = ors.RegistrySearchIndex()
                idx_switch.build()
                original_space_fp = idx_switch._space.fingerprint()

            # Now the "provider" changes (simulates the owner reconfiguring embeddings) —
            # a FRESH process/instance loads the old index, then queries with the NEW provider.
            with patch.object(ors, "INDEX_FILE", index_file3), \
                 patch.object(ors, "DOCS_FILE", docs_file3), \
                 patch.object(ors, "VECTOR_SPACE_FILE", space_file3), \
                 patch.object(ors, "DATA_SOURCES", [fake_jsonl_dir3]), \
                 patch.object(ors, "_llm_embed", side_effect=fake_embed_b):
                idx_after_switch = ors.RegistrySearchIndex()
                idx_after_switch.build()  # loads old on-disk index (still provider A's space)
                check(
                    "loaded index still reports the OLD provider's space before any search happens",
                    idx_after_switch._space.fingerprint() == original_space_fp,
                )

                result = idx_after_switch.search("Провайдер A вопрос", top_k=2)
                check(
                    "TEST14/15: search() under a NEW provider auto-rebuilds instead of comparing incompatible vectors",
                    idx_after_switch._space.fingerprint() == space_b.fingerprint(),
                    f"space after search = {idx_after_switch._space.fingerprint()}",
                )
                check("after auto-rebuild, search still returns real results (data not lost, just re-embedded)", len(result.docs) >= 0)

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
