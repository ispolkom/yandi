"""
agent/db_sql_config_drift_regression_test.py — DATABASE BOOTSTRAP V1,
tenth Phase B attempt: deploy/install-yandi.sh's install_config() drift
detection.

EXACT SERVICE FAILURE (live, confirmed via owner-collected diagnostics
— systemctl status, journalctl, error log, stat, AppArmor journal):

    systemctl status: "Error: 30 (Файловая система доступна только
    для чтения)" (EROFS)
    error log: "Could not create unix socket lock file
    /run/yandi/mysql.sock.lock. Unable to setup unix socket lock file.
    Aborting"
    my.cnf on disk: socket = /run/yandi/mysql.sock (OLD path)
    stat: /run/yandi/mysql does not exist at all
    AppArmor journal: every single line is apparmor="ALLOWED" — not
    an AppArmor denial at all.

ROOT CAUSE: an earlier fix split the single RUNTIME_DIR into
RUNTIME_MYSQL_DIR (systemd-managed, RuntimeDirectory=yandi/mysql) and
RUNTIME_BOOTSTRAP_DIR (root-only), changing SOCKET_PATH/PID_FILE to
/run/yandi/mysql/mysql.sock /.pid. But install_config()'s "if the
config file already exists, leave it in place" idempotency check had
no way to notice that the SCRIPT's own expected socket/pid paths had
changed underneath an EXISTING my.cnf written by an earlier run — the
on-disk file kept the stale /run/yandi/mysql.sock value. Under
ProtectSystem=strict, only RuntimeDirectory=-managed paths and
ReadWritePaths= are writable; /run/yandi itself (the stale value's
parent) is neither (only /run/yandi/mysql, one level deeper, is the
actual RuntimeDirectory= target) — so mysqld's attempt to create its
socket lock file directly under /run/yandi hit a genuine read-only
filesystem (EROFS), exactly matching the observed "Error: 30".

Fix: install_config() now detects drift (the existing file's socket/
pid-file lines don't match the script's CURRENT SOCKET_PATH/PID_FILE
constants) and regenerates — but never destructively: the stale file
is copied to a timestamped `.stale-<epoch>` backup first (this file's
own header says it is 100% installer-managed, no legitimate hand-edits
expected, so regeneration is safe — but nothing is ever silently lost).

install_config() is otherwise pure (writes only to $CONFIG_FILE, based
on global path constants, no systemd/mysqld dependency), so this is
fully dynamically testable without root by overriding those globals to
point at a temp file.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_config_drift_regression_test
"""
from __future__ import annotations

import glob
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
script_text = INSTALL_SCRIPT.read_text(encoding="utf-8")


def _extract_fn(name: str) -> str:
    body = script_text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
    return f"{name}() {{\n" + body + "\n}\n"


_FUNC_SRC = _extract_fn("install_config")


def _run_install_config(*, config_file: str, socket_path: str, pid_file: str):
    # Deliberately NOT `set -e`: install_config()'s final `chown
    # root:root "$CONFIG_FILE"` genuinely requires root (same
    # testability boundary as every other privileged step in this
    # script — see db_sql_runtime_bootstrap_dir_regression_test.py's
    # module docstring for the established pattern). What matters for
    # THIS test is whether the CONTENT-level drift-detection/backup/
    # regeneration logic worked, which completes before that final
    # chown — `set -e` would mask that with an unrelated privilege
    # failure at the very last line.
    snippet = f"""
set -u
log() {{ echo "[log] $*"; }}
die() {{ echo "[die] $*" >&2; exit 1; }}
YANDI_DB_USER="yandi-db"
DATADIR="/var/lib/yandi/mysql/data"
CONFIG_FILE={config_file!r}
SOCKET_PATH={socket_path!r}
PID_FILE={pid_file!r}
ERROR_LOG="/var/log/yandi/mysql-error.log"
TMPDIR_PATH="/var/lib/yandi/tmp"
{_FUNC_SRC}
install_config
"""
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=10)


# ============================================================
# 1. Fresh creation — no existing file.
# ============================================================
with tempfile.TemporaryDirectory() as tmpdir:
    config_file = str(Path(tmpdir) / "my.cnf")
    result = _run_install_config(
        config_file=config_file,
        socket_path="/run/yandi/mysql/mysql.sock",
        pid_file="/run/yandi/mysql/mysql.pid",
    )
    check(
        "1. fresh creation (no existing file) succeeds and writes the "
        "current socket/pid-file paths",
        result.returncode == 0
        and "socket          = /run/yandi/mysql/mysql.sock" in Path(config_file).read_text()
        and "pid-file        = /run/yandi/mysql/mysql.pid" in Path(config_file).read_text(),
        f"rc={result.returncode} stderr={result.stderr!r}",
    )


