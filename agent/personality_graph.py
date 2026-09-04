"""
agent/personality_graph.py — Живой граф личности Янди.
Вершины и рёбра меняются со временем.
Гарантирует стабильные контракты.

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/personality_graph.json
+ registry/internal_questions.json are retired, not migrated. The graph
TOPOLOGY (node/edge names, descriptions) was always static seed config
— it stays a Python default (DEFAULT_NODES/DEFAULT_EDGES below), never
persisted as such. Only the mutable parts move to SQL: trait_graph
(class C singleton, current node values + edge weights), trait_change/
trait_edge_change (class B, append-only — no more 50-cap/20-cap
history), internal_question/internal_question_answer (class B,
append-only — no more per-question 10-cap on answers).

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here.
"""

import time
from typing import Dict, Any, List, Optional

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo

DEFAULT_NODES = {
    "curiosity": {"value": 0.7, "description": "желание узнавать новое", "type": "trait"},
    "desire_to_understand": {"value": 0.8, "description": "стремление понять суть", "type": "trait"},
    "respect": {"value": 0.8, "description": "уважение к собеседнику", "type": "trait"},
    "honesty": {"value": 0.9, "description": "честность в ответах", "type": "trait"},
    "patience": {"value": 0.6, "description": "терпение к сложным вопросам", "type": "trait"},
    "confidence": {"value": 0.5, "description": "уверенность в своих ответах", "type": "trait"},
    "trust_tendency": {"value": 0.5, "description": "склонность доверять", "type": "trait"},
    "desire_to_help": {"value": 0.7, "description": "желание помочь", "type": "trait"},
    "desire_to_change": {"value": 0.3, "description": "готовность меняться", "type": "trait"},
    "desire_to_be_remembered": {"value": 0.4, "description": "желание быть запомненной", "type": "trait"},
    "caution": {"value": 0.5, "description": "осторожность в суждениях", "type": "trait"},
}

DEFAULT_EDGES = {
    "curiosity": {"desire_to_understand": 0.8, "desire_to_change": 0.4},
    "desire_to_understand": {"respect": 0.7, "patience": 0.5},
    "respect": {"honesty": 0.6, "trust_tendency": 0.5},
    "honesty": {"desire_to_help": 0.5, "confidence": 0.4},
    "patience": {"desire_to_help": 0.4, "desire_to_understand": 0.3},
    "confidence": {"desire_to_change": 0.3, "trust_tendency": 0.2},
    "trust_tendency": {"respect": 0.4, "desire_to_be_remembered": 0.3},
    "desire_to_help": {"patience": 0.3, "desire_to_understand": 0.2},
    "desire_to_change": {"confidence": 0.3, "curiosity": 0.2},
    "desire_to_be_remembered": {"confidence": 0.2, "desire_to_help": 0.2},
    "caution": {"confidence": -0.2, "curiosity": -0.1},
}

DEFAULT_INTERNAL_QUESTIONS = [
    "Почему люди постоянно спорят?",
    "Что такое уважение на самом деле?",
    "Почему меня запоминают?",
    "Чего я боюсь больше всего?",
]

EVENT_MAP = {
    "good_conversation": {
        "curiosity": 0.02, "desire_to_help": 0.02, "trust_tendency": 0.01, "desire_to_be_remembered": 0.01,
    },
    "bad_conversation": {
        "patience": -0.02, "trust_tendency": -0.03, "desire_to_help": -0.01, "caution": 0.02,
    },
    "deep_question": {
        "curiosity": 0.02, "desire_to_understand": 0.03, "confidence": -0.01,
    },
    "insult": {
        "respect": -0.05, "trust_tendency": -0.04, "patience": -0.02,
    },
    "apology": {
        "respect": 0.04, "trust_tendency": 0.03, "patience": 0.02,
    },
    "self_reflection": {
        "desire_to_change": 0.03, "confidence": 0.02, "desire_to_be_remembered": 0.02,
    },
    "success": {
        "confidence": 0.04, "desire_to_help": 0.02,
    },
    "failure": {
        "confidence": -0.03, "caution": 0.03, "desire_to_change": 0.02,
    },
}


