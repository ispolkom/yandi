"""
agent/db_sql_temp_password_capture_regression_test.py — DATABASE
BOOTSTRAP V1: deploy/install-yandi.sh's transaction-scoped temp-
password capture (_stat_log_boundary / _read_fresh_log_delta /
_extract_unique_temp_password), extracted for direct dynamic testing
the same way db_sql_ownership_proof_regression_test.py tests
verify_running_instance_ownership() — these three functions are pure
(no mysqld/systemd dependency), so they can be sourced into an
isolated bash subprocess and exercised against real fabricated files.

Live-confirmed bug this replaced (seventh Phase B attempt, after fixing
--defaults-file ordering): once log-error is correctly loaded from
$CONFIG_FILE, Percona/MySQL redirects essentially ALL of its own
logging — including the "A temporary password is generated..." NOTE —
directly into that FILE, never to the invoking process's stdout/
stderr. The installer's original approach (capture only `2>&1`) found
nothing even though --initialize completed successfully. Re-scanning
$ERROR_LOG's WHOLE historical content (an earlier, since-reverted fix)
is explicitly forbidden — it can pick up a password belonging to a
datadir that was since wiped and reinitialized.

Covers (mandate's own test list):
    1. old log has an old temp password + current init appends a new
       one -> capture ONLY the new one (delta-scoped, not whole log).
    2. old log has 10 historical password lines -> all ignored (only
       bytes after the recorded boundary are ever read).
    3. current subprocess output alone has exactly one -> accepted.
    4. current log delta alone has exactly one -> accepted.
    5. both sources contain the SAME event/password -> deduplicated
       safely (not treated as ambiguous).
    6. both sources contain CONFLICTING passwords -> FAIL.
    7. zero current events (neither source) -> FAIL.
    8. multiple DIFFERING events within the current delta -> FAIL.
    9. log file inode changes (replaced/rotated) during the window ->
       fail closed; log file shrinks (truncated) -> fail closed.
    (marker 0600/root ownership, consume-once, no-TCP, UUID-unchanged,
    shared-mysql-untouched, and "password never in installer stdout"
    are already covered by db_sql_live_bootstrap_regression_test.py,
    db_sql_dedicated_instance_design_regression_test.py, and
    db_sql_ownership_proof_regression_test.py — not duplicated here.)

Run: /home/iam/venv/bin/python3 -m agent.db_sql_temp_password_capture_regression_test
"""
from __future__ import annotations

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


_FUNCS_SRC = (
    _extract_fn("_stat_log_boundary")
    + _extract_fn("_read_fresh_log_delta")
    + _extract_fn("_extract_unique_temp_password")
)

_PW_LINE = "A temporary password is generated for root@localhost: {}"


def _stat_boundary(path: str):
    snippet = f"""
set -u
die() {{ echo "[die] $*" >&2; exit 1; }}
{_FUNCS_SRC}
_stat_log_boundary {path!r}
"""
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=10)
    parts = result.stdout.split()
    return (parts[0], parts[1]) if len(parts) == 2 else ("", "0")


def _run_capture(*, log_path: str, pre_inode: str, pre_size: str, init_output: str):
    """Simulates initialize_datadir()'s exact sequence: read delta
    (against the CURRENT file content at log_path, as if --initialize
    just ran and appended to it), then reconcile with init_output."""
    snippet = f"""
set -u
die() {{ echo "[die] $*" >&2; exit 1; }}
{_FUNCS_SRC}
delta="$(_read_fresh_log_delta {log_path!r} {pre_inode!r} {pre_size!r})" || exit 1
pw="$(_extract_unique_temp_password {init_output!r} "$delta")" || exit 1
printf '%s' "$pw"
"""
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=10)