# ============================================================
# 2. Existing file matches current paths -> left untouched (a custom
# canary marker proves it was NOT regenerated).
# ============================================================
with tempfile.TemporaryDirectory() as tmpdir:
    config_file = str(Path(tmpdir) / "my.cnf")
    Path(config_file).write_text(
        "[mysqld]\n"
        "socket          = /run/yandi/mysql/mysql.sock\n"
        "pid-file        = /run/yandi/mysql/mysql.pid\n"
        "# CANARY-MARKER-PROVING-NOT-REGENERATED\n",
        encoding="utf-8",
    )
    result = _run_install_config(
        config_file=config_file,
        socket_path="/run/yandi/mysql/mysql.sock",
        pid_file="/run/yandi/mysql/mysql.pid",
    )
    check(
        "2. existing file whose socket/pid-file lines MATCH current "
        "constants is left untouched (canary marker survives, no "
        ".stale-* backup created)",
        result.returncode == 0
        and "CANARY-MARKER-PROVING-NOT-REGENERATED" in Path(config_file).read_text()
        and not glob.glob(config_file + ".stale-*"),
        f"rc={result.returncode} content={Path(config_file).read_text()!r}",
    )


# ============================================================
# 3. THE LIVE BUG ITSELF: existing file has STALE socket/pid-file
# paths (the pre-split /run/yandi/mysql.sock shape) -> backed up
# (nothing silently lost) AND regenerated with current paths.
# ============================================================
with tempfile.TemporaryDirectory() as tmpdir:
    config_file = str(Path(tmpdir) / "my.cnf")
    stale_content = (
        "[mysqld]\n"
        "socket          = /run/yandi/mysql.sock\n"
        "pid-file        = /run/yandi/mysql.pid\n"
    )
    Path(config_file).write_text(stale_content, encoding="utf-8")
    result = _run_install_config(
        config_file=config_file,
        socket_path="/run/yandi/mysql/mysql.sock",
        pid_file="/run/yandi/mysql/mysql.pid",
    )
    new_content = Path(config_file).read_text()
    check(
        "3. THE LIVE BUG: existing file with STALE (pre-split) socket/"
        "pid-file paths is detected as drifted and regenerated with the "
        "CURRENT paths — this is exactly what prevented the real EROFS "
        "failure (mysqld would otherwise try to create its socket lock "
        "file directly under /run/yandi, which is no longer writable "
        "under ProtectSystem=strict once only /run/yandi/mysql is the "
        "systemd RuntimeDirectory= target)",
        result.returncode == 0
        and "socket          = /run/yandi/mysql/mysql.sock" in new_content
        and "pid-file        = /run/yandi/mysql/mysql.pid" in new_content
        and "socket          = /run/yandi/mysql.sock\n" not in new_content,
        f"rc={result.returncode} new_content={new_content!r} stderr={result.stderr!r}",
    )
    backups = glob.glob(config_file + ".stale-*")
    check(
        "3b. the STALE original content is preserved in a timestamped "
        ".stale-<epoch> backup — nothing silently lost",
        len(backups) == 1 and Path(backups[0]).read_text() == stale_content,
        f"backups={backups}",
    )


# ============================================================
# 4. Only the socket line stale (pid-file happens to already match) —
# still detected as drift (both lines must match, not just one).
# ============================================================
with tempfile.TemporaryDirectory() as tmpdir:
    config_file = str(Path(tmpdir) / "my.cnf")
    Path(config_file).write_text(
        "[mysqld]\n"
        "socket          = /run/yandi/mysql.sock\n"  # stale
        "pid-file        = /run/yandi/mysql/mysql.pid\n"  # already current
        "\n",
        encoding="utf-8",
    )
    result = _run_install_config(
        config_file=config_file,
        socket_path="/run/yandi/mysql/mysql.sock",
        pid_file="/run/yandi/mysql/mysql.pid",
    )
    check(
        "4. partial drift (only the socket line stale) is still "
        "detected and triggers regeneration, not silently accepted "
        "because ONE of the two lines happened to already match",
        result.returncode == 0
        and "socket          = /run/yandi/mysql/mysql.sock" in Path(config_file).read_text(),
        f"rc={result.returncode}",
    )


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
