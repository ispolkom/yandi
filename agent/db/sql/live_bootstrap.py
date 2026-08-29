"""
agent/db/sql/live_bootstrap.py — DATABASE BOOTSTRAP V1 (mandate §7
Phase B, §11, §32): the one orchestration script deploy/install-yandi.
sh's run_python_bootstrap() hands off to, once the dedicated `yandi-db`
mysqld is already running with its Unix socket present (Phase A,
root, OS-level — unchanged, still that script's job alone).

This module does NOT reimplement anything agent/db/sql/bootstrap.py,
security_grants.py, security_selfcheck.py, or instance_identity.py
already do — it only SEQUENCES them against a real connection, plus
the one genuinely new piece of glue those modules can't own themselves:
retiring the ephemeral `mysqld --initialize` temporary root credential
(mandate §32: "Temporary bootstrap credentials must NOT reach git" —
and, just as importantly, must not reach ANY file/log/stdout this
script controls either).

WHAT IS PROVEN THIS PASS: the pure logic (temp-password-line parsing,
protected-secret-file save/idempotency, the call sequence against a
scripted fake connection) — see
agent/db_sql_live_bootstrap_regression_test.py.

WHAT IS NOT PROVEN THIS PASS (mandate §55: don't claim untested proof):
whether `ALTER USER 'root'@'localhost' IDENTIFIED WITH auth_socket`,
issued as the FIRST statement on a freshly-`--initialize`d connection,
actually satisfies MySQL's mandatory post-initialize password-change
"sandbox mode" on THIS EXACT Percona 8.0.46 build — the manual says any
ALTER USER on the connected account should satisfy it, but this has
never been tried against a live server in this codebase. If it does
NOT work, this script fails LOUD (the exception propagates, install-
yandi.sh's `set -e` aborts) rather than falling back to something
unreviewed — see `_retire_temporary_root_password()`'s docstring.

Auth strategy actually used (DEDICATED_INSTANCE_DESIGN.md §H):
    YANDI_RUNTIME  -> auth_socket, mapped to AGENT_OS_USER (no password
                      exists for this role at all).
    YANDI_MIGRATOR / YANDI_READONLY -> random `secrets.token_urlsafe(32)`
                      passwords, stored 0600 next to the KEK (same
                      pattern as agent/db/sql/keys.py's save_kek(),
                      generalized here since this isn't a KEK).
    root@localhost -> converted to auth_socket immediately, retiring
                      the `--initialize`-generated temp password for
                      good (root becomes reachable only by the actual
                      OS root user from then on, same peer-cred model).

CLI (matches the invocation shape install-yandi.sh's run_python_
bootstrap() already documented as its intended shape):
    python3 -m agent.db.sql.live_bootstrap \\
        --socket /run/yandi/mysql.sock \\
        --fresh-init-marker /run/yandi/fresh_init_temp_password \\
        --instance-id-file /etc/yandi/mysql/instance.id \\
        --secrets-dir /var/lib/yandi/keys \\
        --agent-os-user iam

ROOT AUTH STATE — three cases, never a guess (live-confirmed bug this
replaced: scanning install-yandi.sh's ever-growing $ERROR_LOG for the
LAST "temporary password is generated..." line could pick up a real
password belonging to a datadir that was since wiped and reinitialized
without this exact process ever running, producing a real-looking but
WRONG credential and a confusing "Access denied" instead of a clear
diagnostic):

    A) FRESH INITIALIZATION — install-yandi.sh's initialize_datadir()
       just ran `mysqld --initialize` THIS invocation and captured ITS
       OWN output directly into a one-time marker file (never derived
       from re-reading any log). run() consumes that marker exactly
       once (deletes it immediately after reading) and uses ONLY that
       password to retire root's temp credential.
    B) EXISTING MANAGED INSTANCE — no marker present (this invocation
       did not just initialize). run() verifies root is ALREADY
       reachable via auth_socket (an earlier run already completed the
       one-time conversion) and proceeds directly — no password
       anywhere in this path.
    C) AMBIGUOUS AUTH STATE — no marker AND auth_socket doesn't work
       either. run() raises LiveBootstrapError with a precise
       diagnostic and does nothing further. Never scans a historical
       log, never guesses, never retries with a different credential.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
from typing import Optional

from agent.db.sql.bootstrap import run_bootstrap
from agent.db.sql.instance_identity import ensure_instance_id_file, get_db_instance_id
from agent.db.sql.security_selfcheck import run_selfcheck


class LiveBootstrapError(Exception):
    """Raised for any live-bootstrap failure — the caller (install-
    yandi.sh) is expected to treat this as fatal and NOT retry
    automatically (mandate §8: ambiguous/failed state -> STOP)."""


def load_and_consume_fresh_init_marker(path: str) -> Optional[str]:
    """Reads the ONE-TIME temp password install-yandi.sh's
    initialize_datadir() captured directly from THIS invocation's own
    `mysqld --initialize` output (never from re-scanning a shared,
    ever-growing error log — see this module's docstring, Case A/B/C).

    Returns None if the marker is absent (this invocation did not just
    run --initialize — the caller decides what that means: see run()'s
    Case B/C). Deletes the marker immediately after a successful read
    so it can NEVER be reused across runs (mandate: reruns must be
    idempotent and must not need the one-time temp password again)."""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        value = f.read().strip()
    os.remove(path)
    return value or None


def save_protected_secret(path: str, value: str) -> None:
    """Same 0600-file, refuse-to-overwrite, atomic-directory-creation
    contract as agent/db/sql/keys.py's save_kek() — generalized here for
    plain string secrets (migrator/readonly passwords) rather than raw
    key bytes, so this module doesn't reach into keys.py's KEK-specific
    type/size assumptions for an unrelated kind of secret."""
    if os.path.exists(path):
        raise FileExistsError(f"a secret already exists at {path} — refusing to overwrite silently")
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
    finally:
        os.close(fd)


def load_protected_secret(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


_MYSQL_CLIENT_BIN = shutil.which("mysql")

_ALTER_ROOT_TO_AUTH_SOCKET_SQL = "ALTER USER 'root'@'localhost' IDENTIFIED WITH auth_socket;"


def _retire_temporary_root_password(socket_path: str, temp_password: str):
    """Connects ONCE with the ephemeral temp password and immediately
    converts root to auth_socket, permanently retiring that password —
    from this point on nothing in this codebase ever holds a root SQL
    password again. Returns nothing; raises LiveBootstrapError on
    failure.

    Uses the `mysql` CLI client, NOT pymysql, for this one statement.
    Live-confirmed (second Phase B run, after fixing the first found
    bug — HANDLE_EXPIRED_PASSWORDS): even once the connection itself
    succeeds, pymysql's connect() unconditionally issues a "SET NAMES"
    query right after authentication (Connection.set_character_set(),
    for collation-precision reasons its own source comment explains —
    there is no public parameter to suppress this), and a freshly
    `--initialize`d root account is in MySQL's post-initialize
    password-expiration "sandbox mode", which the server enforces by
    rejecting every statement except a small ALTER USER/SET PASSWORD-
    shaped set — SET NAMES included, rejected with error 1820. The
    `mysql` CLI client (the exact client Percona/MySQL's own error
    messages point to as "a client that supports expired passwords")
    does not have this problem, so it is used here for exactly this
    one, security-sensitive, single-statement operation instead.

    The temp password is passed via the MYSQL_PWD environment variable
    of this one subprocess (never a CLI argument, which would be
    visible to any local user via `ps`/`/proc/<pid>/cmdline`) — the
    standard mechanism the mysql client itself documents for
    non-interactive password use; /proc/<pid>/environ is readable only
    by the owning uid (root, here) and root itself, and the value is
    never logged/printed/persisted anywhere by this function.
    """
    if not _MYSQL_CLIENT_BIN:
        raise LiveBootstrapError(
            "the `mysql` CLI client was not found on PATH — required to convert "
            "root@localhost to auth_socket without tripping pymysql's automatic "
            "SET NAMES query (rejected under MySQL's post-initialize password-"
            "expiration sandbox mode). Install the client package that ships "
            "alongside mysqld/percona-server-server."
        )

    result = subprocess.run(
        [_MYSQL_CLIENT_BIN, f"--socket={socket_path}", "--user=root",
         "-e", _ALTER_ROOT_TO_AUTH_SOCKET_SQL],
        env={**os.environ, "MYSQL_PWD": temp_password},
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise LiveBootstrapError(
            f"failed to convert root@localhost to auth_socket via the mysql CLI "
            f"client (exit={result.returncode}) — the temporary password may "
            f"still be active. stderr: {result.stderr.strip()}"
        )


def _root_reachable_via_auth_socket(socket_path: str) -> bool:
    """Case B probe: is root@localhost ALREADY reachable via auth_socket
    (peer credentials, no password) — meaning an EARLIER run already
    completed the one-time conversion? This process must itself be
    running as the OS root user for auth_socket peer-cred matching to
    succeed (true here: install-yandi.sh always invokes this under
    sudo). Uses the mysql CLI, same as _retire_temporary_root_password(),
    so this module's root-auth surface stays in one place rather than
    also involving pymysql for this one check."""
    if not _MYSQL_CLIENT_BIN:
        return False
    result = subprocess.run(
        [_MYSQL_CLIENT_BIN, f"--socket={socket_path}", "--user=root", "-e", "SELECT 1;"],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0


def _connect_as_root_auth_socket(socket_path: str):
    import pymysql
    import pymysql.cursors

    try:
        return pymysql.connect(
            unix_socket=socket_path, user="root", password="",
            charset="utf8mb4", connect_timeout=5, autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as e:
        raise LiveBootstrapError(
            f"could not connect as root@localhost via auth_socket after conversion — "
            f"is this process actually running as the OS root user? {e}"
        ) from e


def run(
    *, socket_path: str, fresh_init_marker: str, instance_id_file: str,
    secrets_dir: str, agent_os_user: str, created_by_host: Optional[str] = None,
) -> dict:
    """The full Phase B sequence. Idempotent per mandate §8: safe to
    re-run against an already-bootstrapped instance (no fresh-init
    marker -> skip retirement step, verify auth_socket instead;
    instance id file already present -> reused unchanged;
    run_bootstrap()'s own idempotency handles the rest; existing secret
    files are never overwritten).

    Case A/B/C (see module docstring for the full rationale):
        A: fresh_init_marker present -> THIS invocation's own
           --initialize just ran; consume that exact password once.
        B: marker absent, root already reachable via auth_socket ->
           an earlier run already converted it; proceed directly.
        C: marker absent AND auth_socket unreachable -> ambiguous;
           raise LiveBootstrapError rather than guess/scan any log.
    """
    instance_uuid = ensure_instance_id_file(instance_id_file)

    temp_password = load_and_consume_fresh_init_marker(fresh_init_marker)
    if temp_password is not None:
        _retire_temporary_root_password(socket_path, temp_password)
    elif not _root_reachable_via_auth_socket(socket_path):
        raise LiveBootstrapError(
            "AMBIGUOUS AUTH STATE: no fresh mysqld --initialize marker is present "
            "for this invocation (install-yandi.sh did not just run --initialize "
            "this time), and root@localhost is not reachable via auth_socket "
            "either. This usually means an earlier attempt left root on a "
            "different/unknown credential. Refusing to guess or scan any "
            "historical log for a password — that is exactly the bug this "
            "replaced. Manual investigation required before retrying."
        )
    # else: Case B — root already on auth_socket from an earlier
    # successful run; nothing to retire, proceed directly below.

    conn = _connect_as_root_auth_socket(socket_path)
    try:
        readonly_secret_path = os.path.join(secrets_dir, "yandi_readonly.secret")
        migrator_secret_path = os.path.join(secrets_dir, "yandi_migrator.secret")

        readonly_password = (
            load_protected_secret(readonly_secret_path) if os.path.exists(readonly_secret_path)
            else secrets.token_urlsafe(32)
        )
        migrator_password = (
            load_protected_secret(migrator_secret_path) if os.path.exists(migrator_secret_path)
            else secrets.token_urlsafe(32)
        )

        result = run_bootstrap(
            conn,
            readonly_password=readonly_password, migrator_password=migrator_password,
            runtime_auth_socket_os_user=agent_os_user,
            instance_uuid=instance_uuid, instance_created_by_host=created_by_host or socket.gethostname(),
        )
        conn.commit()

        if not os.path.exists(readonly_secret_path):
            save_protected_secret(readonly_secret_path, readonly_password)
        if not os.path.exists(migrator_secret_path):
            save_protected_secret(migrator_secret_path, migrator_password)

        selfcheck = run_selfcheck(conn, role="runtime", expected_instance_uuid=instance_uuid)
    finally:
        conn.close()

    return {
        "instance_uuid": instance_uuid,
        "bootstrap": result,
        "selfcheck_ok": selfcheck["ok"],
        "selfcheck": selfcheck,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--fresh-init-marker", required=True)
    parser.add_argument("--instance-id-file", required=True)
    parser.add_argument("--secrets-dir", required=True)
    parser.add_argument("--agent-os-user", required=True)
    args = parser.parse_args(argv)

    try:
        result = run(
            socket_path=args.socket, fresh_init_marker=args.fresh_init_marker,
            instance_id_file=args.instance_id_file, secrets_dir=args.secrets_dir,
            agent_os_user=args.agent_os_user,
        )
    except LiveBootstrapError as e:
        print(f"[live_bootstrap] FATAL: {e}", file=sys.stderr)
        return 1

    # Never print a password/secret value — only structural results.
    print(f"[live_bootstrap] instance_uuid={result['instance_uuid']}")
    print(f"[live_bootstrap] roles_ensured={result['bootstrap']['roles_ensured']}")
    print(f"[live_bootstrap] runtime_auth_mode={result['bootstrap']['runtime_auth_mode']}")
    print(f"[live_bootstrap] triggers_created={len(result['bootstrap']['triggers_created'])}")
    print(f"[live_bootstrap] selfcheck_ok={result['selfcheck_ok']}")
    if not result["selfcheck_ok"]:
        print(f"[live_bootstrap] selfcheck detail: {result['selfcheck']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
