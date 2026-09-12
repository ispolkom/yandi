"""
agent/orch_cache_regression_test.py

Проверяет миграцию orch_cache.py на llm_gateway.embed() + vector_space:
семантический кэш без разметки происхождения (до-миграционный)
уходит в карантин, а не читается как доверенный; новые записи несут
vector_space; смена embedding-провайдера между put() и get() не
приводит к сравнению несовместимых векторов, даже если бы cosine
score оказался высоким.

Redis-клиент подменяется фейковым (см. _FakeRedis) — реальный Redis на
машине хранит общее, разделяемое между процессами и прогонами
состояние, и точный exact-hash путь через него мог бы случайно скрыть
именно то, что здесь проверяется (семантический, FAISS-free
in-memory путь).

Запуск: python3 -m agent.orch_cache_regression_test
"""
from __future__ import annotations

import pickle
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    from agent import orch_cache as oc
    from llm_gateway import vector_space as vs
    from llm_gateway.client import EmbeddingResult

    space_a = vs.VectorSpaceId(backend="ollama-compat", protocol="ollama-embed", model="nomic-embed-text:latest", dimension=3, normalized=False)
    space_b = vs.VectorSpaceId(backend="remote", protocol="remote-openai-embeddings", model="other-embedder", dimension=3, normalized=False)

    def fake_embed_a(text, *, model):
        return EmbeddingResult(vectors=[[1.0, 0.0, 0.0]], space=space_a)

    def fake_embed_b(text, *, model):
        # Намеренно ПОЧТИ идентичный вектор (высокий бы cosine score),
        # чтобы доказать: guard блокирует по identity, а не потому что
        # числа оказались непохожи.
        return EmbeddingResult(vectors=[[0.999, 0.001, 0.0]], space=space_b)

    class _FakeRedis:
        """Реальный Redis на машине — общее состояние между процессами и
        прогонами тестов; точный exact-hash путь через него мог бы
        случайно "подсказать" ответ из прошлого запуска и замаскировать
        то, что здесь реально проверяется (семантический путь). Полная
        in-memory изоляция, ничего не пишет и не читает по-настоящему."""
        def get(self, key): return None
        def setex(self, key, ttl, data): pass
        def delete(self, key): pass
        def scan_iter(self, pattern): return iter(())

    with tempfile.TemporaryDirectory() as tmp:
        sem_file = Path(tmp) / "sem_index_v2.pkl"

        with patch.object(oc, "SEM_INDEX_FILE", sem_file), \
             patch.object(oc, "_redis_client", return_value=_FakeRedis()), \
             patch.object(oc, "_llm_embed", side_effect=fake_embed_a):
            cache = oc.OrchestratorCache()
            check("fresh cache starts empty", cache._sem == [])

            cache.put(
                query="Что такое DHT?", answer="Распределённая хэш-таблица" * 3,
                trust_level="HYPOTHESIS",
                epistemic={"domain": "procedural", "testability": "fully_testable"},
            )
            check("put() writes exactly one semantic entry", len(cache._sem) == 1)
            check("new entry carries a vector_space field", "vector_space" in cache._sem[0])
            check("new entry's vector_space matches the provider used", cache._sem[0]["vector_space"]["model"] == "nomic-embed-text:latest")

            result = cache.get("Что такое DHT?")
            check("TEST10-equivalent: same-provider semantic hit works", result.hit is True, repr(result))

        # ── Restart with the SAME provider (TEST15-equivalent) — identity survives ──
        with patch.object(oc, "SEM_INDEX_FILE", sem_file), \
             patch.object(oc, "_redis_client", return_value=_FakeRedis()), \
             patch.object(oc, "_llm_embed", side_effect=fake_embed_a):
            cache2 = oc.OrchestratorCache()
            check("restart: semantic index reloads from disk with 1 entry intact", len(cache2._sem) == 1)
            result2 = cache2.get("Что такое DHT?")
            check("restart: semantic hit still works with the SAME provider", result2.hit is True)

        # ── TEST14: provider switches AFTER the entry was written — must NOT match,
        #    even though the fake vectors are numerically almost identical ──
        with patch.object(oc, "SEM_INDEX_FILE", sem_file), \
             patch.object(oc, "_redis_client", return_value=_FakeRedis()), \
             patch.object(oc, "_llm_embed", side_effect=fake_embed_b):
            cache3 = oc.OrchestratorCache()
            result3 = cache3.get("Что такое DHT?")
            check(
                "TEST14: provider switch -> old entry NOT matched despite near-identical numeric vectors",
                result3.hit is False, repr(result3),
            )

            # New writes now tag entries with the NEW provider's space.
            cache3.put(
                query="Новый вопрос под новым провайдером", answer="Новый ответ" * 10,
                trust_level="HYPOTHESIS",
                epistemic={"domain": "procedural", "testability": "fully_testable"},
            )
            check("after switch, new entries are tagged with the NEW provider's space", cache3._sem[-1]["vector_space"]["model"] == "other-embedder")
            check("old (provider-A) entry is still physically present, just not matchable", len(cache3._sem) == 2)

        # ── TEST13: legacy pre-migration format (list of dicts WITHOUT vector_space) ──
        with tempfile.TemporaryDirectory() as tmp2:
            legacy_file = Path(tmp2) / "sem_index_v2.pkl"
            legacy_data = [
                {"vec": [1.0, 0.0, 0.0], "answer": "старый ответ", "trust_level": "HYPOTHESIS",
                 "version": "v1", "query": "старый вопрос", "ts": time.time()},
            ]
            with open(legacy_file, "wb") as f:
                pickle.dump(legacy_data, f)

            with patch.object(oc, "SEM_INDEX_FILE", legacy_file), \
                 patch.object(oc, "_redis_client", return_value=_FakeRedis()), \
                 patch.object(oc, "_llm_embed", side_effect=fake_embed_a):
                cache_legacy = oc.OrchestratorCache()
                check("TEST13: legacy format (no vector_space) is NOT loaded as trusted", cache_legacy._sem == [])
                quarantined = list(Path(tmp2).glob("sem_index_v2.pkl.legacy.*"))
                check("legacy file is quarantined (renamed aside), not deleted", len(quarantined) == 1, repr(list(Path(tmp2).iterdir())))
                with open(quarantined[0], "rb") as f:
                    preserved = pickle.load(f)
                check("quarantined file still contains the original answer data (nothing lost)", preserved == legacy_data)

                # Cache is now empty but fully functional going forward.
                result_legacy = cache_legacy.get("старый вопрос")
                check("legacy-quarantine: lookup against the old query is an honest miss, not a crash", result_legacy.hit is False)
                cache_legacy.put(query="старый вопрос", answer="новый пересчитанный ответ" * 5, trust_level="HYPOTHESIS",
                                  epistemic={"domain": "procedural", "testability": "fully_testable"})
                check("cache is writable again immediately after quarantine", len(cache_legacy._sem) == 1 and "vector_space" in cache_legacy._sem[0])

        # ── Corrupted (but present) vector_space metadata on one entry must not crash get() ──
        with tempfile.TemporaryDirectory() as tmp3:
            sem_file3 = Path(tmp3) / "sem_index_v2.pkl"
            corrupted_space = space_a.to_dict()
            del corrupted_space["dimension"]
            with open(sem_file3, "wb") as f:
                pickle.dump([{"vec": [1.0, 0.0, 0.0], "vector_space": corrupted_space, "answer": "x", "query": "q1", "ts": time.time()}], f)
            with patch.object(oc, "SEM_INDEX_FILE", sem_file3), \
                 patch.object(oc, "_redis_client", return_value=_FakeRedis()), \
                 patch.object(oc, "_llm_embed", side_effect=fake_embed_a):
                cache_corrupt = oc.OrchestratorCache()
                try:
                    result_corrupt = cache_corrupt.get("q1")
                    check("corrupted vector_space on one entry does not crash get() (skipped, not fatal)", result_corrupt.hit is False)
                except Exception as e:
                    check("corrupted vector_space on one entry does not crash get() (skipped, not fatal)", False, f"{type(e).__name__}: {e}")

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
