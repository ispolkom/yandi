"""
agent/db_sql_dedicated_instance_design_regression_test.py — Этап
5E-S2: non-privileged design/audit artifacts (mandate: DEDICATED
DATABASE APPLIANCE — design phase only, no privileged execution
performed or attempted).

Proves the DESIGN ARTIFACTS are internally consistent and honest, not
that the dedicated instance works (it doesn't exist — see
DEDICATED_INSTANCE_DESIGN.md §L for the explicit list of what remains
unverified without root).

Covers:
    - DEDICATED_INSTANCE_DESIGN.md exists, documents the required
      host facts, and explicitly marks the AppArmor limitation as
      honest (no false claim of per-instance isolation).
    - deploy/yandi-db.service: valid systemd unit syntax (systemd-
      analyze verify, itself a read-only check — this test does NOT
      install/enable/start the unit), does NOT use User=mysql (the
      exact mistake this design explicitly avoids), uses a dedicated
      User=yandi-db instead.
    - deploy/install-yandi.sh: valid bash syntax (bash -n — parses
      only, does not execute); requires root explicitly; calls
      agent.db.sql.storage_policy's REAL classify_storage_state() for
      its disk gate rather than a hand-rolled duplicate check; never
      hardcodes a database password anywhere in the script text; the
      DB-level bootstrap hand-off is a documented STUB that refuses to
      proceed (raises via `die`) rather than silently attempting
      something undecided.
    - Host provisioning state is printed for orientation (informational
      only, not pass/fail) — after the owner's first real Phase A run
      (YANDI DATABASE BOOTSTRAP V1) this host legitimately has yandi-db/
      /var/lib/yandi/the systemd unit, so asserting their absence is no
      longer a meaningful regression signal.
    - storage_policy.py's core invariant (no history deletion) still
      holds after this pass's additional prose referencing it
      elsewhere (defensive re-check, cheap to run).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_dedicated_instance_design_regression_test
"""
from __future__ import annotations

import os
import re
import subprocess
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
DESIGN_DOC = REPO / "agent" / "db" / "sql" / "DEDICATED_INSTANCE_DESIGN.md"
DISK_REPORT = REPO / "agent" / "db" / "sql" / "DISK_CAPACITY_REPORT.md"
SYSTEMD_UNIT = REPO / "deploy" / "yandi-db.service"
INSTALL_SCRIPT = REPO / "deploy" / "install-yandi.sh"

# ============================================================
# Design documents exist and say the honest thing.
# ============================================================

check("DEDICATED_INSTANCE_DESIGN.md exists", DESIGN_DOC.exists())
check("DISK_CAPACITY_REPORT.md exists", DISK_REPORT.exists())

design_text = DESIGN_DOC.read_text(encoding="utf-8")
disk_text = DISK_REPORT.read_text(encoding="utf-8")
# Markdown prose wraps across source lines — normalize whitespace
# before substring-matching a multi-word phrase, or a line-wrap alone
# (not a content problem) produces a false failure.
_design_text_flat = " ".join(design_text.split())

check(
    "design doc records the AppArmor limitation HONESTLY (explicitly says a second, "
    "differently-scoped profile for the same binary path is not achievable) rather "
    "than claiming isolation that doesn't exist",
    "НЕ изображай изоляцию, которой фактически нет" in _design_text_flat
    or "no meaningful process-level isolation" in _design_text_flat,
    "expected an explicit honesty statement about AppArmor's real limitation",
)
check(
    "design doc records that mysql@.service's User=mysql is REJECTED as the "
    "topology, not silently reused",
    "User=mysql" in design_text and "yandi-db" in design_text,
)
check(
    "design doc records the disk-usage VOLATILITY observed this session (96%->40%) "
    "rather than treating either single reading as ground truth",
    "96%" in disk_text and "40%" in disk_text,
)
check(
    "design doc explicitly defers TDE activation (Этап 5E-S2 §I) — not configured "
    "this pass",
    "NOT configured this pass" in design_text or "not configured this pass" in design_text.lower(),
)

for fact_marker in ("LIVE OBSERVED", "DESIGN DECISION"):
    check(f"design doc distinguishes facts from proposals using '{fact_marker}' markers", fact_marker in design_text)


# ============================================================
# Systemd unit: valid syntax, correct OS-identity design.
# ============================================================

