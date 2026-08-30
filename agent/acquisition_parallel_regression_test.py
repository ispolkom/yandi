"""
Regression tests for the minimal acquisition DAG slice.

Run:
  /home/iam/venv/bin/python3 -m pytest agent/acquisition_parallel_regression_test.py
"""
from __future__ import annotations

import time
from pathlib import Path

from agent.acquisition import (
    AcquisitionCollector,
    AcquisitionRequest,
    AcquisitionStatus,
    AcquisitionTask,
    apply_late_observation_policy,
    count_distinguishable_reported_roots,
    make_observation,
    network_node_stub,
)


BASE = Path(__file__).resolve().parent.parent


def _sleeping_task(channel: str, delay: float, value: str) -> AcquisitionTask:
    req = AcquisitionRequest(channel=channel, prompt="q", provider=channel)

    def run(_req):
        time.sleep(delay)
        return value

    return AcquisitionTask(req, run)


def test_parallel_collector_wall_clock_tracks_max_branch_not_sum():
    collector = AcquisitionCollector(max_workers=4)
    started = time.time()
    observations = collector.collect([
        _sleeping_task("memory", 0.18, "m"),
        _sleeping_task("web_main", 0.24, "w"),
        _sleeping_task("external_ai", 0.12, "a"),
    ])
    elapsed = time.time() - started

    assert len(observations) == 3
    assert elapsed < 0.38, f"elapsed={elapsed:.3f}s indicates serial execution"
    assert {o.status for o in observations} == {AcquisitionStatus.COMPLETED}


def test_failed_ai_provider_and_auth_required_do_not_fail_other_channels():
    ok_req = AcquisitionRequest(channel="external_ai", prompt="q", provider="gpt")
    bad_req = AcquisitionRequest(channel="external_ai", prompt="q", provider="claude")
    auth_req = AcquisitionRequest(channel="external_ai", prompt="q", provider="deepseek")

    def ok(_req):
        return "raw answer"

    def bad(_req):
        raise RuntimeError("provider exploded")

    def auth(_req):
        return {"status": "AUTH_REQUIRED", "raw_response": "", "errors": ["login required"]}

    observations = AcquisitionCollector(max_workers=3).collect([
        AcquisitionTask(ok_req, ok),
        AcquisitionTask(bad_req, bad),
        AcquisitionTask(auth_req, auth),
    ])

    by_provider = {o.provider: o for o in observations}
    assert by_provider["gpt"].status == AcquisitionStatus.COMPLETED
    assert by_provider["claude"].status == AcquisitionStatus.ERROR
    assert by_provider["deepseek"].status == AcquisitionStatus.AUTH_REQUIRED


def test_late_observation_cannot_mutate_finalized_answer():
    req = AcquisitionRequest(channel="external_ai", prompt="q", provider="kimi")
    obs = make_observation(
        req,
        AcquisitionStatus.COMPLETED,
        started_at=10.0,
        finished_at=30.0,
        raw_response="late raw response",
    )

    marked = apply_late_observation_policy(obs, answer_finalized_at=20.0)

    assert marked.status == AcquisitionStatus.LATE
    assert marked.finalized_answer_mutated is False
    assert marked.raw_response == "late raw response"


def test_raw_external_ai_result_does_not_become_canonical_trust():
    req = AcquisitionRequest(channel="external_ai", prompt="q", provider="deepseek")
    obs = make_observation(
        req,
        AcquisitionStatus.COMPLETED,
        started_at=time.time(),
        raw_payload={"verdict": "VERIFIED", "trust": "VERIFIED"},
        raw_response="DeepSeek says VERIFIED",
    )

    assert not hasattr(obs, "trust_level")
    assert not hasattr(obs, "canonical_trust")
    assert obs.raw_payload["verdict"] == "VERIFIED"


def test_three_models_reporting_same_url_count_as_one_reported_root():
    observations = []
    for provider in ("gpt", "claude", "deepseek"):
        req = AcquisitionRequest(channel="external_ai", prompt="q", provider=provider)
        observations.append(
            make_observation(
                req,
                AcquisitionStatus.COMPLETED,
                started_at=time.time(),
                raw_response=f"{provider} reports NASA",
                raw_citations_or_links=["https://www.nasa.gov/kennedy/"],
            )
        )

    assert count_distinguishable_reported_roots(observations) == 1


def test_main_and_counter_channels_remain_separate_observations():
    main = AcquisitionRequest(channel="web_main", prompt="q", provider="web")
    counter = AcquisitionRequest(channel="web_counter", prompt="q", provider="web")

    observations = [
        make_observation(main, AcquisitionStatus.COMPLETED, time.time(), raw_response="main"),
        make_observation(counter, AcquisitionStatus.COMPLETED, time.time(), raw_response="counter"),
    ]

    assert {o.channel for o in observations} == {"web_main", "web_counter"}
    assert observations[0].request_id != observations[1].request_id


def test_local_blind_answer_starts_before_external_result_is_available():
    collector = AcquisitionCollector(max_workers=2)
    observations = collector.collect([
        _sleeping_task("local", 0.05, "local hypothesis"),
        _sleeping_task("external_ai", 0.20, "external raw"),
    ])

    local = next(o for o in observations if o.channel == "local")
    external = next(o for o in observations if o.channel == "external_ai")

    assert local.started_at <= external.started_at + 0.03
    assert local.started_at < external.finished_at


def test_network_node_stub_integrates_without_blocking():
    req = AcquisitionRequest(channel="network_node", prompt="q", provider="stub")
    started = time.time()
    obs = AcquisitionCollector(max_workers=1).collect([
        AcquisitionTask(req, network_node_stub),
    ])[0]

    assert time.time() - started < 0.05
    assert obs.status == AcquisitionStatus.DISABLED
    assert "not implemented" in obs.transport_metadata["reason"]


def test_pipeline_no_longer_discards_prefetched_web_queries():
    src = (BASE / "agent" / "orchestrator" / "pipeline.py").read_text(encoding="utf-8")
    web_decision = src[src.index("# \u2500\u2500 [7] Epistemic-based web search decision"):]

    assert "wq_result = None" not in web_decision
    assert "lambda: formulate_queries(enrich_result)" not in web_decision
    assert "_prefetched_refutation_result" in web_decision


def test_pet_broadcast_is_model_scoped_and_result_messages_keep_task_id():
    src = (BASE / "pet" / "council_chat_server.py").read_text(encoding="utf-8")

    assert '"task_id":   task_id' in src
    assert 'requested_models = payload.get("models")' in src
    assert "active = [m for m in active if m in requested]" in src


if __name__ == "__main__":
    tests = [
        test_parallel_collector_wall_clock_tracks_max_branch_not_sum,
        test_failed_ai_provider_and_auth_required_do_not_fail_other_channels,
        test_late_observation_cannot_mutate_finalized_answer,
        test_raw_external_ai_result_does_not_become_canonical_trust,
        test_three_models_reporting_same_url_count_as_one_reported_root,
        test_main_and_counter_channels_remain_separate_observations,
        test_local_blind_answer_starts_before_external_result_is_available,
        test_network_node_stub_integrates_without_blocking,
        test_pipeline_no_longer_discards_prefetched_web_queries,
        test_pet_broadcast_is_model_scoped_and_result_messages_keep_task_id,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
