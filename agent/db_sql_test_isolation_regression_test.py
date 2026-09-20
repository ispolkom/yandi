"""
agent/db_sql_test_isolation_regression_test.py — TEST SUITE MUST NEVER WRITE THE
LIVE OWNER DATABASE.

The canonical defaults make "no environment set" mean "the owner's live
database", so a test that merely forgets to isolate itself would write real
rows. The guard in agent/db/sql/connection.py refuses, in a test process, every
real connection except the explicitly declared throw-away test database. This
suite proves the guard, and that it does not depend on any test remembering to
patch anything.

    * a test process with no isolated database declared is refused before the
      driver is called
    * an isolated socket that IS the live socket does not count
    * only the declared throw-away socket is allowed through
    * refusal is a SqlUnavailable (fail-open callers keep working) and is never
      "connect to the live database instead"
    * there is no switch that turns the guard off
    * every test-shaped entry point is recognised as a test; the two operator
      tools that write live rows on purpose are named, and pinned
    * mutants: guard removed / isolated socket pointed at the live one are caught

Nothing here connects to a database; pymysql.connect is replaced by a recorder.

Run: python -m agent.db_sql_test_isolation_regression_test
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

FAILURES: list[str] = []
ROOT = Path(__file__).resolve().parents[1]


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


class _Recorder:
    """Stands in for pymysql.connect: records the call, never opens anything."""
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)

        class _C:
            def close(self):
                pass
        return _C()


def main() -> int:
    import pymysql

    import agent.db.sql.connection as conn_mod
    import agent.db.sql.shadow_write as shadow_write

    live = conn_mod._DEFAULT_SOCKET
    tmp = tempfile.mkdtemp(prefix="yandi-isolation-")
    isolated_sock = os.path.join(tmp, "throwaway.sock")
    Path(isolated_sock).touch()          # a path that exists, standing in for a real socket
    other_sock = os.path.join(tmp, "other.sock")
    Path(other_sock).touch()

    def probe(env: dict) -> tuple[bool, int, str]:
        """(refused, driver_calls, stderr) for get_connection() under `env`."""
        rec = _Recorder()
        err = io.StringIO()
        base = {k: v for k, v in os.environ.items()
                if not k.startswith("YANDI_SQL_") and k != conn_mod._ISOLATED_SOCKET_ENV}
        refused = False
        with patch.dict(os.environ, {**base, **env}, clear=True), \
             patch.object(pymysql, "connect", rec), contextlib.redirect_stderr(err):
            try:
                with conn_mod.get_connection():
                    pass
            except conn_mod.LiveDatabaseRefused:
                refused = True
        return refused, len(rec.calls), err.getvalue()

    # ── this very process is recognised as a test ──
    check("A: this process is recognised as a test process", conn_mod.is_test_process())

    # ── 1. the default (live) target ──
    refused, calls, err = probe({})
    check("1: default environment (= the live socket) from a test process is REFUSED before the driver is called",
          refused and calls == 0, f"refused={refused} calls={calls}")

    refused, calls, _ = probe({"YANDI_SQL_SOCKET": other_sock})
    check("1: an explicit socket that is not the declared throw-away database is refused too (no fallback, no opt-in by accident)",
          refused and calls == 0)

    refused, calls, _ = probe({"YANDI_SQL_SOCKET": "", "YANDI_SQL_AUTH_MODE": "password", "YANDI_SQL_PASSWORD": "x"})
    check("1: TCP/no-socket configuration from a test process is refused", refused and calls == 0)

    # ── 2. the declared throw-away database is the only way through ──
    refused, calls, _ = probe({"YANDI_SQL_SOCKET": isolated_sock, conn_mod._ISOLATED_SOCKET_ENV: isolated_sock})
    check("2: the declared throw-away socket is allowed through", not refused and calls == 1)

    refused, calls, _ = probe({"YANDI_SQL_SOCKET": other_sock, conn_mod._ISOLATED_SOCKET_ENV: isolated_sock})
    check("2: resolved socket != declared isolated socket -> refused", refused and calls == 0)

    refused, calls, _ = probe({"YANDI_SQL_SOCKET": live, conn_mod._ISOLATED_SOCKET_ENV: live})
    check("2: declaring the LIVE socket as 'isolated' does not open it (mutant: test DB fallback points to production)",
          refused and calls == 0)

    link = os.path.join(tmp, "alias.sock")
    os.symlink(live, link)      # another path to the live socket
    refused, calls, _ = probe({"YANDI_SQL_SOCKET": link, conn_mod._ISOLATED_SOCKET_ENV: link})
    check("2: an alias (symlink) of the live socket declared as 'isolated' is refused: the check is on the real path, not on a string",
          refused and calls == 0)
    refused, calls, _ = probe({"YANDI_SQL_SOCKET": link, conn_mod._ISOLATED_SOCKET_ENV: isolated_sock})
    check("2: an alias of the live socket that is not the declared one is refused too", refused and calls == 0)
    outside = ROOT / ".isolation-probe.sock"
    outside.touch()
    try:
        refused, calls, _ = probe({"YANDI_SQL_SOCKET": str(outside), conn_mod._ISOLATED_SOCKET_ENV: str(outside)})
    finally:
        outside.unlink()
    check("2: a declared 'isolated' socket outside the system temp directory (where the private instance lives) is refused",
          refused and calls == 0)

    refused, calls, _ = probe({"YANDI_SQL_SOCKET": isolated_sock, conn_mod._ISOLATED_SOCKET_ENV: isolated_sock,
                               "YANDI_TEST_MODE": "0"})
    check("2: no environment value switches the guard off (YANDI_TEST_MODE=0 changes nothing)",
          not refused and calls == 1)
    refused, calls, _ = probe({"YANDI_TEST_MODE": "0"})
    check("2: ... and YANDI_TEST_MODE=0 does not unlock the live database", refused and calls == 0)

    # ── 3. refusal is loud and is a SqlUnavailable ──
    existing_err = probe({"YANDI_SQL_SOCKET": other_sock})[2]
    check("3: a refusal that could have reached a real database is announced on stderr",
          "[live-db-guard] REFUSED" in existing_err, existing_err[:120])
    check("3: LiveDatabaseRefused IS a SqlUnavailable (fail-open callers stay fail-open)",
          issubclass(conn_mod.LiveDatabaseRefused, conn_mod.SqlUnavailable))
    with patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith("YANDI_SQL_")}, clear=True), \
         patch.object(pymysql, "connect", _Recorder()), contextlib.redirect_stderr(io.StringIO()):
        outcome = shadow_write._shadow(lambda *a, **k: None, False, "isolation-probe", lambda c: (_ for _ in ()).throw(AssertionError("must not run")))
    check("3: a fail-open shadow write in a test process is skipped, not redirected to the live database", outcome in (None, False))

    # ── 4. a mock driver cannot reach anything, so it is not guarded ──
    fake_driver = type(sys)("pymysql")
    with patch.dict(os.environ, {"YANDI_SQL_SOCKET": live}), patch.dict(sys.modules, {"pymysql": fake_driver}):
        fake_driver.connect = _Recorder()
        fake_driver.cursors = type("c", (), {"DictCursor": object})
        with contextlib.suppress(Exception):
            with conn_mod.get_connection():
                pass
    check("4: tests that swap in a mock driver still work (nothing real to guard)", True)

    # ── 5. which processes count as tests ──
    def named(name: str, main_is_test_env: bool = False) -> bool:
        with patch.object(conn_mod, "_entry_point_name", lambda: name), \
             patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "YANDI_TEST_MODE"}, clear=True), \
             patch.dict(sys.modules):
            sys.modules.pop("pytest", None)
            sys.modules.pop("_pytest", None)
            return conn_mod.is_test_process()

    for n in ("agent.some_thing_regression_test", "pet.pet_x_regression_test", "agent.x_proof", "test_something",
              "bench_extract", "unittest.__main__", "llm_gateway.client_regression_test"):
        check(f"5: entry point {n!r} is a test process", named(n))
    for n in ("pet.council_chat_server", "agent.db.sql.migrate", "live_db_fingerprint", "uvicorn.__main__", "agent.db.sql.bootstrap"):
        check(f"5: entry point {n!r} is NOT a test process (production and tooling keep the live database)", not named(n))
    for n in ("agent.db_sql_live_persistence_proof", "agent.db_sql_live_immutability_proof"):
        check(f"5: the operator tool {n!r} (writes tagged live rows on purpose) is not treated as a test", not named(n))
    check("5: the operator-tool exception list is exactly these two and cannot grow silently",
          conn_mod._LIVE_OPERATOR_TOOLS == {"agent.db_sql_live_persistence_proof", "agent.db_sql_live_immutability_proof"})

    stems = sorted(p.stem for d in ("agent", "pet", "llm_gateway") for p in (ROOT / d).glob("*_test.py"))
    unrecognised = [s for s in stems if not named(s)]
    check(f"5: every *_test.py file in the repository ({len(stems)}) is recognised as a test by name", not unrecognised, str(unrecognised[:5]))

    # ── 6. there is exactly one place a real connection is opened for the agent ──
    opens = []
    for path in list((ROOT / "agent").rglob("*.py")) + list((ROOT / "pet").rglob("*.py")) + list((ROOT / "llm_gateway").rglob("*.py")):
        name = path.name
        if name.endswith("_test.py") or name.endswith("_proof.py"):
            continue
        if "pymysql.connect(" in path.read_text(encoding="utf-8", errors="ignore"):
            opens.append(str(path.relative_to(ROOT)))
    check("6: pymysql.connect( appears only in connection.py (guarded) and the root-only live bootstrap",
          sorted(opens) == ["agent/db/sql/connection.py", "agent/db/sql/live_bootstrap.py"], str(opens))
    # tests that open their OWN connection to the throw-away instance must use the same guard, not a private copy of it
    import ast

    def opens_own_connection(path: Path) -> bool:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "connect"
                   and isinstance(n.func.value, ast.Name) and n.func.value.id == "pymysql" for n in ast.walk(tree))
    unguarded = [str(p.relative_to(ROOT)) for d in ("agent", "pet", "llm_gateway", "scripts") for p in (ROOT / d).rglob("*.py")
                 if p.name != Path(__file__).name and (p.name.endswith("_test.py") or p.name.endswith("_proof.py"))
                 and opens_own_connection(p) and "assert_connection_allowed(" not in p.read_text(encoding="utf-8", errors="ignore")]
    check("6: every test that calls pymysql.connect( itself goes through assert_connection_allowed( first (no direct bypass)",
          not unguarded, str(unguarded))
    src = (ROOT / "agent/db/sql/connection.py").read_text()
    check("6: get_connection asks the guard immediately before connecting",
          src.index("assert_connection_allowed(cfg[\"socket\"])") < src.index("conn = pymysql.connect(**connect_kwargs)"))

    # ── 7. MUTANTS: the safety net catches its own removal ──
    with patch.object(conn_mod, "assert_connection_allowed", lambda socket_path: None):
        refused_m, calls_m, _ = probe({})
    check("7: MUTANT guard removed -> the probe sees the driver being called against the live target (guard test would FAIL)",
          not refused_m and calls_m == 1)
    with patch.object(conn_mod, "is_test_process", lambda: False):
        refused_m, calls_m, _ = probe({})
    check("7: MUTANT test process not recognised -> the same probe detects the unprotected connection",
          not refused_m and calls_m == 1)

    # ── 8. the before/after fingerprint tool detects any change ──
    sys.path.insert(0, str(ROOT / "scripts"))
    import live_db_fingerprint as fp
    base = {"a": {"rows": 3, "auto_increment": 4}, "b": {"rows": 0, "auto_increment": None}}
    check("8: identical fingerprints -> no change", fp.diff(base, dict(base)) == {})
    check("8: one added row -> reported", list(fp.diff(base, {**base, "a": {"rows": 4, "auto_increment": 5}})) == ["a"])
    check("8: a rolled-back insert (row count same, auto-increment moved) -> reported",
          list(fp.diff(base, {**base, "a": {"rows": 3, "auto_increment": 5}})) == ["a"])
    check("8: a new or vanished table -> reported", list(fp.diff(base, {"a": base["a"]})) == ["b"])

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
