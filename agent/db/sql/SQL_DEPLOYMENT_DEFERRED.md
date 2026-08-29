# SQL Deployment — Deferred (read this before touching `agent/db/sql/` or `deploy/`)

If you're reading this because you think "the dedicated DB isn't
finished, let me pick it up" — **stop and re-read this file first.**
5E-S2 ended deliberately at design/audit, not because it ran out of
time. Details: `DEDICATED_INSTANCE_DESIGN.md`, `SECURITY_ARCHITECTURE.md`,
`SECURITY_THREAT_MODEL.md`. This file is the 30-second version.

## 1. WHAT IS ALREADY DESIGNED (do not redesign — read first, extend if truly needed)

- Dedicated `yandi-db` instance, isolated OS identity from the shared
  FastPanel `mysql` account.
- Socket-only, zero TCP surface (`skip-networking` + systemd
  `RestrictAddressFamilies=AF_UNIX`, defense in depth).
- Role split: `YANDI_BOOTSTRAP` / `YANDI_MIGRATOR` / `YANDI_RUNTIME` /
  `YANDI_READONLY` (`security_grants.py`).
- TDE/keyring direction: Percona 8.0 component keyring
  (`component_keyring_file.so`, confirmed present) — NOT MySQL 8.4's
  mechanism, this host is 8.0.46-37.
- Immutable tables: A/B/C/D classification + BEFORE UPDATE/DELETE
  triggers (`schema.py::TABLE_CLASSIFICATION`, `security_triggers.py`).
- Crypto/integrity: AES-256-GCM + key hierarchy (`crypto.py`,
  `keys.py`), HMAC hash-chain + rollback checkpoint (`integrity.py`).
- Storage policy: NORMAL/LOW/CRITICAL/EXHAUSTED with hysteresis,
  pre-write reserve guard (`storage_policy.py`) — closes T27.
- Privileged-installer boundary: OS bootstrap (root, one-time) vs. DB/
  schema/security bootstrap (existing `bootstrap.py`, unchanged) —
  never merged into one root-running Python file.

All of the above is **unit-tested against fakes**, not live-proven.

## 2. WHAT REQUIRES FUTURE OWNER-SUDO (do not attempt without the owner explicitly re-opening this)

- Create the `yandi-db` OS account.
- Create `/var/lib/yandi/`, `/etc/yandi/`, `/run/yandi/`, `/var/log/yandi/`.
- Install/enable `deploy/yandi-db.service`.
- Add the AppArmor additive local-override
  (`/etc/apparmor.d/local/usr.sbin.mysqld`).
- `mysqld --initialize` the new datadir.
- Live tests that cannot be faked or mocked: `auth_socket` end-to-end,
  TDE/keyring activation + restart-decrypts, GRANT enforcement (`SHOW
  GRANTS` + attempted forbidden ops), trigger enforcement, crash-
  recovery of the installer itself, attack test against the real
  runtime/readonly credentials.

## 3. DO NOT TOUCH UNTIL THIS FILE IS DELETED/REVISED BY THE OWNER

- The shared FastPanel MySQL instance (`*:3306`) — never a YANDI
  dependency, never connected to, never modified.
- `deploy/install-yandi.sh` — do not turn its `run_python_bootstrap()`
  stub into a working installer. It is intentionally incomplete
  (`NOT PRODUCTION / DO NOT RUN`, per its own header).
- Any live dedicated DB instance — none exists; do not create one
  "just to test something."
- TDE configuration — direction is decided, activation is not.
- AppArmor enforcement — the shared profile stays `complain`; do not
  switch it to `enforce`.
- 5F (JSON↔SQL equivalence) — cannot start without a real SQL
  connection, which does not exist. Do not attempt it against the
  shared FastPanel DB under any circumstance.

## Status (canonical, supersedes prose elsewhere on drift)

| Stage | What | Status |
|---|---|---|
| 5E | SQL shadow wiring | DONE |
| 5E-S | SQL Bastion (offline design + regression) | DONE |
| 5E-S2 | Dedicated DB design/audit | DONE |
| 5E-S2-LIVE | Privileged deployment | BLOCKED / DEFERRED |
| 5F | JSON↔SQL equivalence | NOT STARTED |
| RESET | Clearing old runtime history | FORBIDDEN |

**Next SQL work is not "finish the installer."** It is whatever the
project owner decides warrants a real canonical cutover — at which
point THIS file gets revised or removed by that decision, not by a
future session inferring the DB "still needs finishing."
