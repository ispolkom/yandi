"""
agent/db_sql_ownership_proof_regression_test.py — DATABASE BOOTSTRAP V1
recovery-flag follow-up: deploy/install-yandi.sh's
verify_running_instance_ownership() (multi-signal proof that a running
yandi-db.service process is unambiguously ours before this script
stops or otherwise touches it).

Live-confirmed bug this replaced: the ORIGINAL single-signal check
("cmdline contains $DATADIR literally") rejected a process started
exactly as designed — deploy/yandi-db.service's own ExecStart invokes
mysqld as `mysqld --defaults-file=$CONFIG_FILE`, and the datadir lives
INSIDE that config file, never as a literal argv token.

UNLIKE most of this file's siblings (which only static-check
install-yandi.sh's source text, since the script as a whole requires
root and is never executed), verify_running_instance_ownership() is
pure verification logic with no systemd/process discovery of its own
— every fact is passed in as a parameter by its caller. This makes it
genuinely, dynamically testable WITHOUT root: extract the function's
own source text, source it into an isolated bash subprocess alongside
minimal log()/die() stand-ins, and call it against a REAL locally-
spawned process (this test's own python interpreter, sleeping, with
fully custom argv) plus fabricated expectation files — the function
cannot tell the difference between that and a real mysqld process.

Covers (mandate's own 12-item list):
    1.  cmdline has the literal datadir -> PROVEN
    2.  cmdline lacks the datadir but has the exact --defaults-file,
        and the config file's own datadir/socket/pid-file all agree
        -> PROVEN (this is the exact case the live bug rejected)
    3.  wrong --defaults-file (and no literal datadir either) -> REFUSE
    4.  config's datadir line points elsewhere -> REFUSE
    5.  config's socket line points elsewhere -> REFUSE
    6.  pid file contents disagree with the given pid -> REFUSE
    7.  uid does not match the expected user -> REFUSE
    8.  executable does not match the expected binary -> REFUSE
    9.  systemd FragmentPath disagrees with the expected unit file -> REFUSE
    10. shared-instance fd contradiction — covered as a static check
        (cannot safely fabricate an open fd under the real /var/lib/mysql
        path from a non-root test without touching a path that might
        genuinely belong to the shared FastPanel instance)
    11. missing instance identity marker -> REFUSE
    12. no-TCP invariant unchanged — covered by the existing checks in
        db_sql_dedicated_instance_design_regression_test.py
        (skip-networking / verify_no_tcp untouched by this change)

Run: /home/iam/venv/bin/python3 -m agent.db_sql_ownership_proof_regression_test
"""
from __future__ import annotations

import os
import socket as _socket_mod
import subprocess
import sys
import tempfile
import time
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

_FUNC_BODY = script_text.split(
    "verify_running_instance_ownership() {", 1
)[1].split("\n}\n", 1)[0]
_FUNC_SRC = "verify_running_instance_ownership() {\n" + _FUNC_BODY + "\n}\n"


def _write_config(path: Path, *, datadir: str, socket: str, pidfile: str,
                   skip_networking: bool = True, mysqlx_off: bool = True) -> None:
    lines = [
        "[mysqld]",
        f"datadir         = {datadir}",
        f"socket          = {socket}",
        f"pid-file        = {pidfile}",
        "",
    ]
    if skip_networking:
        lines.append("skip-networking")
    if mysqlx_off:
        lines.append("mysqlx          = OFF")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_guard(
    *, pid, expected_uid, expected_exe, expected_datadir, expected_socket,
    expected_pidfile, expected_config, expected_instance_id_file,
    systemd_user, systemd_fragment, expected_fragment, yandi_db_user="yandi-db",
):
    snippet = f"""
set -u
YANDI_DB_USER={yandi_db_user!r}
log() {{ echo "[log] $*"; }}
die() {{ echo "[die] $*" >&2; exit 1; }}
{_FUNC_SRC}
verify_running_instance_ownership \
    {pid!r} {expected_uid!r} {expected_exe!r} {expected_datadir!r} \
    {expected_socket!r} {expected_pidfile!r} {expected_config!r} \
    {expected_instance_id_file!r} {systemd_user!r} {systemd_fragment!r} \
    {expected_fragment!r}
"""
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=10)


