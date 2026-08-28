"""
agent/db_sql_startup_reconciliation_regression_test.py — Этап 5 (SQL
persistence migration) regression: MIGRATION_STATUS.md §41's last
undone item — "shadow_reconcile_stale_runs() — not called from any
daemon startup path. Needs a decision about which process actually
owns 'the daemon' in this codebase (not investigated in this pass)."

That investigation: pet/council_chat_server.py IS the daemon —
chat_orch.py's router (mounted into its `app`) calls agent.
orchestrator_v2.process() in-process for every request, the sole
writer of verification_run rows. Both real launch paths hit this
process: start.sh's direct `python3 pet/council_chat_server.py`
(reaches `if __name__ == "__main__": uvicorn.run(app, ...)`) AND
start_headless.sh's `uvicorn pet.council_chat_server:app` (does NOT
reach that block — imports the `app` object directly). A FastAPI
`@app.on_event("startup")` handler is the one hook both paths share.

Covers:
    A. structural: the startup handler is registered on `app` (not
       buried inside the `if __name__ == "__main__":` block, which
       start_headless.sh's uvicorn invocation never reaches).
    B. functional: using Starlette's TestClient (which runs FastAPI's
       startup/shutdown lifecycle), the handler actually fires and
       calls shadow_reconcile_stale_runs() — via a call-count spy —
       exactly once per app startup.
    C. fail-open: SQL genuinely unconfigured (real environment state)
       — starting the app does not raise, does not block/delay
       startup.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_startup_reconciliation_regression_test
"""
from __future__ import annotations

import inspect
from unittest.mock import patch

import agent.db.sql.connection as sqlconn

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


import pet.council_chat_server as ccs

# ============================================================
# A. Structural: registered as an app startup event, before the
# `if __name__ == "__main__":` block (so uvicorn-direct launch hits it).
# ============================================================

_src = inspect.getsource(ccs)
_pos_on_event = _src.find('@app.on_event("startup")')
_pos_reconcile_import = _src.find("from agent.db.sql.shadow_write import shadow_reconcile_stale_runs")
_pos_main_block = _src.rfind('if __name__ == "__main__":')  # rfind: the handler's OWN
# docstring literally quotes this exact string once (explaining why the
# hook fires under uvicorn-direct launch too) — that mention sits INSIDE
# the docstring, before the decorator/import positions below it; the
# real block is the LAST occurrence, at the actual end of the file.

check(
    "A: @app.on_event(\"startup\") handler is registered (fires under BOTH "
    "uvicorn.run(app,...) in __main__ AND `uvicorn pet.council_chat_server:app`, "
    "unlike code inside the __main__ block)",
    -1 < _pos_on_event < _pos_main_block,
    f"on_event={_pos_on_event} main_block={_pos_main_block}",
)
check(
    "A: the handler imports shadow_reconcile_stale_runs (the documented, "
    "fail-open, startup-intended entrypoint — not raw repository access)",
    _pos_on_event < _pos_reconcile_import < _pos_main_block,
    f"import_pos={_pos_reconcile_import}",
)

_startup_fn = ccs._reconcile_stale_sql_runs
check(
    "A: the registered handler is an async function (required for @app.on_event)",
    inspect.iscoroutinefunction(_startup_fn),
)


# ============================================================
# B. Functional: TestClient's context manager runs FastAPI's startup
# lifecycle — the handler must actually fire, exactly once, and must
# actually call shadow_reconcile_stale_runs() (not just import it).
# ============================================================

_calls = []


def _spy_reconcile(*, log=None, verbose=False, older_than_seconds=3600):
    _calls.append({"older_than_seconds": older_than_seconds})
    return 0


with patch("agent.db.sql.shadow_write.shadow_reconcile_stale_runs", _spy_reconcile):
    from starlette.testclient import TestClient

    with TestClient(ccs.app):
        pass

check(
    "B: shadow_reconcile_stale_runs() is actually called exactly once when the "
    "app starts (TestClient's context manager runs the real FastAPI startup event)",
    len(_calls) == 1,
    f"calls={_calls}",
)


# ============================================================
# C. Fail-open — SQL genuinely unconfigured (real environment state,
# not simulated) — starting the app must not raise.
# ============================================================

check("C precondition: SQL layer genuinely unconfigured", sqlconn.is_configured() is False)

try:
    from starlette.testclient import TestClient as _TC2
    with _TC2(ccs.app):
        pass
    no_raise = True
except Exception as e:
    no_raise = False
check("C: app startup does not raise with no SQL configured (real fail-open, not mocked)", no_raise)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
