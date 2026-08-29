# YANDI Dedicated Database Appliance — Design (Этап 5E-S2)

Read alongside `SECURITY_ARCHITECTURE.md`/`SECURITY_THREAT_MODEL.md`
(Этап 5E-S) and `DISK_CAPACITY_REPORT.md`. This document is
**non-privileged design/audit only** — nothing described here has been
executed. No sudo was requested, obtained, or bypassed to produce it.

Every fact below is marked **LIVE OBSERVED** (checked directly on this
host, without root, this session) vs **DESIGN DECISION** (this
document's own proposal, not yet built or verified) — per the owner's
explicit instruction not to conflate observation with proven security.

## A. Host facts (LIVE OBSERVED)

| Fact | Value |
|---|---|
| Percona version | 8.0.46-37-1.bookworm (`percona-server-server`) |
| `mysqld` binary | `/usr/sbin/mysqld` |
| `mysql`/`mysqladmin`/`mysqld_safe` | `/usr/bin/mysql`, `/usr/bin/mysqladmin`, `/usr/bin/mysqld_safe` |
| Shared instance | Running now, FastPanel-managed (`/etc/mysql/my.cnf.fastpanel/99-fastpanel.cnf`), listening `*:3306` — **untouched, not inspected beyond what Этап 5E-S already read-only-checked** |
| Existing multi-instance mechanism | `mysql@.service` systemd TEMPLATE unit exists (`/lib/systemd/system/mysql@.service`), currently `disabled`, hard-codes `User=mysql`/`Group=mysql` |
| OS user `mysql` | uid=109, gid=119, shell `/bin/false`, home `/var/lib/mysql` — exists, used by the shared instance |
| OS user `yandi-db` | **Does not exist** — would need creation (`useradd`, requires root) |
| AppArmor | Enabled (`/sys/module/apparmor/parameters/enabled` = `Y`); one profile, `/etc/apparmor.d/usr.sbin.mysqld`, mode **`complain`** (observability only, not enforcing) |
| Keyring components installed | `component_keyring_file.so`, `component_keyring_kmip.so`, `component_keyring_kms.so`, plus legacy `keyring_file.so`/`keyring_vault.so`/`keyring_udf.so` — **AVAILABLE, not configured, not TDE-proven** |
| Root filesystem | `/dev/sdb2`, 218G total, currently 126G free (40% used) — see `DISK_CAPACITY_REPORT.md` for the full, separately-flagged volatility note |
| `sudo` group membership | The operating OS user (`iam`) is in the `sudo` group |
| Non-interactive privileged execution | **Absent** — `sudo -n` (any command) fails with "a password is required"; no NOPASSWD rule exists |
| systemd version | 252 (252.39-1~deb12u2) — modern, supports every sandboxing directive referenced in §E below |
| `auth_socket.so` | Present (`/usr/lib/mysql/plugin/auth_socket.so`) |
| PyMySQL | 2.2.8, supports `unix_socket=` connection parameter |
| Docker | Not installed |

## B. Shared FastPanel DB — untouched (proof)

No command in this or the prior 5E-S session wrote to, connected to,
restarted, or altered any file under `/etc/mysql/` (outside read-only
`cat`/`grep`), `/var/lib/mysql/` (a `du` attempt was correctly refused
— `Отказано в доступе`, confirming its `0700` permissions from a
non-root user), or the `mysql`/`mysql@.service` systemd units (read
only). No `GRANT`/`CREATE USER`/`ALTER` statement has ever been issued
against port 3306 in this codebase — confirmed by the same static
grep this session's own regression suite already runs
(`agent/db_sql_security_injection_regression_test.py`,
`agent/db_sql_security_privilege_regression_test.py`).

## C. Dedicated instance topology (DESIGN DECISION)

```
/var/lib/yandi/mysql/data/          -- InnoDB datadir (new, dedicated)
/var/lib/yandi/mysql-keyring/       -- Percona keyring backend storage
/run/yandi/mysql/mysql.sock         -- Unix socket (systemd RuntimeDirectory=yandi/mysql, tmpfs, recreated each boot)
/run/yandi/mysql/mysql.pid          -- pidfile (same systemd-managed directory as the socket)
/run/yandi/bootstrap/               -- root:root 0700, NEVER a systemd RuntimeDirectory= target — the
                                        one-time temp-password marker lives here (survives every mysqld
                                        service stop/start; see install-yandi.sh's ensure_secure_bootstrap_dir())
/etc/yandi/mysql/my.cnf             -- dedicated instance config
/var/log/yandi/mysql-error.log      -- error log (dedicated, never merged with FastPanel's)
/var/lib/yandi/integrity/           -- external integrity checkpoints (mandate §27, Этап 5E-S §15)
```

Chosen over `/mnt/backup` (see `DISK_CAPACITY_REPORT.md`'s candidate-
location analysis) because `/dev/sdb2` follows Linux FHS convention for
service state (`/var/lib/<service>`) and putting the LIVE datadir on a
volume already labeled/mounted for backup purposes would conflate two
different reliability expectations — that decision is recorded, not
silently assumed. `/run/yandi` (not `/var/run/yandi`) matches this
system's own convention — `mysql@.service`'s `RuntimeDirectory=mysqld`
already resolves to `/run/mysqld` for the shared instance.

**None of these paths exist yet.** No directory listed above has been
created.

## D. OS identity (DESIGN DECISION)

A **new, dedicated `yandi-db` system account** — NOT the shared `mysql`
OS user. Reasoning: `mysql@.service`'s packaged template hard-codes
`User=mysql`, and reusing it as-is would run YANDI's dedicated mysqld
under the EXACT SAME OS identity (uid 109) as the shared FastPanel
instance — meaning filesystem ownership alone would not distinguish
"YANDI's files" from "FastPanel's files" at the OS level, undermining
the entire point of a dedicated instance. A custom unit (§F below),
not an instantiation of the packaged template, is required to set
`User=yandi-db`/`Group=yandi-db` instead.

`useradd --system --no-create-home --shell /usr/sbin/nologin
--home-dir /var/lib/yandi yandi-db` (or the distribution-appropriate
equivalent) — a system account, no login shell, no home directory
outside `/var/lib/yandi` — mirrors the existing `mysql` account's own
posture (`/bin/false`, system uid range) rather than inventing a
different convention.

**Not created.** Requires root; this pass does not attempt it.

## E. Socket isolation (DESIGN DECISION)

`/run/yandi/` directory: `root:yandi-db 0750` (or `yandi-db:yandi-db
0750` — decided at install time based on which supervising process
needs directory-traversal access, e.g. a healthcheck script running as
a different user). The socket file itself
(`/run/yandi/mysql/mysql.sock`) inherits mysqld's own `umask`-controlled
creation mode — Percona/MySQL typically creates the socket at `0777`
by default (`--socket` has no built-in mode restriction of its own in
stock Percona 8.0), so the DESIGN explicitly sets
`--socket-umask` behavior via directory permissions (the socket lives
inside a `0750` directory only `yandi-db` and its group can traverse)
rather than relying on the socket file's own mode bits, which Percona
8.0 does not directly expose a config variable to restrict as tightly
as this design wants (verified from installed config documentation
available on this host — NOT from a live test, since the instance
doesn't exist to test the actual resulting socket permissions against;
flagged as a **LIVE VERIFICATION REQUIRED AT INSTALL TIME** item, not
assumed to work exactly as described).

**PET/NODE/web processes get zero access** to `/run/yandi/` by not
being members of the `yandi-db` group. **Current honest boundary**:
AGENT and the persistence layer are still one OS process today (the
same `iam`-owned Python process runs `agent.orchestrator_v2` and
`agent.db.sql.*`) — this design doesn't invent a new IPC split (mandate
§35 is explicit that this is future work, not required now). The
REALISTIC connecting identity for `YANDI_RUNTIME` is therefore whatever
OS user actually runs `pet/council_chat_server.py` today (`iam`, per
`whoami` at the start of this session) — a fact this design records
honestly rather than assuming a cleaner separation that doesn't exist
yet.

