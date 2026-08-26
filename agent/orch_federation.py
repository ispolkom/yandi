"""
assistant/orch_federation.py — Federation Adapter.
Абстрагирует доступ к нодам валидации: MVP=локальные Ollama → будущее=P2P YANDI mesh.

Дизайн:
  NodeDriver — интерфейс
    LocalOllamaDriver  — текущий MVP (разные seed/temperature)
    CouncilDriver      — Council (GPT/Claude/DeepSeek) через council_chat_server
    YandiDriver        — P2P YANDI mesh через AI-RPC (будущее)

CLI:
  python3 assistant/orch_federation.py status   — состояние нод
  python3 assistant/orch_federation.py nodes    — список активных нод
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Optional

from agent.orch_schemas import NodeInfo


# ── Базовый интерфейс ────────────────────────────────────────────────────────

class NodeDriver(ABC):
    """Интерфейс для источника нод."""

    @abstractmethod
    def get_nodes(self, domain: str = "general", n: int = 3) -> list[NodeInfo]:
        """Вернуть N доступных нод для данного домена."""
        ...

    @abstractmethod
    def ping(self) -> bool:
        """Проверить доступность источника нод."""
        ...

    @property
    @abstractmethod
    def driver_type(self) -> str:
        ...


# ── Локальный Ollama Driver (MVP) ─────────────────────────────────────────────

class LocalOllamaDriver(NodeDriver):
    """MVP: локальные Ollama-ноды с разными seed/temperature."""

    ENDPOINT = "http://127.0.0.1:11434"
    NODES = [
        {"node_id": "local-qwen14b-a", "model": "qwen3:14b",       "params": {"temperature": 0.1, "seed": 42}},
        {"node_id": "local-qwen14b-b", "model": "qwen3:14b",       "params": {"temperature": 0.3, "seed": 137}},
        {"node_id": "local-deepseek",  "model": "deepseek-r1:14b", "params": {"temperature": 0.2, "seed": 7}},
    ]

    def get_nodes(self, domain: str = "general", n: int = 3) -> list[NodeInfo]:
        return [
            NodeInfo(node_id=nd["node_id"], model=nd["model"], endpoint=self.ENDPOINT)
            for nd in self.NODES[:n]
        ]

    def ping(self) -> bool:
        try:
            import requests
            s = requests.Session()
            s.trust_env = False
            r = s.get(f"{self.ENDPOINT}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    @property
    def driver_type(self) -> str:
        return "local_ollama"


# ── Council Driver ────────────────────────────────────────────────────────────

class CouncilDriver(NodeDriver):
    """Council (GPT/Claude/DeepSeek) через council_chat_server."""

    SERVER = "http://127.0.0.1:9010"
    # Council-ноды как виртуальные валидаторы
    NODES = [
        {"node_id": "council-claude",   "model": "claude-sonnet-4-6",  "endpoint": "council"},
        {"node_id": "council-gpt",      "model": "gpt-4o",              "endpoint": "council"},
        {"node_id": "council-deepseek", "model": "deepseek-v3",        "endpoint": "council"},
    ]

    def get_nodes(self, domain: str = "general", n: int = 3) -> list[NodeInfo]:
        return [
            NodeInfo(node_id=nd["node_id"], model=nd["model"], endpoint=nd["endpoint"])
            for nd in self.NODES[:n]
        ]

    def ping(self) -> bool:
        try:
            import requests
            s = requests.Session()
            s.trust_env = False
            r = s.get(f"{self.SERVER}/api/council/state", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    @property
    def driver_type(self) -> str:
        return "council"


# ── YANDI Driver (future stub) ────────────────────────────────────────────────

class YandiDriver(NodeDriver):
    """YANDI: валидация через council_chat_server (браузерные модели, без Ollama)."""

    COUNCIL_URL = "http://127.0.0.1:9010"

    def get_nodes(self, domain: str = "general", n: int = 3) -> list[NodeInfo]:
        if not self.ping():
            return []
        return [NodeInfo(
            node_id      = "yandi-council",
            model        = "yandi-browser",
            endpoint     = f"{self.COUNCIL_URL}/api/yandi/validate",
            reputation   = 0.95,
            domain_score = 0.95,
            speed_score  = 0.8,
        )]

    def ping(self) -> bool:
        try:
            import requests
            s = requests.Session()
            s.trust_env = False
            r = s.get(f"{self.COUNCIL_URL}/api/council/state", timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    @property
    def driver_type(self) -> str:
        return "yandi_p2p"


# ── Federation Manager ────────────────────────────────────────────────────────

class FederationManager:
    """
    Выбирает активный Driver и предоставляет доступ к нодам.

    Приоритет (MVP → будущее):
    1. LocalOllamaDriver — всегда доступен
    2. CouncilDriver — если council_chat_server запущен
    3. YandiDriver — когда YANDI Iter 6 реализован
    """

    def __init__(self):
        self._drivers: list[NodeDriver] = [
            LocalOllamaDriver(),
            CouncilDriver(),
            YandiDriver(),
        ]
        self._active: Optional[NodeDriver] = None
        self._last_ping: float = 0

    def _select_driver(self) -> NodeDriver:
        """Выбрать лучший доступный driver."""
        # Попробовать в порядке приоритета (обратном — YANDI лучший)
        for driver in reversed(self._drivers):
            if driver.ping():
                return driver
        # Fallback на первый (LocalOllama)
        return self._drivers[0]

    def get_driver(self, force_refresh: bool = False) -> NodeDriver:
        """Получить активный driver (кэшируется на 60s)."""
        now = time.time()
        if force_refresh or self._active is None or now - self._last_ping > 60:
            self._active = self._select_driver()
            self._last_ping = now
        return self._active

    def get_nodes(self, domain: str = "general", n: int = 3) -> list[NodeInfo]:
        """Получить ноды через активный driver."""
        driver = self.get_driver()
        return driver.get_nodes(domain=domain, n=n)

    def status(self) -> dict:
        """Статус всех drivers."""
        result = {"active": None, "drivers": []}
        for d in self._drivers:
            available = d.ping()
            result["drivers"].append({
                "type":      d.driver_type,
                "available": available,
            })
            if available:
                result["active"] = d.driver_type
        return result


# ── Singleton ─────────────────────────────────────────────────────────────────

_federation: Optional[FederationManager] = None


def get_federation() -> FederationManager:
    global _federation
    if _federation is None:
        _federation = FederationManager()
    return _federation


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    fed = get_federation()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        st = fed.status()
        print(f"Активный driver: {st['active']}")
        for d in st["drivers"]:
            icon = "✓" if d["available"] else "✗"
            print(f"  {icon} {d['type']}")

    elif cmd == "nodes":
        nodes = fed.get_nodes(domain="tech", n=3)
        print(f"Активные ноды ({fed.get_driver().driver_type}):")
        for n in nodes:
            print(f"  - {n.node_id} [{n.model}] @ {n.endpoint}")
