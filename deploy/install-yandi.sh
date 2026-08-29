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
RUNTIME_DIR="/run/yandi"
CONFIG_DIR="/etc/yandi/mysql"
CONFIG_FILE="${CONFIG_DIR}/my.cnf"
LOG_DIR="/var/log/yandi"
ERROR_LOG="${LOG_DIR}/mysql-error.log"
SOCKET_PATH="${RUNTIME_DIR}/mysql.sock"
PID_FILE="${RUNTIME_DIR}/mysql.pid"
SYSTEMD_UNIT_SRC="$(dirname "$0")/yandi-db.service"
SYSTEMD_UNIT_DST="/etc/systemd/system/yandi-db.service"
APPARMOR_LOCAL_DIR="/etc/apparmor.d/local"
APPARMOR_LOCAL_FILE="${APPARMOR_LOCAL_DIR}/usr.sbin.mysqld"
APPARMOR_SHARED_PROFILE="/etc/apparmor.d/usr.sbin.mysqld"
KEK_PATH="${YANDI_DB_HOME}/keys/kek.bin"
SECRETS_DIR="${YANDI_DB_HOME}/keys"
INSTANCE_ID_FILE="${CONFIG_DIR}/instance.id"
YANDI_REPO="/home/iam/yandi"
YANDI_VENV_PYTHON="/home/iam/venv/bin/python3"
# The real OS user the AGENT process runs as today (confirmed during
# the 5E-S2 audit — DEDICATED_INSTANCE_DESIGN.md §H) — YANDI_RUNTIME is
# created with auth_socket mapped to THIS name, not a YANDI-internal
# label. If the agent's OS identity ever changes (e.g. a future
# dedicated `yandi-agent` system account), update this ONE line as
# part of that same change.
AGENT_OS_USER="iam"

log() { echo "[install-yandi] $*"; }
die() { echo "[install-yandi] FATAL: $*" >&2; exit 1; }

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
    install -d -o root -g "$YANDI_DB_USER" -m 0750 "$RUNTIME_DIR"
    install -d -o root -g root -m 0755 "$CONFIG_DIR"
    install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0750 "$LOG_DIR"
    install -d -o "$YANDI_DB_USER" -g "$YANDI_DB_USER" -m 0700 "$(dirname "$KEK_PATH")"
    log "filesystem layout ready under $YANDI_DB_HOME, $CONFIG_DIR, $RUNTIME_DIR, $LOG_DIR"
}

# ============================================================
# 5. INSTALL CONFIG
# ============================================================
install_config() {
    if [ -f "$CONFIG_FILE" ]; then
        log "config already exists at $CONFIG_FILE — leaving in place (idempotent; edit manually to change)"
        return
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
${RUNTIME_DIR}/mysql.sock rw,
${RUNTIME_DIR}/mysql.pid rw,
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
# 8. INITIALIZE DATADIR (idempotent — refuses to re-initialize a
#    non-empty datadir rather than risking data loss)
# ============================================================
initialize_datadir() {
    if [ -n "$(ls -A "$DATADIR" 2>/dev/null)" ]; then
        # Non-empty is only safe to skip when it actually LOOKS LIKE a
        # real, previously-initialized MySQL datadir (mandate: "non-empty
        # UNKNOWN datadir => STOP", not "non-empty => assume fine").
        # ibdata1 + mysql/ are the two things every mysqld --initialize
        # always creates — their absence means this is some other,
        # unidentified content this script has no business guessing about.
        if [ -f "${DATADIR}/ibdata1" ] && [ -d "${DATADIR}/mysql" ]; then
            log "datadir $DATADIR is non-empty and looks like an already-initialized MySQL datadir — skipping --initialize"
            return
        fi
        die "datadir $DATADIR is non-empty but does NOT look like a valid MySQL datadir (missing ibdata1 and/or mysql/) — refusing to guess whether it is safe to initialize over. Investigate manually and clear or relocate it before re-running (mandate: ambiguous state -> STOP, never auto-resolve)."
    fi

    # Re-check storage state with a CURRENT reading immediately before the
    # actual disk-consuming operation — the early disk_gate call above
    # (step 2) is a fast fail-fast check, several idempotent/cheap steps
    # ago; this is the authoritative gate right before mysqld writes data.
    disk_gate

    mysqld --initialize --user="$YANDI_DB_USER" --datadir="$DATADIR" \
        --defaults-file="$CONFIG_FILE" 2>&1 | tee -a "$ERROR_LOG"
    log "datadir initialized — a one-time temporary root password was written to $ERROR_LOG (grep for 'temporary password')"
    log "this script will use it once, immediately, in initial_db_bootstrap() below, then retire it"
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
        --error-log "$ERROR_LOG" \
        --instance-id-file "$INSTANCE_ID_FILE" \
        --secrets-dir "$SECRETS_DIR" \
        --agent-os-user "$AGENT_OS_USER"
    log "DB-level bootstrap complete — see agent/db/sql/DEDICATED_INSTANCE_DESIGN.md §L and"
    log "SQL_DEPLOYMENT_DEFERRED.md for what still needs LIVE verification after this point"
    log "(TDE/keyring activation, full isolation proof, restart-persistence, production"
    log "shadow-write smoke test) — this script's job ends at 'the instance and schema exist'."
}

main() {
    # Required, not cosmetic: makes explicit AT THE CALL SITE — not just
    # implicit in this script's own steps — that this installer only
    # ever provisions the dedicated YANDI database appliance
    # (/var/lib/yandi, yandi-db OS user/service) and never touches the
    # shared FastPanel mysql.service. No other mode exists yet (there is
    # only one thing this script does), so this doesn't change any step
    # below — it just refuses to run silently without the owner typing
    # the explicit, self-documenting flag.
    if [ "${1:-}" != "--database-only" ]; then
        die "usage: sudo $0 --database-only (flag required — this installer only ever provisions the dedicated YANDI database appliance, never the shared FastPanel mysql.service)"
    fi

    log "=== YANDI dedicated database appliance installer (DESIGN — review before running) ==="
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
