"""
agent/orch_node_selector_regression_test.py

Мандат "Real Node Directory Integration" — TEST4/TEST6/TEST10/TEST11 из
мандата на уровне Python-селектора (без реальной Rust-ноды, той стороны
уже касается node/tests/ai_rpc_no_ollama_test.rs):

  TEST4  — offline peer не считается доступным для выбора.
  TEST6  — untrusted/неизвестный peer не попадает в pool.
  TEST10 — нет peers => НЕ фиктивный localhost-node с реальным URL,
           только явно промаркированный local-фоллбэк (endpoint="local").
  TEST11 — два разных canonical node_id остаются РАЗНЫМИ записями.

Плюс: реальный online-пир выбирается с endpoint="p2p" (sentinel, НЕ URL)
и с моделью, честно помеченной как скрытую — Python никогда не
"придумывает" модель чужой ноды.

Запуск: python3 -m agent.orch_node_selector_regression_test
"""
from __future__ import annotations

import sys
from unittest.mock import patch

from agent import orch_node_selector as sel

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def test_no_peers_gives_honest_local_fallback_not_a_fake_url():
    with patch.object(sel, "list_online_trusted_peers", return_value=[]), \
         patch.object(sel, "_reputation_register_node"), \
         patch.object(sel, "get_top_reputation", return_value=[]):
        result = sel.select_nodes(risk=None)

    check("exactly one fallback node when no peers exist", len(result.nodes) == 1)
    n = result.nodes[0]
    check("fallback endpoint is the honest 'local' sentinel, not a fake URL", n.endpoint == "local")
    check("fallback endpoint is NOT the old fake Ollama URL", "11434" not in n.endpoint and "http" not in n.endpoint)


def test_real_online_peer_is_selected_with_p2p_sentinel_and_hidden_model():
    peers = [{"node_id": "aa" * 32, "trusted": True, "online": True, "name": "node-A"}]
    reputation_rows = [{"node_id": "aa" * 32, "model": sel._HIDDEN_MODEL, "endpoint": "p2p",
                         "reputation": 0.8, "domain_score": 0.8}]
    with patch.object(sel, "list_online_trusted_peers", return_value=peers), \
         patch.object(sel, "_reputation_register_node") as reg, \
         patch.object(sel, "get_top_reputation", return_value=reputation_rows):
        result = sel.select_nodes(risk=None)

    check("the real online peer is selected", len(result.nodes) == 1 and result.nodes[0].node_id == "aa" * 32)
    check("endpoint is the p2p sentinel, not a URL", result.nodes[0].endpoint == sel.P2P_ENDPOINT_SENTINEL)
    check("model is honestly marked hidden, never guessed", result.nodes[0].model == sel._HIDDEN_MODEL)
    check("the peer was (idempotently) registered into reputation with the sentinel endpoint", reg.called)
    reg_args = reg.call_args[0]
    check("registration never fabricates a real model/URL for the peer", reg_args[1] == sel._HIDDEN_MODEL and reg_args[2] == "p2p")


def test_offline_peer_with_old_reputation_is_not_selected():
    # B has reputation history from a PAST session but is offline right now.
    with patch.object(sel, "list_online_trusted_peers", return_value=[]), \
         patch.object(sel, "_reputation_register_node"), \
         patch.object(sel, "get_top_reputation", return_value=[
             {"node_id": "bb" * 32, "model": "x", "endpoint": "p2p", "reputation": 0.9, "domain_score": 0.9},
         ]):
        result = sel.select_nodes(risk=None)

    check("an offline peer with old reputation rows is NEVER selected as available",
          all(n.node_id != "bb" * 32 for n in result.nodes))
    check("falls back to the honest local sentinel instead", len(result.nodes) == 1 and result.nodes[0].endpoint == "local")


def test_untrusted_peer_never_enters_pool():
    # get_top_reputation could in principle return a node_id nothing
    # currently trusts (e.g. a stale row); it must never be selected
    # unless it is ALSO in the live online-trusted set.
    with patch.object(sel, "list_online_trusted_peers", return_value=[]), \
         patch.object(sel, "_reputation_register_node"), \
         patch.object(sel, "get_top_reputation", return_value=[
             {"node_id": "untrusted-stranger", "model": "x", "endpoint": "p2p", "reputation": 0.99, "domain_score": 0.99},
         ]):
        result = sel.select_nodes(risk=None)

    check("a node_id with reputation but no live trusted+online entry is never selected",
          all(n.node_id != "untrusted-stranger" for n in result.nodes))


def test_two_real_peers_remain_distinct_records():
    peers = [
        {"node_id": "aa" * 32, "trusted": True, "online": True},
        {"node_id": "bb" * 32, "trusted": True, "online": True},
    ]
    reputation_rows = [
        {"node_id": "aa" * 32, "model": sel._HIDDEN_MODEL, "endpoint": "p2p", "reputation": 0.8, "domain_score": 0.8},
        {"node_id": "bb" * 32, "model": sel._HIDDEN_MODEL, "endpoint": "p2p", "reputation": 0.6, "domain_score": 0.6},
    ]
    with patch.object(sel, "list_online_trusted_peers", return_value=peers), \
         patch.object(sel, "_reputation_register_node"), \
         patch.object(sel, "get_top_reputation", return_value=reputation_rows):
        result = sel.select_nodes(risk=None, limit=5)

    ids = {n.node_id for n in result.nodes}
    check("two distinct real peers remain two distinct node_id records", ids == {"aa" * 32, "bb" * 32})


def main() -> int:
    test_no_peers_gives_honest_local_fallback_not_a_fake_url()
    test_real_online_peer_is_selected_with_p2p_sentinel_and_hidden_model()
    test_offline_peer_with_old_reputation_is_not_selected()
    test_untrusted_peer_never_enters_pool()
    test_two_real_peers_remain_distinct_records()

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
