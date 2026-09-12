"""
agent/orch_peer_directory_regression_test.py

Мандат "Real Node Directory Integration". Проверяет ровно то, что этот
клиент обязан гарантировать:
  1. list_trusted_peers()/list_online_trusted_peers() НИКОГДА не бросают
     исключение при недоступном локальном мосте — честное "пиров нет",
     не сбой (TEST10: "Нет peers. Никакого фиктивного localhost-node"
     начинается именно здесь — молчаливый transport error не должен
     превращаться в фейковую ноду выше по стеку).
  2. offline/недоверенные записи не попадают в list_online_trusted_peers().
  3. infer_on_peer() НИКОГДА не передаёт модель/backend/URL — только
     node_id + сообщения (TEST8: Python не знает backend узла).
  4. infer_on_peer() бросает PeerDirectoryError (не молча возвращает
     пустую строку и не тихо переключается на локальный backend) при
     сетевой ошибке, HTTP-ошибке или малформед-ответе.

Запуск: python3 -m agent.orch_peer_directory_regression_test
"""
from __future__ import annotations

import sys
from unittest.mock import patch, MagicMock

import requests

from agent import orch_peer_directory as pd

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def _fake_response(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    if status_code >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}")
    else:
        r.raise_for_status.return_value = None
    return r


def test_list_peers_never_raises_on_transport_failure():
    with patch.object(pd._session, "get", side_effect=requests.ConnectionError("refused")):
        result = pd.list_trusted_peers()
    check("list_trusted_peers() returns [] (not an exception) when the local bridge is unreachable", result == [])

    with patch.object(pd._session, "get", side_effect=requests.ConnectionError("refused")):
        result2 = pd.list_online_trusted_peers()
    check("list_online_trusted_peers() also degrades to [] gracefully", result2 == [])


def test_online_filter_excludes_offline_and_untrusted():
    peers = [
        {"node_id": "aa", "trusted": True, "online": True},
        {"node_id": "bb", "trusted": True, "online": False},
        {"node_id": "cc", "trusted": False, "online": True},
    ]
    with patch.object(pd, "list_trusted_peers", return_value=peers):
        online = pd.list_online_trusted_peers()
    check("only the trusted AND online peer survives the filter", [p["node_id"] for p in online] == ["aa"])


def test_infer_on_peer_never_sends_backend_selection_fields():
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _fake_response(200, {"content": "ok"})

    with patch.object(pd._session, "post", side_effect=fake_post):
        result = pd.infer_on_peer("deadbeef" * 8, messages=[{"role": "user", "content": "hi"}])

    check("infer_on_peer returns the peer's content", result == "ok")
    check("request body has no 'model' field", "model" not in captured["json"])
    check("request body has no 'base_url'/'backend'/'provider' field", not any(
        k in captured["json"] for k in ("base_url", "backend", "provider", "endpoint")
    ))
    check("request body carries remote_peer = the canonical node_id, not a URL", captured["json"].get("remote_peer") == "deadbeef" * 8)
    check("request goes to the local loopback bridge, not an arbitrary host", captured["url"].startswith("http://127.0.0.1:18082"))


def test_infer_on_peer_raises_on_transport_failure():
    with patch.object(pd._session, "post", side_effect=requests.ConnectionError("refused")):
        try:
            pd.infer_on_peer("aa" * 32, messages=[{"role": "user", "content": "hi"}])
            check("infer_on_peer raises PeerDirectoryError on connection failure", False)
        except pd.PeerDirectoryError:
            check("infer_on_peer raises PeerDirectoryError on connection failure", True)


def test_infer_on_peer_raises_on_backend_failure_not_silent_fallback():
    with patch.object(pd._session, "post", return_value=_fake_response(502, {"error": "backend_error"})):
        try:
            pd.infer_on_peer("aa" * 32, messages=[{"role": "user", "content": "hi"}])
            check("infer_on_peer raises on a peer-side backend failure (never silently substitutes)", False)
        except pd.PeerDirectoryError as e:
            check("infer_on_peer raises on a peer-side backend failure (never silently substitutes)", True)
            check("the error mentions the node_id for traceability", "aa" * 32 in str(e))


def test_infer_on_peer_raises_on_malformed_response():
    with patch.object(pd._session, "post", return_value=_fake_response(200, {"not_content": "x"})):
        try:
            pd.infer_on_peer("aa" * 32, messages=[{"role": "user", "content": "hi"}])
            check("infer_on_peer raises on a malformed (no 'content') response", False)
        except pd.PeerDirectoryError:
            check("infer_on_peer raises on a malformed (no 'content') response", True)


def main() -> int:
    test_list_peers_never_raises_on_transport_failure()
    test_online_filter_excludes_offline_and_untrusted()
    test_infer_on_peer_never_sends_backend_selection_fields()
    test_infer_on_peer_raises_on_transport_failure()
    test_infer_on_peer_raises_on_backend_failure_not_silent_fallback()
    test_infer_on_peer_raises_on_malformed_response()

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
    else:
        print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
