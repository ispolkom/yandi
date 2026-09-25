"""
llm_gateway.node_gateway — клиент к шлюзу моделей ЗАПУЩЕННОГО УЗЛА (Rust): `POST /api/gateway/call` на loopback.

Зачем: выбор backend'а, собственный движок (`llama-server`, вшитый в бинарник узла), настройки владельца — всё это делает узел, ОДИН раз на
машину. Ядро на Python (PET, agent/) не держит второй копии модели в памяти и не нуждается ни в `llama_cpp`, ни в Ollama.

Включается ЯВНО: переменная окружения `YANDI_NODE_GATEWAY_URL` (например `http://127.0.0.1:18082`). Не задана — работает прежний путь Python.
Когда включено, тихого отката на Python-путь НЕТ: недоступный узел — честная ошибка (тот же инвариант «явный выбор владельца сильнее отката»).
"""
from __future__ import annotations

import os
from typing import Any

import requests

ENV_URL = "YANDI_NODE_GATEWAY_URL"
# запуск движка (загрузка модели) может занять до двух минут сверх времени самой генерации
_STARTUP_ALLOWANCE = 150

_session = requests.Session()
_session.trust_env = False  # loopback не должен идти через системный прокси


def gateway_url() -> str | None:
    v = os.environ.get(ENV_URL, "").strip()
    return v.rstrip("/") if v else None


def enabled() -> bool:
    return gateway_url() is not None


class NodeGatewayError(RuntimeError):
    """Узел недоступен или ответил не по контракту (не ошибка модели — та приходит как {"error": {...}})."""


def call(name: str, args: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    """Вызов шлюза узла. Возвращает `{"ok": …}` либо `{"error": {"class", "msg"}}`; недоступность узла — NodeGatewayError."""
    url = gateway_url()
    if url is None:
        raise NodeGatewayError(f"{ENV_URL} не задана")
    try:
        resp = _session.post(f"{url}/api/gateway/call", json={"name": name, "args": args}, timeout=timeout + _STARTUP_ALLOWANCE)
    except requests.RequestException as e:
        raise NodeGatewayError(f"шлюз узла недоступен ({url}): {e}") from e
    try:
        body = resp.json()
    except ValueError as e:
        raise NodeGatewayError(f"шлюз узла вернул не JSON (HTTP {resp.status_code})") from e
    if not isinstance(body, dict) or not ("ok" in body or "error" in body):
        raise NodeGatewayError(f"шлюз узла вернул ответ не по контракту (HTTP {resp.status_code})")
    if resp.status_code == 400 and isinstance(body.get("error"), dict) and body["error"].get("class") == "BadRequest":
        raise NodeGatewayError(f"шлюз узла отклонил вызов: {body['error'].get('msg')}")
    return body
