"""
agent/db_sql_runtime_bootstrap_dir_regression_test.py — DATABASE
BOOTSTRAP V1, ninth Phase B attempt: deploy/install-yandi.sh's
ensure_secure_bootstrap_dir() and the split runtime-directory
architecture it enforces.

Live-confirmed bug this replaced: the OLD design had a single
RUNTIME_DIR (/run/yandi) that was BOTH the systemd RuntimeDirectory=
target for mysqld's socket/pid AND where the bootstrap secret marker
was written. systemd tears down a RuntimeDirectory= target entirely on
`systemctl stop` (RuntimeDirectoryPreserve= defaults to "no") —
initialize_datadir()'s own "stop yandi-db.service before touching the
datadir" step (an earlier fix) silently deleted the whole directory,
and the later marker write failed with "No such file or directory".
This also carried a real TOCTOU risk: mysqld runs as $YANDI_DB_USER,
and if that account could write into the SAME directory a root-owned
secret lived in, it could delete/replace/race the marker even though
it could never read the 0600 file's contents directly.

Fix: split into RUNTIME_MYSQL_DIR (systemd-managed via
`RuntimeDirectory=yandi/mysql`, owned yandi-db:yandi-db, torn down on
stop exactly as before) and RUNTIME_BOOTSTRAP_DIR (root:root 0700,
created/verified by install-yandi.sh only, invisible to
$YANDI_DB_USER, never a systemd RuntimeDirectory= target so it
survives every mysqld service stop/start).

ensure_secure_bootstrap_dir() is pure (operates on the RUNTIME_DIR/
RUNTIME_BOOTSTRAP_DIR globals only, no systemd/mysqld dependency), so
most of its logic is genuinely testable without root by overriding
those globals to point at a temp directory — this file does that
dynamically. What genuinely requires root (creating an ACTUAL
root:root-owned fixture to prove the idempotent "correct existing
state is accepted silently" path) is covered by a static check instead
and flagged as needing live verification, matching this whole file's
established posture for install-yandi.sh (never executed for real,
only design/logic verified).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_runtime_bootstrap_dir_regression_test
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

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


REPO = Path(__file__).parent.parent
INSTALL_SCRIPT = REPO / "deploy" / "install-yandi.sh"
YANDI_DB_SERVICE = REPO / "deploy" / "yandi-db.service"
script_text = INSTALL_SCRIPT.read_text(encoding="utf-8")
unit_text = YANDI_DB_SERVICE.read_text(encoding="utf-8")


def _extract_fn(name: str) -> str:
    body = script_text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
    return f"{name}() {{\n" + body + "\n}\n"


_FUNC_SRC = _extract_fn("ensure_secure_bootstrap_dir")


def _run_guard(*, runtime_dir: str, bootstrap_dir: str):
    snippet = f"""