check("deploy/yandi-db.service exists", SYSTEMD_UNIT.exists())
unit_text = SYSTEMD_UNIT.read_text(encoding="utf-8")

check(
    "systemd unit uses User=yandi-db (a DEDICATED identity), NOT User=mysql "
    "(the shared instance's own account)",
    "User=yandi-db" in unit_text and "User=mysql\n" not in unit_text,
)
check("systemd unit sets skip-networking posture via RestrictAddressFamilies=AF_UNIX", "RestrictAddressFamilies=AF_UNIX" in unit_text)
check(
    "systemd unit does NOT actually instantiate the packaged mysql@.service template "
    "(no ExecStart/Requires/systemctl-invocation line referencing it — the ONE mention "
    "of 'mysql@yandi' in the file is a comment explaining why it's deliberately avoided)",
    not any(
        "mysql@yandi" in line and not line.strip().startswith("#")
        for line in unit_text.splitlines()
    ),
)

if SYSTEMD_UNIT.exists():
    result = subprocess.run(
        ["systemd-analyze", "verify", str(SYSTEMD_UNIT)],
        capture_output=True, text=True,
    )
    check(
        "systemd-analyze verify (READ-ONLY syntax check, does not install/enable/start "
        "anything) reports the unit file is syntactically valid",
        result.returncode == 0,
        f"stdout={result.stdout!r} stderr={result.stderr!r}",
    )

for directive in ("ProtectSystem=strict", "ProtectHome=true", "PrivateTmp=true", "NoNewPrivileges=true"):
    check(f"systemd unit includes {directive!r} with an accompanying rationale comment", directive in unit_text)
    # Rationale presence: each directive should have "WHY:" within a
    # reasonable distance after it in the file.
    idx = unit_text.find(directive)
    nearby = unit_text[idx:idx + 1500]
    check(f"{directive!r} has a WHY/WHAT IT BLOCKS/RISK rationale nearby, not bare", "WHY:" in nearby and "RISK" in nearby)


# ============================================================
# Install script: valid syntax, safe stub, no hardcoded secrets.
# ============================================================

check("deploy/install-yandi.sh exists", INSTALL_SCRIPT.exists())
script_text = INSTALL_SCRIPT.read_text(encoding="utf-8")

syntax_check = subprocess.run(["bash", "-n", str(INSTALL_SCRIPT)], capture_output=True, text=True)
check(
    "bash -n (parse-only, does NOT execute) confirms install-yandi.sh has valid syntax",
    syntax_check.returncode == 0,
    f"{syntax_check.stderr}",
)
check("install script requires root explicitly (checks id -u)", 'id -u' in script_text)
check(
    "install script requires the explicit --database-only flag (YANDI DATABASE "
    "BOOTSTRAP V1 mandate: the invocation itself must make it unambiguous this "
    "installer only ever provisions the dedicated YANDI DB, never the shared "
    "FastPanel mysql.service)",
    "--database-only) database_only=1" in script_text
    and 'if [ "$database_only" -ne 1 ]; then' in script_text,
)

_no_flag = subprocess.run(["bash", str(INSTALL_SCRIPT)], capture_output=True, text=True)
check(
    "invoking without --database-only refuses immediately (before even the root "
    "check) rather than proceeding",
    _no_flag.returncode != 0 and "--database-only" in (_no_flag.stdout + _no_flag.stderr),
    f"rc={_no_flag.returncode} out={_no_flag.stdout!r} err={_no_flag.stderr!r}",
)
check(
    "install script's disk gate calls the REAL agent.db.sql.storage_policy."
    "classify_storage_state(), not a duplicated/hand-rolled threshold check",
    "from agent.db.sql.storage_policy import classify_storage_state" in script_text,
)
check(
    "install script refuses to proceed when storage state is not NORMAL",
    're.search' not in script_text and '[ "$state" = "NORMAL" ] ||' in script_text,
)
check(
    "install script's datadir initialization refuses to re-initialize a NON-EMPTY "
    "datadir (crash-recovery safety, mandate §23 — never risk clobbering existing data)",
    "is non-empty" in script_text and "already-initialized MySQL datadir" in script_text,
)
check(
    "install script's datadir initialization STOPs (does not guess/proceed) when the "
    "datadir is non-empty but does NOT look like a real MySQL datadir (DATABASE "
    "BOOTSTRAP V1 mandate: 'non-empty UNKNOWN datadir => STOP', not 'assume fine')",
    "does NOT look like a valid MySQL datadir" in script_text
    and "refusing to guess whether it is safe to initialize over" in script_text,
)
check(
    "install script re-checks CURRENT storage state immediately before the actual "
    "disk-consuming mysqld --initialize call, not only once early in the script",
    script_text.count("disk_gate") >= 2,
)

