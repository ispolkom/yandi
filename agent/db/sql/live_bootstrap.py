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
        --error-log /var/log/yandi/mysql-error.log \\
        --instance-id-file /etc/yandi/mysql/instance.id \\
        --secrets-dir /var/lib/yandi/keys \\
        --agent-os-user iam
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import socket
import sys
from typing import Optional

from agent.db.sql.bootstrap import run_bootstrap
from agent.db.sql.instance_identity import ensure_instance_id_file, get_db_instance_id
from agent.db.sql.security_selfcheck import run_selfcheck

_TEMP_PASSWORD_RE = re.compile(r"temporary password is generated for root@localhost:\s*(\S+)")


class LiveBootstrapError(Exception):
    """Raised for any live-bootstrap failure — the caller (install-
    yandi.sh) is expected to treat this as fatal and NOT retry
    automatically (mandate §8: ambiguous/failed state -> STOP)."""


def extract_temporary_root_password(error_log_path: str) -> Optional[str]:
    """Reads the `mysqld --initialize` error log ONCE to find the
    one-time temporary root password. Returns None if no such line is
    found (e.g. a second run against an already-initialized datadir,
    where no new temp password was ever generated — NOT an error by
    itself, the caller decides what that means).

    The password is returned to the caller's memory ONLY — this
    function never writes it anywhere, never logs it, and the error log
    file itself is left completely untouched (mandate §32: this
    function only READS a secret that's already unavoidably on disk in
    Percona's own log; it does not make that exposure worse)."""
    if not os.path.exists(error_log_path):
        return None
    with open(error_log_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    match = _TEMP_PASSWORD_RE.search(text)
    return match.group(1) if match else None


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


def _retire_temporary_root_password(socket_path: str, temp_password: str):
    """Connects ONCE with the ephemeral temp password and immediately
    converts root to auth_socket, permanently retiring that password —
    from this point on nothing in this codebase ever holds a root SQL
    password again. Returns nothing; raises LiveBootstrapError if the
    conversion statement itself fails (see this module's own docstring
    for why this exact statement's success is UNVERIFIED until a real
    run happens)."""
    import pymysql
    import pymysql.cursors

    try:
        conn = pymysql.connect(
            unix_socket=socket_path, user="root", password=temp_password,
            charset="utf8mb4", connect_timeout=5, autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as e:
        raise LiveBootstrapError(f"could not connect with the temporary root password: {e}") from e

    try:
        with conn.cursor() as cur:
            cur.execute("ALTER USER 'root'@'localhost' IDENTIFIED WITH auth_socket")
    except Exception as e:
        raise LiveBootstrapError(
            f"failed to convert root@localhost to auth_socket — the temporary password may "
            f"still be active; this is the exact live-verification-required step this "
            f"module's docstring flags as unproven. Original error: {e}"
        ) from e
    finally:
        conn.close()


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
    *, socket_path: str, error_log_path: str, instance_id_file: str,
    secrets_dir: str, agent_os_user: str, created_by_host: Optional[str] = None,
) -> dict:
    """The full Phase B sequence. Idempotent per mandate §8: safe to
    re-run against an already-bootstrapped instance (temp password
    absent -> skip retirement step; instance id file already present ->
    reused unchanged; run_bootstrap()'s own idempotency handles the
    rest; existing secret files are never overwritten)."""
    instance_uuid = ensure_instance_id_file(instance_id_file)

    temp_password = extract_temporary_root_password(error_log_path)
    if temp_password:
        _retire_temporary_root_password(socket_path, temp_password)
    # If no temp password was found, root is assumed to already be on
    # auth_socket from a previous run of this same script — NOT
    # re-derived or guessed, just attempted directly below; a genuine
    # auth failure there raises LiveBootstrapError rather than silently
    # falling back to anything.

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
    parser.add_argument("--error-log", required=True)
    parser.add_argument("--instance-id-file", required=True)
    parser.add_argument("--secrets-dir", required=True)
    parser.add_argument("--agent-os-user", required=True)
    args = parser.parse_args(argv)

    try:
        result = run(
            socket_path=args.socket, error_log_path=args.error_log,
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
