"""
assistant/orch_node_selector.py — выбор нод на основе репутации.
Использует Decision Ledger из orch_reputation.py.

Мандат "Real Node Directory Integration": раньше каждая ветка этого
модуля при отсутствии реальной ноды подставляла фиктивную
"yandi-council" @ 127.0.0.1:11434 — т.е. Python эпистемика никогда не
отличала "нашли реальный пир" от "пиров нет вообще". Теперь источник
живых нод — agent.orch_peer_directory (спрашивает РЕАЛЬНЫЙ P2P peer
directory Rust-ноды, см. отчёт). node_id здесь — canonical identity
(hex node_id() транспортного слоя), НЕ IP и НЕ модель; endpoint для
таких нод — не URL, а sentinel "p2p" (см. orch_validator.py — та же
идиома, что уже используется для "council"/"/api/yandi/validate").
"""
from __future__ import annotations

from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from agent.orch_reputation import (
    get_best_nodes as _get_best_nodes,
    list_nodes as _list_nodes,
    register_node as _reputation_register_node,
)
from agent.orch_peer_directory import list_online_trusted_peers

# Sentinel вместо реального URL — см. orch_validator.py::_validate_on_node().
# Модель/backend реальной удалённой ноды НАМ НЕИЗВЕСТНЫ и НЕ ДОЛЖНЫ быть
# известны (см. llm_gateway/intelligence_bridge.py) — "hidden" честно
# говорит об этом, а не выдумывает имя.
P2P_ENDPOINT_SENTINEL = "p2p"
_HIDDEN_MODEL = "hidden (backend decided by peer owner)"


def yandi_connected() -> bool:
    """Проверить, слушается ли порт 9999 (YANDI P2P нода).

    PET_AGENT_BOUNDARY_AUDIT.md Phase 4C finding: this function did not
    exist at all before, even though chat_orch.py::_bg_validate has
    imported it (`from agent.orch_node_selector import yandi_connected`)
    since that code was written - every single call raised ImportError
    immediately, caught by _bg_validate's own outer try/except, silently
    skipping BOTH the intended P2P branch AND the DeepSeek/local-model
    fallback branch nested inside its `else`. Minimal fix: the same
    TCP-connect check pet/chat_orch.py::_p2p_available() already performs
    independently, moved to its correct owner (agent decides P2P
    availability, pet does not need its own separate copy of this check).
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(("127.0.0.1", 9999))
        s.close()
        return True
    except Exception:
        return False


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
    Получить параметры ноды по ID — ТОЛЬКО для локальной псевдо-
    независимой проверки (endpoint="local", тот самый node_id
    "yandi-council", что и раньше — сохраняем непрерывность истории
    репутации под этим ключом). Для реальных P2P-пиров (endpoint="p2p")
    этот словарь не используется вообще: temperature/seed — внутреннее
    дело владельца ТОЙ ноды, не наше.
    """
    nodes = _list_nodes()
    for node in nodes:
        if node.get("node_id") == node_id:
            return {
                "model": node.get("model", "unknown"),
                "reputation": node.get("reputation", 0.5),
            }
    if node_id == "yandi-council":
        return {"model": "heretic:q8", "reputation": 0.7}
    return {"model": "unknown", "reputation": 0.5}


def select_nodes(risk, domain: str = "general", limit: int = 3) -> NodeSelectionResult:
    """Мандат "Real Node Directory Integration": сначала спрашиваем
    РЕАЛЬНЫЕ доверенные P2P-ноды (canonical node_id, живой online-статус
    от Rust-транспорта). Если такие есть — используем ТОЛЬКО их,
    отранжированные по накопленной репутации; ни при каких условиях не
    считаем офлайн- или незарегистрированный пир доступным (мандат:
    "Не считать offline peer живым. Не выдумывать peers.").

    Если реальных пиров нет вообще — не выдаём себя за них: явный,
    промаркированный локальный self-check (endpoint="local", тот же
    node_id "yandi-council", что и до этого мандата, — непрерывность
    истории репутации), а не фиктивный "remote"-узел на localhost."""
    online_peers = list_online_trusted_peers()
    for p in online_peers:
        node_id = p.get("node_id")
        if not node_id:
            continue
        # Idempotent upsert (INSERT OR IGNORE) — модель/backend этой
        # ноды нам неизвестны и не должны быть известны.
        _reputation_register_node(node_id, _HIDDEN_MODEL, P2P_ENDPOINT_SENTINEL)

    online_ids = {p["node_id"] for p in online_peers if p.get("node_id")}
    top_nodes = get_top_reputation("node", domain, limit)

    nodes = []
    for item in top_nodes:
        node_id = item.get("node_id", "unknown")
        if node_id not in online_ids:
            continue  # реальный пир, но сейчас offline — не выдаём за доступного
        nodes.append(NodeInfo(
            node_id=node_id,
            model=_HIDDEN_MODEL,
            endpoint=P2P_ENDPOINT_SENTINEL,
            reputation=item.get("reputation", 0.0),
            domain_score=item.get("domain_score", 0.0),
        ))
    nodes = nodes[:limit]

    if not nodes:
        nodes.append(NodeInfo(
            node_id="yandi-council",
            model="heretic:q8",
            endpoint="local",
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