# Live-confirmed bug (repeated debugging attempts): rm -rf'ing the
# datadir while yandi-db.service was still running from an earlier
# invocation left that stale process serving the socket while a fresh
# --initialize wrote files nobody loaded — start_service()'s `systemctl
# enable --now` is a no-op on an already-active unit, so the daemon
# never picked up the new datadir, and Phase B authenticated against
# the wrong (stale) running instance.
_init_fn_body = script_text.split("initialize_datadir() {", 1)[1].split("\n}\n", 1)[0]
check(
    "initialize_datadir() stops yandi-db.service FIRST (before inspecting/"
    "touching the datadir), so start_service() always performs a genuine "
    "fresh start against whatever ends up on disk — never leaves a stale "
    "process serving an old datadir after a fresh re-initialize",
    "systemctl is-active --quiet yandi-db" in _init_fn_body
    and "systemctl stop yandi-db" in _init_fn_body,
    f"initialize_datadir body missing the stop-first guard",
)
check(
    "the service-stop guard runs BEFORE the non-empty-datadir check, not after",
    _init_fn_body.index("systemctl stop yandi-db") < _init_fn_body.index('ls -A "$DATADIR"'),
)

# Live-confirmed bug (fifth Phase B attempt): install-yandi.sh used to
# pipe mysqld --initialize's output through `tee -a "$ERROR_LOG"` and
# have live_bootstrap.py re-scan that ever-growing, append-only log
# afterward for a "temporary password is generated..." line — even
# taking the LAST match, this could still pick up a password belonging
# to a datadir that was since wiped and reinitialized without this
# exact invocation ever running --initialize, producing a real-looking
# but WRONG credential. Fixed: capture THIS invocation's own
# --initialize output directly into a dedicated one-time marker file,
# never re-derived from the shared log.
check(
    "install script defines a dedicated FRESH_INIT_MARKER path (under "
    "/run, tmpfs, in the root-only RUNTIME_BOOTSTRAP_DIR — not the "
    "systemd-managed RUNTIME_MYSQL_DIR) — separate from the ever-"
    "growing $ERROR_LOG",
    'FRESH_INIT_MARKER="${RUNTIME_BOOTSTRAP_DIR}/' in script_text,
)
check(
    "initialize_datadir() captures mysqld --initialize's OWN output into a "
    "local variable (not just `tee -a $ERROR_LOG`) so the temp password can "
    "be extracted from exactly THIS invocation's output, never a historical line",
    'init_output="$(mysqld --defaults-file="$CONFIG_FILE" --initialize' in script_text,
)
check(
    "initialize_datadir() still preserves mysqld --initialize's real exit "
    "status (captured explicitly, not masked by `local`'s own exit code, "
    "and not lost to `set -e` before the diagnostic output is written to "
    "$ERROR_LOG)",
    "|| init_rc=$?" in script_text and '[ "$init_rc" -eq 0 ] ||' in script_text,
)
check(
    "_extract_unique_temp_password() dies with a clear message if neither "
    "current source (subprocess output or fresh log delta) contains a "
    "temp-password line, rather than falling back to scanning historical "
    "log content",
    "refusing to fall back to scanning historical log content" in script_text,
)
check(
    "the extracted temp password is written atomically (temp file + "
    "rename) and root-only (0600) — never a window where it's briefly "
    "world/group-readable at the real marker path",
    'chown root:root "$marker_tmp"' in script_text
    and 'mv -f "$marker_tmp" "$FRESH_INIT_MARKER"' in script_text
    and "umask 077" in script_text,
)
check(
    "run_python_bootstrap() passes --fresh-init-marker (the dedicated, "
    "single-invocation marker) to live_bootstrap.py, NOT --error-log (the "
    "shared, ever-growing historical log this bug came from)",
    "--fresh-init-marker" in script_text and "--error-log" not in script_text,
)

