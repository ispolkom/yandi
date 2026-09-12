"""
assistant/orch_registry_search.py — Local Registry Search V2.

Улучшения:
  - Динамическое количество документов (MIN_DOCS = 3, MAX_DOCS = 10)
  - Фильтрация по тематике (domain/topic)
  - Переранжирование кандидатов по релевантности
  - Минимальный порог релевантности (MIN_RELEVANCE_THRESHOLD = 0.35)

CLI:
  python3 assistant/orch_registry_search.py "запрос" [--top-k 5] [--domain general]
  python3 assistant/orch_registry_search.py --stats
"""
from __future__ import annotations

import sys
sys.path.insert(0, '/home/iam/yandi')

import json
import pickle
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

import numpy as np
import faiss

from llm_gateway import embed as _llm_embed
from llm_gateway import vector_space as _vs

from agent.orch_schemas import SearchDoc, SearchResult

BASE       = Path(__file__).parent.parent
INDEX_DIR  = BASE / "registry" / "orch_index"
INDEX_DIR.mkdir(parents=True, exist_ok=True)
INDEX_FILE = INDEX_DIR / "faiss.index"
DOCS_FILE  = INDEX_DIR / "docs.pkl"
# Отдельный файл, не поле внутри docs.pkl — весь docs.pkl исторически
# читался как "просто список словарей", лишний нераспознанный ключ
# внутри рискованнее трогать, чем добавить файл рядом (см.
# YANDI_EMBEDDINGS_ARCHITECTURE_AUDIT.md §6 — риск смешивания векторных
# пространств: этот файл — минимальная разметка происхождения ВСЕГО
# индекса разом, один embedding-провайдер на все документы в нём).
VECTOR_SPACE_FILE = INDEX_DIR / "vector_space.json"

EMBED_MODEL = "nomic-embed-text:latest"
TOP_K      = 5
MIN_DOCS   = 3
MAX_DOCS   = 10
CONF_THRESHOLD = 1.0
MIN_RELEVANCE_THRESHOLD = 0.35

DATA_SOURCES = [
    BASE / "registry" / "dataset" / "model_sessions",
    BASE / "registry" / "dataset" / "final",
    BASE / "registry" / "dataset" / "orch_traces",
]


def _embed(text: str) -> tuple[np.ndarray, _vs.VectorSpaceId]:
    """Через llm_gateway.embed() — сам эндпоинт/формат ответа/backend
    больше не известен этому файлу (было: прямой POST на Ollama). L2-
    нормализация остаётся ЗДЕСЬ, не в шлюзе — FAISS IndexFlatIP требует
    нормализованные векторы для cosine-подобного поведения, это свойство
    ЭТОГО индекса, не общее свойство эмбеддингов вообще."""
    result = _llm_embed(text[:2000], model=EMBED_MODEL)
    arr = np.array(result.vectors[0], dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr /= norm
    return arr, result.space


def _extract_docs_from_file(path: Path) -> list[dict]:
    docs = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)

            if "question" in rec and "answer" in rec and rec.get("answer"):
                text = f"Вопрос: {rec['question']}\nОтвет: {rec['answer']}"
                docs.append({
                    "text": text[:3000],
                    "trust_level": "UNVERIFIED",
                    "source": str(path.name),
                    "topic": rec.get("topic", "general"),
                    "question": rec.get("question", ""),
                    "domain": rec.get("domain", "general"),
                })
            elif "messages" in rec:
                msgs = rec["messages"]
                user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
                asst = next((m["content"] for m in msgs if m.get("role") == "assistant"), "")
                if user and asst:
                    text = f"Вопрос: {user}\nОтвет: {asst}"
                    docs.append({
                        "text": text[:3000],
                        "trust_level": "UNVERIFIED",
                        "source": str(path.name),
                        "topic": rec.get("topic", "general"),
                        "question": user,
                        "domain": rec.get("domain", "general"),
                    })
    except Exception as e:
        print(f"  [registry_search] skip {path.name}: {e}")
    return docs


