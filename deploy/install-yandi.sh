#!/usr/bin/env bash
#
# deploy/install-yandi.sh — Этап 5E-S2 §K design artifact.
#
# ============================================================
# THIS SCRIPT HAS NEVER BEEN RUN. IT IS A DESIGN ARTIFACT.
# ============================================================
#
# It was written during a NON-PRIVILEGED audit/design pass that had no
# sudo/root access in its execution environment (confirmed: `sudo -n`
# fails, no NOPASSWD rule exists) and was explicitly instructed not to
# request, guess, or work around that. Every step below is a PLAN,
# reviewed against this host's real, audited facts (agent/db/sql/
# DEDICATED_INSTANCE_DESIGN.md), not a proven-working installer.
#
# Before running this for real: read DEDICATED_INSTANCE_DESIGN.md §L
# ("Live Deployment Gate") — it lists exactly what remains unverified
# until this script actually runs once, live, on this host.
#
# Usage (once reviewed and accepted by the owner):
#     sudo ./deploy/install-yandi.sh
#
# The human operator enters their OWN sudo password ONCE, for the OS
# installation step — never a MySQL password (mandate §29: "НИКАКОГО
# PLAINTEXT SQL PASSWORD UX"). This script never asks for or displays
# a database credential.

set -euo pipefail

# ── Configuration (all paths from DEDICATED_INSTANCE_DESIGN.md §C) ──
YANDI_DB_USER="yandi-db"
YANDI_DB_HOME="/var/lib/yandi"
DATADIR="${YANDI_DB_HOME}/mysql/data"
KEYRING_DIR="${YANDI_DB_HOME}/mysql-keyring"
TMPDIR_PATH="${YANDI_DB_HOME}/tmp"
INTEGRITY_DIR="${YANDI_DB_HOME}/integrity"
# Live-confirmed bug (ninth Phase B attempt): RUNTIME_DIR used to be a
# single directory (/run/yandi) that was BOTH the systemd
# RuntimeDirectory= for mysqld's socket/pid AND where this script wrote
# the bootstrap secret marker. systemd tears down RuntimeDirectory=
# entirely on `systemctl stop` (RuntimeDirectoryPreserve= defaults to
# "no") — so initialize_datadir()'s own "stop yandi-db.service before
# touching the datadir" step (an earlier fix) silently deleted the
# whole directory, and the later marker write failed with "No such
# file or directory". Splitting into two subdirectories under the same
# stable parent fixes the lifecycle bug AND closes a real TOCTOU risk
# the single-directory design had: mysqld runs as $YANDI_DB_USER, and
# if that account owned (or could write into) the SAME directory a
# root-owned secret lived in, it could delete/replace the marker out
# from under this script even though it could never read the 0600
# file's contents directly.
#
#   RUNTIME_DIR                stable parent, root:root, created by
#                               this script (create_filesystem()),
#                               NEVER declared as a systemd
#                               RuntimeDirectory= target itself, so it
#                               is untouched by any service stop/start.
#   RUNTIME_MYSQL_DIR           systemd-managed (RuntimeDirectory=
#                               yandi/mysql in yandi-db.service),
#                               owned yandi-db:yandi-db, torn down/
#                               recreated with the service's own
#                               lifecycle exactly as before — socket
#                               and pid file live here, unchanged
#                               otherwise.
#   RUNTIME_BOOTSTRAP_DIR       root:root 0700, created/verified by
#                               THIS script only, completely
#                               inaccessible to $YANDI_DB_USER — the
#                               one-time temp-password marker lives
#                               here, and survives every mysqld
#                               service stop/start since it is never
#                               part of any RuntimeDirectory=.
RUNTIME_DIR="/run/yandi"
RUNTIME_MYSQL_DIR="${RUNTIME_DIR}/mysql"
RUNTIME_BOOTSTRAP_DIR="${RUNTIME_DIR}/bootstrap"
CONFIG_DIR="/etc/yandi/mysql"
CONFIG_FILE="${CONFIG_DIR}/my.cnf"
LOG_DIR="/var/log/yandi"
ERROR_LOG="${LOG_DIR}/mysql-error.log"
SOCKET_PATH="${RUNTIME_MYSQL_DIR}/mysql.sock"
PID_FILE="${RUNTIME_MYSQL_DIR}/mysql.pid"
SYSTEMD_UNIT_SRC="$(dirname "$0")/yandi-db.service"
SYSTEMD_UNIT_DST="/etc/systemd/system/yandi-db.service"
APPARMOR_LOCAL_DIR="/etc/apparmor.d/local"
APPARMOR_LOCAL_FILE="${APPARMOR_LOCAL_DIR}/usr.sbin.mysqld"
APPARMOR_SHARED_PROFILE="/etc/apparmor.d/usr.sbin.mysqld"
KEK_PATH="${YANDI_DB_HOME}/keys/kek.bin"
SECRETS_DIR="${YANDI_DB_HOME}/keys"
INSTANCE_ID_FILE="${CONFIG_DIR}/instance.id"
# One-time marker for THIS invocation's own mysqld --initialize temp
# password — see initialize_datadir()/run_python_bootstrap(). Under
# /run (tmpfs), so it can never outlive a reboot by accident, in the
# root-only bootstrap subdirectory (see RUNTIME_BOOTSTRAP_DIR above,
# never RUNTIME_MYSQL_DIR/$YANDI_DB_USER-writable space). Never the
# shared, ever-growing $ERROR_LOG: live_bootstrap.py must consume ONLY
# the password THIS run actually generated, never scrape historical
# log lines from earlier attempts (live-confirmed bug: a stale historical
# password looks exactly like a valid one and produces a confusing
# "Access denied" instead of a clear diagnostic).
FRESH_INIT_MARKER="${RUNTIME_BOOTSTRAP_DIR}/fresh_init_temp_password"
YANDI_REPO="/home/iam/yandi"
YANDI_VENV_PYTHON="/home/iam/venv/bin/python3"
# SECURITY ("10-year bastion" hardening, owner mandate): a DEDICATED
# system account for the real YANDI process — never the operator's own
# interactive login. This used to be "iam", the SAME account any
# interactive shell, Claude Code session, or other coding-assistant
# session runs as — auth_socket authenticates by OS peer UID alone, so
# it could never tell "the real daemon" apart from "a human/assistant
# logged in as iam." YANDI_RUNTIME is created with auth_socket mapped
# to THIS name; create_os_identity() creates the account itself
# (system account, no login shell, no password) if it doesn't exist.
AGENT_OS_USER="yandi-agent"
# SECURITY ("10-year bastion" Layer 3, owner mandate): the owner's own
# personal OS login — YANDI_READONLY binds to THIS via auth_socket
# instead of a stored password (mandate: "видеть трейсы, видеть
# запрос" — the owner keeps read-only visibility, just never a
# password file to leak). Deliberately the SAME account AGENT_OS_USER
# used to be ("iam") — the point here is the OPPOSITE of AGENT_OS_USER's
# separation: this is precisely the interactive human login, on
# purpose, because this role is for the human.
OWNER_OS_USER="iam"

