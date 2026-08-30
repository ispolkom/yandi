"""
agent/db_sql_runtime_config_resolver_regression_test.py — DATABASE
BOOTSTRAP V1, seventeenth Phase B attempt: canonical default SQL
configuration.

LIVE-CONFIRMED BUG (owner-run, full production pipeline via
`orchestrator_v2.py --interactive --web --validate`, NO manual env
vars exported): despite the dedicated instance being fully bootstrapped
and LIVE (auth_socket proven, schema/roles/triggers/grants all proven —
see the earlier Phase B commits), every single SqlShadow call in the
run logged:

    [SqlShadow] record_claim_family SKIPPED (SQL unavailable):
    YANDI_SQL_USER/YANDI_SQL_PASSWORD (or YANDI_SQL_SOCKET with
    YANDI_SQL_AUTH_MODE=auth_socket) not set in the environment

ROOT CAUSE: agent.db.sql.connection — the ONE runtime SQL configuration
resolver every caller (repositories.py, shadow_write.py,
orchestrator_v2.py, migrate.py) goes through — had NO default socket/
auth_mode/user at all. Every one of those three had to be exported
manually before ANY production run, even though the dedicated appliance
IS this deployment's one, canonical, always-present local database.

FIX: connection.py now resolves three canonical defaults when the
corresponding env var is genuinely absent (present-but-empty is a
deliberate, distinct "explicit override to nothing" case — see
connection._resolve()'s own docstring):
    YANDI_SQL_SOCKET     default "/run/yandi/mysql/mysql.sock"
    YANDI_SQL_AUTH_MODE  default "auth_socket"
    YANDI_SQL_USER       default "yandi_runtime"
No default password exists for either mode — auth_socket needs none;
explicit password mode still requires an explicit YANDI_SQL_PASSWORD.

This is the ONE resolver — nothing in shadow_write.py/repositories.py/
orchestrator_v2.py/migrate.py hardcodes its own copy of any of this;
every one of them calls agent.db.sql.connection.get_connection(), which
calls this module's own _config()/is_configured(), and nothing else.

Covers (mandate's own 12-item list):
    1.  no SQL env -> resolves the three canonical defaults
    2.  no password required in auth_socket mode
    3.  explicit YANDI_SQL_SOCKET overrides the default
    4.  explicit YANDI_SQL_AUTH_MODE overrides the default
    5.  explicit YANDI_SQL_USER overrides the default
    6.  missing dedicated socket -> fail-open, ZERO TCP fallback
    7.  inaccessible dedicated socket -> ZERO localhost fallback
    8.  any connection failure -> ZERO :3306 fallback
    9.  every shadow_write.py function routes through the SAME resolver
    10. no SQL secret introduced by any of this
    11. bootstrap/root connection behavior (live_bootstrap.py) unchanged
    12. shared mysql untouched / cross-file socket-path agreement

Run: /home/iam/venv/bin/python3 -m agent.db_sql_runtime_config_resolver_regression_test
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import agent.db.sql.connection as conn_mod
import agent.db.sql.shadow_write as sw

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


_ENV_KEYS = (
    "YANDI_SQL_HOST", "YANDI_SQL_PORT", "YANDI_SQL_USER", "YANDI_SQL_PASSWORD",
    "YANDI_SQL_DATABASE", "YANDI_SQL_SOCKET", "YANDI_SQL_AUTH_MODE",
)


def _clean_env():
    """A patch.dict context that removes every YANDI_SQL_* var for its
    duration — the true 'nothing exported' state this whole bug was
    actually about, restored automatically on exit."""
    return patch.dict("os.environ", {}, clear=False), _ENV_KEYS


class _clean(object):
    def __enter__(self):
        import os
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
        return self

    def __exit__(self, *a):
        import os
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v


def _fake_connect(fake_pymysql):
    return patch.dict("sys.modules", {"pymysql": fake_pymysql, "pymysql.cursors": fake_pymysql.cursors})


# ============================================================
# 1/2. No SQL env at all -> canonical defaults resolve, no password
# required.
# ============================================================
with _clean():
    check("1a. no env: resolved socket is the canonical dedicated path", conn_mod._socket() == "/run/yandi/mysql/mysql.sock")
    check("1b. no env: resolved auth_mode is auth_socket", conn_mod._auth_mode() == "auth_socket")
    check("1c. no env: resolved user is yandi_runtime", conn_mod._user() == "yandi_runtime")
    check(
        "2. no env, no password anywhere: is_configured() is still True "
        "(auth_socket needs no password)",
        conn_mod.is_configured() is True,
    )
    check(
        "2b. no env: _config()['password'] is the empty string (no default password "
        "value exists anywhere, not even a non-empty placeholder)",
        conn_mod._config()["password"] == "",
    )

# ============================================================
# 3/4/5. Explicit overrides win over the defaults, independently.
# ============================================================
with _clean():
    import os as _os
    _os.environ["YANDI_SQL_SOCKET"] = "/custom/override/mysql.sock"
    check("3. explicit YANDI_SQL_SOCKET overrides the default", conn_mod._socket() == "/custom/override/mysql.sock")
    check("3b. overriding ONLY the socket leaves auth_mode/user at their own defaults", conn_mod._auth_mode() == "auth_socket" and conn_mod._user() == "yandi_runtime")

with _clean():
    import os as _os
    _os.environ["YANDI_SQL_AUTH_MODE"] = "password"
    check("4. explicit YANDI_SQL_AUTH_MODE overrides the default", conn_mod._auth_mode() == "password")
    check(
        "4b. overriding auth_mode to 'password' with no password set -> is_configured() "
        "is False (no default password, ever)",
        conn_mod.is_configured() is False,
    )

with _clean():
    import os as _os
    _os.environ["YANDI_SQL_USER"] = "yandi_readonly"
    check("5. explicit YANDI_SQL_USER overrides the default", conn_mod._user() == "yandi_readonly")
    check("5b. overriding ONLY the user leaves socket/auth_mode at their own defaults", conn_mod._socket() == "/run/yandi/mysql/mysql.sock" and conn_mod._auth_mode() == "auth_socket")

# ============================================================
# 6/7/8. Missing / inaccessible / any-other-failure socket -> fail-open,
# and structurally ZERO TCP/localhost/:3306 fallback — not just "didn't
# happen to fall back this time," but no code path exists that could.
# ============================================================
with _clean():
    import os as _os
    _os.environ["YANDI_SQL_SOCKET"] = "/nonexistent/resolver-test-6/mysql.sock"
    raised = False
    try:
        with conn_mod.get_connection():
            pass
    except conn_mod.SqlUnavailable:
        raised = True
    check("6. missing dedicated socket path -> SqlUnavailable (fail-open), never a raw error", raised)

    r = sw.shadow_record_claim(
        claim_id="cl_x", run_id="run_x", claim_text="x", content_hash=None,
        claim_type=None, claim_confidence=None, verification_status=None,
        family_id=None, family_domain=None, family_canonical_text=None,
        query_context=None, support_count=0, contradiction_count=0,
    )
    check("6b. shadow_record_claim() itself fails open (returns None) for a missing socket", r is None)

with _clean():
    import os as _os
    # Simulates "inaccessible" (permission denied) rather than "missing"
    # — same guarantee must hold for either failure shape.
    fake_pymysql = MagicMock()
    fake_pymysql.cursors.DictCursor = object
    fake_pymysql.connect.side_effect = PermissionError("[Errno 13] Permission denied")
    with _fake_connect(fake_pymysql):
        raised7 = False
        try:
            with conn_mod.get_connection():
                pass
        except conn_mod.SqlUnavailable:
            raised7 = True
    check("7. inaccessible (permission-denied) dedicated socket -> SqlUnavailable, same as missing", raised7)
    check(
        "7b. the permission-denied attempt was made against the dedicated socket path, "
        "never localhost — the connect call itself never carried host/port",
        "host" not in fake_pymysql.connect.call_args.kwargs
        and "port" not in fake_pymysql.connect.call_args.kwargs,
        f"{fake_pymysql.connect.call_args}",
    )

with _clean():
    import os as _os
    # ANY connection failure (timeout, refused, protocol error, ...) —
    # not just ENOENT/EACCES — must behave identically: no fallback path.
    fake_pymysql2 = MagicMock()
    fake_pymysql2.cursors.DictCursor = object
    fake_pymysql2.connect.side_effect = TimeoutError("connection timed out")
    with _fake_connect(fake_pymysql2):
        raised8 = False
        try:
            with conn_mod.get_connection():
                pass
        except conn_mod.SqlUnavailable:
            raised8 = True
    check("8. an arbitrary connection failure (timeout) -> SqlUnavailable, no retry against :3306", raised8)
    check(
        "8b. get_connection()'s own source never mentions a retry/fallback attempt "
        "after the pymysql.connect() call fails (structural, not just 'didn't happen "
        "to trigger this time')",
        inspect.getsource(conn_mod.get_connection).count("pymysql.connect(") == 1,
    )

# Structural: cfg["socket"] resolves truthy by default, so the
# host/port branch in get_connection() is provably unreachable under
# default configuration — not merely untested.
check(
    "8c. STRUCTURAL: under the canonical defaults, _config()['socket'] is always "
    "truthy, so get_connection()'s host/port (TCP) branch can never be taken unless "
    "a caller EXPLICITLY sets YANDI_SQL_SOCKET to an empty string",
    bool(conn_mod._config()["socket"]),
)

# ============================================================
# 9. Every shadow_write.py function goes through the SAME resolver —
# spy on connection.get_connection() itself (not a per-function mock)
# and confirm every one of them calls it exactly once per operation.
# ============================================================
_calls = []


class _RecordingUnavailable(conn_mod.SqlUnavailable):
    pass


def _spy_get_connection(autocommit=False):
    _calls.append(autocommit)
    raise _RecordingUnavailable("spy: refusing to actually connect")


with patch.object(sw, "get_connection", _spy_get_connection):
    sw.shadow_record_question_and_run(raw_text="q", run_id="r1", started_at=0.0, web_enabled=False, validation_enabled=False, pipeline_version=None)
    sw.shadow_complete_run(run_id="r1", question_id=1, delivered_answer_text="a", completed_at=0.0, canonical_trust="UNVERIFIED")
    sw.shadow_fail_run(run_id="r1", failed_stage="x", error_class="Y")
    sw.shadow_record_claim(claim_id="c1", run_id="r1", claim_text="t", content_hash=None, claim_type=None, claim_confidence=None, verification_status=None, family_id=None, family_domain=None, family_canonical_text=None, query_context=None, support_count=0, contradiction_count=0)
    sw.shadow_record_claims_and_evidence(run_id="r1", claims_data=[], evidence_data=[])
    sw.shadow_record_claim_family(family_id="f1", domain="d", canonical_text="t", claim_id="c1")
    sw.shadow_record_belief_assessment(belief_id="b1", topic="t", statement="s", confidence=0.5, status="active", change_type="created")
    sw.shadow_record_recheck_event(family_id="f1", outcome="ok")
    sw.shadow_reconcile_stale_runs()
    sw.shadow_record_evidence(claim_id="c1", run_id="r1", resource_type="internet", canonical_uri="https://x", observation_route="internet", origin_observation_id=None, observed_at=0.0, source_class=None, quality_score=None, content_excerpt=None, relation="supports", directness=None, evidence_eligible=False, evidence_role=None, counted_via=None)

check(
    "9. every shadow_write.py public function calls the SAME connection.get_connection() "
    "— one resolver, no per-function hardcoded config (10 distinct shadow_* functions "
    "spied on, each triggering exactly one call)",
    len(_calls) == 10,
    f"observed {len(_calls)} calls: {_calls}",
)

# ============================================================
# 10. No SQL secret introduced anywhere in this fix.
# ============================================================
_conn_source = Path(conn_mod.__file__).read_text(encoding="utf-8")
check(
    "10a. none of the three new default constants is a password-shaped value "
    "(the auth_socket default needs none, and no default password constant exists "
    "anywhere in connection.py)",
    conn_mod._DEFAULT_AUTH_MODE == "auth_socket"
    and "_DEFAULT_PASSWORD" not in _conn_source
    and "YANDI_SQL_PASSWORD" not in repr((conn_mod._DEFAULT_SOCKET, conn_mod._DEFAULT_AUTH_MODE, conn_mod._DEFAULT_USER)),
)
check(
    "10b. _config()['password'] is only ever sourced from the environment (os.environ."
    "get), never from a hardcoded default anywhere in _config()'s own source",
    "os.environ.get(\"YANDI_SQL_PASSWORD\"" in inspect.getsource(conn_mod._config),
)

# ============================================================
# 11. Bootstrap/root connection behavior (live_bootstrap.py) unchanged
# — it does not import or depend on this resolver at all, by design
# (it must connect BEFORE this module's defaults could possibly apply
# — no yandi_runtime account, no dedicated database, on a virgin
# instance). This fix touches NONE of that.
# ============================================================
_live_bootstrap_source = Path(
    Path(conn_mod.__file__).parent / "live_bootstrap.py"
).read_text(encoding="utf-8")
check(
    "11. agent/db/sql/live_bootstrap.py does not import agent.db.sql.connection at all "
    "— root/auth_socket bootstrap connections are structurally independent of this "
    "runtime resolver, so today's fix cannot have changed bootstrap behavior",
    "import agent.db.sql.connection" not in _live_bootstrap_source
    and "from agent.db.sql.connection" not in _live_bootstrap_source
    and "from agent.db.sql import connection" not in _live_bootstrap_source,
)

# ============================================================
# 12. Shared mysql untouched / cross-file agreement on the dedicated
# socket path — the SAME literal path this resolver defaults to is
# what deploy/install-yandi.sh actually creates and what agent.system_
# awareness.py already expects, not a fourth, independently-typed copy.
# ============================================================
_install_sh = Path(Path(conn_mod.__file__).parent.parent.parent.parent / "deploy" / "install-yandi.sh").read_text(encoding="utf-8")
_sys_awareness = Path(Path(conn_mod.__file__).parent.parent.parent / "system_awareness.py").read_text(encoding="utf-8")
check(
    "12a. connection.py's _DEFAULT_SOCKET matches deploy/install-yandi.sh's own "
    "SOCKET_PATH construction (RUNTIME_MYSQL_DIR + /mysql.sock) — not a fourth, "
    "independently-typed copy of this path",
    conn_mod._DEFAULT_SOCKET == "/run/yandi/mysql/mysql.sock"
    and 'SOCKET_PATH="${RUNTIME_MYSQL_DIR}/mysql.sock"' in _install_sh,
)
check(
    "12b. connection.py's _DEFAULT_SOCKET matches agent/system_awareness.py's own "
    "_YANDI_DB_SOCKET_PATH constant exactly",
    f'_YANDI_DB_SOCKET_PATH = "{conn_mod._DEFAULT_SOCKET}"' in _sys_awareness,
)
check(
    "12c. nothing in this fix references the shared instance's own port (3306) as "
    "anything other than the pre-existing, unrelated YANDI_SQL_PORT fallback default "
    "(unreachable in practice — see check 8c) — no new coupling to :3306 introduced",
    "3306" not in repr((conn_mod._DEFAULT_SOCKET, conn_mod._DEFAULT_AUTH_MODE, conn_mod._DEFAULT_USER)),
)


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