class RegistrySearchIndex:
    def __init__(self):
        self._index: Optional[faiss.IndexFlatIP] = None
        self._docs: list[dict] = []
        self._dim: int = 0
        self._space: Optional[_vs.VectorSpaceId] = None

    def _load(self) -> bool:
        if not (INDEX_FILE.exists() and DOCS_FILE.exists()):
            return False
        if not VECTOR_SPACE_FILE.exists():
            # Индекс существует, но без разметки происхождения векторов —
            # до-миграционный файл. Доказать совместимость с текущим
            # embedding-провайдером нельзя — не грузим как доверенный,
            # вызывающий код (build()) пересоберёт с нуля из тех же
            # исходных JSONL (дёшево, документы там сохранены отдельно).
            print(
                "[registry_search] Найден индекс без vector_space.json (до-миграционный) — "
                "происхождение векторов доказать нельзя, будет пересобран."
            )
            return False
        try:
            self._index = faiss.read_index(str(INDEX_FILE))
            with open(DOCS_FILE, "rb") as f:
                self._docs = pickle.load(f)
            with open(VECTOR_SPACE_FILE, "r", encoding="utf-8") as f:
                self._space = _vs.VectorSpaceId.from_dict(json.load(f))
            self._dim = self._index.d
            return True
        except Exception:
            return False

    def _save(self):
        faiss.write_index(self._index, str(INDEX_FILE))
        with open(DOCS_FILE, "wb") as f:
            pickle.dump(self._docs, f)
        with open(VECTOR_SPACE_FILE, "w", encoding="utf-8") as f:
            json.dump(self._space.to_dict(), f)

    def build(self, force: bool = False):
        if not force and self._load():
            print(
                f"[registry_search] Индекс загружен: {len(self._docs)} документов, "
                f"vector_space={self._space.fingerprint()}"
            )
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
        space: Optional[_vs.VectorSpaceId] = None
        for i, doc in enumerate(all_docs):
            try:
                vec, doc_space = _embed(doc["text"][:1000])
                if space is None:
                    space = doc_space
                elif not _vs.compatible(space, doc_space):
                    # Не ожидается в одном проходе (тот же провайдер на
                    # каждой итерации) — но если он всё же сменился ПОСРЕДИ
                    # построения индекса, не смешиваем разные пространства
                    # в одном FAISS-индексе.
                    print(f"  skip doc {i}: embedding-пространство сменилось посреди построения индекса")
                    continue
                vectors.append(vec)
                valid_docs.append(doc)
                if (i + 1) % 10 == 0:
                    print(f"  {i+1}/{len(all_docs)}...")
            except Exception as e:
                print(f"  skip doc {i}: {e}")

        if not vectors or space is None:
            return

        mat = np.stack(vectors)
        dim = mat.shape[1]
        self._dim = dim
        self._space = space
        self._index = faiss.IndexFlatIP(dim)
        self._index.add(mat)
        self._docs = valid_docs
        self._save()
        print(
            f"[registry_search] Индекс готов: {len(valid_docs)} документов, dim={dim}, "
            f"vector_space={space.fingerprint()}"
        )

    def search(
        self,
        query: str,
        top_k: int = TOP_K,
        domain: Optional[str] = None,
    ) -> SearchResult:
        if self._index is None:
            if not self._load():
                self.build()
            if self._index is None:
                return SearchResult(docs=[], confidence=0.0, source="local", top_k=top_k)

        try:
            vec, query_space = _embed(query)
        except Exception as e:
            print(f"[registry_search] search error: {e}")
            return SearchResult(docs=[], confidence=0.0, source="local", top_k=top_k)

        if not _vs.compatible(self._space, query_space):
            # Индекс на диске построен другим embedding-пространством, чем
            # то, что реально отвечает прямо сейчас (провайдер сменился
            # между запусками) — НЕ сравниваем несовместимые векторы,
            # пересобираем индекс текущим провайдером (дёшево — исходные
            # документы отдельно в JSONL, не только в этом индексе).
            print(
                f"[registry_search] пространство запроса ({query_space.fingerprint()}) не совпадает "
                f"с пространством индекса ({self._space.fingerprint() if self._space else 'нет'}) "
                f"— пересобираю индекс, а не сравниваю несовместимые векторы."
            )
            self.build(force=True)
            if self._index is None or not _vs.compatible(self._space, query_space):
                # Пересборка не помогла (например сам embedding-провайдер
                # недоступен) — безопаснее вернуть "ничего не найдено", чем
                # сравнить векторы из разных пространств.
                return SearchResult(docs=[], confidence=0.0, source="local", top_k=top_k)

        try:
            vec_2d = vec.reshape(1, -1)
            scores, indices = self._index.search(vec_2d, min(MAX_DOCS, len(self._docs)))
        except Exception as e:
            print(f"[registry_search] search error: {e}")
            return SearchResult(docs=[], confidence=0.0, source="local", top_k=top_k)

        docs = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._docs):
                continue
            if score < MIN_RELEVANCE_THRESHOLD:
                continue

            d = self._docs[idx]
            doc_domain = d.get("domain", "general")
            if domain and domain != "general" and doc_domain != domain:
                if not any(t in doc_domain for t in domain.split(":")):
                    continue

            docs.append(SearchDoc(
                text=d["text"],
                trust_level=d.get("trust_level", "HYPOTHESIS"),
                score=float(score),
                source=d.get("source", "council_synthesis"),
                topic=d.get("topic", d.get("domain", "general")),
                meta={"question": d.get("question", ""), "domain": doc_domain},
            ))

        def _rank(doc: SearchDoc) -> float:
            boost = 0.05 if doc.meta and doc.meta.get("question") else 0.0
            return doc.score + boost

        docs.sort(key=_rank, reverse=True)
        docs = docs[:max(MIN_DOCS, min(top_k, MAX_DOCS))]

        confidence = sum(d.score for d in docs) / len(docs) if docs else 0.0
        confidence = round(min(1.0, confidence), 3)

        return SearchResult(
            docs=docs,
            confidence=confidence,
            source="local",
            top_k=top_k,
        )