# Set by main()'s argument parsing — default OFF. See
# reinitialize_empty_instance_guard() for the fail-closed conditions
# that must ALL hold before this flag has any effect at all.
REINIT_EMPTY_INSTANCE=0

log() { echo "[install-yandi] $*"; }
die() { echo "[install-yandi] FATAL: $*" >&2; exit 1; }

# Short, ONE-WAY, non-secret diagnostic fingerprint (first 12 hex
# chars of SHA-256) — safe to print/log, never reversible. Same format
# as agent/db/sql/live_bootstrap.py's _fingerprint(), so the two can be
# compared directly in console output without ever exposing the real
# secret (mandate: only length + fingerprint, never the value itself).
_temp_pw_fp() {
    printf '%s' "$1" | sha256sum | cut -c1-12
}

# ============================================================
# 1. PRECHECK
# ============================================================
precheck() {
    [ "$(id -u)" -eq 0 ] || die "must run as root (sudo ./install-yandi.sh)"

    command -v mysqld >/dev/null 2>&1 || die "mysqld not found — is percona-server-server installed?"

    local version
    version="$(mysqld --version | grep -oP 'Ver \K[0-9]+\.[0-9]+' || true)"
    case "$version" in
        8.0) log "Percona 8.0.x detected ($version) — matches this design's target." ;;
        *)   die "unexpected Percona major.minor version '$version' — this design was written against 8.0.x (this host had 8.0.46-37 at audit time). Refusing to proceed against an unverified version rather than guessing compatibility." ;;
    esac

    [ -f "$SYSTEMD_UNIT_SRC" ] || die "expected $SYSTEMD_UNIT_SRC next to this script"
    [ -f "$APPARMOR_SHARED_PROFILE" ] || log "WARNING: $APPARMOR_SHARED_PROFILE not found — AppArmor step will be skipped (no profile to extend)"
}

# ============================================================
# 2. DISK GATE — reuses agent.db.sql.storage_policy, not a
#    hand-rolled duplicate threshold check.
# ============================================================
disk_gate() {
    local target_fs
    target_fs="$(df --output=target "$YANDI_DB_HOME" 2>/dev/null | tail -1 || df --output=target / | tail -1)"

    local state
    state="$("$YANDI_VENV_PYTHON" - "$target_fs" <<'PYEOF'
import sys, shutil
sys.path.insert(0, "/home/iam/yandi")
from agent.db.sql.storage_policy import classify_storage_state, StorageState
target = sys.argv[1] if len(sys.argv) > 1 else "/"
usage = shutil.disk_usage(target)
print(classify_storage_state(free_bytes=usage.free, total_bytes=usage.total))
PYEOF
)"
    log "storage_policy state for install target: $state"
    [ "$state" = "NORMAL" ] || die "refusing to initialize a new datadir while storage state is '$state' (mandate: never provision new persistence on a disk that isn't NORMAL). Free up space or choose a different target and re-run."
}

# ============================================================
# SECURE BOOTSTRAP MARKER DIRECTORY
#
# root:root 0700, completely inaccessible to $YANDI_DB_USER — the
# one-time temp-password marker (FRESH_INIT_MARKER) lives here,
# deliberately separate from RUNTIME_MYSQL_DIR (systemd-managed,
# yandi-db:yandi-db, torn down on every service stop — mysqld's own
# OS account owns/can write into that directory) so mysqld can never
# delete/replace/race the marker even though it could never read the
# 0600 file's contents directly, and so this directory survives every
# mysqld service stop/start untouched (it is never declared as a
# systemd RuntimeDirectory= target).
#
# Idempotent AND paranoid: verifies canonical path, rejects a symlink
# or wrong-type entry at either RUNTIME_DIR or RUNTIME_BOOTSTRAP_DIR
# rather than blindly chown/chmod-ing over something unexpected
# (mandate: reject symlink, reject unexpected owner/type, fail closed
# rather than guess). Called both early (create_filesystem()) and
# again immediately before the marker write in initialize_datadir()
# — cheap, and removes any dependence on exactly how systemd handles
# an implied intermediate directory for a nested RuntimeDirectory=
# path, which this script does not assume without live verification.
# ============================================================
ensure_secure_bootstrap_dir() {
    if [ -L "$RUNTIME_DIR" ]; then
        die "SECURITY: $RUNTIME_DIR is a symlink — refusing to follow it for a security-sensitive bootstrap path."
    fi
    if [ -e "$RUNTIME_DIR" ] && [ ! -d "$RUNTIME_DIR" ]; then
        die "SECURITY: $RUNTIME_DIR exists but is not a directory (unexpected type) — refusing to proceed."
    fi
    install -d -o root -g root -m 0755 "$RUNTIME_DIR"

    if [ -L "$RUNTIME_BOOTSTRAP_DIR" ]; then
        die "SECURITY: $RUNTIME_BOOTSTRAP_DIR is a symlink — refusing to follow it for a security-sensitive bootstrap path."
    fi
    if [ -e "$RUNTIME_BOOTSTRAP_DIR" ]; then
        if [ ! -d "$RUNTIME_BOOTSTRAP_DIR" ]; then
            die "SECURITY: $RUNTIME_BOOTSTRAP_DIR exists but is not a directory (unexpected type) — refusing to proceed."
        fi
        local owner mode
        owner="$(stat -c '%U:%G' "$RUNTIME_BOOTSTRAP_DIR")"
        mode="$(stat -c '%a' "$RUNTIME_BOOTSTRAP_DIR")"
        if [ "$owner" != "root:root" ] || [ "$mode" != "700" ]; then
            die "SECURITY: $RUNTIME_BOOTSTRAP_DIR has unexpected owner/mode ($owner $mode, expected root:root 700) — refusing to chown/chmod over an unexpected existing directory. Investigate manually rather than assume it is safe."
        fi
    else
        install -d -o root -g root -m 0700 "$RUNTIME_BOOTSTRAP_DIR"
    fi
}

