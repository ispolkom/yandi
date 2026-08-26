"""
assistant/orch_node_selector.py — выбор нод на основе репутации.
Использует Decision Ledger из orch_reputation.py.
"""
from __future__ import annotations

from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from agent.orch_reputation import get_best_nodes as _get_best_nodes, list_nodes as _list_nodes


@dataclass
class NodeInfo:
    node_id: str
    model: str = ""
    endpoint: str = ""
    reputation: float = 0.0
    speed: float = 0.0
    domain_score: float = 0.0


@dataclass
class NodeSelectionResult:
    nodes: List[NodeInfo]
    selected: bool = True


class NodeSelector:
    def __init__(self):
        pass

    def get_best_nodes(self, domain: str = "general", limit: int = 3) -> List[Dict[str, Any]]:
        return _get_best_nodes(domain, limit)

    def list_nodes(self) -> List[Dict[str, Any]]:
        return _list_nodes()


def get_top_reputation(entity_type: str, domain: str = "general", limit: int = 3) -> List[Dict[str, Any]]:
    if entity_type == "node":
        return _get_best_nodes(domain, limit)
    return []


def get_ledger():
    return None


def get_node_params(node_id: str) -> Dict[str, Any]:
    """
    Получить параметры ноды по ID.
    Возвращает словарь с model, endpoint, reputation.
    """
    nodes = _list_nodes()
    for node in nodes:
        if node.get("node_id") == node_id:
            return {
                "model": node.get("model", "unknown"),
                "endpoint": node.get("endpoint", "http://127.0.0.1:11434"),
                "reputation": node.get("reputation", 0.5),
            }
    # Fallback для yandi-council
    if node_id == "yandi-council":
        return {
            "model": "heretic:q8",
            "endpoint": "http://127.0.0.1:11434",
            "reputation": 0.7,
        }
    return {
        "model": "unknown",
        "endpoint": "http://127.0.0.1:11434",
        "reputation": 0.5,
    }


def select_nodes(risk, domain: str = "general", limit: int = 3) -> NodeSelectionResult:
    top_nodes = get_top_reputation("node", domain, limit)
    
    nodes = []
    for item in top_nodes:
        nodes.append(NodeInfo(
            node_id=item.get("node_id", "unknown"),
            model=item.get("model", "unknown"),
            endpoint=item.get("endpoint", "unknown"),
            reputation=item.get("reputation", 0.0),
            domain_score=item.get("domain_score", 0.0),
        ))
    
    if not nodes:
        nodes.append(NodeInfo(
            node_id="yandi-council",
            model="heretic:q8",
            endpoint="http://127.0.0.1:11434",
            reputation=0.7,
            domain_score=0.7,
        ))
    
    return NodeSelectionResult(nodes=nodes)


def select_nodes_federated(risk, domain: str = "general", limit: int = 3) -> NodeSelectionResult:
    return select_nodes(risk, domain, limit)


def _should_use_federation() -> bool:
    return False


def register_node(node_id: str, model: str, endpoint: str):
    pass


def update_node(node_id: str, correct: bool, latency: float, domain: str = "general"):
    pass


def get_best_nodes(domain: str = "general", n: int = 3) -> List[Dict[str, Any]]:
    return NodeSelector().get_best_nodes(domain, n)


def list_nodes() -> List[Dict[str, Any]]:
    return NodeSelector().list_nodes()