_search_index: Optional[RegistrySearchIndex] = None


def get_search_index() -> RegistrySearchIndex:
    global _search_index
    if _search_index is None:
        _search_index = RegistrySearchIndex()
        _search_index.build()
    return _search_index


def search_registry(
    query: str,
    top_k: int = TOP_K,
    domain: Optional[str] = None,
) -> SearchResult:
    return get_search_index().search(query, top_k, domain)


if __name__ == "__main__":
    import sys
    if "--build" in sys.argv:
        idx = RegistrySearchIndex()
        idx.build(force=True)
    elif "--stats" in sys.argv:
        idx = get_search_index()
        print(f"Индекс: {len(idx._docs)} документов, dim={idx._dim}")
        domains = {}
        for d in idx._docs:
            dom = d.get("domain", "general")
            domains[dom] = domains.get(dom, 0) + 1
        print(f"  По доменам: {domains}")
    else:
        query = " ".join(sys.argv[1:]) or "DHT distributed hash table P2P LLM"
        domain = None
        if "--domain" in sys.argv:
            idx = sys.argv.index("--domain")
            domain = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        print(f"Поиск: {query} (domain={domain})")
        result = search_registry(query, domain=domain)
        print(f"\nНайдено: {len(result.docs)} документов, confidence={result.confidence:.3f}")
        for i, doc in enumerate(result.docs[:5]):
            print(f"\n[{i+1}] score={doc.score:.3f} trust={doc.trust_level} src={doc.source}")
            print(f"  {doc.text[:200]}...")