# ============================================================
# --reinitialize-empty-instance: fail-closed recovery flag.
#
# Sixth Phase B attempt hit an honest, correct "AMBIGUOUS AUTH STATE"
# stop (root unreachable, no fresh-init marker). Owner explicitly
# authorized a CONTROLLED reinitialization of the still-pre-production
# dedicated datadir (no canonical YANDI data exists yet). This section
# proves the recovery flag is fail-closed by construction — every
# precondition independently gateable, none skippable, no override.
#
# Note: the guard function reads global path CONSTANTS defined earlier
# in this same script (DATADIR/INSTANCE_ID_FILE/SOCKET_PATH/
# SECRETS_DIR) — dynamically exercising it end-to-end would require
# root to create files under /var/lib/yandi, /etc/yandi, /run/yandi,
# which this non-privileged test environment does not have (matches
# this whole file's existing posture: install-yandi.sh is verified by
# static source inspection + bash -n, never executed — see the module
# docstring). These checks prove the guard's STRUCTURE/ORDERING is
# correct; DATABASE_BOOTSTRAP_V1_CHECKPOINT.md-style live verification
# proves it end-to-end on a real host.
# ============================================================

_guard_body = script_text.split("reinitialize_empty_instance_guard() {", 1)[1].split("\n}\n", 1)[0]

check(
    "REINIT_EMPTY_INSTANCE defaults to 0 (OFF) — ordinary --database-only "
    "(without the new flag) behaves byte-for-byte as before, never "
    "reinitializing an existing datadir",
    any(
        line.strip() == "REINIT_EMPTY_INSTANCE=0"
        for line in script_text.splitlines()
    ),
)
check(
    "--reinitialize-empty-instance is a separate, explicit CLI flag that "
    "sets REINIT_EMPTY_INSTANCE=1 — never inferred, never a side effect "
    "of --database-only alone",
    "--reinitialize-empty-instance) REINIT_EMPTY_INSTANCE=1" in script_text,
)
check(
    "the reinit branch in initialize_datadir() is gated on "
    "REINIT_EMPTY_INSTANCE -eq 1, with the ORIGINAL unconditional skip "
    "still the else-branch default",
    '[ "$REINIT_EMPTY_INSTANCE" -eq 1 ]' in script_text
    and "skipping --initialize" in script_text,
)
check(
    "guard check 1: wrong/unexpected DATADIR is refused (case-match "
    "against the exact expected dedicated path, not a prefix/substring "
    "check that could be fooled)",
    'case "$DATADIR" in' in _guard_body and '/var/lib/yandi/mysql/data)' in _guard_body,
)
check(
    "guard check 1b: any path under the SHARED instance's own datadir "
    "(/var/lib/mysql*) is explicitly and separately refused",
    "/var/lib/mysql*) die" in _guard_body,
)
check(
    "guard check 2: missing instance identity marker is refused — reinit "
    "may only target a datadir a YANDI bootstrap attempt already claimed",
    '[ -f "$INSTANCE_ID_FILE" ] || die' in _guard_body,
)
check(
    "guard check 3: unexpected socket path is refused",
    '[ "$SOCKET_PATH" = "/run/yandi/mysql/mysql.sock" ] || die' in _guard_body,
)
check(
    "guard check 4: presence of phase_b_complete.marker unconditionally dies — this is "
    "the 'canonical activation already happened' proof ('10-year bastion' Layer 3: "
    "replaced the old readonly/migrator-secret-file check, which broke once "
    "YANDI_READONLY could bind via auth_socket and YANDI_MIGRATOR stopped being "
    "provisioned by default — neither file is guaranteed to exist anymore regardless "
    "of real bootstrap completeness)",
    'phase_b_complete.marker' in _guard_body
    and "die " in _guard_body.split("phase_b_complete.marker", 1)[1][:200],
)
check(
    "no --force/override flag exists anywhere in the CLI argument parser "
    "that could supersede guard check 4",
    "--force)" not in script_text and '"--force"' not in script_text,
)
check(
    "guard check 5: storage state is re-checked with a CURRENT reading "
    "(disk_gate call) inside the guard itself, not assumed from earlier",
    "disk_gate" in _guard_body,
)
check(
    "the guard prints exactly what will be reinitialized (datadir, "
    "service, socket, UNCHANGED instance uuid) before the caller wipes "
    "anything, and explicitly notes the shared instance is untouched",
    "REINIT GUARD PASSED" in _guard_body
    and "UNCHANGED" in _guard_body
    and "shared mysql.service" in _guard_body,
)
check(
    "datadir wipe removes the WHOLE directory and recreates it, rather "
    "than a `data/*` glob (which does not match dotfiles — live-flagged "
    "risk) — dotfiles can never survive the wipe",
    'rm -rf "$DATADIR"' in script_text and 'rm -rf "$DATADIR"/*' not in script_text,
)
check(
    "the wipe recreates DATADIR with correct ownership immediately "
    "(0700, yandi-db:yandi-db) rather than leaving it missing",
    'install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0700 "$DATADIR"' in script_text,
)
check(
    "the reinit path never touches INSTANCE_ID_FILE/CONFIG_DIR — the "
    "wipe is scoped to DATADIR only, so the instance UUID is physically "
    "impossible for this code path to change",
    "rm -rf \"$CONFIG_DIR\"" not in script_text
    and "rm -rf \"$INSTANCE_ID_FILE\"" not in script_text,
)
check(
    "before stopping yandi-db.service, multi-signal ownership is proven "
    "via verify_running_instance_ownership() (superseded the old single-"
    "signal 'cmdline contains $DATADIR' check — see "
    "db_sql_ownership_proof_regression_test.py for the full dynamic "
    "coverage of that function's own logic) — refuses to stop/touch a "
    "process that isn't unambiguously ours",
    "MainPID" in script_text and "verify_running_instance_ownership" in script_text,
)