with tempfile.TemporaryDirectory() as tmpdir:
    log_path = str(Path(tmpdir) / "mysql-error.log")

    # ---- 1 & 2: old log has historical password(s), current init
    # appends a new one -> capture ONLY the new one. ----
    Path(log_path).write_text(
        "\n".join(_PW_LINE.format(f"OLD-STALE-{i}") for i in range(10)) + "\n",
        encoding="utf-8",
    )
    pre_inode, pre_size = _stat_boundary(log_path)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("[Note] fresh startup\n")
        f.write(_PW_LINE.format("FRESH-CURRENT-PW") + "\n")
    result = _run_capture(log_path=log_path, pre_inode=pre_inode, pre_size=pre_size, init_output="")
    check(
        "1&2. old log has 10 historical temp-password lines, current "
        "init appends a new one -> capture ONLY the new one (never the "
        "historical lines, delta-scoped not whole-log-scoped)",
        result.returncode == 0 and result.stdout == "FRESH-CURRENT-PW",
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    # ---- 3: current subprocess output alone has exactly one -> accepted. ----
    Path(log_path).write_text("[Note] nothing relevant here\n", encoding="utf-8")
    pre_inode, pre_size = _stat_boundary(log_path)
    result = _run_capture(
        log_path=log_path, pre_inode=pre_inode, pre_size=pre_size,
        init_output=_PW_LINE.format("FROM-SUBPROCESS-OUTPUT"),
    )
    check(
        "3. current subprocess output alone has exactly one event -> accepted",
        result.returncode == 0 and result.stdout == "FROM-SUBPROCESS-OUTPUT",
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    # ---- 4: current log delta alone has exactly one -> accepted. ----
    Path(log_path).write_text("[Note] baseline\n", encoding="utf-8")
    pre_inode, pre_size = _stat_boundary(log_path)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(_PW_LINE.format("FROM-LOG-DELTA") + "\n")
    result = _run_capture(log_path=log_path, pre_inode=pre_inode, pre_size=pre_size, init_output="")
    check(
        "4. current log delta alone has exactly one event -> accepted",
        result.returncode == 0 and result.stdout == "FROM-LOG-DELTA",
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    # ---- 5: both sources contain the SAME event -> deduplicated safely. ----
    Path(log_path).write_text("[Note] baseline\n", encoding="utf-8")
    pre_inode, pre_size = _stat_boundary(log_path)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(_PW_LINE.format("SHARED-PW") + "\n")
    result = _run_capture(
        log_path=log_path, pre_inode=pre_inode, pre_size=pre_size,
        init_output=_PW_LINE.format("SHARED-PW"),
    )
    check(
        "5. subprocess output AND log delta contain the SAME password "
        "-> deduplicated safely, not treated as ambiguous",
        result.returncode == 0 and result.stdout == "SHARED-PW",
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    # ---- 6: both sources contain CONFLICTING passwords -> FAIL. ----
    Path(log_path).write_text("[Note] baseline\n", encoding="utf-8")
    pre_inode, pre_size = _stat_boundary(log_path)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(_PW_LINE.format("PW-FROM-LOG") + "\n")
    result = _run_capture(
        log_path=log_path, pre_inode=pre_inode, pre_size=pre_size,
        init_output=_PW_LINE.format("PW-FROM-SUBPROCESS"),
    )
    check(
        "6. subprocess output and log delta contain CONFLICTING "
        "passwords -> FAIL (ambiguous, never guess)",
        result.returncode != 0 and "DIFFERING" in result.stderr,
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    # ---- 7: zero current events (neither source) -> FAIL. ----
    Path(log_path).write_text("[Note] baseline\n", encoding="utf-8")
    pre_inode, pre_size = _stat_boundary(log_path)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("[Note] init complete, nothing password-shaped here\n")
    result = _run_capture(log_path=log_path, pre_inode=pre_inode, pre_size=pre_size, init_output="")
    check(
        "7. zero temp-password events in either source -> FAIL",
        result.returncode != 0 and "no 'temporary password' event was found" in result.stderr,
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    # ---- 8: multiple DIFFERING events within the current delta -> FAIL. ----
    Path(log_path).write_text("[Note] baseline\n", encoding="utf-8")
    pre_inode, pre_size = _stat_boundary(log_path)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(_PW_LINE.format("FIRST-EVENT") + "\n")
        f.write(_PW_LINE.format("SECOND-DIFFERENT-EVENT") + "\n")
    result = _run_capture(log_path=log_path, pre_inode=pre_inode, pre_size=pre_size, init_output="")
    check(
        "8. multiple DIFFERING temp-password events within the current "
        "delta -> FAIL (never guess first/last)",
        result.returncode != 0 and "DIFFERING" in result.stderr,
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    # ---- 9a: log file inode changes (replaced) during the window -> fail closed. ----
    # A plain unlink()+recreate at the SAME path can have the freed
    # inode number immediately reused by the filesystem (confirmed on
    # this host) — not a reliable way to force a genuinely different
    # inode. rename() over the target, exactly how real log rotation
    # (logrotate, mv) works, guarantees two independently-allocated
    # files, hence a genuinely different inode.
    Path(log_path).write_text("[Note] baseline\n", encoding="utf-8")
    pre_inode, pre_size = _stat_boundary(log_path)
    replacement = Path(log_path).with_suffix(".replacement")
    replacement.write_text(_PW_LINE.format("AFTER-REPLACE") + "\n", encoding="utf-8")
    replacement.rename(log_path)
    result = _run_capture(log_path=log_path, pre_inode=pre_inode, pre_size=pre_size, init_output="")
    check(
        "9a. log file's inode changed (replaced) during the window -> "
        "fail closed rather than guessing which content is new",
        result.returncode != 0 and "inode changed" in result.stderr,
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    # ---- 9b: log file shrinks (truncated) during the window -> fail closed. ----
    Path(log_path).write_text("[Note] a fairly long baseline line to shrink from\n", encoding="utf-8")
    pre_inode, pre_size = _stat_boundary(log_path)
    Path(log_path).write_text("x\n", encoding="utf-8")  # same-ish path, shorter content
    result = _run_capture(log_path=log_path, pre_inode=pre_inode, pre_size=pre_size, init_output="")
    check(
        "9b. log file shrank (truncated) during the window -> fail "
        "closed rather than guessing",
        result.returncode != 0 and ("shrank" in result.stderr or "inode changed" in result.stderr),
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    # ---- absent-before, present-after (first-run case): whole file is "new". ----
    fresh_path = str(Path(tmpdir) / "brand-new.log")
    pre_inode, pre_size = _stat_boundary(fresh_path)  # file doesn't exist yet
    Path(fresh_path).write_text(
        "[Note] very first log ever\n" + _PW_LINE.format("FIRST-EVER-INIT") + "\n",
        encoding="utf-8",
    )
    result = _run_capture(log_path=fresh_path, pre_inode=pre_inode, pre_size=pre_size, init_output="")
    check(
        "absent-before/present-after (the very first --initialize ever, "
        "log file created fresh) treats the WHOLE new file as the delta",
        result.returncode == 0 and result.stdout == "FIRST-EVER-INIT",
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
    )


# ============================================================
# Marker write is atomic (temp file + rename), 0600 root-owned.
# ============================================================
check(
    "marker write uses umask 077 (restrictive from the START, no window "
    "where it's briefly world/group-readable) plus atomic rename into place",
    "( umask 077; printf '%s' \"$temp_pw\" > \"$marker_tmp\" )" in script_text
    and 'mv -f "$marker_tmp" "$FRESH_INIT_MARKER"' in script_text,
)
check(
    "marker temp file is chowned root:root before the atomic rename",
    'chown root:root "$marker_tmp"' in script_text,
)
check(
    "captured secrets are unset from the shell's variable table as soon "
    "as they are no longer needed",
    "unset temp_pw init_output log_delta" in script_text,
)


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
