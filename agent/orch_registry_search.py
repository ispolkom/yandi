"""
assistant/orch_registry_search.py — Local Registry Search.
Векторный поиск по локальной базе знаний: nomic-embed-text + FAISS.
Индексирует model_sessions, final dataset, orch_traces.
"""
from __future__ import annotations

import json
import pickle
import time
from pathlib import Path
from typing import Optional

import numpy as np
import faiss

import requests as _requests
_session = _requests.Session()
_session.trust_env = False  # игнорировать HTTP_PROXY для localhost/Ollama

from agent.orch_schemas import SearchDoc, SearchResult, TrustLevel

BASE       = Path(__file__).parent.parent
INDEX_DIR  = BASE / "registry" / "orch_index"
INDEX_DIR.mkdir(parents=True, exist_ok=True)
INDEX_FILE = INDEX_DIR / "faiss.index"
DOCS_FILE  = INDEX_DIR / "docs.pkl"

OLLAMA   = "http://127.0.0.1:11434"
EMBED_MODEL = "nomic-embed-text"
TOP_K    = 5
CONF_THRESHOLD = 0.55  # cosine similarity порог (nomic-embed даёт max ~0.6 для разных формулировок)
MIN_DOC_SCORE  = 0.35  # минимальная оценка для включения документа в контекст

# Источники для индексации
DATA_SOURCES = [
    BASE / "registry" / "dataset" / "model_sessions",
    BASE / "registry" / "dataset" / "final",
    BASE / "registry" / "dataset" / "orch_traces",
]


def _embed(text: str) -> np.ndarray:
    """Получить embedding через nomic-embed-text."""
    resp = _session.post(
        f"{OLLAMA}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text[:2000]},
        timeout=30,
    )
    resp.raise_for_status()
    vec = resp.json()["embedding"]
    arr = np.array(vec, dtype=np.float32)
    # Нормализовать для cosine similarity
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr /= norm
    return arr


def _trust_from_source(source: str, outcome: str = "") -> TrustLevel:
    if "model_sessions" in source and outcome == "success":
        return "HYPOTHESIS"
    if "final" in source:
        return "VERIFIED"
    if "orch_traces" in source:
        return "HYPOTHESIS"
    return "PERSONAL"


def _extract_docs_from_file(path: Path) -> list[dict]:
    """Извлечь документы из JSONL файла."""
    docs = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)

            # model_sessions формат
            if "question" in rec and "answer" in rec and rec.get("answer"):
                text = f"Вопрос: {rec['question']}\nОтвет: {rec['answer']}"
                docs.append({
                    "text": text[:3000],
                    "trust_level": _trust_from_source(str(path), rec.get("outcome", "")),
                    "source": str(path.name),
                    "topic": rec.get("topic", ""),
                })

            # final dataset / HuggingFace формат
            elif "messages" in rec:
                msgs = rec["messages"]
                user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
                asst = next((m["content"] for m in msgs if m.get("role") == "assistant"), "")
                if user and asst:
                    text = f"Вопрос: {user}\nОтвет: {asst}"
                    docs.append({
                        "text": text[:3000],
                        "trust_level": _trust_from_source(str(path)),
                        "source": str(path.name),
                        "topic": rec.get("topic", ""),
                    })

            # orch_traces формат
            elif "request" in rec and "response" in rec:
                text = f"Запрос: {rec['request']}\nОтвет: {rec.get('response', '')}"
                docs.append({
                    "text": text[:3000],
                    "trust_level": "HYPOTHESIS",
                    "source": str(path.name),
                    "topic": rec.get("skill", ""),
                })
    except Exception as e:
        print(f"  [registry_search] skip {path.name}: {e}")
    return docs