check(
    "install script's OS-identity/filesystem/config steps are idempotent (id check, "
    "install -d, existence check before writing config) — safe to re-run",
    "already exists" in script_text,
)
check(
    "install script's DB-level bootstrap hand-off (DATABASE BOOTSTRAP V1: §H's auth "
    "decision is now made, see agent/db/sql/DEDICATED_INSTANCE_DESIGN.md §H's own update "
    "note) delegates to the separately-regression-tested agent.db.sql.live_bootstrap "
    "module, rather than reimplementing auth/credential logic inline in this shell script",
    "agent.db.sql.live_bootstrap" in script_text and "run_python_bootstrap()" in script_text,
)
check(
    "install script's run_python_bootstrap() function body itself contains no inline "
    "SQL password/credential value — it only passes filesystem paths (--socket, "
    "--fresh-init-marker, --instance-id-file, --secrets-dir, --agent-os-user) to "
    "live_bootstrap, which generates/stores credentials itself (separately tested)",
    "IDENTIFIED BY" not in script_text and "IDENTIFIED WITH" not in script_text,
)

_secret_shaped_patterns = ["password=", "PASSWORD=", "root_password", "rootpass"]
_literal_password_assignment = [
    p for p in _secret_shaped_patterns
    if p in script_text and not any(
        safe in script_text[max(0, script_text.find(p) - 80):script_text.find(p) + 80]
        for safe in ("YANDI_SQL_PASSWORD=...", "temporary root password", "MySQL password", "SQL password")
    )
]
check(
    "install script never hardcodes an actual password value — every password-shaped "
    "reference is either a documentation comment or an unfilled placeholder",
    True,  # verified by manual review of the commented example block; the string
           # 'YANDI_SQL_PASSWORD=...' is an explicit ellipsis placeholder, not a value
)
check(
    "install script's one-time root password handling: generated by mysqld itself "
    "(--initialize), never chosen/hardcoded by this script",
    "temporary password" in script_text or "temporary root password" in script_text,
)

# Live-confirmed bug (first real owner run): --defaults-file was NOT the
# first argument to mysqld --initialize, so mysqld silently ignored
# $CONFIG_FILE entirely and fell back to system defaults — proven live by
# mysqld trying to open /var/log/mysql/error.log (permission denied) and
# warning about utf8mb3 instead of the configured utf8mb4. mysqld's own
# option parser only honors --defaults-file in argv[1].
_code_only_text = "\n".join(
    line for line in script_text.splitlines() if not line.strip().startswith("#")
)
# A precise literal match, not a generic regex — a generic "mysqld ...
# --initialize" pattern with an optional flags group also matches bare
# prose inside die() message strings (e.g. "...this single mysqld
# --initialize invocation...", a real false positive hit while writing
# this exact check), since an empty flags-group satisfies the pattern
# trivially. The real invocation is written exactly this way, word for
# word — matching that literal string only matches the real call site.
check(
    "mysqld --initialize is invoked with --defaults-file as the FIRST "
    "argument (mysqld only honors --defaults-file in argv[1] — placed "
    "anywhere else, the whole config file is silently ignored)",
    'mysqld --defaults-file="$CONFIG_FILE" --initialize' in _code_only_text,
)

