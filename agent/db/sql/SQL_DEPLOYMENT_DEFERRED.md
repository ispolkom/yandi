# SQL Deployment — Deferred, then REOPENED (DATABASE BOOTSTRAP V1)

**Update (DATABASE BOOTSTRAP V1)**: this file's original "do not touch
`install-yandi.sh`'s stub" instruction has been explicitly superseded —
the project owner reopened this work under a new mandate ("YANDI
DATABASE BOOTSTRAP V1"), which is exactly the "real canonical cutover"
moment this file said would justify it. `run_python_bootstrap()` is no
longer a bare stub (see `MIGRATION_STATUS.md`'s current-phase section
for the full list of what changed); the script itself is still
`sudo`-gated and has never been executed. Sections 1-3 below are kept
as-written for historical/orientation context — where they say "do not
touch," read that as "did not touch WITHOUT this reopening," not as a
rule still in force after it. **The one thing sections 1-3 say that
remains an absolute, unconditional rule regardless of any future
reopening: the shared FastPanel MySQL instance (`*:3306`) is never a
YANDI dependency, never connected to, never modified — see mandate §0/
§26, "Это абсолютный запрет."**

If you're reading this because you think "the dedicated DB isn't
finished, let me pick it up" **without** a mandate that explicitly
reopens it — **stop and re-read this file first**, then check
`MIGRATION_STATUS.md`'s canonical status table for the CURRENT stage
before assuming anything below still blocks you.
Details: `DEDICATED_INSTANCE_DESIGN.md`, `SECURITY_ARCHITECTURE.md`,
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

## Status (canonical, supersedes prose elsewhere on drift — but
## `MIGRATION_STATUS.md`'s own table is the most current; this one is
## kept in sync with it, not the other way around)

| Stage | What | Status |
|---|---|---|
| 5E | SQL shadow wiring | DONE |
| 5E-S | SQL Bastion (offline design + regression) | DONE |
| 5E-S2 | Dedicated DB design/audit | DONE |
| 5E-S2-LIVE (DATABASE BOOTSTRAP V1) | Privileged deployment | OFFLINE PREP DONE — AWAITING OWNER SUDO |
| 5F | JSON↔SQL equivalence | NOT STARTED |
| RESET | Clearing old runtime history | FORBIDDEN |

**This reopening was exactly the "real canonical cutover" decision the
original version of this file said would justify revisiting it** — see
the update note at the top of this file and `MIGRATION_STATUS.md`'s
current-phase section for what changed. What is STILL true regardless:
the shared FastPanel instance stays off-limits forever, and 5F stays
NOT STARTED until a dedicated instance genuinely exists and the owner
decides to start it as its own separate task.