set -u
log() {{ echo "[log] $*"; }}
die() {{ echo "[die] $*" >&2; exit 1; }}
RUNTIME_DIR={runtime_dir!r}
RUNTIME_BOOTSTRAP_DIR={bootstrap_dir!r}
{_FUNC_SRC}
ensure_secure_bootstrap_dir
"""
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=10)


# ============================================================
# Structural / static checks — the constants and split themselves.
# ============================================================

check(
    "RUNTIME_DIR/RUNTIME_MYSQL_DIR/RUNTIME_BOOTSTRAP_DIR are three "
    "distinct paths, not one shared directory",
    'RUNTIME_DIR="/run/yandi"' in script_text
    and 'RUNTIME_MYSQL_DIR="${RUNTIME_DIR}/mysql"' in script_text
    and 'RUNTIME_BOOTSTRAP_DIR="${RUNTIME_DIR}/bootstrap"' in script_text,
)
check(
    "SOCKET_PATH/PID_FILE live under RUNTIME_MYSQL_DIR (systemd-managed), "
    "FRESH_INIT_MARKER lives under RUNTIME_BOOTSTRAP_DIR (root-only, "
    "install-yandi.sh-managed) — never mixed",
    'SOCKET_PATH="${RUNTIME_MYSQL_DIR}/mysql.sock"' in script_text
    and 'PID_FILE="${RUNTIME_MYSQL_DIR}/mysql.pid"' in script_text
    and 'FRESH_INIT_MARKER="${RUNTIME_BOOTSTRAP_DIR}/fresh_init_temp_password"' in script_text,
)
check(
    "yandi-db.service's RuntimeDirectory= is the nested yandi/mysql path, "
    "not bare 'yandi' (which would also cover the unrelated bootstrap "
    "directory and tear it down on every service stop)",
    "RuntimeDirectory=yandi/mysql" in unit_text
    and "RuntimeDirectory=yandi\n" not in unit_text,
)
check(
    "AppArmor local override references the socket/pid under "
    "RUNTIME_MYSQL_DIR, not the old bare RUNTIME_DIR path",
    "${RUNTIME_MYSQL_DIR}/mysql.sock rw," in script_text
    and "${RUNTIME_MYSQL_DIR}/mysql.pid rw," in script_text,
)
check(
    "reinitialize_empty_instance_guard()'s hardcoded socket-path check "
    "was updated to the new nested path",
    '"$SOCKET_PATH" = "/run/yandi/mysql/mysql.sock"' in script_text,
)
check(
    "ensure_secure_bootstrap_dir() is called both in create_filesystem() "
    "(early) and again immediately before the marker write in "
    "initialize_datadir() (defensive re-check, no dependence on exactly "
    "how systemd handles the nested RuntimeDirectory= parent)",
    script_text.count("ensure_secure_bootstrap_dir") >= 3,  # def + 2 call sites
)


# ============================================================
# Dynamic checks — ensure_secure_bootstrap_dir()'s actual logic,
# exercised against real fabricated paths (no root needed for these
# specific scenarios).
# ============================================================

with tempfile.TemporaryDirectory() as tmpdir:
    runtime_dir = str(Path(tmpdir) / "run-yandi")
    bootstrap_dir = str(Path(runtime_dir) / "bootstrap")

    # ---- Fresh creation genuinely requires root: `install -d -o root
    # -g root` fails outright ("Operation not permitted") when this
    # test runs as a non-root user, EXACTLY the same way it would for
    # any other root-only operation in this script — not a bug, just a
    # boundary this non-privileged test cannot cross (matches this
    # whole file's established posture: install-yandi.sh is verified
    # by design/logic checks, never actually executed as non-root for
    # a real privileged effect). What CAN be verified here: the
    # directory is at least created (mkdir succeeds even though the
    # chown/chmod that follows in the same `install` invocation does
    # not), so a SUBSEQUENT call has something concrete to evaluate —
    # checked next.
    _run_guard(runtime_dir=runtime_dir, bootstrap_dir=bootstrap_dir)
    check(
        "fresh creation attempt (lacking root) still leaves a real "
        "directory behind (install(1) creates the dir before its own "
        "chown/chmod step fails) — not a symlink, not left half-created "
        "in some other unexpected shape",
        Path(bootstrap_dir).is_dir() and not Path(bootstrap_dir).is_symlink(),
    )

    # ---- Idempotent re-call on that same directory — now NOT owned by
    # root (this test process's own uid, since it cannot create a real
    # root-owned fixture without actual root) — must REFUSE rather than
    # silently chmod/chown over unexpected ownership. This is the real,
    # meaningful assertion for this scenario: whatever state a failed/
    # partial privileged step leaves behind, a LATER call (e.g. the
    # operator correctly re-running this script as real root) must
    # detect the mismatch and stop, never silently "fix" it by force.
    result2 = _run_guard(runtime_dir=runtime_dir, bootstrap_dir=bootstrap_dir)
    check(
        "second call against a bootstrap dir NOT owned by root:root "
        "(this test process's own uid, since it cannot create a real "
        "root-owned fixture) correctly REFUSES rather than silently "
        "chown/chmod-ing over unexpected ownership",
        result2.returncode != 0 and "unexpected owner/mode" in result2.stderr,
        f"rc={result2.returncode} stderr={result2.stderr!r}",
    )

with tempfile.TemporaryDirectory() as tmpdir:
    # ---- Symlink at RUNTIME_DIR -> REFUSE. ----
    real_target = Path(tmpdir) / "somewhere-else"
    real_target.mkdir()
    runtime_dir_symlink = Path(tmpdir) / "run-yandi-symlink"
    runtime_dir_symlink.symlink_to(real_target)
    bootstrap_dir = str(runtime_dir_symlink / "bootstrap")

    result = _run_guard(runtime_dir=str(runtime_dir_symlink), bootstrap_dir=bootstrap_dir)
    check(
        "RUNTIME_DIR being a symlink -> REFUSE (never follow it for a "
        "security-sensitive bootstrap path)",
        result.returncode != 0 and "symlink" in result.stderr.lower(),
        f"rc={result.returncode} stderr={result.stderr!r}",
    )

with tempfile.TemporaryDirectory() as tmpdir:
    # ---- Symlink at RUNTIME_BOOTSTRAP_DIR -> REFUSE. ----
    runtime_dir = str(Path(tmpdir) / "run-yandi")
    os.makedirs(runtime_dir)
    real_target = Path(tmpdir) / "elsewhere"
    real_target.mkdir()
    bootstrap_symlink = Path(runtime_dir) / "bootstrap"
    bootstrap_symlink.symlink_to(real_target)

    result = _run_guard(runtime_dir=runtime_dir, bootstrap_dir=str(bootstrap_symlink))
    check(
        "RUNTIME_BOOTSTRAP_DIR being a symlink -> REFUSE",
        result.returncode != 0 and "symlink" in result.stderr.lower(),
        f"rc={result.returncode} stderr={result.stderr!r}",
    )

with tempfile.TemporaryDirectory() as tmpdir:
    # ---- Wrong type (a plain file, not a directory) at RUNTIME_DIR -> REFUSE. ----
    runtime_dir_as_file = str(Path(tmpdir) / "run-yandi-is-a-file")
    Path(runtime_dir_as_file).write_text("not a directory", encoding="utf-8")
    bootstrap_dir = str(Path(tmpdir) / "run-yandi-is-a-file" / "bootstrap")

    result = _run_guard(runtime_dir=runtime_dir_as_file, bootstrap_dir=bootstrap_dir)
    check(
        "RUNTIME_DIR existing as a plain FILE (not a directory) -> REFUSE",
        result.returncode != 0 and "not a directory" in result.stderr,
        f"rc={result.returncode} stderr={result.stderr!r}",
    )

with tempfile.TemporaryDirectory() as tmpdir:
    # ---- Wrong type (a plain file) at RUNTIME_BOOTSTRAP_DIR -> REFUSE. ----
    runtime_dir = str(Path(tmpdir) / "run-yandi")
    os.makedirs(runtime_dir)
    bootstrap_as_file = str(Path(runtime_dir) / "bootstrap")
    Path(bootstrap_as_file).write_text("not a directory either", encoding="utf-8")

    result = _run_guard(runtime_dir=runtime_dir, bootstrap_dir=bootstrap_as_file)
    check(
        "RUNTIME_BOOTSTRAP_DIR existing as a plain FILE -> REFUSE",
        result.returncode != 0 and "not a directory" in result.stderr,
        f"rc={result.returncode} stderr={result.stderr!r}",
    )


# ============================================================
# Requires real root to fabricate a genuine root:root 0700 fixture —
# static check that the CORRECT-ownership acceptance path exists in
# the source (the logic itself is exercised, just not against a real
# root-owned directory in this non-privileged test environment).
# ============================================================
check(
    "the function's happy-path acceptance check compares against "
    "EXACTLY root:root and mode 700 (requires live verification with "
    "a real root-owned fixture — not exercisable as a non-root test)",
    '[ "$owner" != "root:root" ] || [ "$mode" != "700" ]' in _FUNC_SRC,
)


# ============================================================
# Debug-secrets gating (mandate: fingerprint diagnostics must not
# print in production by default after this fix — explicit opt-in only).
# ============================================================
check(
    "TEMP_SOURCE_FP/MARKER_FP diagnostic prints are gated behind an "
    "explicit YANDI_INSTALL_DEBUG_SECRETS=1 opt-in, not unconditional",
    script_text.count('"${YANDI_INSTALL_DEBUG_SECRETS:-0}" = "1"') >= 2,
)


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