## F. Network exposure (DESIGN DECISION)

`skip-networking` (not merely binding to `127.0.0.1`) — **zero TCP
listener**, matching mandate §3's explicit "NOT 127.0.0.1:3307 — NO TCP
LISTENER" instruction. `mysqlx=OFF` (the X Plugin, port 33060) —
matches the pattern the shared instance's own FastPanel config already
uses (`mysqlx=OFF` was found there too, independently), for the same
reason. Verification at install time: `ss -lntp` / `ss -lx` must show
NO listener process matching the dedicated instance — this is a
**LIVE VERIFICATION REQUIRED AT INSTALL TIME** item (§L below), not
achievable to confirm without the instance actually running.

## G. AppArmor (DESIGN DECISION + HONEST LIMITATION)

**Cannot create a second, differently-scoped AppArmor profile for the
same `/usr/sbin/mysqld` binary path using a simple, safe mechanism.**
AppArmor profile *attachment* in this distribution's configuration is
keyed by the executable's path (`/usr/sbin/mysqld ...`), not by the
invoking OS user, arguments, or systemd unit — the dedicated instance,
launched as `yandi-db` running the SAME `/usr/sbin/mysqld` binary,
would be confined by the EXACT SAME existing profile as the shared
instance. There is no supported way to give the two instances different
AppArmor confinement without either (a) editing the ONE shared profile
file (forbidden — risks the shared FastPanel instance, explicitly
ruled out by the owner) or (b) placing a second copy of the `mysqld`
binary at a distinct path outside package management specifically to
get a distinct attachment — rejected as fragile and against Debian
packaging norms (a security-critical binary maintained outside `apt`'s
update/security-patch path).

