"""
assistant/orch_cache.py — Cache Layer v2.
Двухуровневый кэш: Redis (точный hash) + FAISS (семантический cosine).
С поддержкой версии индекса для инвалидации.
И с полным хранением объекта знания (claims, evidence, epistemic).

v2:
- Хранит полный объект: claims, evidence, epistemic
- TTL для разных типов
- Инвалидация по версии
- Фрейм-карта для интерпретативных вопросов
"""
from __future__ import annotations

import sys
from pathlib import Path
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import hashlib
import json
import pickle
import time
from typing import Optional, Dict, Any, List

import numpy as np
import redis as _redis

from agent.orch_schemas import CacheResult, TrustLevel, EvidenceRecord, ClaimRecord

REDIS_HOST   = "127.0.0.1"
REDIS_PORT   = 6379
CACHE_PREFIX = "orch:cache:v2:"
CACHE_TTL    = 86400  # 24 часа
SEM_THRESHOLD = 0.95  # cosine similarity для семантического совпадения

BASE_DIR     = Path(__file__).parent.parent
SEM_DIR      = BASE_DIR / "registry" / "orch_cache"
SEM_DIR.mkdir(parents=True, exist_ok=True)
SEM_INDEX_FILE = SEM_DIR / "sem_index_v2.pkl"

# Версия индекса для инвалидации кэша
INDEX_VERSION_FILE = BASE_DIR / "registry" / "orch_index" / "version.txt"


def _get_index_version() -> str:
    """Получить версию индекса (или hash документов)."""
    try:
        if INDEX_VERSION_FILE.exists():
            return INDEX_VERSION_FILE.read_text().strip()
    except Exception:
        pass
    try:
        docs_file = BASE_DIR / "registry" / "orch_index" / "docs.pkl"
        if docs_file.exists():
            with open(docs_file, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
        pass
    return "unknown"


def _redis_client() -> _redis.Redis:
    return _redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)


def _hash_query(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode()).hexdigest()[:16]


def _cache_key(query: str) -> str:
    """Ключ кэша с учётом версии индекса."""
    version = _get_index_version()
    hash_q = _hash_query(query)
    return f"{CACHE_PREFIX}{version}:{hash_q}"


