"""
assistant/orch_cache.py — Cache Layer.
Двухуровневый кэш: Redis (точный hash) + FAISS (семантический cosine).
"""
from __future__ import annotations

import hashlib
import json
import pickle
import time
from pathlib import Path
from typing import Optional

import numpy as np
import redis as _redis

from agent.orch_schemas import CacheResult, TrustLevel

REDIS_HOST   = "127.0.0.1"
REDIS_PORT   = 6379
CACHE_PREFIX = "orch:cache:"
CACHE_TTL    = 86400  # 24 часа
SEM_THRESHOLD = 0.95  # cosine similarity для семантического совпадения

BASE       = Path(__file__).parent.parent
SEM_DIR    = BASE / "registry" / "orch_cache"
SEM_DIR.mkdir(parents=True, exist_ok=True)
SEM_INDEX_FILE = SEM_DIR / "sem_index.pkl"  # простой список (vec, answer, trust, ts)


def _redis_client() -> _redis.Redis:
    return _redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)


def _hash_query(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode()).hexdigest()[:16]


def _embed_query(query: str) -> Optional[np.ndarray]:
    """Получить embedding через nomic-embed-text (без прокси)."""
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = False
        resp = s.post(
            "http://127.0.0.1:11434/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": query[:1000]},
            timeout=15,
        )
        vec = np.array(resp.json()["embedding"], dtype=np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec
    except Exception:
        return None


class OrchestratorCache:
    def __init__(self):
        self._r   = _redis_client()
        self._sem: list[dict] = self._load_sem()

    def _load_sem(self) -> list[dict]:
        if SEM_INDEX_FILE.exists():
            try:
                with open(SEM_INDEX_FILE, "rb") as f:
                    return pickle.load(f)
            except Exception:
                pass
        return []

    def _save_sem(self):
        with open(SEM_INDEX_FILE, "wb") as f:
            pickle.dump(self._sem, f)

    def get(self, query: str) -> CacheResult:
        """Поиск в кэше: сначала точный hash, потом семантический."""

        # 1. Точный match по hash (Redis)
        key = CACHE_PREFIX + _hash_query(query)
        try:
            raw = self._r.get(key)
            if raw:
                data = json.loads(raw)
                return CacheResult(
                    hit=True,
                    answer=data["answer"],
                    trust_level=data.get("trust_level", "HYPOTHESIS"),
                    similarity=1.0,
                )
        except Exception:
            pass

        # 2. Семантический match (FAISS-подобный через numpy)
        if self._sem:
            vec = _embed_query(query)
            if vec is not None:
                best_score = 0.0
                best_entry = None
                for entry in self._sem:
                    ev = np.array(entry["vec"], dtype=np.float32)
                    score = float(np.dot(vec, ev))
                    if score > best_score:
                        best_score = score
                        best_entry = entry

                if best_score >= SEM_THRESHOLD and best_entry:
                    return CacheResult(
                        hit=True,
                        answer=best_entry["answer"],
                        trust_level=best_entry.get("trust_level", "HYPOTHESIS"),
                        similarity=round(best_score, 3),
                    )

        return CacheResult(hit=False, similarity=0.0)

    def put(self, query: str, answer: str, trust_level: TrustLevel = "HYPOTHESIS"):
        """Сохранить ответ в кэш. Пустые/мусорные ответы не кэшируются."""
        _BAD_PHRASES = ("нет информации", "недостаточно для формирования", "данных недостаточно")
        # Маркеры сырых документов из контекста FAISS
        _DOC_MARKERS = ("(доверие: HYPOTHESIS", "(доверие: VERIFIED", "Вопрос:", "Ответ: Claude")
        if len(answer) < 50 or any(p in answer for p in _BAD_PHRASES):
            return
        if any(m in answer for m in _DOC_MARKERS):
            return  # Синтезатор не переработал контекст — не кэшировать

        # Redis (точный hash)
        key  = CACHE_PREFIX + _hash_query(query)
        data = json.dumps({"answer": answer, "trust_level": trust_level, "ts": time.time()})
        try:
            self._r.setex(key, CACHE_TTL, data.encode())
        except Exception:
            pass

        # Семантический индекс
        vec = _embed_query(query)
        if vec is not None:
            self._sem.append({
                "vec":         vec.tolist(),
                "answer":      answer,
                "trust_level": trust_level,
                "query":       query[:200],
                "ts":          time.time(),
            })
            self._save_sem()

    def invalidate(self, query: str):
        """Удалить конкретный запрос из кэша."""
        key = CACHE_PREFIX + _hash_query(query)
        try:
            self._r.delete(key)
        except Exception:
            pass

    def stats(self) -> dict:
        try:
            redis_keys = len(self._r.keys(CACHE_PREFIX + "*"))
        except Exception:
            redis_keys = 0
        return {
            "redis_entries": redis_keys,
            "semantic_entries": len(self._sem),
        }


_cache: Optional[OrchestratorCache] = None

def get_cache() -> OrchestratorCache:
    global _cache
    if _cache is None:
        _cache = OrchestratorCache()
    return _cache


if __name__ == "__main__":
    c = get_cache()
    print("Stats before:", c.stats())

    q = "Как работает DHT в P2P-сетях?"
    r = c.get(q)
    print(f"Cache miss: {r.hit}")

    c.put(q, "DHT — это распределённая хэш-таблица...", "HYPOTHESIS")
    r = c.get(q)
    print(f"Cache hit (exact): {r.hit}, similarity={r.similarity}")
    print("Stats after:", c.stats())
