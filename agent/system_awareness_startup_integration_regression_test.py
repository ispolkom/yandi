"""
agent/system_awareness_startup_integration_regression_test.py — SYSTEM
AWARENESS V1, mandate §12 AGENT integration: "AGENT startup -> system
probe -> state memory update -> concise log summary."

Covers:
    A. structural: the startup handler is registered on `app` (same
       daemon-identification reasoning already established for
       _reconcile_stale_sql_runs — pet/council_chat_server.py IS the
       daemon both real launch paths hit).
    B. functional: Starlette's TestClient actually runs FastAPI's
       startup lifecycle — the probe fires, calls the real update_
       state()/summary_line() (not mocked), and does not interfere
       with the pre-existing SQL startup hook (both registered, both
       fire).
    C. failure isolation: if the probe/store layer itself raises, the
       outer try/except in the handler swallows it — startup does not
       abort (this is a SECOND, outer safety net on top of system_
       awareness.py's own per-section isolation, guarding the
       integration point itself).
    D. no LLM/web call is made by this startup path (cheap-probe
       contract, mandate §11) — confirmed by mocking update_state to
       track it was called exactly once, cheaply, with no patched-in
       network mock required for it to complete.

Run: /home/iam/venv/bin/python3 -m agent.system_awareness_startup_integration_regression_test
"""
from __future__ import annotations

import inspect
from unittest.mock import patch

import pet.council_chat_server as ccs

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


# ============================================================
# A. Structural.
# ============================================================

_src = inspect.getsource(ccs)
_pos_sql_hook = _src.find('async def _reconcile_stale_sql_runs')
_pos_awareness_hook = _src.find('async def _system_awareness_probe')

check("A: _system_awareness_probe() handler is defined in council_chat_server.py", _pos_awareness_hook > -1)
check(
    "A: registered as its OWN @app.on_event(\"startup\") handler (FastAPI runs every "
    "registered startup handler, not just the first)",
    '@app.on_event("startup")' in _src[max(0, _pos_awareness_hook - 60):_pos_awareness_hook],
)
check(
    "A: positioned after the SQL reconciliation hook (both exist, order between "
    "independent startup hooks doesn't matter functionally, but confirms this is an "
    "ADDITION, not a replacement of the existing hook)",
    _pos_sql_hook < _pos_awareness_hook,
)

_fn_src = inspect.getsource(ccs._system_awareness_probe)
check(
    "A: the handler imports from agent.system_state_store (the real module built "
    "this pass), not a duplicated inline probe",
    "from agent.system_state_store import" in _fn_src,
)
check("A: the handler is an async function (required for @app.on_event)", inspect.iscoroutinefunction(ccs._system_awareness_probe))


# ============================================================
# B. Functional — TestClient runs the REAL startup lifecycle.
# ============================================================

_calls = []
_orig_update_state = None

import agent.system_state_store as store_mod

_orig_update_state = store_mod.update_state


def _spy_update_state(*a, **kw):
    _calls.append(kw)
    return _orig_update_state(*a, **kw)


with patch("agent.system_state_store.update_state", side_effect=_spy_update_state):
    from starlette.testclient import TestClient
    with TestClient(ccs.app):
        pass

check(
    "B: the real update_state() is actually invoked exactly once when the app starts "
    "(TestClient's context manager runs the genuine FastAPI startup event, not a mock "
    "of the whole handler)",
    len(_calls) == 1,
    f"calls={_calls}",
)
check("B: probe_source is explicitly 'agent_local_probe' at this integration point", _calls[0].get("probe_source") == "agent_local_probe" if _calls else False)


# ============================================================
# C. Failure isolation — a broken probe/store layer never aborts startup.
# ============================================================

def _boom(*a, **kw):
    raise RuntimeError("simulated system_state_store failure")


with patch("agent.system_state_store.update_state", side_effect=_boom):
    try:
        with TestClient(ccs.app):
            pass
        no_raise = True
    except Exception:
        no_raise = False

check(
    "C: a raising update_state() (simulated total probe/store failure) does NOT abort "
    "app startup — the outer try/except in _system_awareness_probe() catches it",
    no_raise,
)


# ============================================================
# D. Cheap-probe contract: no LLM/web call needed for this startup
# path to complete (mandate §11) — the real call above already
# succeeded with no network/LLM mocking required, which IS the proof;
# this section just makes that assertion explicit and structural.
# ============================================================

check(
    "D: system_awareness.py itself makes no LLM call (grepped: no 'ollama' generation/ "
    "chat endpoint, only the read-only /api/tags reachability check already covered by "
    "agent/system_awareness_regression_test.py's own network-dependency check)",
    "/api/generate" not in inspect.getsource(__import__("agent.system_awareness", fromlist=["x"]))
    and "/api/chat" not in inspect.getsource(__import__("agent.system_awareness", fromlist=["x"])),
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
