"""
agent/external_ai_acquisition.py - browser-auth external AI transport adapter.

Uses the existing PET Council Bridge as transport. Returned values are raw
AcquisitionObservation objects; no verdict/Trust/consensus is computed here.
"""
from __future__ import annotations

import concurrent.futures
import time
from typing import Dict, Iterable, List, Optional

import requests

from agent.acquisition import (
    AcquisitionObservation,
    AcquisitionStatus,
    make_observation,
    AcquisitionRequest,
)

PET_URL = "http://127.0.0.1:9010"
DEFAULT_PROVIDERS = ["gpt", "deepseek", "claude", "kimi"]


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def submit_raw_provider_request(
    prompt: str,
    providers: Optional[Iterable[str]] = None,
    *,
    request_id: str = "",
    timeout_s: float = 90.0,
    pet_url: str = PET_URL,
) -> Dict:
    s = _session()
    r = s.post(
        f"{pet_url}/api/acquisition/raw/submit",
        json={
            "prompt": prompt,
            "providers": list(providers or DEFAULT_PROVIDERS),
            "request_id": request_id,
            "timeout_s": timeout_s,
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def wait_raw_provider_result(
    request_id: str,
    provider: str,
    *,
    task_id: str = "",
    timeout_s: float = 90.0,
    poll_s: float = 1.0,
    pet_url: str = PET_URL,
) -> AcquisitionObservation:
    req = AcquisitionRequest(
        channel="external_ai",
        prompt="",
        provider=provider,
        request_id=request_id,
        timeout_s=timeout_s,
    )
    started = time.time()
    s = _session()
    deadline = started + timeout_s

    while time.time() < deadline:
        r = s.get(
            f"{pet_url}/api/acquisition/raw/result",
            params={
                "request_id": request_id,
                "provider": provider,
                "task_id": task_id,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("status", "")
        if status in {
            "COMPLETED",
            "ERROR",
            "AUTH_REQUIRED",
            "TAB_MISSING",
            "DISABLED",
            "TIMEOUT",
        }:
            return make_observation(
                req,
                AcquisitionStatus(status),
                started_at=float(data.get("started_at") or started),
                finished_at=float(data.get("finished_at") or time.time()),
                raw_payload=data,
                raw_response=data.get("raw_response") or "",
                raw_citations_or_links=data.get("reported_links") or [],
                transport_metadata=data.get("transport_metadata") or {},
                errors=data.get("errors") or [],
            )
        time.sleep(poll_s)

    return make_observation(
        req,
        AcquisitionStatus.TIMEOUT,
        started_at=started,
        errors=[f"provider {provider} did not complete within {timeout_s:.1f}s"],
        transport_metadata={"task_id": task_id, "pet_url": pet_url},
    )


def acquire_external_ai_parallel(
    prompt: str,
    providers: Optional[Iterable[str]] = None,
    *,
    request_id: str = "",
    provider_timeout_s: float = 90.0,
    overall_deadline_s: Optional[float] = None,
    pet_url: str = PET_URL,
) -> tuple[List[AcquisitionObservation], Dict]:
    """
    Submit once, then wait for each provider concurrently.

    A failed/hung provider returns its own observation and cannot block
    already-completed providers. The caller decides whether to finalize
    without late results.
    """
    submit = submit_raw_provider_request(
        prompt,
        providers,
        request_id=request_id,
        timeout_s=provider_timeout_s,
        pet_url=pet_url,
    )
    rid = submit["request_id"]
    tasks = submit.get("providers", {})
    observations: List[AcquisitionObservation] = []
    started = time.time()
    timeout = overall_deadline_s if overall_deadline_s is not None else provider_timeout_s

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(tasks)))
    try:
        future_map = {
            ex.submit(
                wait_raw_provider_result,
                rid,
                provider,
                task_id=info.get("task_id", ""),
                timeout_s=min(provider_timeout_s, timeout),
                pet_url=pet_url,
            ): provider
            for provider, info in tasks.items()
        }
        done, not_done = concurrent.futures.wait(
            future_map,
            timeout=timeout,
            return_when=concurrent.futures.ALL_COMPLETED,
        )
        for future in done:
            observations.append(future.result())
        for future in not_done:
            provider = future_map[future]
            observations.append(make_observation(
                AcquisitionRequest(
                    channel="external_ai",
                    prompt="",
                    provider=provider,
                    request_id=rid,
                    timeout_s=provider_timeout_s,
                ),
                AcquisitionStatus.TIMEOUT,
                started_at=started,
                errors=[f"overall acquisition deadline {timeout:.1f}s reached"],
                transport_metadata={
                    "task_id": tasks.get(provider, {}).get("task_id", ""),
                    "pet_url": pet_url,
                },
            ))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    return observations, submit