# ============================================================
# 3. CREATE OS IDENTITY (idempotent)
# ============================================================
create_os_identity() {
    if id "$YANDI_DB_USER" >/dev/null 2>&1; then
        log "OS user $YANDI_DB_USER already exists — skipping useradd"
    else
        useradd --system --no-create-home --shell /usr/sbin/nologin \
            --home-dir "$YANDI_DB_HOME" "$YANDI_DB_USER"
        log "created system user $YANDI_DB_USER"
    fi

    # SECURITY ("10-year bastion"): the dedicated identity the real
    # YANDI agent process runs as — see AGENT_OS_USER's own comment at
    # the top of this script for why this must never be an interactive
    # operator/assistant login. No shell, no home directory, no
    # password — this account exists ONLY to be peer-UID-matched by
    # auth_socket and to run the orchestrator's own systemd service
    # (see deploy/yandi-orchestrator.service).
    if id "$AGENT_OS_USER" >/dev/null 2>&1; then
        log "OS user $AGENT_OS_USER already exists — skipping useradd"
    else
        useradd --system --no-create-home --shell /usr/sbin/nologin "$AGENT_OS_USER"
        log "created system user $AGENT_OS_USER (dedicated YANDI agent identity)"
    fi
    # Group membership is what actually lets this identity traverse
    # RUNTIME_MYSQL_DIR (0750, owned $YANDI_DB_USER:$YANDI_DB_USER) to
    # reach the dedicated socket at all — idempotent, usermod -aG is a
    # safe no-op when the membership already exists.
    usermod -aG "$YANDI_DB_USER" "$AGENT_OS_USER"
}

# ============================================================
# 4. CREATE FILESYSTEM (idempotent — mkdir -p, chown/chmod are
#    safe to re-apply)
# ============================================================
create_filesystem() {
    install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0750 "$YANDI_DB_HOME"
    install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0750 "$(dirname "$DATADIR")"
    install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0700 "$DATADIR"
    install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0700 "$KEYRING_DIR"
    install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0750 "$TMPDIR_PATH"
    install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0750 "$INTEGRITY_DIR"
    # RUNTIME_DIR itself: stable root:root parent, NOT a systemd
    # RuntimeDirectory= target (that's RUNTIME_MYSQL_DIR, declared in
    # yandi-db.service) — created here so it exists independent of
    # mysqld's own service lifecycle. Mode 0755: nothing sensitive
    # lives directly in it, only the two subdirectories below, each
    # with their own restrictive mode.
    install -d -o root -g root -m 0755 "$RUNTIME_DIR"
    # RUNTIME_MYSQL_DIR is NOT created here — it is systemd's
    # RuntimeDirectory= responsibility (created on service start, torn
    # down on stop, owned yandi-db:yandi-db). Creating it here too
    # would just be immediately superseded/removed by systemd anyway.
    ensure_secure_bootstrap_dir
    install -d -o root -g root -m 0755 "$CONFIG_DIR"
    install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0750 "$LOG_DIR"
    install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0700 "$(dirname "$KEK_PATH")"

    # Live-confirmed bug (second owner run, after the --defaults-file
    # fix): this script's own `mysqld ... | tee -a "$ERROR_LOG"` in
    # initialize_datadir() runs under sudo (root), so if $ERROR_LOG did
    # not already exist, tee created it root-owned. mysqld itself drops
    # privileges to $YANDI_DB_USER (--user=) BEFORE opening its
    # log-error target, so it then fails with Permission denied against
    # a root-owned file. `touch` (not `install`, which would truncate
    # any real historical log content on every idempotent re-run) plus
    # explicit chown/chmod fixes ownership unconditionally, whether the
    # file is new or was left root-owned by an earlier failed attempt.
    touch "$ERROR_LOG"
    chown "$YANDI_DB_USER:$YANDI_DB_USER" "$ERROR_LOG"
    chmod 0640 "$ERROR_LOG"

    log "filesystem layout ready under $YANDI_DB_HOME, $CONFIG_DIR, $RUNTIME_DIR, $LOG_DIR"
}

# ============================================================
# 5. INSTALL CONFIG
# ============================================================
install_config() {
    if [ -f "$CONFIG_FILE" ]; then
        if grep -qF "socket          = ${SOCKET_PATH}" "$CONFIG_FILE" \
            && grep -qF "pid-file        = ${PID_FILE}" "$CONFIG_FILE"; then
            log "config already exists at $CONFIG_FILE and matches current socket/pid-file paths — leaving in place (idempotent; edit manually to change)"
            return
        fi
        # Live-confirmed bug (tenth Phase B attempt): the runtime-
        # directory split (RUNTIME_MYSQL_DIR vs RUNTIME_BOOTSTRAP_DIR,
        # an earlier fix) changed this script's own SOCKET_PATH/
        # PID_FILE constants, but the OLD idempotent "leave in place if
        # it exists" check never noticed the drift — the on-disk
        # my.cnf kept pointing mysqld at the stale /run/yandi/mysql.sock
        # (no longer inside any systemd RuntimeDirectory= or
        # ReadWritePaths= target under ProtectSystem=strict), causing a
        # real "Read-only file system" (EROFS) failure trying to create
        # the socket lock file there. This file's own header says it is
        # 100% installer-managed (no legitimate hand-edits expected —
        # "DO NOT EDIT by hand"), so regenerating is safe, not a guess
        # — but a timestamped backup is kept regardless (never destroy
        # without a trace, mandate discipline throughout this session).
        local stale_backup="${CONFIG_FILE}.stale-$(date +%s)"
        cp -p "$CONFIG_FILE" "$stale_backup"
        log "$CONFIG_FILE has STALE socket/pid-file paths not matching this script's current constants — backed up to $stale_backup and regenerating with current paths"
    fi
    cat > "$CONFIG_FILE" <<EOF
# YANDI dedicated instance — generated by install-yandi.sh
# DO NOT EDIT by hand for routine changes; re-run the installer's
# config step after updating deploy/install-yandi.sh instead, so the
# source of truth stays the repo, not a drifted live file.
[mysqld]
user            = ${YANDI_DB_USER}
datadir         = ${DATADIR}
socket          = ${SOCKET_PATH}
pid-file        = ${PID_FILE}
log-error       = ${ERROR_LOG}
tmpdir          = ${TMPDIR_PATH}

# mandate §3/§F: zero TCP surface, not just loopback-bound.
skip-networking
mysqlx          = OFF

# Unicode by default (mandate §10) — never depend on server defaults.
character-set-server = utf8mb4
collation-server      = utf8mb4_unicode_ci

# Keyring (mandate §12/§13 of Этап 5E-S2 §I) — component-based,
# matching this host's actual Percona 8.0.x build, NOT enabled yet by
# this script (see configure_keyring(), a separate, later step this
# script does not call automatically — TDE activation is a deliberate,
# reviewed decision, not a side effect of installation).
EOF
    chown root:root "$CONFIG_FILE"
    chmod 0644 "$CONFIG_FILE"
    log "wrote $CONFIG_FILE"
}

