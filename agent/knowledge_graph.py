#!/usr/bin/env python3
"""
assistant/knowledge_graph.py — персистентный граф знаний проекта.

Узлы (типы):
  session   — сессия совета
  topic     — тема (council, code, culinary, ...)
  concept   — ключевое понятие, извлечённое из сессий
  decision  — принятое решение
  task      — задача из NEXT_TASK / роадмапа
  dataset   — версия датасета
  file      — файл проекта

Рёбра (типы):
  mentions       — сессия/решение упоминает концепт
  derived_from   — датасет/концепт получен из сессии
  resolves       — решение закрывает задачу
  contradicts    — концепт противоречит другому
  belongs_to     — концепт/сессия относится к теме
  co_occurs      — два концепта часто встречаются вместе
  next           — задача следует за задачей

Хранение: SQLite (`registry/knowledge/graph.db`) + NetworkX в памяти (кеш).

API:
  kg = KnowledgeGraph()
  kg.add_node("Knowledge Graph", "concept", meta={"desc": "..."})
  kg.add_edge("session_20260516", "Knowledge Graph", "mentions")
  results = kg.query("Knowledge Graph", depth=2)
  related = kg.related("Knowledge Graph", limit=10)
  kg.auto_index_session(session_id, topic, messages)

Команды:
  python3 assistant/knowledge_graph.py stats
  python3 assistant/knowledge_graph.py query "Knowledge Graph"
  python3 assistant/knowledge_graph.py add_concept "название" "тип"
  python3 assistant/knowledge_graph.py index_datasets
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import networkx as nx
import redis

BASE      = Path(__file__).parent.parent
KG_DIR    = BASE / "registry" / "knowledge"
DB_PATH   = KG_DIR / "graph.db"
FINAL_DIR = BASE / "registry" / "dataset" / "final"
SESSION_DIR = BASE / "registry" / "council" / "sessions"

KG_DIR.mkdir(parents=True, exist_ok=True)

REPORT_KEY = "council:skill:reports"
REPORT_CH  = "council:skill:report"

# ── ключевые концепты для авто-извлечения ─────────────────────────────────────

CONCEPT_PATTERNS = [
    # архитектура
    r"\b(Knowledge Graph|KG|граф знаний)\b",
    r"\b(Redis|pub.?sub|pubsub)\b",
    r"\b(FAISS|HDBSCAN|KMeans|clustering)\b",
    r"\b(sentence.transformers|embeddings?|эмбеддинг)\b",
    r"\b(daemon|демон|pet_claude)\b",
    r"\b(council|совет|Council Bridge)\b",
    r"\b(dataset|датасет|DatasetPipeline)\b",
    r"\b(ActiveSampler|active.sampler)\b",
    r"\b(DatasetValidator|validator)\b",
    r"\b(TopicClusterer|кластеризац)\b",
    r"\b(FileWatcher|watcher)\b",
    r"\b(orchestrator|оркестратор)\b",
    r"\b(NetworkX|SQLite)\b",
    # концепции
    r"\b(fine.tun|LoRA|файн.тюн)\b",
    r"\b(YANDI|mesh|federat)\b",
    r"\b(RAG|retrieval)\b",
    r"\b(decision.track|Decision Tracker)\b",
    r"\b(reflection|Reflection Daemon)\b",
    r"\b(adversarial|adversarial probing)\b",
    r"\b(continual.learn|continual learning)\b",
    r"\b(self.eval|self-evaluation)\b",
    r"\b(qwen3|deepseek|gemma4|ollama)\b",
    r"\b(токен|token.count|токенов)\b",
    r"\b(пайплайн|pipeline)\b",
    r"\b(валидаци|validation)\b",
    r"\b(консенсус|consensus)\b",
]

TOPIC_KEYWORDS = {
    "council" : ["совет", "council", "broadcast", "relay", "модел"],
    "code"    : ["python", "класс", "функц", "модул", "import", "def "],
    "culinary": ["рецепт", "готов", "блюд", "ингредиент", "повар"],
    "dataset" : ["датасет", "dataset", "обучен", "sample", "валидац"],
    "kg"      : ["граф", "knowledge", "memory", "узел", "ребро"],
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _r() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def _publish(r: redis.Redis, report: dict):
    payload = json.dumps(report, ensure_ascii=False)
    r.lpush(REPORT_KEY, payload)
    r.ltrim(REPORT_KEY, 0, 49)
    r.publish(REPORT_CH, payload)


def _extract_concepts(text: str) -> list[str]:
    found = set()
    for pat in CONCEPT_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            found.add(m.group(0).strip())
    return list(found)


def _detect_topic(text: str) -> str:
    text_l = text.lower()
    scores = {t: sum(1 for kw in kws if kw in text_l)
              for t, kws in TOPIC_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


# ── KnowledgeGraph ────────────────────────────────────────────────────────────

class KnowledgeGraph:
    """
    Персистентный граф знаний: NetworkX (память) + SQLite (диск).
    Thread-safe через одиночное соединение с WAL-режимом.
    """

    def __init__(self, db_path: Path = DB_PATH, r: Optional[redis.Redis] = None):
        self.db_path = db_path
        self.r       = r or _r()
        self.G       = nx.DiGraph()
        self._init_db()
        self._load_into_memory()

    # ── инициализация ─────────────────────────────────────────────────────────

    def _init_db(self):
        with self._conn() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS nodes (
                    id       TEXT PRIMARY KEY,
                    type     TEXT NOT NULL,
                    label    TEXT NOT NULL,
                    meta     TEXT DEFAULT '{}',
                    created  TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS edges (
                    src      TEXT NOT NULL,
                    dst      TEXT NOT NULL,
                    rel      TEXT NOT NULL,
                    weight   REAL DEFAULT 1.0,
                    meta     TEXT DEFAULT '{}',
                    created  TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (src, dst, rel)
                );
                CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
                CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
                CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
            """)

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _load_into_memory(self):
        with self._conn() as con:
            for row in con.execute("SELECT id, type, label, meta FROM nodes"):
                meta = json.loads(row["meta"] or "{}")
                self.G.add_node(row["id"], type=row["type"],
                                label=row["label"], **meta)
            for row in con.execute("SELECT src, dst, rel, weight FROM edges"):
                self.G.add_edge(row["src"], row["dst"],
                                rel=row["rel"], weight=row["weight"])

    # ── добавление ────────────────────────────────────────────────────────────

    def add_node(self, node_id: str, node_type: str,
                 label: Optional[str] = None, meta: Optional[dict] = None) -> bool:
        """Добавить узел. Возвращает True если новый, False если уже был."""
        label = label or node_id
        meta  = meta or {}
        if node_id in self.G:
            return False
        self.G.add_node(node_id, type=node_type, label=label, **meta)
        with self._conn() as con:
            con.execute(
                "INSERT OR IGNORE INTO nodes (id, type, label, meta) VALUES (?,?,?,?)",
                (node_id, node_type, label, json.dumps(meta, ensure_ascii=False))
            )
        return True

    def add_edge(self, src: str, dst: str, rel: str,
                 weight: float = 1.0, meta: Optional[dict] = None) -> bool:
        """Добавить ребро. Если ребро есть — увеличить вес."""
        meta = meta or {}
        if self.G.has_edge(src, dst) and self.G[src][dst].get("rel") == rel:
            # усилить существующее ребро
            new_w = self.G[src][dst].get("weight", 1.0) + 0.5
            self.G[src][dst]["weight"] = new_w
            with self._conn() as con:
                con.execute("UPDATE edges SET weight=? WHERE src=? AND dst=? AND rel=?",
                            (new_w, src, dst, rel))
            return False

        self.G.add_edge(src, dst, rel=rel, weight=weight, **meta)
        with self._conn() as con:
            con.execute(
                "INSERT OR IGNORE INTO edges (src, dst, rel, weight, meta) VALUES (?,?,?,?,?)",
                (src, dst, rel, weight, json.dumps(meta, ensure_ascii=False))
            )
        return True

    def ensure_node(self, node_id: str, node_type: str,
                    label: Optional[str] = None, meta: Optional[dict] = None):
        """add_node без флага — удобно при массовой загрузке."""
        self.add_node(node_id, node_type, label, meta)

    # ── запросы ───────────────────────────────────────────────────────────────

    def query(self, node_id: str, depth: int = 2) -> dict:
        """
        Возвращает окрестность узла глубиной depth.
        """
        if node_id not in self.G:
            # попробовать найти по метке (частичное совпадение)
            matches = [n for n in self.G.nodes
                       if node_id.lower() in n.lower()
                       or node_id.lower() in self.G.nodes[n].get("label", "").lower()]
            if not matches:
                return {"node": node_id, "found": False, "neighbors": []}
            node_id = matches[0]

        neighbors = []
        visited   = {node_id}
        queue     = [(node_id, 0)]

        while queue:
            cur, d = queue.pop(0)
            if d >= depth:
                continue
            for nxt in list(self.G.successors(cur)) + list(self.G.predecessors(cur)):
                if nxt in visited:
                    continue
                visited.add(nxt)
                edge_data = self.G.get_edge_data(cur, nxt) or self.G.get_edge_data(nxt, cur) or {}
                neighbors.append({
                    "node" : nxt,
                    "type" : self.G.nodes[nxt].get("type", "?"),
                    "label": self.G.nodes[nxt].get("label", nxt),
                    "rel"  : edge_data.get("rel", "?"),
                    "weight": edge_data.get("weight", 1.0),
                    "depth": d + 1,
                })
                queue.append((nxt, d + 1))

        node_data = dict(self.G.nodes.get(node_id, {}))
        return {
            "node"     : node_id,
            "found"    : True,
            "type"     : node_data.get("type", "?"),
            "label"    : node_data.get("label", node_id),
            "neighbors": sorted(neighbors, key=lambda x: -x["weight"]),
        }

    def related(self, node_id: str, limit: int = 10) -> list[dict]:
        """Самые сильно связанные узлы."""
        if node_id not in self.G:
            return []
        result = []
        for nxt in list(self.G.successors(node_id)) + list(self.G.predecessors(node_id)):
            ed = self.G.get_edge_data(node_id, nxt) or self.G.get_edge_data(nxt, node_id) or {}
            result.append({
                "node"  : nxt,
                "label" : self.G.nodes[nxt].get("label", nxt),
                "rel"   : ed.get("rel", "?"),
                "weight": ed.get("weight", 1.0),
            })
        result.sort(key=lambda x: -x["weight"])
        return result[:limit]

    def search(self, query: str, node_type: Optional[str] = None,
               limit: int = 20) -> list[dict]:
        """Полнотекстовый поиск по меткам узлов."""
        q = query.lower()
        results = []
        for nid, data in self.G.nodes(data=True):
            if node_type and data.get("type") != node_type:
                continue
            label = data.get("label", nid).lower()
            if q in label or q in nid.lower():
                degree = self.G.degree(nid)
                results.append({
                    "node"  : nid,
                    "label" : data.get("label", nid),
                    "type"  : data.get("type", "?"),
                    "degree": degree,
                })
        results.sort(key=lambda x: -x["degree"])
        return results[:limit]

    def stats(self) -> dict:
        types = defaultdict(int)
        rels  = defaultdict(int)
        for _, data in self.G.nodes(data=True):
            types[data.get("type", "?")] += 1
        for _, _, data in self.G.edges(data=True):
            rels[data.get("rel", "?")] += 1
        return {
            "nodes"      : self.G.number_of_nodes(),
            "edges"      : self.G.number_of_edges(),
            "node_types" : dict(types),
            "edge_types" : dict(rels),
            "db_path"    : str(self.db_path),
        }

    # ── авто-индексация ───────────────────────────────────────────────────────

    def auto_index_session(self, session_id: str, topic: str, messages: list[dict]) -> int:
        """
        Индексирует одну сессию: создаёт узлы session+topic, извлекает концепты,
        строит рёбра. Возвращает число новых узлов.
        """
        new_nodes = 0

        # узел темы
        self.ensure_node(f"topic:{topic}", "topic", label=topic)

        # узел сессии
        sid_node = f"session:{session_id}"
        ts = session_id[:16] if len(session_id) >= 16 else session_id
        if self.add_node(sid_node, "session", label=f"Сессия {ts}",
                         meta={"topic": topic, "msg_count": len(messages)}):
            new_nodes += 1

        # сессия → тема
        self.add_edge(sid_node, f"topic:{topic}", "belongs_to")

        # извлечь концепты из текста всех сообщений
        full_text = " ".join(m.get("content", m.get("text", "")) for m in messages)
        concepts = _extract_concepts(full_text)

        concept_counts: dict[str, int] = defaultdict(int)
        for c in concepts:
            concept_counts[c.lower()] += 1

        for concept_raw, count in concept_counts.items():
            concept_id = f"concept:{concept_raw}"
            if self.add_node(concept_id, "concept", label=concept_raw):
                new_nodes += 1
            self.add_edge(sid_node, concept_id, "mentions", weight=float(count))

        # co-occurrence рёбра между концептами
        concept_ids = [f"concept:{c.lower()}" for c in concept_counts]
        for i, ca in enumerate(concept_ids):
            for cb in concept_ids[i+1:]:
                self.add_edge(ca, cb, "co_occurs", weight=0.5)

        return new_nodes

    def index_datasets(self, verbose: bool = True) -> dict:
        """Индексирует все финальные HF-датасеты."""
        total_new = 0
        sessions_indexed = 0

        for f in sorted(FINAL_DIR.glob("*_hf.jsonl")):
            rows = []
            with open(f, encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass

            # группируем по session_id
            by_session: dict[str, list] = defaultdict(list)
            for row in rows:
                sid = row.get("session_id", "unknown")
                by_session[sid].append(row)

            for sid, msgs in by_session.items():
                topic = msgs[0].get("topic", "general") if msgs else "general"
                new = self.auto_index_session(sid, topic, msgs)
                total_new += new
                sessions_indexed += 1

            # узел датасета
            ds_id = f"dataset:{f.stem}"
            self.add_node(ds_id, "dataset", label=f.name,
                          meta={"path": str(f), "rows": len(rows)})
            for sid in by_session:
                self.add_edge(f"session:{sid}", ds_id, "derived_from")

        result = {
            "sessions_indexed": sessions_indexed,
            "new_nodes"       : total_new,
            "total_nodes"     : self.G.number_of_nodes(),
            "total_edges"     : self.G.number_of_edges(),
        }
        if verbose:
            print(f"  [kg] индексировано: {sessions_indexed} сессий, {total_new} новых узлов")
        return result

    def index_sessions_md(self, verbose: bool = True) -> dict:
        """Индексирует все markdown-файлы сессий совета."""
        total_new = 0
        count = 0
        for f in sorted(SESSION_DIR.glob("*.md")):
            if f.stat().st_size < 100:
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            messages = [{"content": l} for l in lines if not l.startswith("---")]

            topic = _detect_topic(text)
            sid = f.stem  # имя файла как ID
            new = self.auto_index_session(sid, topic, messages)
            total_new += new
            count += 1

        result = {"md_indexed": count, "new_nodes": total_new}
        if verbose:
            print(f"  [kg] md сессий: {count}, новых узлов: {total_new}")
        return result

    def add_decision(self, decision_id: str, text: str,
                     reason: str = "", expected: str = "") -> bool:
        meta = {"reason": reason, "expected": expected,
                "outcome": None, "created": datetime.now().isoformat()}
        new = self.add_node(f"decision:{decision_id}", "decision",
                            label=text[:80], meta=meta)
        if new:
            concepts = _extract_concepts(text + " " + reason)
            for c in concepts:
                cid = f"concept:{c.lower()}"
                self.ensure_node(cid, "concept", label=c)
                self.add_edge(f"decision:{decision_id}", cid, "mentions")
        return new


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    cmd  = sys.argv[1] if len(sys.argv) > 1 else "stats"
    kg   = KnowledgeGraph()

    if cmd == "stats":
        s = kg.stats()
        print(json.dumps(s, ensure_ascii=False, indent=2))

    elif cmd == "index":
        r1 = kg.index_datasets(verbose=True)
        r2 = kg.index_sessions_md(verbose=True)
        print(json.dumps({**r1, **r2}, ensure_ascii=False, indent=2))

    elif cmd == "index_datasets":
        r = kg.index_datasets(verbose=True)
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "query":
        node = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "council"
        result = kg.query(node, depth=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "search":
        q = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        results = kg.search(q)
        for r in results:
            print(f"  [{r['type']}] {r['label']}  (degree={r['degree']})")

    elif cmd == "add_concept":
        label = sys.argv[2] if len(sys.argv) > 2 else "test"
        ntype = sys.argv[3] if len(sys.argv) > 3 else "concept"
        new = kg.add_node(f"{ntype}:{label.lower()}", ntype, label=label)
        print("added" if new else "already exists")

    else:
        print(f"Неизвестная команда: {cmd}")
        print("Доступно: stats, index, index_datasets, query <node>, search <query>, add_concept <label> [type]")
        sys.exit(1)