def _embed_query(query: str) -> Optional[np.ndarray]:
    """Получить embedding через nomic-embed-text (без прокси)."""
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = False
        resp = s.post(
            "http://127.0.0.1:11434/api/embeddings",
            json={"model": "nomic-embed-text:latest", "prompt": query[:1000]},
            timeout=15,
        )
        vec = np.array(resp.json()["embedding"], dtype=np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec
    except Exception:
        return None


def _get_ttl(epistemic: Optional[Dict[str, Any]] = None) -> int:
    """
    Получить TTL в зависимости от типа вопроса.
    """
    if not epistemic:
        return CACHE_TTL
    
    domain = epistemic.get("domain", "")
    testability = epistemic.get("testability", "")
    
    # Факты — долго
    if testability == "fully_testable":
        return CACHE_TTL * 30  # 30 дней
    
    # Научные данные — средний срок
    if domain in ["scientific", "mathematical"]:
        return CACHE_TTL * 7  # 7 дней
    
    # Новости/события — коротко
    if domain == "historical" and testability == "fully_testable":
        return CACHE_TTL  # 1 день
    
    # Интерпретативные — долго (меняются редко)
    if testability in ["interpretive", "non_falsifiable"]:
        return CACHE_TTL * 90  # 90 дней
    
    # Медиа — средний срок
    if domain == "media_interpretation":
        return CACHE_TTL * 14  # 14 дней
    
    return CACHE_TTL


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
        version = _get_index_version()

        # 1. Точный match по hash (Redis) с версией
        key = _cache_key(query)
        try:
            raw = self._r.get(key)
            if raw:
                data = json.loads(raw)
                if data.get("version") == version:
                    return CacheResult(
                        hit=True,
                        answer=data["answer"],
                        trust_level=data.get("trust_level", "HYPOTHESIS"),
                        similarity=1.0,
                        claims=data.get("claims", []),
                        evidence=data.get("evidence", []),
                        epistemic=data.get("epistemic", {}),
                        entity_id=data.get("entity_id"),
                        entity_type=data.get("entity_type"),
                        created_at=data.get("created_at", time.time()),
                        ttl=data.get("ttl", CACHE_TTL),
                    )
                else:
                    self._r.delete(key)
        except Exception:
            pass

        # 2. Семантический match
        if self._sem:
            vec = _embed_query(query)
            if vec is not None:
                best_score = 0.0
                best_entry = None
                for entry in self._sem:
                    ev = np.array(entry["vec"], dtype=np.float32)
                    norm_ev = ev / np.linalg.norm(ev) if np.linalg.norm(ev) > 0 else ev
                    score = float(np.dot(vec, norm_ev))
                    if score > best_score:
                        best_score = score
                        best_entry = entry

                if best_score >= SEM_THRESHOLD and best_entry:
                    if best_entry.get("version") == version:
                        return CacheResult(
                            hit=True,
                            answer=best_entry["answer"],
                            trust_level=best_entry.get("trust_level", "HYPOTHESIS"),
                            similarity=round(best_score, 3),
                            claims=best_entry.get("claims", []),
                            evidence=best_entry.get("evidence", []),
                            epistemic=best_entry.get("epistemic", {}),
                            entity_id=best_entry.get("entity_id"),
                            entity_type=best_entry.get("entity_type"),
                            created_at=best_entry.get("created_at", time.time()),
                            ttl=best_entry.get("ttl", CACHE_TTL),
                        )

        return CacheResult(
            hit=False,
            answer="",
            trust_level="HYPOTHESIS",
            similarity=0.0,
            claims=[],
            evidence=[],
            epistemic={},
        )

    def put(
        self,
        query: str,
        answer: str,
        trust_level: TrustLevel = "HYPOTHESIS",
        claims: Optional[List[Dict[str, Any]]] = None,
        evidence: Optional[List[Dict[str, Any]]] = None,
        epistemic: Optional[Dict[str, Any]] = None,
        entity_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        created_at: Optional[float] = None,
    ):
        """
        Сохранить ответ в кэш с полным объектом.
        """
        if trust_level == "UNVERIFIED":
            return

        _BAD_PHRASES = ("нет информации", "недостаточно для формирования", "данных недостаточно")
        if len(answer) < 50 or any(p in answer for p in _BAD_PHRASES):
            return

        version = _get_index_version()
        ttl = _get_ttl(epistemic)
        ts = created_at or time.time()

        claims_data = claims or []
        evidence_data = evidence or []
        epistemic_data = epistemic or {}

        key = _cache_key(query)
        data = json.dumps({
            "answer": answer,
            "trust_level": trust_level,
            "version": version,
            "claims": claims_data,
            "evidence": evidence_data,
            "epistemic": epistemic_data,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "created_at": ts,
            "ttl": ttl,
            "ts": ts,
        })
        try:
            self._r.setex(key, ttl, data.encode())
        except Exception:
            pass

        vec = _embed_query(query)
        if vec is not None:
            self._sem.append({
                "vec": vec.tolist(),
                "answer": answer,
                "trust_level": trust_level,
                "version": version,
                "claims": claims_data,
                "evidence": evidence_data,
                "epistemic": epistemic_data,
                "entity_id": entity_id,
                "entity_type": entity_type,
                "created_at": ts,
                "ttl": ttl,
                "query": query[:200],
                "ts": ts,
            })
            self._save_sem()

    def put_from_synthesis(
        self,
        query: str,
        synthesis_result,
        epistemic: Optional[Dict[str, Any]] = None,
        claims: Optional[List[Dict[str, Any]]] = None,
        evidence: Optional[List[Dict[str, Any]]] = None,
        entity_id: Optional[str] = None,
        entity_type: Optional[str] = None,
    ):
        """
        Сохранить результат синтеза в кэш.
        """
        claims = claims or []
        evidence = evidence or []
        if hasattr(synthesis_result, "_reasoning"):
            reasoning = getattr(synthesis_result, "_reasoning", {})
            if not claims:
                claims = reasoning.get("claims", [])
            if not evidence:
                evidence = reasoning.get("evidence_records", [])

        self.put(
            query=query,
            answer=synthesis_result.answer,
            trust_level=synthesis_result.trust_level,
            claims=claims,
            evidence=evidence,
            epistemic=epistemic,
            entity_id=entity_id,
            entity_type=entity_type,
        )

    def invalidate(self, query: str):
        """Удалить конкретный запрос из кэша."""
        key = _cache_key(query)
        try:
            self._r.delete(key)
        except Exception:
            pass

    def invalidate_all(self):
        """Полная инвалидация кэша."""
        try:
            for key in self._r.scan_iter(f"{CACHE_PREFIX}*"):
                self._r.delete(key)
        except Exception:
            pass
        self._sem = []
        self._save_sem()

    def invalidate_by_entity(self, entity_id: str):
        """Инвалидация кэша по entity_id."""
        try:
            for key in self._r.scan_iter(f"{CACHE_PREFIX}*"):
                raw = self._r.get(key)
                if raw:
                    data = json.loads(raw)
                    if data.get("entity_id") == entity_id:
                        self._r.delete(key)
        except Exception:
            pass
        
        self._sem = [e for e in self._sem if e.get("entity_id") != entity_id]
        self._save_sem()

    def stats(self) -> dict:
        try:
            redis_keys = len(list(self._r.scan_iter(f"{CACHE_PREFIX}*")))
        except Exception:
            redis_keys = 0
        return {
            "redis_entries": redis_keys,
            "semantic_entries": len(self._sem),
            "index_version": _get_index_version(),
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

    c.put(
        query=q,
        answer="DHT — это распределённая хэш-таблица...",
        trust_level="HYPOTHESIS",
        claims=[{"claim_id": "cl_001", "claim_text": "DHT — распределённая система"}],
        evidence=[{"evidence_id": "ev_001", "source_uri": "https://wikipedia.org/dht"}],
        epistemic={"domain": "procedural", "testability": "fully_testable"},
    )
    r = c.get(q)
    print(f"Cache hit (exact): {r.hit}, similarity={r.similarity}")
    if r.hit:
        print(f"  claims: {len(r.claims)}")
        print(f"  evidence: {len(r.evidence)}")
        print(f"  epistemic: {r.epistemic}")
    
    print("Stats after:", c.stats())