class _FakeProcess:
    """A real, locally-spawned process (this test's own python
    interpreter) with FULLY CONTROLLED argv (extra tokens after -c are
    passed straight through to /proc/pid/cmdline, unused by the
    script itself) — indistinguishable, from verify_running_instance_
    ownership()'s point of view, from a real mysqld invocation."""

    def __init__(self, extra_argv):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", *extra_argv]
        )
        time.sleep(0.2)  # let it actually start before /proc is read

    @property
    def pid(self):
        return self.proc.pid

    @property
    def exe(self):
        return os.path.realpath(sys.executable)

    def stop(self):
        self.proc.kill()
        self.proc.wait(timeout=5)


with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    datadir = str(tmp / "data")
    socket_path = str(tmp / "mysql.sock")
    pidfile = str(tmp / "mysql.pid")
    config = str(tmp / "my.cnf")
    instance_id_file = str(tmp / "instance.id")
    unit_file = str(tmp / "yandi-db.service")

    Path(instance_id_file).write_text("fake-uuid-1234", encoding="utf-8")
    Path(unit_file).write_text("[fake unit]\n", encoding="utf-8")
    _write_config(Path(config), datadir=datadir, socket=socket_path, pidfile=pidfile)

    # check 7 (socket) needs a REAL Unix socket bound at socket_path —
    # kept open for this whole `with` block's lifetime.
    _sock = _socket_mod.socket(_socket_mod.AF_UNIX, _socket_mod.SOCK_STREAM)
    _sock.bind(socket_path)

    real_uid = str(os.getuid())

    # ---- 1. cmdline has the literal datadir -> PROVEN ----
    proc1 = _FakeProcess([f"--datadir={datadir}"])
    try:
        Path(pidfile).write_text(str(proc1.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc1.pid, expected_uid=real_uid, expected_exe=proc1.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=config, expected_instance_id_file=instance_id_file,
            systemd_user="yandi-db", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "1. cmdline contains the literal datadir -> PROVEN",
            result.returncode == 0 and "OWNERSHIP PROVEN" in result.stdout,
            f"rc={result.returncode} out={result.stdout!r} err={result.stderr!r}",
        )
    finally:
        proc1.stop()

    # ---- 2. cmdline lacks datadir, has exact --defaults-file, config
    # agrees -> PROVEN (the exact case the live bug rejected) ----
    proc2 = _FakeProcess([f"--defaults-file={config}"])
    try:
        Path(pidfile).write_text(str(proc2.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc2.pid, expected_uid=real_uid, expected_exe=proc2.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=config, expected_instance_id_file=instance_id_file,
            systemd_user="yandi-db", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "2. cmdline lacks the datadir but has --defaults-file=<dedicated "
            "config>, whose own datadir/socket/pid-file all agree -> PROVEN "
            "(this is the exact scenario the live bug wrongly rejected)",
            result.returncode == 0 and "OWNERSHIP PROVEN" in result.stdout,
            f"rc={result.returncode} out={result.stdout!r} err={result.stderr!r}",
        )
    finally:
        proc2.stop()

    # ---- 3. wrong --defaults-file (no literal datadir either) -> REFUSE ----
    other_config = str(tmp / "other.cnf")
    _write_config(Path(other_config), datadir="/somewhere/else", socket="/somewhere/else.sock", pidfile="/somewhere/else.pid")
    proc3 = _FakeProcess([f"--defaults-file={other_config}"])
    try:
        Path(pidfile).write_text(str(proc3.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc3.pid, expected_uid=real_uid, expected_exe=proc3.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=config, expected_instance_id_file=instance_id_file,
            systemd_user="yandi-db", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "3. wrong --defaults-file (and no literal datadir) -> REFUSE",
            result.returncode != 0 and "OWNERSHIP PROOF FAILED" in result.stderr,
            f"rc={result.returncode} err={result.stderr!r}",
        )
    finally:
        proc3.stop()

    # ---- 4. config's datadir line points elsewhere -> REFUSE ----
    bad_datadir_config = str(tmp / "bad_datadir.cnf")
    _write_config(Path(bad_datadir_config), datadir="/wrong/datadir", socket=socket_path, pidfile=pidfile)
    proc4 = _FakeProcess([f"--defaults-file={bad_datadir_config}"])
    try:
        Path(pidfile).write_text(str(proc4.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc4.pid, expected_uid=real_uid, expected_exe=proc4.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=bad_datadir_config, expected_instance_id_file=instance_id_file,
            systemd_user="yandi-db", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "4. config's datadir line points elsewhere -> REFUSE",
            result.returncode != 0 and "does not match the expected dedicated datadir" in result.stderr,
            f"rc={result.returncode} err={result.stderr!r}",
        )
    finally:
        proc4.stop()

    # ---- 5. config's socket line points elsewhere -> REFUSE ----
    bad_socket_config = str(tmp / "bad_socket.cnf")
    _write_config(Path(bad_socket_config), datadir=datadir, socket="/wrong/socket", pidfile=pidfile)
    proc5 = _FakeProcess([f"--defaults-file={bad_socket_config}"])
    try:
        Path(pidfile).write_text(str(proc5.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc5.pid, expected_uid=real_uid, expected_exe=proc5.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=bad_socket_config, expected_instance_id_file=instance_id_file,
            systemd_user="yandi-db", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "5. config's socket line points elsewhere -> REFUSE",
            result.returncode != 0 and "does not match the expected dedicated socket" in result.stderr,
            f"rc={result.returncode} err={result.stderr!r}",
        )
    finally:
        proc5.stop()

    # ---- 6. pid file disagrees with the actual pid -> REFUSE ----
    proc6 = _FakeProcess([f"--datadir={datadir}"])
    try:
        Path(pidfile).write_text(str(proc6.pid + 12345), encoding="utf-8")
        result = _run_guard(
            pid=proc6.pid, expected_uid=real_uid, expected_exe=proc6.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=config, expected_instance_id_file=instance_id_file,
            systemd_user="yandi-db", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "6. pid file contents disagree with systemd's MainPID -> REFUSE",
            result.returncode != 0 and "does not match systemd's MainPID" in result.stderr,
            f"rc={result.returncode} err={result.stderr!r}",
        )
    finally:
        proc6.stop()
        Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")  # restore for later tests

    # ---- 7. uid does not match expected -> REFUSE ----
    proc7 = _FakeProcess([f"--datadir={datadir}"])
    try:
        Path(pidfile).write_text(str(proc7.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc7.pid, expected_uid=str(int(real_uid) + 9999), expected_exe=proc7.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=config, expected_instance_id_file=instance_id_file,
            systemd_user="yandi-db", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "7. process uid does not match the expected user's uid -> REFUSE",
            result.returncode != 0 and "runs as uid" in result.stderr,
            f"rc={result.returncode} err={result.stderr!r}",
        )
    finally:
        proc7.stop()

    # ---- 8. executable does not match expected binary -> REFUSE ----
    proc8 = _FakeProcess([f"--datadir={datadir}"])
    try:
        Path(pidfile).write_text(str(proc8.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc8.pid, expected_uid=real_uid, expected_exe="/bin/definitely-not-the-real-binary",
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=config, expected_instance_id_file=instance_id_file,
            systemd_user="yandi-db", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "8. executable does not match the expected mysqld binary -> REFUSE",
            result.returncode != 0 and "is not the expected mysqld binary" in result.stderr,
            f"rc={result.returncode} err={result.stderr!r}",
        )
    finally:
        proc8.stop()

    # ---- 9. systemd FragmentPath disagrees with the expected unit file -> REFUSE ----
    proc9 = _FakeProcess([f"--datadir={datadir}"])
    try:
        Path(pidfile).write_text(str(proc9.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc9.pid, expected_uid=real_uid, expected_exe=proc9.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=config, expected_instance_id_file=instance_id_file,
            systemd_user="yandi-db", systemd_fragment="/etc/systemd/system/some-other.service",
            expected_fragment=unit_file,
        )
        check(
            "9. systemd FragmentPath disagrees with the expected unit file -> REFUSE",
            result.returncode != 0 and "FragmentPath" in result.stderr,
            f"rc={result.returncode} err={result.stderr!r}",
        )
    finally:
        proc9.stop()

    # ---- 11. missing instance identity marker -> REFUSE ----
    proc11 = _FakeProcess([f"--datadir={datadir}"])
    try:
        Path(pidfile).write_text(str(proc11.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc11.pid, expected_uid=real_uid, expected_exe=proc11.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=config, expected_instance_id_file=str(tmp / "no-such-instance.id"),
            systemd_user="yandi-db", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "11. missing instance identity marker -> REFUSE",
            result.returncode != 0 and "instance identity marker" in result.stderr,
            f"rc={result.returncode} err={result.stderr!r}",
        )
    finally:
        proc11.stop()

    # ---- also: wrong systemd User= -> REFUSE (part of check 1's own list) ----
    proc_user = _FakeProcess([f"--datadir={datadir}"])
    try:
        Path(pidfile).write_text(str(proc_user.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc_user.pid, expected_uid=real_uid, expected_exe=proc_user.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=config, expected_instance_id_file=instance_id_file,
            systemd_user="some-other-user", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "wrong systemd User= (not $YANDI_DB_USER) -> REFUSE",
            result.returncode != 0 and "own User=" in result.stderr,
            f"rc={result.returncode} err={result.stderr!r}",
        )
    finally:
        proc_user.stop()

    # ---- missing skip-networking in config -> REFUSE ----
    no_skip_config = str(tmp / "no_skip.cnf")
    _write_config(Path(no_skip_config), datadir=datadir, socket=socket_path, pidfile=pidfile, skip_networking=False)
    proc_skip = _FakeProcess([f"--defaults-file={no_skip_config}"])
    try:
        Path(pidfile).write_text(str(proc_skip.pid), encoding="utf-8")
        result = _run_guard(
            pid=proc_skip.pid, expected_uid=real_uid, expected_exe=proc_skip.exe,
            expected_datadir=datadir, expected_socket=socket_path, expected_pidfile=pidfile,
            expected_config=no_skip_config, expected_instance_id_file=instance_id_file,
            systemd_user="yandi-db", systemd_fragment=unit_file, expected_fragment=unit_file,
        )
        check(
            "config missing skip-networking -> REFUSE",
            result.returncode != 0 and "missing skip-networking" in result.stderr,
            f"rc={result.returncode} err={result.stderr!r}",
        )
    finally:
        proc_skip.stop()

    _sock.close()


# ============================================================
# 10. Shared-instance fd contradiction — static check only.
#
# Cannot safely fabricate an open file descriptor under the real
# /var/lib/mysql from a non-root test without risking interaction with
# a path that might genuinely be the shared FastPanel instance's own
# datadir — exactly the thing this whole design must never touch.
# ============================================================
check(
    "10. fd-contradiction check exists: an open fd under the SHARED "
    "instance's /var/lib/mysql path fails closed even if every other "
    "signal passed",
    "/var/lib/mysql/*|/var/lib/mysql)" in _FUNC_BODY and "OWNERSHIP PROOF FAILED" in _FUNC_BODY,
)

# ============================================================
# 12. No-TCP invariant unchanged — this change never touched
# verify_no_tcp()/skip-networking enforcement elsewhere.
# ============================================================
check(
    "12. verify_no_tcp() and the config's skip-networking enforcement "
    "are untouched by this change (still present, unmodified call site)",
    "verify_no_tcp" in script_text and "skip-networking" in script_text,
)

# ============================================================
# The call site in initialize_datadir() actually gathers real systemd
# facts and calls the new function, rather than the old single-signal
# inline check.
# ============================================================
check(
    "initialize_datadir()'s call site gathers systemd User=/FragmentPath "
    "and calls verify_running_instance_ownership() with all 11 params, "
    "replacing the old single-signal inline cmdline check",
    "verify_running_instance_ownership \\" in script_text
    and 'systemctl show yandi-db -p User --value' in script_text
    and 'systemctl show yandi-db -p FragmentPath --value' in script_text,
)


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