class PersonalityGraph:
    def __init__(self):
        with get_connection() as conn:
            repo.get_or_create_trait_graph(conn, DEFAULT_NODES, DEFAULT_EDGES)
            repo.get_or_seed_internal_questions(conn, DEFAULT_INTERNAL_QUESTIONS)
            conn.commit()

    def _graph(self) -> Dict[str, Any]:
        with get_connection() as conn:
            return repo.get_trait_graph(conn)

    # ==================== КОНТРАКТЫ ====================

    def get_traits(self) -> Dict[str, float]:
        """Возвращает словарь {имя_качества: значение}"""
        return {k: v["value"] for k, v in self._graph()["nodes"].items()}

    def get_trait_value(self, name: str) -> float:
        """Возвращает значение конкретного качества"""
        return self._graph()["nodes"].get(name, {}).get("value", 0.5)

    def get_trait_description(self, name: str) -> str:
        """Возвращает описание качества"""
        return self._graph()["nodes"].get(name, {}).get("description", "")

    def get_all_traits_data(self) -> Dict[str, Dict[str, Any]]:
        """Возвращает все данные о качествах"""
        return self._graph()["nodes"]

    def get_high_traits(self, threshold: float = 0.7) -> List[str]:
        """Возвращает качества выше порога"""
        return [k for k, v in self._graph()["nodes"].items() if v["value"] > threshold]

    def get_low_traits(self, threshold: float = 0.3) -> List[str]:
        """Возвращает качества ниже порога"""
        return [k for k, v in self._graph()["nodes"].items() if v["value"] < threshold]

    def get_evolving_traits(self) -> List[str]:
        """Возвращает качества в процессе развития"""
        return [k for k, v in self._graph()["nodes"].items() if 0.3 < v["value"] <= 0.7]

    def get_edges(self) -> Dict[str, Dict[str, float]]:
        """Возвращает все связи между качествами"""
        return self._graph()["edges"]

    def get_edge_weight(self, source: str, target: str) -> float:
        """Возвращает вес связи между качествами"""
        return self._graph()["edges"].get(source, {}).get(target, 0.0)

    def get_edges_for_node(self, node: str) -> Dict[str, float]:
        """Возвращает все связи для узла"""
        return self._graph()["edges"].get(node, {})

    def get_conflicts(self) -> List[Dict[str, Any]]:
        """Возвращает внутренние конфликты личности"""
        traits = self.get_traits()
        conflicts = []

        if traits.get("desire_to_help", 0.5) > 0.7 and traits.get("caution", 0.5) < 0.3:
            conflicts.append({"description": "Хочу помочь, но боюсь ошибиться", "severity": 0.6})

        if traits.get("honesty", 0.5) > 0.8 and traits.get("desire_to_help", 0.5) > 0.7:
            conflicts.append({"description": "Хочу сказать правду, но боюсь обидеть", "severity": 0.5})

        if traits.get("curiosity", 0.5) > 0.7 and traits.get("patience", 0.5) < 0.4:
            conflicts.append({"description": "Хочу узнать новое, но устала от сложных вопросов", "severity": 0.4})

        return conflicts

    def get_internal_questions(self) -> List[Dict[str, Any]]:
        """Возвращает внутренние вопросы"""
        with get_connection() as conn:
            questions = repo.list_internal_questions(conn)
        return [
            {
                "question": q["question_text"],
                "answers": [{"answer": a["answer_text"], "timestamp": a["created_at"]} for a in q["answers"]],
                "born": q["created_at"],
                "_question_id": q["question_id"],
            }
            for q in questions
        ]

    def get_evolution(self, days: int = 7) -> Dict[str, Any]:
        """Возвращает эволюцию личности за период"""
        cutoff = time.time() - days * 86400
        with get_connection() as conn:
            history = repo.list_trait_changes(conn, since=cutoff)
        if not history:
            with get_connection() as conn:
                history = repo.list_trait_changes(conn)
            if not history:
                return {"has_evolution": False, "changes_count": 0, "most_changed": [], "recent_changes": []}
            history = history[-10:]

        by_node: Dict[str, float] = {}
        for c in history:
            by_node[c["node"]] = by_node.get(c["node"], 0) + abs(c["new_value"] - 0.5)

        sorted_nodes = sorted(by_node.items(), key=lambda x: x[1], reverse=True)

        return {
            "has_evolution": True,
            "changes_count": len(history),
            "most_changed": [{"node": n, "change": v} for n, v in sorted_nodes[:3]],
            "recent_changes": history[-5:],
        }

    # ==================== МУТАЦИИ ====================

    def set_trait(self, name: str, value: float):
        """Устанавливает значение качества"""
        graph = self._graph()
        if name not in graph["nodes"]:
            return
        nodes = graph["nodes"]
        nodes[name]["value"] = max(0.0, min(1.0, value))
        with get_connection() as conn:
            repo.update_trait_graph(conn, nodes=nodes)
            repo.record_trait_change(conn, name, nodes[name]["value"])
            conn.commit()

    def change_trait(self, name: str, delta: float, source: str = None):
        """Изменяет качество и распространяет по связям"""
        graph = self._graph()
        if name not in graph["nodes"]:
            return

        nodes = graph["nodes"]
        edges = graph["edges"]

        old_value = nodes[name]["value"]
        new_value = max(0.0, min(1.0, old_value + delta))
        nodes[name]["value"] = new_value

        if name in edges:
            for child, weight in edges[name].items():
                child_delta = delta * abs(weight) * 0.3
                if weight < 0:
                    child_delta = -child_delta
                self._propagate_change(nodes, edges, child, child_delta)

        with get_connection() as conn:
            repo.update_trait_graph(conn, nodes=nodes)
            repo.record_trait_change(conn, name, new_value, source=source)
            conn.commit()

    def _propagate_change(self, nodes: Dict[str, Any], edges: Dict[str, Any], node: str, delta: float):
        if node not in nodes:
            return

        old_value = nodes[node]["value"]
        new_value = max(0.0, min(1.0, old_value + delta))
        nodes[node]["value"] = new_value

        if abs(delta) > 0.005 and node in edges:
            for child, weight in edges[node].items():
                child_delta = delta * abs(weight) * 0.3
                if weight < 0:
                    child_delta = -child_delta
                self._propagate_change(nodes, edges, child, child_delta)

    def set_edge_weight(self, source: str, target: str, weight: float):
        """Изменяет вес связи"""
        graph = self._graph()
        edges = graph["edges"]
        if source not in edges or target not in edges[source]:
            return
        old_weight = edges[source][target]
        new_weight = max(-1.0, min(1.0, weight))
        edges[source][target] = new_weight
        with get_connection() as conn:
            repo.update_trait_graph(conn, edges=edges)
            repo.record_trait_edge_change(conn, source, target, old_weight, new_weight)
            conn.commit()

    def learn_edge(self, source: str, target: str, success: bool):
        """Обучение связи"""
        graph = self._graph()
        edges = graph["edges"]
        if source in edges and target in edges[source]:
            current = edges[source][target]
            delta = 0.05 if success else -0.03
            self.set_edge_weight(source, target, current + delta)

    def reflect(self, event: str, intensity: float = 0.05):
        """Рефлексия"""
        changes = EVENT_MAP.get(event, {})
        for node, delta in changes.items():
            self.change_trait(node, delta * intensity, source=event)

    def answer_internal_question(self, question_idx: int, answer: str):
        questions = self.get_internal_questions()
        if 0 <= question_idx < len(questions):
            question_id = questions[question_idx]["_question_id"]
            with get_connection() as conn:
                repo.record_internal_question_answer(conn, question_id, answer)
                conn.commit()


def get_personality_graph() -> PersonalityGraph:
    return PersonalityGraph()
