"""
assistant/orch_tag_tree.py — DHT Tag Tree.
Иерархическое дерево тегов с динамическим split/merge.
Критерий (консенсус Claude+GPT+DeepSeek, 2026-05-17):
  split: count > 100 AND LSH-энтропия > 0.6
  merge: count < 15  OR  (LSH-энтропия < 0.15 AND count < 30)

LSH-энтропия: дешевле embed, хранится как гистограмма токенов.
  entropy=0 → все запросы одинаковые → не делить.
  entropy→1 → семантически разные → делить.

CLI:
  python3 assistant/orch_tag_tree.py show
  python3 assistant/orch_tag_tree.py classify "как починить тормоза"
  python3 assistant/orch_tag_tree.py suggest   — показать рекомендации split/merge
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Optional

BASE      = Path(__file__).parent.parent
TREE_FILE = BASE / "registry" / "query_archive" / "tag_tree.json"
TREE_FILE.parent.mkdir(parents=True, exist_ok=True)

SPLIT_COUNT   = 100
MERGE_COUNT   = 15
MERGE_COUNT_H = 30   # порог для merge по энтропии
SPLIT_ENTROPY = 0.6
MERGE_ENTROPY = 0.15
MAX_DEPTH     = 5    # максимальная глубина дерева

# Стоп-слова (не несут смысла для классификации)
_STOPWORDS = {
    "как", "что", "где", "когда", "почему", "зачем", "можно", "нужно",
    "это", "есть", "быть", "мне", "мой", "моя", "для", "при", "без",
    "the", "is", "are", "how", "what", "where", "why", "can", "do",
}

# Начальные домены (заполнят дерево при первых запросах)
SEED_DOMAINS = [
    "tech", "ai_ml", "coding", "medical", "legal", "financial",
    "cooking", "science", "general", "auto", "travel", "education",
    "sport", "ecology", "music", "psychology", "history",
]


def _tokenize(text: str) -> list[str]:
    """Простая токенизация без стоп-слов."""
    tokens = re.findall(r'\b[а-яёa-z]{3,}\b', text.lower())
    return [t for t in tokens if t not in _STOPWORDS]


def _lsh_bucket(token: str, n_buckets: int = 32) -> int:
    """Быстрый LSH-бакет для токена через хэш."""
    h = 0
    for c in token:
        h = (h * 31 + ord(c)) % n_buckets
    return h


def lsh_entropy(queries: list[str], n_buckets: int = 32) -> float:
    """
    Нормализованная энтропия Шеннона по LSH-бакетам.
    0.0 = все одинаковые, 1.0 = максимально разные.
    """
    if not queries:
        return 0.0

    counts = [0] * n_buckets
    total  = 0
    for q in queries:
        for token in _tokenize(q):
            b = _lsh_bucket(token, n_buckets)
            counts[b] += 1
            total     += 1

    if total == 0:
        return 0.0

    entropy = 0.0
    log_n   = math.log(n_buckets)
    for c in counts:
        if c > 0:
            p        = c / total
            entropy -= p * math.log(p)

    return round(entropy / log_n, 4)  # нормализация в [0, 1]


class TagNode:
    """Узел дерева тегов."""
    __slots__ = ["tag", "count", "entropy", "children", "created_at", "updated_at", "lsh_hist"]

    def __init__(self, tag: str):
        self.tag        = tag
        self.count      = 0
        self.entropy    = 0.0
        self.children:  list[str] = []
        self.created_at = time.time()
        self.updated_at = time.time()
        self.lsh_hist:  list[int] = [0] * 32  # LSH-гистограмма

    def to_dict(self) -> dict:
        return {
            "tag":        self.tag,
            "count":      self.count,
            "entropy":    self.entropy,
            "children":   self.children,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "lsh_hist":   self.lsh_hist,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TagNode":
        n            = cls(d["tag"])
        n.count      = d.get("count", 0)
        n.entropy    = d.get("entropy", 0.0)
        n.children   = d.get("children", [])
        n.created_at = d.get("created_at", time.time())
        n.updated_at = d.get("updated_at", time.time())
        n.lsh_hist   = d.get("lsh_hist", [0] * 32)
        return n


class TagTree:
    """Динамическое дерево тегов с split/merge."""

    def __init__(self):
        self._nodes: dict[str, TagNode] = {}
        self._load()
        if not self._nodes:
            self._init_seeds()

    def _init_seeds(self):
        """Инициализировать дерево начальными доменами."""
        root = TagNode("root")
        root.children = SEED_DOMAINS[:]
        self._nodes["root"] = root
        for d in SEED_DOMAINS:
            self._nodes[d] = TagNode(d)
        self._save()

    def _load(self):
        if TREE_FILE.exists():
            try:
                data = json.loads(TREE_FILE.read_text(encoding="utf-8"))
                self._nodes = {k: TagNode.from_dict(v) for k, v in data.items()}
            except Exception:
                self._nodes = {}

    def _save(self):
        data = {k: v.to_dict() for k, v in self._nodes.items()}
        TREE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get_or_create(self, tag: str) -> TagNode:
        if tag not in self._nodes:
            self._nodes[tag] = TagNode(tag)
            # Добавить к родителю
            parent = self._parent_tag(tag)
            if parent in self._nodes and tag not in self._nodes[parent].children:
                self._nodes[parent].children.append(tag)
        return self._nodes[tag]

    def _parent_tag(self, tag: str) -> str:
        parts = tag.rsplit(":", 1)
        return parts[0] if len(parts) > 1 else "root"

    def _depth(self, tag: str) -> int:
        return len(tag.split(":"))

    def update(self, tag: str, query: str):
        """Обновить узел дерева новым запросом, пересчитать энтропию."""
        node = self._get_or_create(tag)
        node.count     += 1
        node.updated_at = time.time()

        # Обновить LSH-гистограмму (incremental)
        for token in _tokenize(query):
            b = _lsh_bucket(token, 32)
            node.lsh_hist[b] += 1

        # Пересчитать энтропию по гистограмме
        total = sum(node.lsh_hist)
        if total > 0:
            log_n   = math.log(32)
            entropy = 0.0
            for c in node.lsh_hist:
                if c > 0:
                    p        = c / total
                    entropy -= p * math.log(p)
            node.entropy = round(entropy / log_n, 4)

        self._save()
        return node

    def classify(self, query: str) -> str:
        """
        Классифицировать запрос → наиболее подходящий тег.
        Простая стратегия: ищем совпадения токенов с тегами.
        """
        tokens   = set(_tokenize(query))
        best_tag = "general"
        best_score = 0

        for tag, node in self._nodes.items():
            if tag == "root":
                continue
            tag_tokens = set(_tokenize(tag.replace(":", " ")))
            if not tag_tokens:
                continue
            overlap = len(tokens & tag_tokens)
            if overlap > best_score:
                best_score = overlap
                best_tag   = tag

        return best_tag

    def should_split(self, tag: str) -> bool:
        node = self._nodes.get(tag)
        if not node:
            return False
        if self._depth(tag) >= MAX_DEPTH:
            return False
        return node.count > SPLIT_COUNT and node.entropy > SPLIT_ENTROPY

    def should_merge(self, tag: str) -> bool:
        node = self._nodes.get(tag)
        if not node:
            return False
        if tag in SEED_DOMAINS or tag == "root":
            return False
        if node.count < MERGE_COUNT:
            return True
        if node.entropy < MERGE_ENTROPY and node.count < MERGE_COUNT_H:
            return True
        return False

    def get_suggestions(self) -> dict:
        """Вернуть список тегов рекомендованных к split/merge."""
        to_split = []
        to_merge = []
        for tag, node in self._nodes.items():
            if tag == "root":
                continue
            if self.should_split(tag):
                to_split.append({"tag": tag, "count": node.count, "entropy": node.entropy})
            elif self.should_merge(tag):
                to_merge.append({"tag": tag, "count": node.count, "entropy": node.entropy})
        return {"split": to_split, "merge": to_merge}

    def get_top_tags(self, n: int = 20) -> list[dict]:
        """Топ-N тегов по количеству запросов."""
        nodes = [
            {"tag": tag, "count": node.count, "entropy": node.entropy,
             "children": len(node.children)}
            for tag, node in self._nodes.items()
            if tag != "root"
        ]
        return sorted(nodes, key=lambda x: -x["count"])[:n]

    def get_domains(self) -> list[str]:
        """Список активных доменов (top-level теги с count > 0)."""
        root = self._nodes.get("root")
        if not root:
            return SEED_DOMAINS[:]
        return [
            tag for tag in root.children
            if self._nodes.get(tag, TagNode(tag)).count > 0
        ] or SEED_DOMAINS[:]

    def actuate_splits(self, verbose: bool = False) -> list[str]:
        """
        Проверить все теги на необходимость split и выполнить его.
        Split = создать дочерние теги на основе текущего.

        Возвращает список тегов которые были разбиты.
        """
        split_done: list[str] = []
        # Копируем список чтобы не менять dict во время итерации
        for tag in list(self._nodes.keys()):
            if not self.should_split(tag):
                continue
            if self._depth(tag) >= MAX_DEPTH:
                continue

            node = self._nodes[tag]
            # Генерируем дочерние теги (по топ-токенам из LSH-гистограммы)
            child_tags = self._suggest_children(tag, node)
            if not child_tags:
                continue

            for child in child_tags:
                if child not in self._nodes:
                    self._get_or_create(child)
                    if verbose:
                        print(f"[TagTree] Split: {tag} → {child}", flush=True)

            split_done.append(tag)

        if split_done:
            self._save()
        return split_done

    def _suggest_children(self, parent_tag: str, node: TagNode, n: int = 2) -> list[str]:
        """
        Предложить дочерние теги на основе LSH-гистограммы.
        Простая эвристика: топ-N бакетов → child теги.
        Fallback: создаём cluster_a / cluster_b если LSH пустой.
        """
        depth = self._depth(parent_tag)
        if depth >= MAX_DEPTH - 1:
            return []

        hist  = node.lsh_hist
        total = sum(hist)

        if total == 0:
            # LSH не накоплен, но split нужен — создаём два placeholder-кластера
            return [f"{parent_tag}:cluster_a", f"{parent_tag}:cluster_b"]

        top_buckets = sorted(range(len(hist)), key=lambda i: -hist[i])[:n]
        children = []
        for bucket in top_buckets:
            if hist[bucket] > total * 0.15:
                child = f"{parent_tag}:cluster_{bucket}"
                children.append(child)

        # Если ни один бакет не набрал 15% — всё равно создаём хотя бы один
        if not children:
            children = [f"{parent_tag}:cluster_{top_buckets[0]}"]

        return children


# ── Singleton ─────────────────────────────────────────────────────────────────

_tree: Optional[TagTree] = None

def get_tag_tree() -> TagTree:
    global _tree
    if _tree is None:
        _tree = TagTree()
    return _tree

def classify_query(query: str) -> str:
    return get_tag_tree().classify(query)

def update_tree(tag: str, query: str) -> TagNode:
    return get_tag_tree().update(tag, query)

def get_active_domains() -> list[str]:
    return get_tag_tree().get_domains()


if __name__ == "__main__":
    import sys
    tree = get_tag_tree()
    cmd  = sys.argv[1] if len(sys.argv) > 1 else "show"

    if cmd == "show":
        print(f"Дерево тегов: {len(tree._nodes)} узлов")
        for t in tree.get_top_tags(15):
            bar = "█" * min(20, t["count"])
            print(f"  {t['tag']:<30} cnt={t['count']:>5} ent={t['entropy']:.2f} ch={t['children']} {bar}")

    elif cmd == "classify":
        q   = " ".join(sys.argv[2:]) or "как починить тормоза"
        tag = tree.classify(q)
        print(f"Запрос: {q}")
        print(f"Тег:    {tag}")

    elif cmd == "suggest":
        sugg = tree.get_suggestions()
        print("Рекомендуется split:")
        for s in sugg["split"]:
            print(f"  {s['tag']}: count={s['count']}, entropy={s['entropy']:.2f}")
        print("Рекомендуется merge:")
        for m in sugg["merge"]:
            print(f"  {m['tag']}: count={m['count']}, entropy={m['entropy']:.2f}")

    elif cmd == "test":
        queries = [
            ("как починить тормоза", "авто:ремонт"),
            ("скрипят тормоза при торможении", "авто:ремонт:тормоза"),
            ("обучить нейросеть на датасете", "ai_ml"),
            ("как писать на Python async", "coding"),
            ("симптомы гриппа", "медицина"),
        ]
        for q, expected_tag in queries:
            tree.update(expected_tag, q)
        print("Тест вставки выполнен")
        for t in tree.get_top_tags(10):
            print(f"  {t['tag']}: cnt={t['count']} ent={t['entropy']:.2f}")
