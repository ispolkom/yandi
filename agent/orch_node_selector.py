"""
assistant/orch_node_selector.py — Node Selector.
Выбирает ноды для валидации по репутации + domain_score.
MVP: локальные Ollama-ноды с разными seed для независимости.
Future: P2P YANDI через FederationManager (orch_federation.py).
"""
from __future__ import annotations

import socket

from agent.orch_schemas  import RiskResult, NodeInfo, NodeSelectorResult
from agent.orch_reputation import get_best_nodes, register_node, list_nodes

YANDI_COUNCIL_URL = "http://127.0.0.1:9010"


def yandi_connected(timeout: float = 2.0) -> bool:
    """Проверить доступность YANDI council (браузерные модели)."""
    try:
        import requests
        s = requests.Session()
        s.trust_env = False
        r = s.get(f"{YANDI_COUNCIL_URL}/api/council/state", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False

# Флаг: использовать Federation Manager для discovery нод
# True = через orch_federation.py (поддержка Council/YANDI)
# False = только локальный реестр репутации (MVP по умолчанию)
# AUTO = проверять council_available() при каждом запросе
USE_FEDERATION = "auto"  # "auto" | True | False

# Локальные Ollama-ноды (MVP).
# Одна физическая нода, но разные seed/temperature → псевдо-независимость.
LOCAL_NODES = [
    {"node_id": "local-qwen14b-a", "model": "qwen3:14b",      "endpoint": "http://127.0.0.1:11434"},
    {"node_id": "local-qwen14b-b", "model": "qwen3:14b",      "endpoint": "http://127.0.0.1:11434"},
    {"node_id": "local-deepseek",  "model": "deepseek-r1:14b","endpoint": "http://127.0.0.1:11434"},
]

# Параметры нод (разные seed для псевдо-независимости)
NODE_PARAMS = {
    "local-qwen14b-a": {"temperature": 0.1, "seed": 42},
    "local-qwen14b-b": {"temperature": 0.3, "seed": 137},
    "local-deepseek":  {"temperature": 0.2, "seed": 7},
}


def _ensure_registered():
    """Зарегистрировать локальные ноды если ещё не зарегистрированы."""
    existing = {n["node_id"] for n in list_nodes()}
    for n in LOCAL_NODES:
        if n["node_id"] not in existing:
            register_node(n["node_id"], n["model"], n["endpoint"])


def select_nodes(risk: RiskResult, domain: str = "general") -> NodeSelectorResult:
    """
    Выбрать ноды для валидации.

    Args:
        risk:   результат Risk Engine (определяет сколько нод нужно)
        domain: домен запроса (для domain_score)

    Returns:
        NodeSelectorResult со списком нод
    """
    _ensure_registered()
    n_required = risk.nodes_required
    fallback   = False

    # Попробовать выбрать лучшие ноды из реестра репутации
    best = get_best_nodes(domain, n=n_required)

    if len(best) < n_required:
        # Fallback на локальные ноды
        fallback = True
        best = LOCAL_NODES[:n_required]
        nodes = [
            NodeInfo(
                node_id=n["node_id"],
                model=n["model"],
                endpoint=n["endpoint"],
                reputation=0.7,
                domain_score=0.7,
                speed_score=0.5,
            )
            for n in best
        ]
    else:
        nodes = [
            NodeInfo(
                node_id=n["node_id"],
                model=n["model"],
                endpoint=n["endpoint"],
                reputation=n.get("reputation", 0.7),
                domain_score=n.get("domain_score", 0.7),
                speed_score=max(0.1, 1.0 - n.get("speed", 10) / 60),
            )
            for n in best
        ]

    return NodeSelectorResult(nodes=nodes, fallback_used=fallback)


def select_nodes_federated(risk: RiskResult, domain: str = "general") -> NodeSelectorResult:
    """
    Выбрать ноды через Federation Manager (Council / YANDI).
    Используется когда USE_FEDERATION=True или USE_FEDERATION="auto" и Council доступен.
    """
    try:
        from agent.orch_federation import get_federation
        fed    = get_federation()
        driver = fed.get_driver()
        nodes  = driver.get_nodes(domain=domain, n=risk.nodes_required)
        return NodeSelectorResult(nodes=nodes, fallback_used=False)
    except Exception:
        return select_nodes(risk, domain=domain)


def _should_use_federation() -> bool:
    """Определить нужна ли федерация (авто-детект или явный флаг)."""
    if USE_FEDERATION is True:
        return True
    if USE_FEDERATION is False:
        return False
    # "auto": использовать Council если доступен
    try:
        from agent.orch_council_connector import council_available
        return council_available()
    except Exception:
        return False


def get_node_params(node_id: str) -> dict:
    """Получить параметры генерации для конкретной ноды."""
    return NODE_PARAMS.get(node_id, {"temperature": 0.2, "seed": 0})


if __name__ == "__main__":
    from agent.orch_risk import assess_risk
    for q in ["Как лечить кашель?", "Что такое DHT?"]:
        risk   = assess_risk(q)
        result = select_nodes(risk)
        print(f"\n[{risk.risk_level}] {q}")
        print(f"  fallback={result.fallback_used}, nodes={len(result.nodes)}")
        for n in result.nodes:
            print(f"  · {n.node_id} ({n.model}) rep={n.reputation:.2f}")
