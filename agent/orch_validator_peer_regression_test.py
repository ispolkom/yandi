"""
agent/orch_validator_peer_regression_test.py

Мандат "Real Node Directory Integration" — проверяет, что
orch_validator.py::_validate_on_node() правильно диспетчерует
endpoint=="p2p" на РЕАЛЬНОГО пира (через orch_peer_directory), и что
отказ пира честно резолвится в verdict="partial", а не тихо
переключается на локальный backend или падает наружу.

Запуск: python3 -m agent.orch_validator_peer_regression_test
"""
from __future__ import annotations

import sys
from unittest.mock import patch

from agent import orch_validator as v
from agent.orch_peer_directory import PeerDirectoryError

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def test_p2p_endpoint_dispatches_to_real_peer_path():
    with patch.object(v, "infer_on_peer", return_value='{"verdict":"agree","reason":"looks right"}') as mock_infer, \
         patch.object(v, "update_node"):
        result = v._validate_on_node("aa" * 32, model="irrelevant", endpoint="p2p",
                                      question="Q?", answer="A.", domain="general")

    check("endpoint='p2p' routes through infer_on_peer, not llm_complete", mock_infer.called)
    check("the node_id passed to infer_on_peer is the canonical node_id, unchanged", mock_infer.call_args[0][0] == "aa" * 32)
    check("verdict is parsed correctly from the peer's JSON reply", result.verdict == "agree")
    _, kwargs = mock_infer.call_args
    check("no model/backend field is ever passed to infer_on_peer", "model" not in kwargs and "backend" not in kwargs)


def test_peer_failure_resolves_to_honest_partial_not_silent_local_fallback():
    with patch.object(v, "infer_on_peer", side_effect=PeerDirectoryError("peer unreachable")), \
         patch.object(v, "update_node") as mock_update, \
         patch.object(v, "llm_complete") as mock_local:
        result = v._validate_on_node("bb" * 32, model="irrelevant", endpoint="p2p",
                                      question="Q?", answer="A.", domain="general")

    check("a failed peer resolves to verdict=partial, not an exception", result.verdict == "partial")
    check("the failure is never silently answered by the local backend instead", not mock_local.called)
    check("reputation is still updated (as a failure) for this node_id", mock_update.called and mock_update.call_args[0][0] == "bb" * 32)
    check("reputation update marks it as NOT correct", mock_update.call_args.kwargs.get("correct") is False)


def test_local_endpoint_still_uses_llm_gateway_not_peer_path():
    with patch.object(v, "llm_complete", return_value='{"verdict":"partial","reason":"local self-check"}') as mock_local, \
         patch.object(v, "infer_on_peer") as mock_infer, \
         patch.object(v, "update_node"):
        v._validate_on_node("yandi-council", model="heretic:q8", endpoint="local",
                             question="Q?", answer="A.", domain="general")

    check("endpoint='local' never calls infer_on_peer", not mock_infer.called)
    check("endpoint='local' still uses the local llm_gateway path", mock_local.called)


def main() -> int:
    test_p2p_endpoint_dispatches_to_real_peer_path()
    test_peer_failure_resolves_to_honest_partial_not_silent_local_fallback()
    test_local_endpoint_still_uses_llm_gateway_not_peer_path()

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
