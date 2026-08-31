"""
Regression tests for the minimal acquisition DAG slice.

Run:
  /home/iam/venv/bin/python3 -m pytest agent/acquisition_parallel_regression_test.py
"""
from __future__ import annotations

import time
import tempfile
import os
from pathlib import Path

import agent.acquisition as acquisition
import agent.external_ai_acquisition as external_ai
from agent.acquisition import (
    AcquisitionCollector,
    AcquisitionRequest,
    AcquisitionStatus,
    AcquisitionTask,
    apply_late_observation_policy,
    count_distinguishable_reported_roots,
    make_observation,
    network_node_stub,
    persist_acquisition_observation,
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


def test_initial_acquisition_dag_wall_clock_tracks_slowest_channel():
    collector = AcquisitionCollector(max_workers=5)
    started = time.time()
    observations = collector.collect([
        _sleeping_task("local", 0.70, "local"),
        _sleeping_task("web_main", 0.80, "web"),
        _sleeping_task("external_ai", 0.90, "ai"),
        _sleeping_task("memory", 0.40, "registry"),
        AcquisitionTask(
            AcquisitionRequest(channel="network_node", prompt="q", provider="stub"),
            network_node_stub,
        ),
    ])
    elapsed = time.time() - started

    assert len(observations) == 5
    assert elapsed < 1.25, f"elapsed={elapsed:.3f}s indicates serial DAG acquisition"


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
    for provider in ("gpt", "claude", "deepseek", "kimi"):
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


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeRawSession:
    def __init__(self, statuses=None, delay=0.0):
        self.statuses = statuses or {}
        self.delay = delay

    def post(self, _url, json, timeout):
        request_id = json["request_id"] or "req"
        providers = {}
        for provider in json["providers"]:
            providers[provider] = {
                "request_id": request_id,
                "task_id": f"{request_id}:{provider}:fake",
                "provider": provider,
                "status": "READY",
                "started_at": time.time(),
            }
        return _FakeResponse({
            "ok": True,
            "request_id": request_id,
            "providers": providers,
            "transport": "council_bridge_extension",
        })

    def get(self, _url, params, timeout):
        if self.delay:
            time.sleep(self.delay)
        provider = params["provider"]
        status = self.statuses.get(provider, "COMPLETED")
        raw = f"raw answer from {provider}" if status == "COMPLETED" else ""
        return _FakeResponse({
            "ok": status == "COMPLETED",
            "request_id": params["request_id"],
            "task_id": params["task_id"],
            "provider": provider,
            "status": status,
            "started_at": time.time() - self.delay,
            "finished_at": time.time(),
            "raw_response": raw,
            "reported_links": [],
            "transport_metadata": {"provider": provider},
            "errors": [] if status == "COMPLETED" else [status.lower()],
        })


def test_external_ai_provider_waits_are_parallel_not_serial():
    old_session = external_ai._session
    external_ai._session = lambda: _FakeRawSession(delay=1.0)
    try:
        started = time.time()
        observations, submit = external_ai.acquire_external_ai_parallel(
            "q",
            ["gpt", "claude", "deepseek", "kimi"],
            request_id="parallel_fake",
            provider_timeout_s=5.0,
            overall_deadline_s=5.0,
            pet_url="http://fake",
        )
        elapsed = time.time() - started
    finally:
        external_ai._session = old_session

    assert submit["request_id"] == "parallel_fake"
    assert len(observations) == 4
    assert elapsed < 2.2, f"elapsed={elapsed:.3f}s indicates serial provider waits"
    assert {obs.status for obs in observations} == {AcquisitionStatus.COMPLETED}


def test_external_ai_initial_submit_happens_once_per_provider():
    calls = {"post": [], "get": []}

    class CountingSession(_FakeRawSession):
        def post(self, url, json, timeout):
            calls["post"].append((url, json))
            return super().post(url, json, timeout)

        def get(self, url, params, timeout):
            calls["get"].append((url, params))
            return super().get(url, params, timeout)

    old_session = external_ai._session
    external_ai._session = lambda: CountingSession()
    try:
        observations, submit = external_ai.acquire_external_ai_parallel(
            "q",
            ["gpt", "claude", "deepseek", "kimi"],
            request_id="one_submit",
            provider_timeout_s=5.0,
            overall_deadline_s=5.0,
            pet_url="http://fake",
        )
        consumed = list(observations)
    finally:
        external_ai._session = old_session

    assert len(calls["post"]) == 1
    assert len(calls["post"][0][1]["prompt_identity"]) == 64
    assert calls["post"][0][1]["request_marker"] == "YANDI_REQUEST_ID: one_submit"
    task_ids = [info["task_id"] for info in submit["providers"].values()]
    assert len(task_ids) == len(set(task_ids))
    assert {o.provider for o in consumed} == {"gpt", "claude", "deepseek", "kimi"}
    assert len(calls["post"]) == 1, "consuming observations must not resubmit"


def test_external_ai_mixed_provider_statuses_do_not_drop_completed_result():
    statuses = {
        "gpt": "COMPLETED",
        "claude": "TIMEOUT",
        "deepseek": "AUTH_REQUIRED",
        "kimi": "ERROR",
    }
    old_session = external_ai._session
    external_ai._session = lambda: _FakeRawSession(statuses=statuses)
    try:
        observations, _submit = external_ai.acquire_external_ai_parallel(
            "q",
            ["gpt", "claude", "deepseek", "kimi"],
            request_id="mixed_fake",
            provider_timeout_s=5.0,
            overall_deadline_s=5.0,
            pet_url="http://fake",
        )
    finally:
        external_ai._session = old_session

    by_provider = {obs.provider: obs for obs in observations}
    assert by_provider["gpt"].status == AcquisitionStatus.COMPLETED
    assert by_provider["gpt"].raw_response == "raw answer from gpt"
    assert by_provider["claude"].status == AcquisitionStatus.TIMEOUT
    assert by_provider["deepseek"].status == AcquisitionStatus.AUTH_REQUIRED
    assert by_provider["kimi"].status == AcquisitionStatus.ERROR


def test_pet_raw_result_api_uses_strict_task_and_provider_correlation():
    src = (BASE / "pet" / "council_chat_server.py").read_text(encoding="utf-8")

    assert 'f"{RAW_RESULT_PFX}{task_id}:{provider}"' in src
    assert "result.get(\"request_id\") == request_id" in src
    assert "result.get(\"provider\") == provider" in src
    assert "result.get(\"task_id\") == task_id" in src
    assert '"prompt_identity": raw_task.get("prompt_identity", "")' in src


def test_legacy_orch_poll_claims_tasks_instead_of_replaying_stale_head():
    src = (BASE / "pet" / "council_chat_server.py").read_text(encoding="utf-8")

    assert 'raw = await r.lpop("orch:ai:queue")' in src
    assert 'lrange("orch:ai:queue"' not in src
    assert "ORCH_AI_TASK_MAX_AGE" in src
    assert "continue" in src[src.index("async def orch_ai_poll"):]


def test_writeback_does_not_enqueue_second_browser_ai_validation():
    src = (BASE / "agent" / "orchestrator" / "response" / "writeback.py").read_text(encoding="utf-8")

    assert "send_to_deepseek(" not in src
    assert "legacy_deepseek_validation_skipped" in src


def test_raw_acquisition_result_is_not_written_to_council_history_or_relay():
    src = (BASE / "pet" / "council_chat_server.py").read_text(encoding="utf-8")
    raw_branch = src[
        src.index('raw_task_raw = await r.get(f"{RAW_TASK_PFX}{task_id}")'):
        src.index("text = await _localize(text, from_who)"):
    ]

    assert 'return {"ok": True, "raw_acquisition": True' in raw_branch
    assert "await r.lpush(MESSAGES_KEY" not in raw_branch
    assert "_relay_ctx" not in raw_branch
    assert "await _localize" not in raw_branch


def test_existing_raw_acquisition_messages_are_hidden_from_council_history():
    import pet.council_chat_server as pet_server

    messages = [
        {"from": "human", "text": "normal question", "task_id": "uuid-like"},
        {"from": "gpt", "text": "raw transport artifact", "task_id": "trace_x:gpt:deadbeef"},
        {"from": "deepseek", "text": "normal council answer", "task_id": "uuid-like-2"},
    ]

    visible = pet_server._visible_council_messages(messages)

    assert [m["text"] for m in visible] == ["normal question", "normal council answer"]
    assert pet_server._is_raw_acquisition_task_id("trace_x:deepseek:0123abcd")
    assert not pet_server._is_raw_acquisition_task_id("normal-council-uuid")


def test_extension_raw_acquisition_uses_isolated_provider_tab():
    src = (BASE / "pet" / "extension" / "background.js").read_text(encoding="utf-8")
    handle = src[src.index("async function handleTask"):src.index("async function postCouncilResult")]

    assert "rawAcquisition = task && task.raw_acquisition === true" in handle
    assert "rawAcquisition ? await openIsolatedTab(model) : await findTab(model)" in handle
    assert "raw_acquisition: rawAcquisition" in handle


def test_external_ai_prompt_requests_raw_provenance_not_yandi_verdict():
    prompt = external_ai.build_external_ai_prompt("q", request_id="req_marker")
    low = prompt.lower()

    assert "YANDI_REQUEST_ID: req_marker" in prompt
    assert "первой строке ответа" in low
    assert "собственный анализ" in low
    assert "источники" in low
    assert "url" in low
    assert "не выдумывай ссылки" in low
    assert "не можешь его проверить" in low
    assert "trust" in low
    assert "verdict" in low
    assert "не делай вывод" in low


def test_pet_rejects_raw_provider_response_without_request_marker():
    import pet.council_chat_server as pet_server

    status, errors = pet_server._raw_status_from_text(
        "ANSWER:\nold unrelated provider response",
        "YANDI_REQUEST_ID: current_run",
    )

    assert status == "ERROR"
    assert "missing request marker" in errors[0]


def test_late_external_ai_observation_is_persisted_without_answer_mutation():
    old_dir = acquisition.ACQUISITION_EVENTS_DIR
    old_env = os.environ.get("YANDI_ACQUISITION_DEBUG_JSONL")
    acquisition.ACQUISITION_EVENTS_DIR = Path(tempfile.mkdtemp())
    os.environ["YANDI_ACQUISITION_DEBUG_JSONL"] = "1"
    try:
        req = AcquisitionRequest(
            channel="external_ai",
            prompt="q",
            provider="claude",
            request_id="late_req",
        )
        obs = make_observation(
            req,
            AcquisitionStatus.COMPLETED,
            started_at=10.0,
            finished_at=30.0,
            raw_response="late answer",
        )
        path = persist_acquisition_observation(
            obs,
            run_id="run_late",
            answer_finalized_at=20.0,
        )
        row = path.read_text(encoding="utf-8")
    finally:
        acquisition.ACQUISITION_EVENTS_DIR = old_dir
        if old_env is None:
            os.environ.pop("YANDI_ACQUISITION_DEBUG_JSONL", None)
        else:
            os.environ["YANDI_ACQUISITION_DEBUG_JSONL"] = old_env

    assert '"status": "LATE"' in row
    assert '"finalized_answer_mutated": false' in row
    assert '"run_id": "run_late"' in row


def test_acquisition_persistence_defaults_to_sql_not_operational_jsonl():
    old_dir = acquisition.ACQUISITION_EVENTS_DIR
    old_env = os.environ.pop("YANDI_ACQUISITION_DEBUG_JSONL", None)
    acquisition.ACQUISITION_EVENTS_DIR = Path(tempfile.mkdtemp())
    try:
        req = AcquisitionRequest(channel="external_ai", prompt="q", provider="gpt", request_id="sql_default")
        obs = make_observation(req, AcquisitionStatus.COMPLETED, started_at=time.time(), raw_response="raw")
        path = persist_acquisition_observation(obs, run_id="")
    finally:
        acquisition.ACQUISITION_EVENTS_DIR = old_dir
        if old_env is not None:
            os.environ["YANDI_ACQUISITION_DEBUG_JSONL"] = old_env

    assert path is None


def test_pipeline_waits_for_external_ai_after_web_scrape_not_before():
    src = (BASE / "agent" / "orchestrator" / "pipeline.py").read_text(encoding="utf-8")

    assert src.index("main_scrape_future = parallel_executor.submit") < src.index("external_ai_future.result")
    assert src.count("acquire_external_ai_parallel") == 2  # import + one submit call


def test_pipeline_initial_acquisition_call_sites_are_singletons():
    src = (BASE / "agent" / "orchestrator" / "pipeline.py").read_text(encoding="utf-8")
    body = src[src.index("def run_standard_pipeline("):]

    assert body.count("search_registry,") == 1
    assert body.count("generate_local_answer,") == 1
    assert body.count("scrape_budgeted_side,") == 2  # main + counter
    assert body.count("formulate_queries,") == 1
    assert body.count("formulate_refutation_queries,") == 1


def test_no_stale_cross_run_result_is_accepted_by_raw_provider_client():
    class WrongResultSession(_FakeRawSession):
        def get(self, _url, params, timeout):
            return _FakeResponse({
                "ok": True,
                "request_id": "run_a",
                "task_id": "run_a:gpt:old",
                "provider": "gpt",
                "status": "COMPLETED",
                "raw_response": "old answer",
                "reported_links": [],
                "transport_metadata": {},
                "errors": [],
            })

    old_session = external_ai._session
    external_ai._session = lambda: WrongResultSession()
    try:
        obs = external_ai.wait_raw_provider_result(
            "run_b",
            "gpt",
            task_id="run_b:gpt:new",
            timeout_s=0.05,
            poll_s=0.01,
            pet_url="http://fake",
        )
    finally:
        external_ai._session = old_session

    assert obs.status == AcquisitionStatus.TIMEOUT
    assert obs.raw_response == ""


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
    assert '@app.post("/api/acquisition/raw/submit")' in src
    assert '@app.get("/api/acquisition/raw/result")' in src


if __name__ == "__main__":
    tests = [
        test_parallel_collector_wall_clock_tracks_max_branch_not_sum,
        test_initial_acquisition_dag_wall_clock_tracks_slowest_channel,
        test_failed_ai_provider_and_auth_required_do_not_fail_other_channels,
        test_late_observation_cannot_mutate_finalized_answer,
        test_raw_external_ai_result_does_not_become_canonical_trust,
        test_three_models_reporting_same_url_count_as_one_reported_root,
        test_external_ai_provider_waits_are_parallel_not_serial,
        test_external_ai_initial_submit_happens_once_per_provider,
        test_external_ai_mixed_provider_statuses_do_not_drop_completed_result,
        test_pet_raw_result_api_uses_strict_task_and_provider_correlation,
        test_legacy_orch_poll_claims_tasks_instead_of_replaying_stale_head,
        test_writeback_does_not_enqueue_second_browser_ai_validation,
        test_raw_acquisition_result_is_not_written_to_council_history_or_relay,
        test_existing_raw_acquisition_messages_are_hidden_from_council_history,
        test_extension_raw_acquisition_uses_isolated_provider_tab,
        test_external_ai_prompt_requests_raw_provenance_not_yandi_verdict,
        test_pet_rejects_raw_provider_response_without_request_marker,
        test_late_external_ai_observation_is_persisted_without_answer_mutation,
        test_acquisition_persistence_defaults_to_sql_not_operational_jsonl,
        test_pipeline_waits_for_external_ai_after_web_scrape_not_before,
        test_pipeline_initial_acquisition_call_sites_are_singletons,
        test_no_stale_cross_run_result_is_accepted_by_raw_provider_client,
        test_main_and_counter_channels_remain_separate_observations,
        test_local_blind_answer_starts_before_external_result_is_available,
        test_network_node_stub_integrates_without_blocking,
        test_pipeline_no_longer_discards_prefetched_web_queries,
        test_pet_broadcast_is_model_scoped_and_result_messages_keep_task_id,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
