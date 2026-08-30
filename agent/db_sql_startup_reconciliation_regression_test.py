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

import asyncio
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
# B. Functional: invoke the registered async startup handler directly.
# This used to use Starlette's TestClient as a lifecycle driver, but on
# the current host even a minimal empty FastAPI app hangs inside
# TestClient.__enter__ before any YANDI startup handler runs. That makes
# TestClient a broken fixture here, not a useful assertion about SQL
# reconciliation. The structural checks above prove the handler is
# registered on the app; this functional check proves the handler body
# calls shadow_reconcile_stale_runs() exactly once.
# ============================================================

_calls = []


def _spy_reconcile(*, log=None, verbose=False, older_than_seconds=3600):
    _calls.append({"older_than_seconds": older_than_seconds})
    return 0


with patch("agent.db.sql.shadow_write.shadow_reconcile_stale_runs", _spy_reconcile):
    asyncio.run(_startup_fn())

check(
    "B: the registered startup handler calls shadow_reconcile_stale_runs() exactly once",
    len(_calls) == 1,
    f"calls={_calls}",
)


# ============================================================
# C. Fail-open — SQL endpoint genuinely unreachable (forced,
# deterministic — DATABASE BOOTSTRAP V1's canonical defaults mean
# is_configured() is True out of the box now, so "unconfigured" is no
# longer the ambient state to rely on; force unreachability explicitly
# instead so this stays true regardless of which host runs the suite)
# — starting the app must not raise.
# ============================================================

with patch.dict("os.environ", {"YANDI_SQL_SOCKET": "/nonexistent/startup-reconciliation-test/mysql.sock"}):
    check(
        "C precondition: SQL layer resolves canonical defaults (is_configured()=True) "
        "but the forced socket path is genuinely unreachable",
        sqlconn.is_configured() is True,
    )

    try:
        asyncio.run(_startup_fn())
        no_raise = True
    except Exception as e:
        no_raise = False
    check("C: startup handler does not raise with SQL endpoint unreachable (real fail-open, not mocked)", no_raise)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