# ============================================================
# 5b. INSTANCE IDENTITY MARKER (mandate §4/§8) — written BEFORE
#     mysqld --initialize, so the ownership marker predates the datadir
#     it will end up describing. Idempotent: reuses agent.db.sql.
#     instance_identity.ensure_instance_id_file(), which returns the
#     EXISTING id unchanged if the file is already there (never
#     regenerates over a prior identity).
# ============================================================
create_instance_identity_marker() {
    "$YANDI_VENV_PYTHON" - "$INSTANCE_ID_FILE" <<'PYEOF'
import sys
sys.path.insert(0, "/home/iam/yandi")
from agent.db.sql.instance_identity import ensure_instance_id_file
instance_id = ensure_instance_id_file(sys.argv[1])
print(f"instance identity: {instance_id}")
PYEOF
    chown root:root "$INSTANCE_ID_FILE"
    chmod 0644 "$INSTANCE_ID_FILE"
}

# ============================================================
# 6. APPARMOR — additive only, never touches the shared profile's
#    existing rules, only adds an include point + a local file.
# ============================================================
apparmor_setup() {
    [ -f "$APPARMOR_SHARED_PROFILE" ] || { log "no shared AppArmor profile found — skipping"; return; }

    mkdir -p "$APPARMOR_LOCAL_DIR"
    if [ ! -f "$APPARMOR_LOCAL_FILE" ]; then
        cat > "$APPARMOR_LOCAL_FILE" <<EOF
# Local AppArmor override for YANDI's dedicated mysqld paths.
# Additive only — this file EXTENDS, never restricts, the shared
# profile at ${APPARMOR_SHARED_PROFILE}. See
# agent/db/sql/DEDICATED_INSTANCE_DESIGN.md §G for why a fully separate
# profile for the same /usr/sbin/mysqld binary path isn't possible.
${DATADIR}/ r,
${DATADIR}/** rwk,
${KEYRING_DIR}/ r,
${KEYRING_DIR}/** rwk,
${TMPDIR_PATH}/ r,
${TMPDIR_PATH}/** rwk,
${LOG_DIR}/ r,
${LOG_DIR}/** rw,
${RUNTIME_MYSQL_DIR}/mysql.sock rw,
${RUNTIME_MYSQL_DIR}/mysql.pid rw,
${CONFIG_DIR}/ r,
${CONFIG_DIR}/** r,
EOF
        log "wrote $APPARMOR_LOCAL_FILE"
    else
        log "$APPARMOR_LOCAL_FILE already exists — leaving in place"
    fi

    if ! grep -q "include.*local/usr.sbin.mysqld" "$APPARMOR_SHARED_PROFILE"; then
        log "WARNING: $APPARMOR_SHARED_PROFILE has no '#include <local/usr.sbin.mysqld>' line."
        log "This script does NOT add one automatically — that is a one-line edit to a"
        log "file this design considers 'shared, do not touch without deliberate review'"
        log "(DEDICATED_INSTANCE_DESIGN.md §G). Add it manually, then re-run, or accept"
        log "that AppArmor (currently complain-mode anyway) will not see these extra paths."
    fi

    if command -v apparmor_parser >/dev/null 2>&1; then
        apparmor_parser -r "$APPARMOR_SHARED_PROFILE" || log "WARNING: apparmor_parser reload failed — check syntax manually before relying on this"
    fi
}

# ============================================================
# 7. SYSTEMD
# ============================================================
install_systemd_unit() {
    cp "$SYSTEMD_UNIT_SRC" "$SYSTEMD_UNIT_DST"
    systemctl daemon-reload
    log "installed $SYSTEMD_UNIT_DST"
}

# ============================================================
# 7b. RECOVERY GUARD — --reinitialize-empty-instance
#
# Only reachable from initialize_datadir() when the datadir already
# looks like a valid, previously-initialized MySQL datadir AND the
# owner explicitly passed --reinitialize-empty-instance. Every check
# below is independent and must ALL pass; any single failure refuses
# (die) rather than proceeding partially or guessing. This function
# NEVER deletes anything itself — it only decides whether the caller
# is allowed to.
# ============================================================
reinitialize_empty_instance_guard() {
    # 1. Target is unambiguously OUR dedicated path, never the shared
    #    instance's own datadir (defense in depth — DATADIR is a fixed
    #    constant above, this just refuses to proceed if that constant
    #    is ever pointed somewhere unexpected in the future).
    case "$DATADIR" in
        /var/lib/yandi/mysql/data) ;;
        *) die "REFUSING --reinitialize-empty-instance: DATADIR ('$DATADIR') is not the expected dedicated path." ;;
    esac
    case "$DATADIR" in
        /var/lib/mysql*) die "REFUSING --reinitialize-empty-instance: DATADIR resolves under the SHARED instance's path — absolute stop." ;;
    esac

    # 2. The dedicated instance identity marker must already exist — we
    #    are only ever reinitializing something a YANDI bootstrap
    #    attempt already claimed, never an unknown/unclaimed directory.
    [ -f "$INSTANCE_ID_FILE" ] || die "REFUSING --reinitialize-empty-instance: no instance identity marker at $INSTANCE_ID_FILE — this datadir was never claimed by a YANDI bootstrap attempt."

    # 3. Socket path is the dedicated one, never the shared instance's.
    [ "$SOCKET_PATH" = "/run/yandi/mysql/mysql.sock" ] || die "REFUSING --reinitialize-empty-instance: unexpected socket path '$SOCKET_PATH'."

    # 4. Canonical/production activation must NOT have happened yet.
    #    live_bootstrap.py's run() only ever writes phase_b_complete.
    #    marker AFTER schema/roles/grants exist AND selfcheck passes —
    #    its presence is proof a prior Phase B run already completed
    #    real persistence bootstrap. This flag must become permanently
    #    unusable from that point on (mandate §8: never leave "delete
    #    the database in one command" without a strong guard) — no
    #    override, no --force, nothing supersedes this.
    #
    #    NOTE ("10-year bastion" Layer 3): this guard used to check for
    #    yandi_readonly.secret/yandi_migrator.secret instead — that
    #    broke once YANDI_READONLY could bind via auth_socket (no
    #    secret file at all) and YANDI_MIGRATOR stopped being
    #    provisioned by default (ditto) — neither file is guaranteed to
    #    exist anymore regardless of how thoroughly bootstrapped the
    #    instance actually is. phase_b_complete.marker is written
    #    unconditionally on success, independent of which auth mode any
    #    individual role happens to use.
    if [ -f "${SECRETS_DIR}/phase_b_complete.marker" ]; then
        die "REFUSING --reinitialize-empty-instance: found ${SECRETS_DIR}/phase_b_complete.marker — this proves a prior Phase B run already completed schema/role bootstrap (canonical activation). This flag may ONLY be used BEFORE that point. Manual, deliberate action is required from here — this installer will not do it automatically."
    fi

    # 5. Storage state must be a CURRENT reading, not cached/hardcoded.
    disk_gate

    log "REINIT GUARD PASSED — about to wipe and reinitialize:"
    log "  datadir:                 $DATADIR"
    log "  service:                 yandi-db.service"
    log "  socket:                  $SOCKET_PATH"
    log "  instance uuid (UNCHANGED, file preserved): $(cat "$INSTANCE_ID_FILE" 2>/dev/null || echo unknown)"
    log "  shared mysql.service / FastPanel DB: NOT TOUCHED"
}

# ============================================================
# PROOF OF OWNERSHIP — verify_running_instance_ownership()
#
# Live-confirmed bug: a single-signal check ("cmdline contains
# $DATADIR literally") is too strict. deploy/yandi-db.service's own
# ExecStart invokes mysqld as `mysqld --defaults-file=$CONFIG_FILE` —
# the datadir lives INSIDE that config file, never as a literal argv
# token, so a process started exactly as designed was being rejected
# as "not ours." Fail-closed still holds: cmdline may be missing the
# literal datadir ONLY IF it names our exact --defaults-file instead,
# and EVERY other independent signal below must still agree — any
# single contradiction refuses (die), never a weaker OR-of-any-one-
# signal check.
#
# Pure verification logic only — no systemd/process discovery of its
# own, so it can be exercised directly against any pid + fabricated
# expectations (this is what makes it unit-testable without root or a
# real systemd unit). The caller gathers every fact first.
#
# Params (all required, in order):
#   1  pid                       the MainPID to verify
#   2  expected_uid              numeric uid $YANDI_DB_USER resolves to
#   3  expected_exe              realpath of the expected mysqld binary
#   4  expected_datadir          $DATADIR
#   5  expected_socket           $SOCKET_PATH
#   6  expected_pidfile          $PID_FILE
#   7  expected_config           $CONFIG_FILE
#   8  expected_instance_id_file $INSTANCE_ID_FILE
#   9  systemd_user              systemctl show yandi-db -p User --value
#  10  systemd_fragment          systemctl show yandi-db -p FragmentPath --value
#  11  expected_fragment         $SYSTEMD_UNIT_DST
# ============================================================
verify_running_instance_ownership() {
    local pid="$1" expected_uid="$2" expected_exe="$3" expected_datadir="$4"
    local expected_socket="$5" expected_pidfile="$6" expected_config="$7"
    local expected_instance_id_file="$8" systemd_user="$9" systemd_fragment="${10}"
    local expected_fragment="${11}"

    [ -n "$pid" ] && [ "$pid" != "0" ] || die "OWNERSHIP PROOF FAILED: no MainPID reported for yandi-db.service."

    # 1. systemd's own view of the unit: User= and FragmentPath must be
    #    exactly the dedicated ones — never inferred, never assumed.
    [ -n "$expected_uid" ] || die "OWNERSHIP PROOF FAILED: could not resolve \$YANDI_DB_USER to a uid — cannot verify anything against an unknown expectation."
    [ "$systemd_user" = "$YANDI_DB_USER" ] || die "OWNERSHIP PROOF FAILED: yandi-db.service's own User= is '$systemd_user', expected '$YANDI_DB_USER'."
    [ "$(realpath -m "$systemd_fragment" 2>/dev/null)" = "$(realpath -m "$expected_fragment" 2>/dev/null)" ] \
        || die "OWNERSHIP PROOF FAILED: yandi-db.service's FragmentPath ('$systemd_fragment') is not the expected unit file ($expected_fragment)."

    # 2. Process UID must be exactly $YANDI_DB_USER's — not root, not
    #    the shared instance's mysql user, not anyone else.
    [ -r "/proc/$pid/status" ] || die "OWNERSHIP PROOF FAILED: /proc/$pid/status unreadable — cannot verify process uid."
    local proc_uid
    proc_uid="$(awk '/^Uid:/{print $2}' "/proc/$pid/status")"
    [ "$proc_uid" = "$expected_uid" ] || die "OWNERSHIP PROOF FAILED: pid $pid runs as uid $proc_uid, expected $YANDI_DB_USER's uid ($expected_uid)."

    # 3. Executable must be the real mysqld binary — symlinks resolved
    #    on BOTH sides so a path-traversal/alias trick can't fool this.
    [ -r "/proc/$pid/exe" ] || die "OWNERSHIP PROOF FAILED: /proc/$pid/exe unreadable."
    local proc_exe
    proc_exe="$(realpath "/proc/$pid/exe" 2>/dev/null || echo "")"
    [ -n "$proc_exe" ] && [ -n "$expected_exe" ] && [ "$proc_exe" = "$expected_exe" ] \
        || die "OWNERSHIP PROOF FAILED: pid $pid's executable ('$proc_exe') is not the expected mysqld binary ('$expected_exe')."

    # 4. Config file must name OUR exact dedicated datadir/socket/
    #    pid-file, plus the isolation settings this whole design
    #    depends on — this is what lets cmdline be missing the literal
    #    datadir (case 5 below) without weakening the proof at all.
    [ -f "$expected_config" ] || die "OWNERSHIP PROOF FAILED: $expected_config does not exist — cannot verify the dedicated instance's own config."
    local cfg_datadir cfg_socket cfg_pidfile
    cfg_datadir="$(awk -F'=' '/^[[:space:]]*datadir[[:space:]]*=/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "$expected_config" | tail -1)"
    cfg_socket="$(awk -F'=' '/^[[:space:]]*socket[[:space:]]*=/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "$expected_config" | tail -1)"
    cfg_pidfile="$(awk -F'=' '/^[[:space:]]*pid-file[[:space:]]*=/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "$expected_config" | tail -1)"
    [ "$(realpath -m "$cfg_datadir" 2>/dev/null)" = "$(realpath -m "$expected_datadir" 2>/dev/null)" ] \
        || die "OWNERSHIP PROOF FAILED: $expected_config's datadir ('$cfg_datadir') does not match the expected dedicated datadir ($expected_datadir)."
    [ "$(realpath -m "$cfg_socket" 2>/dev/null)" = "$(realpath -m "$expected_socket" 2>/dev/null)" ] \
        || die "OWNERSHIP PROOF FAILED: $expected_config's socket ('$cfg_socket') does not match the expected dedicated socket ($expected_socket)."
    [ "$(realpath -m "$cfg_pidfile" 2>/dev/null)" = "$(realpath -m "$expected_pidfile" 2>/dev/null)" ] \
        || die "OWNERSHIP PROOF FAILED: $expected_config's pid-file ('$cfg_pidfile') does not match the expected dedicated pid file ($expected_pidfile)."
    grep -qE '^[[:space:]]*skip-networking[[:space:]]*$' "$expected_config" \
        || die "OWNERSHIP PROOF FAILED: $expected_config is missing skip-networking."
    grep -qE '^[[:space:]]*mysqlx[[:space:]]*=[[:space:]]*OFF[[:space:]]*$' "$expected_config" \
        || die "OWNERSHIP PROOF FAILED: $expected_config is missing mysqlx = OFF."

    # 5. cmdline: either the datadir appears directly, OR the exact
    #    dedicated --defaults-file is present (check 4 above already
    #    proves THAT file names our own paths — one missing argv token
    #    is not itself a failure when the alternative path is this
    #    fully deterministic).
    [ -r "/proc/$pid/cmdline" ] || die "OWNERSHIP PROOF FAILED: /proc/$pid/cmdline unreadable."
    local cmdline
    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
    if ! printf '%s' "$cmdline" | grep -qF -- "$expected_datadir"; then
        printf '%s' "$cmdline" | grep -qF -- "--defaults-file=$expected_config" \
            || die "OWNERSHIP PROOF FAILED: pid $pid's cmdline contains neither the dedicated datadir nor --defaults-file=$expected_config."
    fi

    # 6. PID file must name the SAME pid systemd reports as MainPID.
    [ -f "$expected_pidfile" ] || die "OWNERSHIP PROOF FAILED: pid file $expected_pidfile does not exist."
    local pidfile_pid
    pidfile_pid="$(tr -d '[:space:]' < "$expected_pidfile")"
    [ "$pidfile_pid" = "$pid" ] || die "OWNERSHIP PROOF FAILED: pid file $expected_pidfile contains '$pidfile_pid', which does not match systemd's MainPID ($pid)."

    # 7. Socket must exist at the exact dedicated path.
    [ -S "$expected_socket" ] || die "OWNERSHIP PROOF FAILED: $expected_socket is not a socket."

    # 8. Instance identity marker must exist — this process may only be
    #    treated as ours if a YANDI bootstrap attempt already claimed it.
    [ -f "$expected_instance_id_file" ] || die "OWNERSHIP PROOF FAILED: instance identity marker $expected_instance_id_file does not exist."

    # 9 (best-effort, contradiction-only): open file descriptors must
    #    not show anything open under the SHARED instance's own datadir
    #    — not required for a positive proof, but a genuine
    #    contradiction here fails closed regardless of every other
    #    signal above passing.
    if [ -r "/proc/$pid/fd" ]; then
        local fd fd_link
        for fd in "/proc/$pid/fd"/*; do
            [ -e "$fd" ] || continue
            fd_link="$(readlink -f "$fd" 2>/dev/null || true)"
            case "$fd_link" in
                /var/lib/mysql/*|/var/lib/mysql)
                    die "OWNERSHIP PROOF FAILED: pid $pid has an open file descriptor under the SHARED instance's datadir ($fd_link) — contradicts dedicated-instance ownership." ;;
            esac
        done
    fi

    log "OWNERSHIP PROVEN for pid $pid: systemd User=$systemd_user, uid=$proc_uid, exe=$proc_exe, config datadir/socket/pid-file/isolation settings all match, pidfile agrees with MainPID."
}

# ============================================================
# TRANSACTION-SCOPED TEMP-PASSWORD CAPTURE
#
# Three small, independently-testable pure functions — extracted
# rather than inlined so each piece (log-delta boundary/read,
# multi-source password reconciliation) can be exercised directly
# against fabricated inputs without needing a real mysqld invocation.
#
# Live-confirmed bug (seventh Phase B attempt, after fixing
# --defaults-file ordering): once log-error is correctly loaded from
# $CONFIG_FILE, Percona/MySQL redirects essentially ALL of its own
# logging — including the "A temporary password is generated..."
# NOTE — directly into that FILE, never to the process's stdout/
# stderr. Capturing only the subprocess's own `2>&1` output (this
# script's original approach) found nothing, even though --initialize
# completed successfully. Re-scanning $ERROR_LOG's WHOLE historical
# content (an earlier, since-reverted fix) is explicitly forbidden by
# mandate — it can pick up a password belonging to a datadir that was
# since wiped and reinitialized.
#
# Fix: record $ERROR_LOG's inode+size BEFORE running --initialize (or
# "absent"), run --initialize ONCE, then read ONLY the bytes appended
# after that exact boundary. Combined with the subprocess's own
# stdout/stderr as an additional allowed source (mandate:
# "CURRENT_PROCESS_OUTPUT OR CURRENT_INITIALIZATION_LOG_DELTA, NEVER
# whole historical error log"), both unioned and deduplicated by VALUE
# — an identical event in both sources is not ambiguous, two DIFFERING
# values are.
# ============================================================

# Prints "<inode> <size>" for an existing file, nothing if absent.
_stat_log_boundary() {
    local path="$1"
    [ -f "$path" ] && stat -c '%i %s' "$path"
    return 0
}

# Reads ONLY the bytes appended to $path after the given boundary.
# Dies on inode change or shrinkage during the window — both are
# inherently anomalous (nothing else should touch this file while we
# hold it) and this refuses rather than guessing which part is new.
_read_fresh_log_delta() {
    local path="$1" pre_inode="$2" pre_size="${3:-0}"
    [ -f "$path" ] || return 0
    local post_inode post_size
    read -r post_inode post_size < <(stat -c '%i %s' "$path")
    if [ -n "$pre_inode" ] && [ "$post_inode" != "$pre_inode" ]; then
        die "$path's inode changed during this single mysqld --initialize invocation (was $pre_inode, now $post_inode) — refusing to guess which content is new; something replaced/rotated this file unexpectedly."
    fi
    if [ "$post_size" -lt "$pre_size" ]; then
        die "$path shrank during this invocation (was ${pre_size} bytes, now ${post_size}) — refusing to guess; the file was truncated unexpectedly."
    fi
    tail -c +"$((pre_size + 1))" "$path"
}

# Extracts the ONE temp password consistent across BOTH sources — 0
# matches or 2+ DIFFERING values both fail closed (never guesses
# first/last).
_extract_unique_temp_password() {
    local init_output="$1" log_delta="$2"
    local all_matches unique_matches match_count
    all_matches="$( { printf '%s\n' "$init_output"; printf '%s\n' "$log_delta"; } \
        | grep -oP 'temporary password is generated for root@localhost:\s*\K\S+' || true )"
    unique_matches="$(printf '%s\n' "$all_matches" | grep -v '^$' | sort -u)"
    match_count="$(printf '%s\n' "$unique_matches" | grep -c . || true)"

    if [ "$match_count" -eq 0 ]; then
        die "mysqld --initialize completed but no 'temporary password' event was found in THIS invocation's own subprocess output OR the fresh \$ERROR_LOG segment written during it — refusing to fall back to scanning historical log content. Check whether log_error_verbosity or a custom log-error path suppressed the NOTE."
    elif [ "$match_count" -gt 1 ]; then
        die "mysqld --initialize produced $match_count DIFFERING temporary-password values across this invocation's own output/log segment — refusing to guess which is correct (ambiguous)."
    fi
    printf '%s' "$unique_matches"
}

# ============================================================
# 8. INITIALIZE DATADIR (idempotent — refuses to re-initialize a
#    non-empty datadir rather than risking data loss, UNLESS
#    --reinitialize-empty-instance was passed AND the guard above
#    clears it)
# ============================================================
initialize_datadir() {
    # Stop any currently-running instance FIRST, unconditionally.
    #
    # Live-confirmed bug (repeated debugging attempts): `rm -rf`'ing the
    # datadir out from under an ALREADY-RUNNING yandi-db.service
    # (started by an EARLIER invocation of this same script) leaves
    # that old process still serving its own orphaned-but-open file
    # handles, while a brand new `mysqld --initialize` writes fresh
    # files that nobody is serving. start_service()'s `systemctl enable
    # --now` is a NO-OP when the unit is already active, so the stale
    # process never gets replaced — Phase B then reads the LATEST temp
    # password (correctly, from the fresh --initialize) but connects to
    # the socket of the OLD, still-running process, which has different
    # credentials/state -> "Access denied" with a real-looking password.
    # Stopping here guarantees start_service() always performs a
    # genuine fresh start against whatever ends up on disk, regardless
    # of how many times a human clears the datadir between invocations.
    if systemctl is-active --quiet yandi-db 2>/dev/null; then
        # Verify the process we are about to stop is unambiguously OURS
        # before touching it — multi-signal proof (see
        # verify_running_instance_ownership()'s own docstring for why a
        # single "cmdline contains $DATADIR" check was too strict).
        local main_pid systemd_user systemd_fragment
        main_pid="$(systemctl show yandi-db -p MainPID --value 2>/dev/null || echo 0)"
        systemd_user="$(systemctl show yandi-db -p User --value 2>/dev/null || echo "")"
        systemd_fragment="$(systemctl show yandi-db -p FragmentPath --value 2>/dev/null || echo "")"
        verify_running_instance_ownership \
            "$main_pid" \
            "$(id -u "$YANDI_DB_USER" 2>/dev/null || echo "")" \
            "$(realpath "$(command -v mysqld 2>/dev/null)" 2>/dev/null || echo "")" \
            "$DATADIR" "$SOCKET_PATH" "$PID_FILE" "$CONFIG_FILE" "$INSTANCE_ID_FILE" \
            "$systemd_user" "$systemd_fragment" "$SYSTEMD_UNIT_DST"
        log "yandi-db.service is currently active — stopping it before inspecting/touching the datadir"
        systemctl stop yandi-db
    fi

    if [ -n "$(ls -A "$DATADIR" 2>/dev/null)" ]; then
        # Non-empty is only safe to skip when it actually LOOKS LIKE a
        # real, previously-initialized MySQL datadir (mandate: "non-empty
        # UNKNOWN datadir => STOP", not "non-empty => assume fine").
        # ibdata1 + mysql/ are the two things every mysqld --initialize
        # always creates — their absence means this is some other,
        # unidentified content this script has no business guessing about.
        if [ -f "${DATADIR}/ibdata1" ] && [ -d "${DATADIR}/mysql" ]; then
            if [ "$REINIT_EMPTY_INSTANCE" -eq 1 ]; then
                reinitialize_empty_instance_guard
                # Whole-directory removal, not a `data/*` glob — a glob
                # does NOT match dotfiles, live-confirmed as a real risk
                # (mysqld/InnoDB can leave dotfile-shaped entries).
                # Removing and recreating the directory itself catches
                # everything, dotfiles included.
                rm -rf "$DATADIR"
                install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0700 "$DATADIR"
                log "dedicated datadir wiped and recreated empty (instance identity file UNCHANGED) — proceeding to fresh --initialize"
                # Falls through to the real --initialize below —
                # deliberately no `return` here.
            else
                log "datadir $DATADIR is non-empty and looks like an already-initialized MySQL datadir — skipping --initialize"
                return
            fi
        else
            die "datadir $DATADIR is non-empty but does NOT look like a valid MySQL datadir (missing ibdata1 and/or mysql/) — refusing to guess whether it is safe to initialize over. Investigate manually and clear or relocate it before re-running (mandate: ambiguous state -> STOP, never auto-resolve)."
        fi
    fi

    # Re-check storage state with a CURRENT reading immediately before the
    # actual disk-consuming operation — the early disk_gate call above
    # (step 2) is a fast fail-fast check, several idempotent/cheap steps
    # ago; this is the authoritative gate right before mysqld writes data.
    disk_gate

    # --defaults-file MUST be the FIRST argument to mysqld — this is a
    # real, live-confirmed bug fix, not a style preference: mysqld's own
    # option parser only recognizes --defaults-file in argv[1]. Placed
    # after --initialize/--user/--datadir (as this line originally was),
    # it is silently ignored and mysqld falls back to its compiled-in/
    # system default config search path instead of $CONFIG_FILE — this
    # is exactly what happened on the first live run: log-error pointed
    # at the DEFAULT /var/log/mysql/error.log (permission denied for the
    # yandi-db user, since that path belongs to the shared instance's
    # own logging setup) instead of $ERROR_LOG, and character-set-server
    # silently reverted to utf8mb3 instead of $CONFIG_FILE's utf8mb4 —
    # both proven by that run's own mysqld warnings/errors. --datadir
    # and --user were unaffected only because they were ALSO passed as
    # explicit CLI flags (which apply regardless of defaults-file
    # loading) — the dedicated datadir itself was never at risk.
    # Capture THIS invocation's own --initialize temp password via a
    # TRANSACTION-SCOPED LOG DELTA — see the three _stat_log_boundary/
    # _read_fresh_log_delta/_extract_unique_temp_password functions
    # above for the full rationale (live-confirmed bug: once log-error
    # is correctly loaded, Percona writes the temp-password NOTE
    # directly to that file, never to stdout/stderr).
    local pre_inode pre_size
    read -r pre_inode pre_size < <(_stat_log_boundary "$ERROR_LOG"; echo)

    # `|| init_rc=$?` (not a bare command) so `set -e` does not abort
    # BEFORE the diagnostic output below gets appended to $ERROR_LOG —
    # a real mysqld --initialize failure must still leave a full trail.
    local init_output init_rc=0
    init_output="$(mysqld --defaults-file="$CONFIG_FILE" --initialize \
        --user="$YANDI_DB_USER" --datadir="$DATADIR" 2>&1)" || init_rc=$?

    local log_delta
    log_delta="$(_read_fresh_log_delta "$ERROR_LOG" "$pre_inode" "$pre_size")"

    # Append the diagnostic trail AFTER computing the delta above, so
    # this script's own appended text is never mistaken for part of
    # mysqld's own fresh segment.
    printf '%s\n' "$init_output" >> "$ERROR_LOG"
    [ "$init_rc" -eq 0 ] || die "mysqld --initialize failed (exit=$init_rc) — see $ERROR_LOG for full output"

    local temp_pw
    temp_pw="$(_extract_unique_temp_password "$init_output" "$log_delta")"
    if [ "${YANDI_INSTALL_DEBUG_SECRETS:-0}" = "1" ]; then
        log "TEMP_SOURCE_LEN=${#temp_pw} TEMP_SOURCE_FP=$(_temp_pw_fp "$temp_pw")"
    fi

    # Re-verify (not just assume) the secure bootstrap directory
    # immediately before writing into it — live-confirmed bug: the
    # service-stop call above tore down the OLD single-level
    # RUNTIME_DIR (a systemd RuntimeDirectory= target back then), and
    # this script had nothing recreating it before this exact write.
    # Cheap, idempotent, and removes any dependence on precisely how
    # systemd handles the (now separate) RUNTIME_MYSQL_DIR's own
    # teardown/recreation for this UNRELATED directory.
    ensure_secure_bootstrap_dir

    # Atomic write: create with a restrictive umask FROM THE START (no
    # window where the file is briefly world/group-readable), then
    # rename into place — a crash mid-write can never leave a
    # half-written or wrongly-permissioned marker visible at the real path.
    local marker_tmp="${FRESH_INIT_MARKER}.tmp.$$"
    ( umask 077; printf '%s' "$temp_pw" > "$marker_tmp" )
    chown root:root "$marker_tmp"
    mv -f "$marker_tmp" "$FRESH_INIT_MARKER"

    if [ "${YANDI_INSTALL_DEBUG_SECRETS:-0}" = "1" ]; then
        # Read the marker straight back and fingerprint THAT — proves
        # (or disproves) byte-for-byte round-trip fidelity of the
        # bash-side write/rename alone, independent of anything Python
        # does later.
        local marker_readback
        marker_readback="$(cat "$FRESH_INIT_MARKER")"
        log "MARKER_LEN=${#marker_readback} MARKER_FP=$(_temp_pw_fp "$marker_readback")"
        unset marker_readback
    fi

    unset temp_pw init_output log_delta

    log "datadir initialized — this invocation's own one-time temp password was captured via transaction-scoped log delta (never re-derived from \$ERROR_LOG's historical content)"
    log "this script will use it once, immediately, in run_python_bootstrap() below, then retire it"
}

# ============================================================
# 9. START ISOLATED MYSQL
# ============================================================
start_service() {
    systemctl enable --now yandi-db
    for _ in $(seq 1 30); do
        [ -S "$SOCKET_PATH" ] && break
        sleep 1
    done
    [ -S "$SOCKET_PATH" ] || die "socket $SOCKET_PATH did not appear after 30s — check: journalctl -u yandi-db"
    log "yandi-db service is running, socket present at $SOCKET_PATH"
}

# ============================================================
# 10. VERIFY NO TCP
# ============================================================
verify_no_tcp() {
    if ss -lntp 2>/dev/null | grep -q "mysqld.*yandi" ; then
        systemctl stop yandi-db
        die "a TCP listener was found for the yandi mysqld process — refusing to continue with a network-exposed dedicated instance. Check skip-networking in $CONFIG_FILE."
    fi
    log "confirmed: no TCP listener for the dedicated instance"
}

# ============================================================
# 11-17: initial DB bootstrap, TDE, schema/grants/triggers, crypto,
# integrity, selfcheck, revoke bootstrap capability.
#
# Deliberately delegates to the EXISTING Python modules
# (agent.db.sql.bootstrap / migrate / security_selfcheck / keys) —
# this shell script does OS-level work only (§J of DEDICATED_
# INSTANCE_DESIGN.md: "agent/db/sql/bootstrap.py is NOT extended to do
# OS-level work"). It does not reimplement any DB-level logic here.
# ============================================================
run_python_bootstrap() {
    log "handing off to agent.db.sql.live_bootstrap for DB-level work (instance identity, "
    log "root temp-password retirement, schema/roles/triggers, selfcheck)"
    "$YANDI_VENV_PYTHON" -m agent.db.sql.live_bootstrap \
        --socket "$SOCKET_PATH" \
        --fresh-init-marker "$FRESH_INIT_MARKER" \
        --instance-id-file "$INSTANCE_ID_FILE" \
        --secrets-dir "$SECRETS_DIR" \
        --agent-os-user "$AGENT_OS_USER" \
        --owner-os-user "$OWNER_OS_USER"
    log "DB-level bootstrap complete — see agent/db/sql/DEDICATED_INSTANCE_DESIGN.md §L and"
    log "SQL_DEPLOYMENT_DEFERRED.md for what still needs LIVE verification after this point"
    log "(TDE/keyring activation, full isolation proof, restart-persistence, production"
    log "shadow-write smoke test) — this script's job ends at 'the instance and schema exist'."
}

main() {
    # --database-only is required, not cosmetic: makes explicit AT THE
    # CALL SITE — not just implicit in this script's own steps — that
    # this installer only ever provisions the dedicated YANDI database
    # appliance (/var/lib/yandi, yandi-db OS user/service) and never
    # touches the shared FastPanel mysql.service.
    #
    # --reinitialize-empty-instance is an OPTIONAL, additional
    # destructive modifier (owner-explicit, mandate §8) — see
    # reinitialize_empty_instance_guard() for the fail-closed
    # conditions that must ALL hold before it does anything at all.
    # Ordinary --database-only (without this flag) NEVER reinitializes
    # an existing datadir — behavior is byte-for-byte unchanged from
    # before this flag existed.
    local database_only=0
    for arg in "$@"; do
        case "$arg" in
            --database-only) database_only=1 ;;
            --reinitialize-empty-instance) REINIT_EMPTY_INSTANCE=1 ;;
            *) die "unknown argument: '$arg' (usage: sudo $0 --database-only [--reinitialize-empty-instance])" ;;
        esac
    done
    if [ "$database_only" -ne 1 ]; then
        die "usage: sudo $0 --database-only [--reinitialize-empty-instance] (--database-only required — this installer only ever provisions the dedicated YANDI database appliance, never the shared FastPanel mysql.service)"
    fi

    log "=== YANDI dedicated database appliance installer (DESIGN — review before running) ==="
    if [ "$REINIT_EMPTY_INSTANCE" -eq 1 ]; then
        log "--reinitialize-empty-instance requested — will be evaluated against a fail-closed guard if/when an already-initialized datadir is found (see reinitialize_empty_instance_guard())"
    fi
    precheck
    disk_gate
    create_os_identity
    create_filesystem
    create_instance_identity_marker
    install_config
    apparmor_setup
    install_systemd_unit
    initialize_datadir
    start_service
    verify_no_tcp
    run_python_bootstrap
    log "=== install-yandi.sh: OS-level phase complete ==="
}

main "$@"
