#!/usr/bin/env python3
"""
assistant/embeddings.py — эмбеддинги + тематическая кластеризация + семантический поиск.

EmbeddingSkill:    текст → вектор (sentence-transformers или TF-IDF fallback)
TopicClusterer:    HDBSCAN кластеризация сессий датасета
SemanticSearch:    FAISS поиск похожих сессий
DatasetValidator:  авто-валидация спорных записей через совет моделей

Команды:
  python3 embeddings.py cluster    — кластеризовать последний датасет
  python3 embeddings.py search "запрос"
  python3 embeddings.py validate
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import redis

BASE        = Path(__file__).parent.parent
DATASET_DIR = BASE / "registry" / "dataset"
FINAL_DIR   = DATASET_DIR / "final"
EMB_DIR     = DATASET_DIR / "embeddings"
TOPICS_FILE = DATASET_DIR / "topics.json"
VAL_DIR     = DATASET_DIR / "validation_reports"

for d in (EMB_DIR, VAL_DIR):
    d.mkdir(parents=True, exist_ok=True)

COUNCIL_API = "http://127.0.0.1:9010"
REPORT_CH   = "council:skill:report"
REPORT_KEY  = "council:skill:reports"


# ── helpers ───────────────────────────────────────────────────────────────────

def _publish(r: redis.Redis, report: dict):
    payload = json.dumps(report, ensure_ascii=False)
    r.lpush(REPORT_KEY, payload)
    r.ltrim(REPORT_KEY, 0, 49)
    r.publish(REPORT_CH, payload)


def _load_final_rows() -> list[dict]:
    finals = sorted(FINAL_DIR.glob("*_hf.jsonl"), reverse=True)
    if not finals:
        return []
    rows = []
    with open(finals[0], encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


# ── EmbeddingSkill ────────────────────────────────────────────────────────────

class EmbeddingSkill:
    """
    Текст → вектор.
    Приоритет: sentence-transformers > TF-IDF fallback.
    Модель кешируется в памяти после первой загрузки.
    """

    MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"  # ~120MB, русский + английский
    _model = None
    _tfidf = None
    _tfidf_matrix = None
    _tfidf_texts  = None

    def _load_model(self):
        if self._model is not None:
            return True
        try:
            from sentence_transformers import SentenceTransformer
            print(f"  [embed] загружаю {self.MODEL_NAME}...")
            EmbeddingSkill._model = SentenceTransformer(self.MODEL_NAME)
            print("  [embed] модель готова")
            return True
        except Exception as e:
            print(f"  [embed] sentence-transformers недоступен ({e}), использую TF-IDF")
            return False

    def encode(self, texts: list[str]) -> np.ndarray:
        """texts → matrix (N, dim)."""
        if not texts:
            return np.zeros((0, 384))

        if self._load_model():
            return self._model.encode(texts, show_progress_bar=False,
                                      convert_to_numpy=True, normalize_embeddings=True)

        # TF-IDF fallback
        return self._tfidf_encode(texts)

    def _tfidf_encode(self, texts: list[str]) -> np.ndarray:
        from sklearn.feature_extraction.text import TfidfVectorizer
        vec = TfidfVectorizer(max_features=512, sublinear_tf=True)
        matrix = vec.fit_transform(texts).toarray().astype(np.float32)
        # L2 normalize
        norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
        return matrix / norms

    def similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


# ── TopicClusterer ────────────────────────────────────────────────────────────

class TopicClusterer:
    """HDBSCAN кластеризация сессий датасета."""

    def __init__(self, r: redis.Redis):
        self.r     = r
        self.embed = EmbeddingSkill()

    def _hdbscan_available(self) -> bool:
        try:
            import hdbscan  # noqa
            return True
        except ImportError:
            return False

    def cluster(self, min_cluster_size: int = 2, verbose: bool = True) -> dict:
        ts = datetime.now().strftime("%H:%M:%S")
        rows = _load_final_rows()
        if not rows:
            report = {"skill": "cluster", "ts": ts, "status": "no_data",
                      "message": "нет финальных датасетов"}
            _publish(self.r, report)
            return report

        # Группируем по session_id, берём текст сессии
        sessions: dict[str, list[str]] = {}
        for row in rows:
            sid  = row.get("session_id", "?")
            text = row.get("content", "")
            sessions.setdefault(sid, []).append(text)

        texts    = [" ".join(v[:5])[:1000] for v in sessions.values()]
        sids     = list(sessions.keys())

        if verbose:
            print(f"  [cluster] эмбеддинги для {len(texts)} сессий...")
        vectors = self.embed.encode(texts)

        n = len(texts)
        if n < 6:
            # Мало данных — KMeans с мягким числом кластеров
            labels = self._kmeans_fallback(vectors, n_clusters=max(2, n // 2))
        elif self._hdbscan_available():
            labels = self._hdbscan(vectors, min_cluster_size)
            # Если HDBSCAN всё размечает как шум — fallback
            if all(l == -1 for l in labels):
                labels = self._kmeans_fallback(vectors)
        else:
            labels = self._kmeans_fallback(vectors)

        # Собираем результат
        topic_map: dict[str, dict] = {}
        for sid, label in zip(sids, labels):
            topic_key = f"topic_{label}" if label >= 0 else "noise"
            topic_map.setdefault(topic_key, {"sessions": [], "label": label})
            topic_map[topic_key]["sessions"].append(sid)

        # Сохраняем
        result = {
            "ts":            datetime.now().isoformat(),
            "sessions_total": len(sids),
            "topics_found":  len([t for t in topic_map if t != "noise"]),
            "noise_count":   len(topic_map.get("noise", {}).get("sessions", [])),
            "topics":        topic_map,
            "session_labels": dict(zip(sids, [int(l) for l in labels])),
        }
        TOPICS_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))

        # Финальные датасеты по темам
        self._write_topic_datasets(rows, result["session_labels"])

        if verbose:
            for topic, data in sorted(topic_map.items()):
                print(f"  {topic}: {len(data['sessions'])} сессий")

        report = {
            "skill":          "cluster",
            "ts":             ts,
            "status":         "ok",
            "topics_found":   result["topics_found"],
            "sessions_total": result["sessions_total"],
            "topics_file":    str(TOPICS_FILE),
        }
        _publish(self.r, report)
        return report

    def _hdbscan(self, vectors: np.ndarray, min_cluster_size: int) -> list[int]:
        import hdbscan
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=min_cluster_size,
            metric="euclidean",
            cluster_selection_method="eom",
        )
        return clusterer.fit_predict(vectors).tolist()

    def _kmeans_fallback(self, vectors: np.ndarray, n_clusters: int = None) -> list[int]:
        from sklearn.cluster import KMeans
        n = n_clusters or max(2, min(5, len(vectors) // 2))
        n = min(n, len(vectors))
        km = KMeans(n_clusters=n, n_init=10, random_state=42)
        return km.fit_predict(vectors).tolist()

    def _write_topic_datasets(self, rows: list[dict], session_labels: dict[str, int]):
        by_topic: dict[int, list[dict]] = {}
        for row in rows:
            sid   = row.get("session_id", "?")
            label = session_labels.get(sid, -1)
            by_topic.setdefault(label, []).append(row)

        for label, topic_rows in by_topic.items():
            name = f"topic_{label}" if label >= 0 else "topic_noise"
            path = FINAL_DIR / f"{name}.jsonl"
            with open(path, "w", encoding="utf-8") as f:
                for row in topic_rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ── SemanticSearch ────────────────────────────────────────────────────────────

class SemanticSearch:
    """FAISS поиск похожих строк в датасете."""

    INDEX_FILE = EMB_DIR / "faiss.index"
    META_FILE  = EMB_DIR / "faiss_meta.json"

    def __init__(self, r: redis.Redis):
        self.r      = r
        self.embed  = EmbeddingSkill()
        self._index = None
        self._meta  = []

    def build_index(self, verbose: bool = True) -> dict:
        ts   = datetime.now().strftime("%H:%M:%S")
        rows = _load_final_rows()
        if not rows:
            return {"status": "no_data"}

        texts = [r.get("content", "")[:500] for r in rows]
        if verbose:
            print(f"  [faiss] индексирую {len(texts)} строк...")

        vecs = self.embed.encode(texts)

        try:
            import faiss
            dim   = vecs.shape[1]
            index = faiss.IndexFlatIP(dim)  # Inner product = cosine на нормализованных
            index.add(vecs.astype(np.float32))
            faiss.write_index(index, str(self.INDEX_FILE))
            self._index = index
        except Exception as e:
            print(f"  [faiss] ошибка: {e}")
            return {"status": f"error: {e}"}

        self._meta = rows
        with open(self.META_FILE, "w", encoding="utf-8") as f:
            json.dump([{"session_id": r.get("session_id"),
                        "role":       r.get("role"),
                        "topic":      r.get("topic"),
                        "content":    r.get("content","")[:200]}
                       for r in rows], f, ensure_ascii=False)

        report = {"skill": "semantic_index", "ts": ts, "status": "ok",
                  "rows": len(rows), "dim": vecs.shape[1]}
        _publish(self.r, report)
        return report

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if self._index is None:
            if not self.INDEX_FILE.exists():
                self.build_index(verbose=False)
            else:
                import faiss
                self._index = faiss.read_index(str(self.INDEX_FILE))
                with open(self.META_FILE, encoding="utf-8") as f:
                    self._meta = json.load(f)

        q_vec = self.embed.encode([query]).astype(np.float32)
        scores, indices = self._index.search(q_vec, top_k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue
            row = self._meta[idx]
            results.append({**row, "score": float(score)})
        return results


# ── DatasetValidator ──────────────────────────────────────────────────────────

class DatasetValidator:
    """
    Авто-валидация спорных записей через совет моделей.

    Берёт записи с verdict=review из черновика,
    отправляет выборку в broadcast,
    ждёт голосования,
    auto-accept при консенсусе ≥ 2/3.
    """

    SAMPLE_SIZE = 5   # сколько примеров показываем совету

    def __init__(self, r: redis.Redis):
        self.r = r

    def validate(self, draft_path: Optional[Path] = None, verbose: bool = True) -> dict:
        ts = datetime.now().strftime("%H:%M:%S")

        from agent.dataset_pipeline import DRAFT_DIR, QualityFilter
        if draft_path is None:
            drafts = sorted(DRAFT_DIR.glob("*.jsonl"), reverse=True)
            if not drafts:
                return {"status": "no_drafts"}
            draft_path = drafts[0]

        # Читаем черновик: review + низкоскоринговые keep (60-75)
        review_msgs = []
        borderline_msgs = []
        with open(draft_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                session = json.loads(line)
                qf = QualityFilter()
                for msg in session.get("messages", []):
                    scored = qf.score_message(msg)
                    entry = {
                        "session": session["session_id"],
                        "from":    msg.get("from", "?"),
                        "text":    msg.get("text", "")[:300],
                        "score":   scored["score"],
                        "verdict": scored["verdict"],
                        "issues":  scored.get("issues", []),
                    }
                    if scored["verdict"] == "review":
                        review_msgs.append(entry)
                    elif scored["verdict"] == "keep" and scored["score"] <= 75:
                        borderline_msgs.append(entry)

        # Приоритет: review → borderline keep → reject
        candidates = review_msgs + borderline_msgs[:max(0, self.SAMPLE_SIZE - len(review_msgs))]

        if not candidates:
            # Берём случайные keep для проверки качества
            import random
            all_keep = []
            with open(draft_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    session = json.loads(line)
                    qf2 = QualityFilter()
                    for msg in session.get("messages", []):
                        sc = qf2.score_message(msg)
                        if sc["verdict"] == "keep":
                            all_keep.append({
                                "session": session["session_id"],
                                "from": msg.get("from", "?"),
                                "text": msg.get("text", "")[:300],
                                "score": sc["score"],
                                "verdict": "keep",
                                "issues": [],
                            })
            candidates = random.sample(all_keep, min(self.SAMPLE_SIZE, len(all_keep)))

        if not candidates:
            report = {"skill": "validate", "ts": ts, "status": "nothing_to_review",
                      "message": "черновик пуст"}
            _publish(self.r, report)
            return report

        sample = candidates[:self.SAMPLE_SIZE]
        if verbose:
            print(f"  [validate] review={len(review_msgs)} borderline={len(borderline_msgs)} → совету {len(sample)}")

        # Формируем вопрос для совета
        items = "\n".join(
            f"{i+1}. [{m['from']}] (score={m['score']}): {m['text'][:150]}"
            for i, m in enumerate(sample)
        )
        question = (
            f"Оцени качество этих {len(sample)} записей для датасета обучения AI.\n"
            f"Для каждой дай: KEEP или REJECT и одну причину.\n\n{items}\n\n"
            f"Ответь кратко: список номер → KEEP/REJECT, причина."
        )

        # Отправляем в broadcast
        try:
            import requests
            resp = requests.post(
                f"{COUNCIL_API}/api/council/broadcast",
                json={"text": question},
                timeout=10,
                proxies={"http": None, "https": None},
            )
            task_id = resp.json().get("task_id", "?")
            if verbose:
                print(f"  [validate] broadcast task_id={task_id}, жду 90s...")
        except Exception as e:
            return {"status": f"broadcast_error: {e}"}

        # Ждём ответы (90 секунд)
        time.sleep(90)

        # Читаем последние сообщения из Redis
        messages_key = "council:chat:messages"
        raw = self.r.lrange(messages_key, 0, 5)
        votes: dict[str, list[str]] = {}  # "1" → ["KEEP", "KEEP", "REJECT"]

        for item in raw:
            try:
                d = json.loads(item)
                who  = d.get("from", "")
                text = d.get("text", "")
                if who not in ("claude", "gpt", "deepseek"):
                    continue
                # Парсим паттерны: "1 → KEEP", "1. KEEP", "1: REJECT"
                for m in re.finditer(r'(\d+)[^\w]*(KEEP|REJECT|keep|reject)', text):
                    num    = m.group(1)
                    verdict = m.group(2).upper()
                    votes.setdefault(num, []).append(verdict)
            except Exception:
                pass

        # Консенсус ≥ 2/3
        decisions = {}
        for num, v_list in votes.items():
            keep_count = v_list.count("KEEP")
            total      = len(v_list)
            decisions[num] = "KEEP" if keep_count / max(total, 1) >= 2/3 else "REJECT"

        # Сохраняем REJECT-примеры в FailureCollector
        try:
            from agent.failure_collector import FailureCollector
            fc = FailureCollector(r=self.r)
            for num, dec in decisions.items():
                if dec == "REJECT":
                    idx = int(num) - 1
                    if 0 <= idx < len(sample):
                        reason_votes = ", ".join(votes.get(num, []))
                        fc.add_rejected(sample[idx], votes.get(num, []),
                                        reason=f"REJECT от совета: {reason_votes}")
        except Exception:
            pass

        # Сохраняем отчёт
        ts_slug = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = VAL_DIR / f"validation_{ts_slug}.json"
        report_data = {
            "ts":       datetime.now().isoformat(),
            "draft":    str(draft_path),
            "sample":   sample,
            "votes":    votes,
            "decisions": decisions,
            "consensus_threshold": "2/3",
        }
        report_path.write_text(json.dumps(report_data, ensure_ascii=False, indent=2))

        if verbose:
            for num, d in decisions.items():
                print(f"  [{num}] → {d} (голоса: {votes.get(num,[])})")

        report = {
            "skill":     "validate",
            "ts":        ts,
            "status":    "ok",
            "reviewed":  len(sample),
            "keep":      sum(1 for d in decisions.values() if d == "KEEP"),
            "reject":    sum(1 for d in decisions.values() if d == "REJECT"),
            "report":    str(report_path),
        }
        _publish(self.r, report)
        return report


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    r   = redis.from_url("redis://127.0.0.1:6379/0")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cluster"

    if cmd == "cluster":
        tc = TopicClusterer(r)
        tc.cluster(verbose=True)

    elif cmd == "index":
        ss = SemanticSearch(r)
        ss.build_index(verbose=True)

    elif cmd == "search":
        query = " ".join(sys.argv[2:]) or "dataset pipeline"
        ss = SemanticSearch(r)
        results = ss.search(query, top_k=5)
        for r_ in results:
            print(f"[{r_['score']:.3f}] {r_['session_id']} / {r_['role']}: {r_['content'][:120]}")

    elif cmd == "validate":
        dv = DatasetValidator(r)
        result = dv.validate(verbose=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