class RegistrySearchIndex:
    def __init__(self):
        self._index: Optional[faiss.IndexFlatIP] = None
        self._docs: list[dict] = []
        self._dim: int = 0

    def _load(self) -> bool:
        if INDEX_FILE.exists() and DOCS_FILE.exists():
            try:
                self._index = faiss.read_index(str(INDEX_FILE))
                with open(DOCS_FILE, "rb") as f:
                    self._docs = pickle.load(f)
                self._dim = self._index.d
                return True
            except Exception:
                pass
        return False

    def _save(self):
        faiss.write_index(self._index, str(INDEX_FILE))
        with open(DOCS_FILE, "wb") as f:
            pickle.dump(self._docs, f)

    def build(self, force: bool = False):
        """Построить или перестроить индекс из всех источников."""
        if not force and self._load():
            print(f"[registry_search] Индекс загружен: {len(self._docs)} документов")
            return

        print("[registry_search] Строю индекс...")
        all_docs = []
        for src_dir in DATA_SOURCES:
            if not src_dir.exists():
                continue
            for path in sorted(src_dir.glob("*.jsonl")):
                docs = _extract_docs_from_file(path)
                all_docs.extend(docs)
                if docs:
                    print(f"  {path.name}: {len(docs)} документов")

        if not all_docs:
            print("[registry_search] Нет документов для индексации")
            return

        print(f"[registry_search] Embedding {len(all_docs)} документов...")
        vectors = []
        valid_docs = []
        for i, doc in enumerate(all_docs):
            try:
                vec = _embed(doc["text"][:1000])  # первые 1k символов для embed
                vectors.append(vec)
                valid_docs.append(doc)
                if (i + 1) % 10 == 0:
                    print(f"  {i+1}/{len(all_docs)}...")
            except Exception as e:
                print(f"  skip doc {i}: {e}")

        if not vectors:
            return

        mat = np.stack(vectors)
        dim = mat.shape[1]
        self._dim = dim
        self._index = faiss.IndexFlatIP(dim)  # Inner Product = cosine после нормализации
        self._index.add(mat)
        self._docs = valid_docs
        self._save()
        print(f"[registry_search] Индекс готов: {len(valid_docs)} документов, dim={dim}")

    def search(self, query: str, top_k: int = TOP_K) -> SearchResult:
        """Поиск документов по запросу."""
        if self._index is None:
            if not self._load():
                self.build()
            if self._index is None:
                return SearchResult(docs=[], confidence=0.0, source="local", top_k=top_k)

        try:
            vec = _embed(query).reshape(1, -1)
            scores, indices = self._index.search(vec, min(top_k, len(self._docs)))
        except Exception as e:
            print(f"[registry_search] search error: {e}")
            return SearchResult(docs=[], confidence=0.0, source="local", top_k=top_k)

        docs = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or score < MIN_DOC_SCORE:
                continue
            d = self._docs[idx]
            docs.append(SearchDoc(
                text=d["text"],
                trust_level=d.get("trust_level", "HYPOTHESIS"),
                score=float(score),
                source=d.get("source", "council_synthesis"),
                topic=d.get("topic", d.get("domain", "")),
            ))

        # Confidence = среднее top-3
        top3_scores = [d.score for d in docs[:3]]
        confidence = sum(top3_scores) / len(top3_scores) if top3_scores else 0.0

        return SearchResult(
            docs=docs,
            confidence=round(confidence, 3),
            source="local",
            top_k=top_k,
        )

    def add_entry(self, question: str, synthesis: str, domain: str = "", models: list | None = None) -> bool:
        """Добавить новый Q&A синтез в индекс без полной перестройки."""
        if self._index is None:
            if not self._load():
                self.build()
        if self._index is None:
            return False

        text = f"Вопрос: {question}\nОтвет: {synthesis}"
        try:
            vec = _embed(text[:1000])
        except Exception as e:
            print(f"[registry_search] embed error: {e}")
            return False

        if vec.shape[0] != self._dim:
            return False

        mat = vec.reshape(1, -1)
        self._index.add(mat)
        self._docs.append({
            "text": text,
            "source": "council_synthesis",
            "domain": domain,
            "models": models or [],
            "question": question,
        })
        self._save()

        # Сохраняем также в JSONL для будущих перестроек
        jsonl_path = BASE / "registry" / "dataset" / "council_synthesis" / "entries.jsonl"
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        from datetime import datetime as _dt
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps({
                "text": text, "question": question, "synthesis": synthesis,
                "domain": domain, "models": models or [],
                "ts": _dt.now().isoformat(),
            }, ensure_ascii=False) + "\n")

        print(f"[registry_search] +1 запись в реестр (всего {len(self._docs)})")
        return True

    def stats(self) -> dict:
        if self._index is None:
            self._load()
        return {
            "docs": len(self._docs),
            "dim": self._dim,
            "index_built": self._index is not None,
        }


# Синглтон
_search_index: Optional[RegistrySearchIndex] = None

def get_search_index() -> RegistrySearchIndex:
    global _search_index
    if _search_index is None:
        _search_index = RegistrySearchIndex()
        _search_index.build()
    return _search_index


def search_registry(query: str, top_k: int = TOP_K) -> SearchResult:
    """Удобная функция для поиска."""
    return get_search_index().search(query, top_k)


def store_synthesis(question: str, synthesis: str, domain: str = "", models: list | None = None) -> bool:
    """Сохранить синтез совета в реестр знаний."""
    return get_search_index().add_entry(question, synthesis, domain, models)


if __name__ == "__main__":
    import sys
    if "--build" in sys.argv:
        idx = RegistrySearchIndex()
        idx.build(force=True)
    else:
        query = " ".join(sys.argv[1:]) or "DHT distributed hash table P2P LLM"
        print(f"Поиск: {query}")
        result = search_registry(query)
        print(f"\nНайдено: {len(result.docs)} документов, confidence={result.confidence:.3f}")
        print(f"Достаточно для ответа: {'ДА' if result.confidence >= CONF_THRESHOLD else 'НЕТ'}")
        for i, doc in enumerate(result.docs[:3]):
            print(f"\n[{i+1}] score={doc.score:.3f} trust={doc.trust_level} src={doc.source}")
            print(f"  {doc.text[:200]}...")