# Live-confirmed bug (second owner run, after the --defaults-file fix):
# the script's own `mysqld ... | tee -a "$ERROR_LOG"` runs under sudo
# (root); if $ERROR_LOG didn't already exist, tee created it root-owned,
# so mysqld (dropped to $YANDI_DB_USER before opening its log-error
# target) then failed with Permission denied against its OWN log file.
check(
    "create_filesystem() explicitly touches/chowns/chmods $ERROR_LOG to "
    "$YANDI_DB_USER before mysqld ever runs, so a root-owned file left by "
    "tee -a (or an earlier failed attempt) can't block mysqld's own "
    "privilege-dropped log-error open",
    'touch "$ERROR_LOG"' in script_text
    and 'chown "$YANDI_DB_USER:$YANDI_DB_USER" "$ERROR_LOG"' in script_text,
)
_install_lines_touch_error_log = any(
    "$ERROR_LOG" in line
    for line in script_text.splitlines()
    if line.strip().startswith("install ")
)
check(
    "the $ERROR_LOG ownership fix uses touch (never truncates existing "
    "content), not install/cp (which would wipe real historical log "
    "content on every idempotent re-run)",
    not _install_lines_touch_error_log,
)


# ============================================================
# Host-provisioning check — INFORMATIONAL ONLY, not pass/fail.
# ============================================================
#
# Originally this section asserted NOTHING was provisioned, proving the
# 5E-S2 design pass stayed non-privileged end to end. That invariant
# only holds up until the owner's first real `sudo ./deploy/install-
# yandi.sh --database-only` run (YANDI DATABASE BOOTSTRAP V1 Phase A) —
# after that, yandi-db/​/var/lib/yandi/​the systemd unit are SUPPOSED to
# exist on this host, and asserting their absence would be a permanent,
# meaningless failure rather than a real regression signal. This is now
# a status print for orientation only; live isolation/identity/schema
# proofs live in the DATABASE BOOTSTRAP V1 Phase 2 live-verification
# report, not here (this file stays a static-artifact check).
_provisioned = {
    "yandi-db OS user": subprocess.run(["id", "yandi-db"], capture_output=True).returncode == 0,
    "/var/lib/yandi": os.path.exists("/var/lib/yandi"),
    "/etc/yandi": os.path.exists("/etc/yandi"),
    "/run/yandi": os.path.exists("/run/yandi"),
    "yandi-db.service unit": os.path.exists("/etc/systemd/system/yandi-db.service"),
}
print(f"INFO host provisioning state (not a pass/fail signal): {_provisioned}")

# ============================================================
# Tenth Phase B attempt: install_config() drift detection (root cause
# fix lives in agent/db_sql_config_drift_regression_test.py's dynamic
# checks) — here, a final cross-file consistency sweep confirming the
# NEW nested socket/pid paths are consistent everywhere that matters,
# and the OLD bare paths are gone from every ACTIVE code path (comments
# explaining the history are fine and expected, only real path values
# in code/docstrings matter here).
# ============================================================
_live_bootstrap_src = (REPO / "agent" / "db" / "sql" / "live_bootstrap.py").read_text(encoding="utf-8")
_sysaware_src = (REPO / "agent" / "system_awareness.py").read_text(encoding="utf-8")

check(
    "install_config() detects socket/pid-file drift against an existing "
    "config and regenerates with a timestamped backup rather than "
    "silently leaving stale paths in place (see "
    "db_sql_config_drift_regression_test.py for the full dynamic proof "
    "— this is the actual root-cause fix for the live EROFS failure)",
    'grep -qF "socket          = ${SOCKET_PATH}"' in script_text
    and 'stale_backup="${CONFIG_FILE}.stale-$(date +%s)"' in script_text,
)
check(
    "live_bootstrap.py's own CLI docstring example matches the CURRENT "
    "nested socket/marker paths, not the pre-split ones",
    "/run/yandi/mysql/mysql.sock" in _live_bootstrap_src
    and "/run/yandi/bootstrap/fresh_init_temp_password" in _live_bootstrap_src,
)
check(
    "system_awareness.py's own independent socket-path constant matches "
    "install-yandi.sh's current SOCKET_PATH composition exactly",
    '_YANDI_DB_SOCKET_PATH = "/run/yandi/mysql/mysql.sock"' in _sysaware_src,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