**What IS safe and additive**: a LOCAL override file,
`/etc/apparmor.d/local/usr.sbin.mysqld` — Debian's own supported
customization mechanism for `#include <local/usr.sbin.mysqld>`-style
extension (the packaged profile does NOT currently include this
directive, so this itself is a small, one-line ADDITIVE change to the
shared profile file to enable the include point — this line is the
only shared-profile touch this design proposes, and it is additive
only: it adds a path the mysqld binary MAY use, it removes nothing and
restricts nothing already granted, so the shared instance's own
confinement is unaffected). The local override then lists YANDI's
dedicated paths (`/var/lib/yandi/mysql/data/`, `/var/lib/yandi/mysql-
keyring/`, `/run/yandi/mysql/mysql.sock`, `/etc/yandi/mysql/`, `/var/log/
yandi/`) as allowed read/write targets.

**Since the shared profile is currently `complain` mode, this addition
has NO enforcement effect on EITHER instance today** — it only
prepares for a future point where the profile might be switched to
`enforce` (which this design does NOT propose doing to the shared
profile — that decision belongs to whoever administers the shared
FastPanel host, explicitly out of scope here). Recorded honestly per
the owner's instruction: **"НЕ изображай изоляцию, которой фактически
нет"** — AppArmor does NOT provide meaningful process-level isolation
between the two mysqld instances in this design, today or as designed;
real isolation between them comes from OS user/filesystem ownership
(§D), no shared TCP surface (§F), and separate SQL-level credentials/
grants (Этап 5E-S), not from AppArmor.

## H. Authentication (DESIGN DECISION, two options recorded)

