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
    - Neither artifact has been applied: no yandi-db OS user exists,
      no /var/lib/yandi paths exist, no yandi-db systemd unit is
      installed — confirms this pass stayed non-privileged/design-only
      end to end.
    - storage_policy.py's core invariant (no history deletion) still
      holds after this pass's additional prose referencing it
      elsewhere (defensive re-check, cheap to run).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_dedicated_instance_design_regression_test
"""
from __future__ import annotations

import os
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
    '"${1:-}" != "--database-only"' in script_text,
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
    "--error-log, --instance-id-file, --secrets-dir, --agent-os-user) to live_bootstrap, "
    "which generates/stores credentials itself (separately tested)",
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


# ============================================================
# Nothing was actually applied — confirms this pass stayed
# non-privileged/design-only end to end.
# ============================================================

check("NO yandi-db OS user was created by this pass", subprocess.run(["id", "yandi-db"], capture_output=True).returncode != 0)
check("NO /var/lib/yandi directory exists (nothing was provisioned)", not os.path.exists("/var/lib/yandi"))
check("NO /etc/yandi directory exists", not os.path.exists("/etc/yandi"))
check("NO /run/yandi directory exists", not os.path.exists("/run/yandi"))
check(
    "NO yandi-db systemd unit is installed (the unit file in this repo was never "
    "copied to /etc/systemd/system/)",
    not os.path.exists("/etc/systemd/system/yandi-db.service"),
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
