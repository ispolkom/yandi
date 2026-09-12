"""
agent/orch_peer_directory.py — тонкий Python-клиент к реальному P2P
peer directory ноды (мандат "Real Node Directory Integration").

Проблема, которую это закрывает: Python-эпистемика (orch_validator.py,
orch_node_selector.py) раньше НЕ знала реальные физические P2P-ноды —
"реестр нод" (orch_reputation.py) был локальной абстракцией и в каждой
ветке подставлял фиктивный `127.0.0.1:11434`.

Этот модуль НЕ хранит peers сам, НЕ делает discovery, НЕ знает NAT/relay
— это задача Rust-транспорта. Он только спрашивает уже существующий
локальный HTTP-интерфейс Rust-ноды (`node/src/web/ai_rpc_server.rs`,
127.0.0.1:18082, тот же самый, что уже использовался для inference) —
"какие реальные доверенные ноды ты знаешь и кто из них сейчас онлайн" —
и "выполни интеллектуальный запрос на ноде X". Rust решает, как физически
доставить; этот модуль просто транспортирует HTTP до localhost.

Canonical node identity — hex-encoded node_id() (см. отчёт, раздел
"CANONICAL NODE ID") — НЕ IP, НЕ модель, НЕ backend. Ни эта функция, ни
её вызывающие никогда не видят и не могут задать backend/model чужой
ноды — см. llm_gateway/intelligence_bridge.py и предыдущий мандат.
"""
from __future__ import annotations

import requests

_BASE_URL = "http://127.0.0.1:18082"
_TIMEOUT_LIST = 3
_TIMEOUT_INFER = 90

_session = requests.Session()
_session.trust_env = False  # см. shell-обёртки start*.sh: локальный трафик никогда через системный прокси


class PeerDirectoryError(RuntimeError):
    """Настоящая ошибка похода к Rust-ноде или к пиру — никогда не
    проглатывается молча вызывающим кодом в честный ответ."""


def list_trusted_peers() -> list[dict]:
    """Список доверенных AI-RPC пиров этой ноды с их живым online-статусом.

    Никогда не бросает исключение — если Rust-нода не запущена или
    AI-RPC отключён, это ЧЕСТНОЕ "пиров нет", а не повод упасть: вызывающий
    код (orch_node_selector.py) должен трактовать пустой список как
    NO_REMOTE_NODE_AVAILABLE, не как ошибку транспорта.
    """
    try:
        r = _session.get(f"{_BASE_URL}/api/ai-rpc/peers", timeout=_TIMEOUT_LIST)
        r.raise_for_status()
        return r.json().get("peers", [])
    except requests.RequestException:
        return []


def list_online_trusted_peers() -> list[dict]:
    """Только реально доступные прямо сейчас пиры (мандат: "Не считать
    offline peer живым")."""
    return [p for p in list_trusted_peers() if p.get("trusted") and p.get("online")]


def infer_on_peer(
    node_id: str,
    messages: list[dict[str, str]],
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Попросить конкретную (по canonical node_id) доверенную ноду
    выполнить интеллектуальный запрос. Никогда не передаёт и не может
    передать имя модели/backend — это решает ТА нода сама (см.
    llm_gateway/intelligence_bridge.py).

    Бросает PeerDirectoryError при любой неудаче (пир недоступен,
    неавторизован, его backend упал, таймаут) — вызывающий код должен
    сам решить, как реагировать (например orch_validator.py трактует
    это как verdict="partial"), но никогда не должен получить сюда
    тихую подмену ответом с локальной модели.
    """
    body: dict = {"remote_peer": node_id, "messages": messages}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature

    try:
        r = _session.post(f"{_BASE_URL}/api/ai-rpc/infer", json=body, timeout=_TIMEOUT_INFER)
    except requests.RequestException as e:
        raise PeerDirectoryError(f"local AI-RPC bridge unreachable: {e}") from e

    if r.status_code != 200:
        detail = ""
        try:
            detail = r.json().get("error", "")
        except Exception:
            pass
        raise PeerDirectoryError(f"peer {node_id} inference failed (HTTP {r.status_code}): {detail}")

    data = r.json()
    content = data.get("content")
    if not isinstance(content, str):
        raise PeerDirectoryError(f"peer {node_id} returned a malformed response: {data!r}")
    return content
