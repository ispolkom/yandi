# DATABASE BOOTSTRAP V1 — CHECKPOINT

current_commit: ae264cd

## PROVEN OFFLINE (regression-tested, no live server)
- instance_identity.py: instance_identity table (class A) + /etc/yandi/mysql/instance.id file marker, verify_instance_identity()
- connection.py: YANDI_SQL_SOCKET (unix socket) + YANDI_SQL_AUTH_MODE=auth_socket, no TCP/socket fallback
- security_grants.py / bootstrap.py: auth_socket variant for YANDI_RUNTIME (passwordless, OS-user-bound)
- security_selfcheck.py: optional expected_instance_uuid identity gate
- live_bootstrap.py: Phase B orchestration (temp root password parse, root->auth_socket conversion, run_bootstrap() call, migrator/readonly secret generation, 0600, never printed)
- install-yandi.sh: run_python_bootstrap() no longer a stub, calls live_bootstrap (script itself never executed)
- system_awareness.py: yandi_db_instance section (service_running / socket_present / identity_file_present)
- Full regression: 84/84 files, 1698 checks green. Secret scan: clean.

## DESIGN ONLY (not wired/executed)
- TDE/keyring activation
- AppArmor enforcement (shared profile stays complain)
- 5F (JSON<->SQL equivalence)

## UNPROVEN LIVE
- Everything in live_bootstrap.py against a real mysqld process
- OS identity/filesystem/systemd unit install (deploy/install-yandi.sh has never run)
- GRANT/trigger enforcement against a real server
- restart-persistence, shadow-write against a live dedicated instance

## KNOWN RISK
First live unknown: whether `ALTER USER ... IDENTIFIED WITH auth_socket` succeeds as the
first command immediately after `mysqld --initialize` (password-sandbox exit path).
If it fails, live_bootstrap.py must fail closed, not fall back to any other auth mode.

## EXACT OWNER COMMAND
```
sudo ./deploy/install-yandi.sh
```

## EXPECTED NEXT LIVE CHECKS (after owner runs the command, real stdout/stderr only)
1. yandi-db OS user created, /var/lib/yandi + /etc/yandi + /run/yandi + /var/log/yandi exist
2. mysqld --initialize succeeded, temp root password captured from error log
3. ALTER USER ... IDENTIFIED WITH auth_socket succeeded for root (the known risk above)
4. run_bootstrap() applied schema + triggers + grants without error
5. instance_identity row + /etc/yandi/mysql/instance.id agree
6. socket-only reachable (no TCP listener on the dedicated instance)
7. systemd unit active, AppArmor did not block startup (complain mode, check dmesg for denials anyway)
8. system_awareness.py's yandi_db_instance section reflects the new live state correctly

## STOP CONDITIONS
- Any step above fails -> STOP, report exact stdout/stderr, do not retry with elevated workarounds
- ALTER USER auth_socket fails -> STOP, do not fall back to password auth silently
- Any ambiguity about which mysqld instance was touched -> STOP, do not proceed
- Any output suggesting the shared FastPanel instance was touched -> STOP immediately

## ABSOLUTE PROHIBITIONS (unchanged, still in force)
- NO PUSH
- NO RESET
- NO shared FastPanel :3306 access, ever, for any reason
- NO fallback to TCP if socket/auth_socket path fails
- NO starting 5F (JSON<->SQL equivalence) before this checkpoint is live-proven
- NO FastPanel configuration changes of any kind
