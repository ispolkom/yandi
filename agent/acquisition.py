"""
agent/acquisition.py - minimal raw acquisition collector.

This module is transport/collection only. It does not compute Trust,
truth, support/contradiction, consensus, or canonical answers.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional

BASE = Path(__file__).resolve().parent.parent
ACQUISITION_EVENTS_DIR = BASE / "registry" / "dataset" / "acquisition_observations"


class AcquisitionStatus(str, Enum):
    DISABLED = "DISABLED"
    TAB_MISSING = "TAB_MISSING"
    STARTING = "STARTING"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    READY = "READY"
    BUSY = "BUSY"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    COMPLETED = "COMPLETED"
    LATE = "LATE"


@dataclass
class AcquisitionRequest:
    channel: str
    prompt: str
    provider: str = ""
    request_id: str = field(default_factory=lambda: f"acq_{uuid.uuid4().hex[:12]}")
    enabled_by_user: bool = True
    required_before_finalization: bool = True
    timeout_s: float = 30.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AcquisitionObservation:
    request_id: str
    channel: str
    provider: str
    status: AcquisitionStatus
    started_at: float
    finished_at: float
    raw_payload: Any = None
    raw_response: str = ""
    raw_citations_or_links: List[str] = field(default_factory=list)
    transport_metadata: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    finalized_answer_mutated: bool = False


@dataclass
class AcquisitionTask:
    request: AcquisitionRequest
    call: Callable[[AcquisitionRequest], Any]


def make_observation(
    request: AcquisitionRequest,
    status: AcquisitionStatus,
    started_at: float,
    finished_at: Optional[float] = None,
    raw_payload: Any = None,
    raw_response: str = "",
    raw_citations_or_links: Optional[List[str]] = None,
    transport_metadata: Optional[Dict[str, Any]] = None,
    errors: Optional[List[str]] = None,
) -> AcquisitionObservation:
    return AcquisitionObservation(
        request_id=request.request_id,
        channel=request.channel,
        provider=request.provider,
        status=status,
        started_at=started_at,
        finished_at=finished_at if finished_at is not None else time.time(),
        raw_payload=raw_payload,
        raw_response=raw_response,
        raw_citations_or_links=list(raw_citations_or_links or []),
        transport_metadata=dict(transport_metadata or {}),
        errors=list(errors or []),
    )


class AcquisitionCollector:
    """Bounded parallel fan-out collector for raw observations."""

    def __init__(self, max_workers: int = 8, clock: Callable[[], float] = time.time):
        self.max_workers = max_workers
        self._clock = clock
        self.timeline: List[Dict[str, Any]] = []

    def collect(self, tasks: Iterable[AcquisitionTask]) -> List[AcquisitionObservation]:
        tasks = list(tasks)
        if not tasks:
            return []

        observations: List[AcquisitionObservation] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.max_workers, len(tasks))) as executor:
            future_map = {}
            for task in tasks:
                start = self._clock()
                self.timeline.append({
                    "event": "start",
                    "request_id": task.request.request_id,
                    "channel": task.request.channel,
                    "provider": task.request.provider,
                    "ts": start,
                })
                future_map[executor.submit(self._run_one, task, start)] = task

            for future in concurrent.futures.as_completed(future_map):
                obs = future.result()
                self.timeline.append({
                    "event": "finish",
                    "request_id": obs.request_id,
                    "channel": obs.channel,
                    "provider": obs.provider,
                    "status": obs.status.value,
                    "ts": obs.finished_at,
                })
                observations.append(obs)

        return observations

    def _run_one(self, task: AcquisitionTask, started_at: float) -> AcquisitionObservation:
        req = task.request
        if not req.enabled_by_user:
            return make_observation(req, AcquisitionStatus.DISABLED, started_at)
        try:
            payload = task.call(req)
        except TimeoutError as e:
            return make_observation(req, AcquisitionStatus.TIMEOUT, started_at, errors=[str(e)])
        except Exception as e:
            return make_observation(
                req,
                AcquisitionStatus.ERROR,
                started_at,
                errors=[f"{type(e).__name__}: {e}"],
            )

        if isinstance(payload, AcquisitionObservation):
            return payload
        if isinstance(payload, dict) and payload.get("status") in AcquisitionStatus.__members__:
            status = AcquisitionStatus[payload["status"]]
            return make_observation(
                req,
                status,
                started_at,
                raw_payload=payload,
                raw_response=str(payload.get("raw_response") or payload.get("text") or ""),
                raw_citations_or_links=payload.get("raw_citations_or_links") or payload.get("links") or [],
                transport_metadata=payload.get("transport_metadata") or {},
                errors=payload.get("errors") or [],
            )
        return make_observation(
            req,
            AcquisitionStatus.COMPLETED,
            started_at,
            raw_payload=payload,
            raw_response=payload if isinstance(payload, str) else "",
        )


def network_node_stub(request: AcquisitionRequest) -> Dict[str, Any]:
    """Future P2P boundary: integrated, disabled, non-blocking."""
    return {
        "status": "DISABLED",
        "raw_response": "",
        "transport_metadata": {
            "reason": "network nodes are not implemented in this slice",
            "channel": request.channel,
        },
    }


def apply_late_observation_policy(
    observation: AcquisitionObservation,
    answer_finalized_at: Optional[float],
) -> AcquisitionObservation:
    """
    Mark late observations without mutating a finalized answer.

    Persistence/recheck can consume the raw observation later, but this
    function deliberately never changes canonical answer state.
    """
    if answer_finalized_at is not None and observation.finished_at > answer_finalized_at:
        observation.status = AcquisitionStatus.LATE
        observation.finalized_answer_mutated = False
    return observation


def persist_acquisition_observation(
    observation: AcquisitionObservation,
    *,
    run_id: str = "",
    answer_finalized_at: Optional[float] = None,
) -> Optional[Path]:
    """
    Persist a raw acquisition observation for later recheck/reflection.

    Production persistence goes to the existing SQL AI observation tables.
    JSONL is an explicit debug export only, never operational state.
    """
    obs = apply_late_observation_policy(observation, answer_finalized_at)
    try:
        from agent.db.sql.shadow_write import shadow_record_ai_observation
        prompt_identity = (obs.raw_payload or {}).get("prompt_identity")
        if not prompt_identity and obs.request_id:
            prompt_identity = hashlib.sha256(obs.request_id.encode("utf-8")).hexdigest()
        shadow_record_ai_observation(
            run_id=run_id or None,
            provider=obs.provider or obs.channel,
            model_id=obs.provider or "unknown",
            prompt_identity=prompt_identity,
            answer_excerpt=(obs.raw_response or "")[:2000],
            reported_sources=obs.raw_citations_or_links,
            observed_at=obs.finished_at or obs.started_at,
            provenance_mode_reported="UNKNOWN",
            live_search_used_reported="UNKNOWN",
            provenance_parse_status="missing",
        )
    except Exception:
        pass

    if os.environ.get("YANDI_ACQUISITION_DEBUG_JSONL") != "1":
        return None

    ACQUISITION_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ACQUISITION_EVENTS_DIR / f"{time.strftime('%Y%m%d')}.jsonl"
    row = {
        "run_id": run_id,
        "request_id": obs.request_id,
        "channel": obs.channel,
        "provider": obs.provider,
        "status": obs.status.value,
        "started_at": obs.started_at,
        "finished_at": obs.finished_at,
        "raw_response_excerpt": (obs.raw_response or "")[:2000],
        "raw_citations_or_links": obs.raw_citations_or_links,
        "transport_metadata": obs.transport_metadata,
        "errors": obs.errors,
        "answer_finalized_at": answer_finalized_at,
        "finalized_answer_mutated": obs.finalized_answer_mutated,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def count_distinguishable_reported_roots(observations: Iterable[AcquisitionObservation]) -> int:
    """
    Count distinguishable self-reported roots, not model/node votes.

    This is not a proof of verified provenance. It is only a safe
    deduplicated diagnostic over raw reported URLs/names.
    """
    roots = set()
    for obs in observations:
        for item in obs.raw_citations_or_links:
            root = str(item).strip().lower().rstrip("/")
            if root:
                roots.add(root)
    return len(roots)
