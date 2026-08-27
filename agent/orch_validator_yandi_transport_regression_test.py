"""
agent/orch_validator_yandi_transport_regression_test.py — Foundation
Repair / PET_AGENT_BOUNDARY_AUDIT.md Phase 4A regression:
orch_validator.py::_validate_on_yandi_node() against the new pure-transport
contract of pet/council_chat_server.py::/api/yandi/validate.

Before this fix: the endpoint itself parsed agree/disagree/partial and
returned a computed "verdict" - epistemic interpretation living in pet/.
After: the endpoint returns raw_text + transport_status only
(unavailable/timeout/completed/error); _validate_on_yandi_node() is the
only place that calls _parse_free_text_verdict() to turn raw text into a
verdict.

Mocks agent.orch_validator._session.post (module-level requests.Session)
so no live server or network call is needed; a separate live HTTP round
trip against a real running server is done manually as part of this
phase's live verification (see the Foundation Repair report), not here.

Run: /home/iam/venv/bin/python3 -m agent.orch_validator_yandi_transport_regression_test
"""
from __future__ import annotations

import agent.orch_validator as ov

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


class _FakeResponse:
    def __init__(self, json_data: dict, status: int = 200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _FakePost:
    """Stand-in for agent.orch_validator._session.post, returns a fixed payload."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    def __call__(self, endpoint, json=None, timeout=None):
        self.calls.append({"endpoint": endpoint, "json": json, "timeout": timeout})
        return _FakeResponse(self.payload)


_orig_post = ov._session.post


def _run_case(payload: dict):
    fake = _FakePost(payload)
    ov._session.post = fake
    try:
        return ov._validate_on_yandi_node(
            node_id="yandi-council",
            endpoint="http://127.0.0.1:9010/api/yandi/validate",
            question="Что такое DHT?",
            answer="DHT - распределённая хэш-таблица.",
            domain="tech",
        ), fake
    finally:
        ov._session.post = _orig_post


# ── transport_status == "unavailable" -> partial, never disagree ──
result, fake = _run_case({
    "ok": False, "transport_status": "unavailable",
    "transport_error": "no active browser models", "raw_text": "",
})
check(
    "unavailable transport_status resolves to verdict=partial (not disagree)",
    result.verdict == "partial",
    f"verdict={result.verdict!r}",
)
check(
    "unavailable reason text distinguishes it from a real disagreement",
    "нет активных браузерных моделей" in result.explanation,
    f"reason={result.explanation!r}",
)

# ── transport_status == "timeout" -> partial, distinct reason ──
result, fake = _run_case({
    "ok": False, "transport_status": "timeout",
    "transport_error": "no relay response within 60s", "raw_text": "",
})
check(
    "timeout transport_status resolves to verdict=partial (not disagree)",
    result.verdict == "partial",
    f"verdict={result.verdict!r}",
)
check(
    "timeout reason text is distinct from the unavailable reason text",
    "нет ответа за отведённое время" in result.explanation
    and "нет активных браузерных моделей" not in result.explanation,
    f"reason={result.explanation!r}",
)

# ── transport_status == "completed" with real agree JSON -> parsed correctly ──
result, fake = _run_case({
    "ok": True, "transport_status": "completed", "provider": "claude",
    "raw_text": '{"verdict": "agree", "reason": "Ответ верный и полный."}',
})
check(
    "a completed transport with a real JSON verdict is parsed by "
    "_parse_free_text_verdict, not pre-parsed by pet",
    result.verdict == "agree" and "верный" in result.explanation,
    f"verdict={result.verdict!r} reason={result.explanation!r}",
)

# ── transport_status == "completed" with free-text (non-JSON) -> keyword fallback ──
result, fake = _run_case({
    "ok": True, "transport_status": "completed", "provider": "gpt",
    # Deliberately avoids the substring "верн" (present inside "неверный")
    # which _parse_free_text_verdict's own keyword list also scores as an
    # agree-hit - not this fix's concern, just picking an unambiguous
    # disagree example for this test.
    "raw_text": "Ответ содержит грубую ошибку и вводит пользователя в заблуждение.",
})
check(
    "a completed transport with free (non-JSON) text still resolves via "
    "the keyword-based fallback in _parse_free_text_verdict",
    result.verdict == "disagree",
    f"verdict={result.verdict!r} reason={result.explanation!r}",
)

# ── ok=True but empty raw_text -> treated as no answer, not a silent crash ──
result, fake = _run_case({
    "ok": True, "transport_status": "completed", "raw_text": "",
})
check(
    "empty raw_text on a nominally-ok response does not crash and "
    "resolves to partial",
    result.verdict == "partial",
    f"verdict={result.verdict!r}",
)

# ── network/transport exception -> partial, exception message preserved ──
def _raising_post(endpoint, json=None, timeout=None):
    raise ConnectionError("connection refused")


ov._session.post = _raising_post
try:
    result = ov._validate_on_yandi_node(
        node_id="yandi-council", endpoint="http://127.0.0.1:9010/api/yandi/validate",
        question="q", answer="a", domain="tech",
    )
finally:
    ov._session.post = _orig_post
check(
    "a raised transport exception (e.g. connection refused) still returns "
    "a NodeValidation with verdict=partial instead of propagating",
    result.verdict == "partial" and "connection refused" in result.explanation,
    f"verdict={result.verdict!r} reason={result.explanation!r}",
)

# ── the endpoint receives question/answer only, no fabricated verdict field ──
result, fake = _run_case({
    "ok": True, "transport_status": "completed", "raw_text": '{"verdict":"agree","reason":"ok"}',
})
check(
    "the POST payload sent to /api/yandi/validate carries only "
    "question/answer, confirming this function does not ask pet to also "
    "compute anything",
    set(fake.calls[0]["json"].keys()) == {"question", "answer"},
    f"payload_keys={list(fake.calls[0]['json'].keys())}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