**Option 1 (preferred): Unix socket peer-credential auth
(`auth_socket`)**. `auth_socket.so` is present on this build;
PyMySQL 2.2.8 supports `unix_socket=` connections. If `YANDI_RUNTIME`
is created `IDENTIFIED WITH auth_socket AS 'iam'` (mapping to the
actual OS user that runs the AGENT process today), a local connection
authenticates by kernel-verified peer UID — **no SQL password exists
to leak from config/env/logs at all**, closing T7's entire attack
class rather than mitigating it. Constraint, recorded honestly: this
ties DB access to whichever OS user the AGENT process happens to run
as — if that ever changes (e.g. a future dedicated `yandi-agent`
system account, per §35's future IPC split), the `auth_socket` mapping
must be updated as part of that same change, not forgotten.
`connection.py` would need a `unix_socket=` code path added (currently
TCP-`host`/`port`-only) — **not built this pass**.

**Option 2 (fallback): cryptographically random credential + protected
OS secret file**. If `auth_socket` proves fragile in practice (e.g. a
future containerized deployment where the connecting process's real
UID isn't stable/predictable), fall back to the SAME pattern already
proven this session for the KEK (`agent/db/sql/keys.py`'s `0600`-file,
outside-repo convention) — a high-entropy (`secrets.token_urlsafe(32)`
-class) password, generated once at bootstrap, stored the same way the
KEK already is. Never a human-chosen or hardcoded password (mandate
§29: "NO PLAINTEXT SQL PASSWORD UX").

**Update (DATABASE BOOTSTRAP V1)**: Option 1 has now been IMPLEMENTED
as the default for YANDI_RUNTIME — `security_grants.
yandi_runtime_auth_socket_statement()`, wired through `bootstrap.
run_bootstrap(runtime_auth_socket_os_user=...)` and orchestrated by
`agent/db/sql/live_bootstrap.py`, mapped to `AGENT_OS_USER="iam"` in
`deploy/install-yandi.sh`. This is still a DESIGN/OFFLINE-TESTED choice,
not a live-proven one — **LIVE VERIFICATION IS STILL REQUIRED AT
INSTALL TIME**, specifically whether `auth_socket` peer-credential
matching actually behaves as expected against this exact Percona
8.0.46 build once a real connection is attempted. Option 2 (random
secret) remains the documented fallback and is what YANDI_MIGRATOR/
YANDI_READONLY already use unconditionally (auth_socket was only judged
worth the complexity for the hot-path runtime role).

## I. `verify_database_encryption()` / TDE — NOT configured this pass

Per the owner's explicit instruction: TDE is not configured until the
dedicated instance exists. This design only records that `component_
keyring_file.so` is AVAILABLE on this build (Percona 8.0's component-
based keyring, not the legacy plugin) — matching what `SECURITY_
ARCHITECTURE.md` §22 already anticipated for an 8.0.x target rather
than 8.4. Actual keyring configuration, tablespace/redo/undo/binlog
encryption enablement, and — critically — the "does it actually
decrypt after a restart" test (mandate §13) are ALL deferred to the
live install phase (§L, item marked BLOCKED).

## J. `bootstrap.py`'s architectural boundary (proposal, no refactor performed)

```
OS BOOTSTRAP           (NEW, this pass's design — a shell script,
                         requires root, run ONCE by the human operator
                         via `sudo`)
    |  creates: yandi-db user, directories, ownership, my.cnf,
    |  AppArmor local-override line + file, systemd unit,
    |  initializes datadir, starts the service
    v
DB INSTANCE BOOTSTRAP   (part of the same OS-level script — starting
                         mysqld for the first time with the initial
                         admin capability the distribution's own
                         `mysqld --initialize` flow provides)
    v
YANDI SCHEMA BOOTSTRAP  (EXISTING agent/db/sql/bootstrap.py, UNCHANGED
                         this pass — connects via pymysql as whatever
                         admin capability the OS bootstrap step handed
                         it, creates yandi_epistemic + the 3 lesser
                         roles + schema + triggers)
    v
SECURITY BOOTSTRAP      (ALSO already inside the existing bootstrap.py
                         — apply_immutability_triggers(), no separate
                         module needed)
```

**`agent/db/sql/bootstrap.py` is NOT extended to do OS-level work**
(directory creation, `useradd`, systemd, AppArmor, `mysqld
--initialize`) — it stays exactly what it already is: a DB-level
Python bootstrapper that assumes a live, reachable MySQL server and a
bootstrap-capable SQL connection already exist. The NEW OS-level shell
script (§K) is a separate, thin orchestration layer whose LAST step is
to invoke the existing `agent.db.sql.migrate`/`agent.db.sql.bootstrap`
Python modules over the freshly-started dedicated socket — reusing
them, not duplicating their logic, matching this whole project's
established "no parallel truths" discipline.

## K. Privileged install plan (exact operations, NOT executed)

The eventual `sudo ./install-yandi` entry point (`deploy/install-
yandi.sh` in this repo, written this pass, NOT run) performs, in
order:

1. **PRECHECK**: confirm running as root; confirm `percona-server-
   server` package present; confirm target Percona major/minor
   version is one this design has been written against (8.0.x) —
   refuse to proceed silently on an unexpected version.
2. **DISK GATE**: read free bytes/inodes on the target filesystem via
   `agent.db.sql.storage_policy.classify_storage_state()` — refuse to
   initialize a new datadir if the CURRENT state is anything worse
   than `NORMAL`. (This is the install-time use of the same module
   Этап 5E-S2 §3/§4 built.)
3. **CREATE OS IDENTITY**: `useradd --system yandi-db` (idempotent —
   skip if already present, never a duplicate).
4. **CREATE FILESYSTEM**: the directories in §C, correct ownership
   (`yandi-db:yandi-db`) and modes (`0750` for the parent dirs, `0700`
   for the keyring dir specifically).
5. **INSTALL CONFIG**: write `/etc/yandi/mysql/my.cnf` (skip-
   networking, dedicated datadir/socket/pid/log paths, keyring
   component config).
6. **APPARMOR**: add the local-override include line to the shared
   profile (ONE line, additive, see §G) + write `/etc/apparmor.d/
   local/usr.sbin.mysqld` with YANDI's paths; `apparmor_parser -r` to
   reload (does NOT touch the shared instance's own running
   confinement beyond adding new allowed paths).
7. **SYSTEMD**: install `deploy/yandi-db.service` (§ below) to `/etc/
   systemd/system/`, `systemctl daemon-reload`.
8. **INITIALIZE DATADIR**: `mysqld --initialize --user=yandi-db
   --datadir=/var/lib/yandi/mysql/data --defaults-file=/etc/yandi/
   mysql/my.cnf` — produces a temporary, randomly-generated root
   password Percona prints once to its own error log (the modern,
   NOT-insecure initialization mode — mandate §6 explicitly permits
   this exact flow for a brand-new dedicated datadir).
9. **START ISOLATED MYSQL**: `systemctl start yandi-db`.
10. **VERIFY NO TCP**: `ss -lntp`/`ss -lx` show no listener for this
    instance — refuse to continue (and stop the service) if one exists.
11. **INITIAL DB BOOTSTRAP**: connect via the one-time root password
    from step 8 over the PRIVATE socket only, immediately change/
    retire it, create the lesser roles.
12. **TDE/KEYRING**: configure `component_keyring_file`, verify
    `SELECT * FROM performance_schema.keyring_component_status` (or
    the 8.0-appropriate equivalent) reports the expected backend.
13. **SCHEMA / GRANTS / TRIGGERS**: invoke the EXISTING `agent.db.sql.
    migrate`/`bootstrap.run_bootstrap()` Python modules (§J) — no
    duplicated logic.
14. **CRYPTO**: generate the application KEK (`keys.py::generate_kek()`
    + `save_kek()`) — a SEPARATE secret from anything MySQL-side,
    per Этап 5E-S §14's "two different walls" principle.
15. **INTEGRITY**: initialize the first checkpoint file location under
    `/var/lib/yandi/integrity/`.
16. **SELFCHECK**: `agent.db.sql.security_selfcheck.run_selfcheck()`
    against the live instance — refuse to declare install complete on
    any failure.
17. **REMOVE INITIAL BOOTSTRAP CAPABILITY**: drop/lock the temporary
    root-equivalent account used in steps 11-13.
18. **READY**: print a plain-language success message — the human
    operator never sees or enters a MySQL password anywhere in this
    flow (mandate §29).

**Crash-recovery contract for this sequence** (mandate §23): every
step from 3 onward is checked for "already done" before being redone
(directories: `mkdir -p` semantics; OS user: `useradd` skipped if
exists; datadir: refuse to re-`--initialize` an existing non-empty
datadir — detect and switch to a resume/verify path instead; keys:
**never regenerated if a datadir already contains encrypted data** —
mirrors `keys.py::save_kek()`'s own existing "refuse to overwrite"
behavior, extended to the install script's own logic). A crash between
steps 8 and 17 must be resumable by re-running the same script, which
re-detects how far a previous attempt got via LIVE INSPECTION (does
the datadir exist and have content? does the systemd unit exist? does
the database exist? do the roles exist?) — **never from a separately-
trusted state file** (mandate §24: state files may assist, must never
replace live verification).

## L. Live Deployment Gate — what remains genuinely impossible without owner action

Everything in §K is a **plan**, not a completed action. The following
cannot be verified, and this design does not claim they are, until an
actual `sudo ./install-yandi` run happens:

- Real TCP-exposure absence (`ss -lntp` against a running dedicated
  instance).
- Real socket permission enforcement.
- Real AppArmor local-override syntax correctness (untested against
  `apparmor_parser`).
- Real `auth_socket` end-to-end behavior with this Percona build +
  PyMySQL 2.2.8 (§H).
- Real TDE/keyring activation and the restart-decrypts test (§I,
  mandate §13/§28).
- Real GRANT enforcement (`SHOW GRANTS`, attempted forbidden
  operations — Этап 5E-S2 §25/§26).
- Real trigger enforcement (attempted UPDATE/DELETE against canonical
  tables).
- Real crash-recovery behavior of the install script itself.
- Real disk-usage growth rate under actual YANDI workload (needed to
  validate `storage_policy.py`'s default thresholds are the right
  SIZE for this workload, not just the right SHAPE).

None of these can be responsibly claimed PROVEN from design and unit
tests alone — consistent with this whole engagement's own established
`PROVEN (live)` / `PROVEN (static/mock)` / `DESIGNED` / `BLOCKED`
vocabulary (SECURITY_ARCHITECTURE.md §0).
