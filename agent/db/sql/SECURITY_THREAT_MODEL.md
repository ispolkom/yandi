# YANDI SQL — Security Threat Model (Этап 5E-S)

Read alongside `SECURITY_ARCHITECTURE.md`. This file is the adversarial
half: concrete attacks against `agent/db/sql/*` and its canonical
memory, not general security advice.

**Honesty rule this document follows throughout**: a defense is listed
only where it actually exists in code today, or is explicitly marked
DESIGNED/NOT YET LIVE. Nothing here claims a live-tested guarantee that
wasn't live-tested — see each threat's `PROOF STATUS`.

## Real environment facts this threat model is grounded in

Checked directly on this machine during the 5E-S audit (2026-08-28),
not assumed:

- **Percona Server 8.0.46-37** (`percona-server-server` package),
  `default-authentication-plugin=mysql_native_password`,
  `performance-schema=OFF`, `disable-log-bin`.
- **This is a SHARED, externally-managed instance** — config is owned
  by `/etc/mysql/my.cnf.fastpanel/99-fastpanel.cnf` (FastPanel hosting
  control panel), not a dedicated YANDI-managed database server. This
  changes the applicable trust model — see `SECURITY_ARCHITECTURE.md`
  §4 (MANAGED vs EXTERNAL profile) and §13.
- **MySQL is listening on `*:3306`** (`ss -tln` confirms `LISTEN 0 151
  *:3306 *:*`), not loopback-only. Whether an external firewall blocks
  this from the actual internet was NOT verified from inside this
  environment (no `ufw`, no passwordless `iptables -L`) — treat as
  UNKNOWN, not SAFE.
- `mysqlx=OFF` is already set (X Plugin / port 33060 already disabled
  by the existing FastPanel config) — one hardening item already
  satisfied, not by YANDI.
- No `YANDI_SQL_USER`/`YANDI_SQL_PASSWORD` exist in this environment,
  and no passwordless local admin path exists either (`sudo -n mysql`
  fails: "a password is required"). Every threat below that requires a
  live connection is therefore additionally gated by "credentials
  don't exist yet" on top of whatever this document says about it.
- `agent/db/sql/connection.py` never sets `client_flag=MULTI_STATEMENTS`
  and never passes `local_infile=True` — both are pymysql defaults
  (off) that this codebase does not override.
- Every `cur.execute(...)` call in `agent/db/sql/repositories.py` (37
  call sites, grepped exhaustively) binds values via `%s` placeholders
  with a separate params tuple — zero f-string/`.format()`/string-
  concatenation SQL construction found anywhere in that file or in
  `shadow_write.py`/`schema.py`/`migrate.py`/`connection.py`.

---

## T1 — SQL injection through user-supplied query text

**ATTACK**: a user submits a question containing `'; DROP TABLE
question; --` or similar, hoping it reaches a SQL statement unescaped.

**BOUNDARY**: `agent/db/sql/repositories.py::resolve_question()` is the
only function that ever inserts raw user text (`raw_text`,
`anonymized_text`) into SQL.

**PREVENTION**: `raw_text` is always bound as a `%s` parameter
(pymysql), never concatenated into the SQL string. Verified by direct
reading of every `.execute()` call in the file (see facts above) and by
`agent/db_sql_security_injection_regression_test.py` (this pass),
which runs the actual repository functions with adversarial payloads
against a `FakeConnection` and asserts the payload always arrives as a
bound parameter tuple element, never inside the SQL text string.

**DETECTION**: static regression greps the whole `agent/db/sql/`
package for dangerous SQL-construction patterns (`f"..SELECT`,
`.format(` near SQL, `%` string interpolation, `+` concatenation next
to SQL keywords) — new code that introduces one fails CI immediately.

**RECOVERY**: N/A — prevented structurally, not detected-and-repaired.

**RESIDUAL RISK**: a future contributor could still write
`cur.execute(f"...")` by hand; the static grep regression is the
safety net, not a language-level guarantee (Python has no compile-time
SQL-safety check). **PROOF STATUS: STATIC + FUNCTIONAL (mock),
LIVE not tested (no DB).**

## T2 — SQL injection through URL / source metadata

**ATTACK**: a scraped web page's URL, title, or excerpt contains SQL
metacharacters or a crafted string designed to break out of a query.

**BOUNDARY**: `shadow_record_claims_and_evidence()` /
`get_or_create_resource()` / `record_source_observation()` — all
values (`canonical_uri`, `content_excerpt`, `source_class`, etc.)
originate from web content.

**PREVENTION**: same as T1 — every value is a bound parameter. URI
length is bounded by the column type (`VARCHAR(2048)`) and
`content_excerpt` is truncated to 2000 chars at the call site
(`agent/db/sql/shadow_write.py`) before it ever reaches SQL — belt and
suspenders, not a security boundary by itself (truncation prevents
resource exhaustion, not injection; parameterization prevents
injection).

**DETECTION/RECOVERY**: same as T1.

**RESIDUAL RISK**: same as T1. **PROOF STATUS: STATIC + FUNCTIONAL
(mock).**

## T3 — SQL injection through remote NETWORK_NODE text

**ATTACK**: a future remote YANDI node sends an adversarial answer/claim
string in its provenance packet.

**BOUNDARY**: N/A today — **NETWORK_NODE transport does not exist**
(confirmed: `agent/db_sql_network_node_provenance_regression_test.py`
proves zero write-path code exists for it). The only exposure is the
schema's `node_id` column and the future documented extension point.

**PREVENTION**: whenever a NODE_OBSERVATION writer is eventually built,
it MUST go through `agent/db/sql/repositories.py` and inherit the same
parameterized-query discipline this threat model verifies for every
other writer — this is process/architecture guidance, not code that
exists yet.

**RESIDUAL RISK**: a future implementer could bypass the repository
layer. Mitigated by T-generic (§ REPOSITORY IS SOLE SQL OWNER, see
SECURITY_ARCHITECTURE.md §8) and by this threat model existing as a
checklist for that future work. **PROOF STATUS: NOT APPLICABLE YET
(no transport exists) — architecturally anticipated only.**

## T4 — SQL injection through AI_CHAT response text

**ATTACK**: an external AI model's self-reported answer/source text
contains adversarial content.

**BOUNDARY/PREVENTION/RESIDUAL RISK**: identical reasoning to T3 — no
AI_CHAT transport or writer exists yet (confirmed:
`agent/db_sql_ai_provenance_schema_regression_test.py` proves zero
write-path code for `ai_observation`/`ai_reported_source`).
**PROOF STATUS: NOT APPLICABLE YET.**

## T5 — Malicious / malformed JSON input

**ATTACK**: a legacy JSON store (`registry/*.json`, trace JSONL) is
crafted to break the loader, or a JSON-derived value is passed to SQL
in an unexpected type (e.g. a `dict` where a `str` is expected).

**BOUNDARY**: every `ClaimFamilyRegistry`/`BeliefManager`/
`FamilyDependencyGraph` `_load()` already wraps `json.load()` in
`try/except Exception: start empty` (confirmed by reading all three
this session) — a corrupt file degrades to an empty in-memory registry,
never a crash, never partial/torn data written back.

**PREVENTION**: pymysql's parameter binding also defeats type-confusion
injection — passing a `dict` where a `str` column is expected raises a
driver-level error (caught by the shadow layer's fail-open `except
Exception`), not a query with attacker-controlled structure.

**DETECTION**: `_shadow()`'s per-call exception log line
(`[SqlShadow] ... FAILED`) when `verbose=True`.

**RECOVERY**: N/A — the failed shadow write is simply skipped; the
JSON canonical path (today) is unaffected.

**RESIDUAL RISK**: a malformed value that happens to be the RIGHT
Python type but wrong semantic shape (e.g. a `family_id` string that's
actually attacker-controlled arbitrary text) is still accepted as data
— that's correct (mandate: NETWORK INPUT IS DATA, NEVER CODE), not a
gap. **PROOF STATUS: STATIC (code read) + existing regression
(`claim_family_registry`/`belief_manager` fail-safe `_load()` tests).**

## T6 — Stored XSS/JS inside a saved answer/trace

**ATTACK**: a claim or answer contains `<script>alert(1)</script>` or
similar, hoping a future UI renders it unescaped.

**BOUNDARY**: `agent/db/sql/repositories.py` stores it as an opaque
`TEXT`/`VARCHAR` value — SQL has no concept of "executing" stored text.

**PREVENTION**: this is fundamentally an **output-encoding** problem at
whatever future UI/API renders decrypted knowledge, not a database
problem — mandate §31: "DECRYPTED TEXT != HTML CODE." This threat model
does not extend a full web-security rewrite (out of scope, mandate
§31), but the invariant is recorded so a future UI implementer inherits
it explicitly.

**DETECTION/RECOVERY**: N/A at this layer.

**RESIDUAL RISK**: real, but **deliberately out of scope for 5E-S** —
tracked as a documented obligation for whatever renders this content,
not fixed here. **PROOF STATUS: NOT APPLICABLE TO THIS LAYER —
documented handoff only.**

## T7 — Stolen runtime SQL credential (`YANDI_SQL_USER`/`PASSWORD`)

**ATTACK**: an attacker who compromises the YANDI process (or its env)
obtains the runtime DB credential and connects directly.

**BOUNDARY**: whatever privileges the `YANDI_RUNTIME` role has (see
`SECURITY_ARCHITECTURE.md` §privilege model) — this is the entire point
of capability separation.

**PREVENTION**: `YANDI_RUNTIME` (DESIGNED, not yet provisioned on a
live server — no credentials to create it with) has no `DROP`,
`TRUNCATE`, `ALTER`, `CREATE`, `GRANT`, `SUPER`, `FILE`, `PROCESS`; no
`UPDATE`/`DELETE` on canonical tables (enforced by GRANT absence AND by
`BEFORE UPDATE`/`BEFORE DELETE` triggers as defense-in-depth — see
`agent/db/sql/security_grants.py`/`security_triggers.py`, this pass).

**DETECTION**: `agent/db/sql/security_selfcheck.py` (this pass) is
designed to run `SHOW GRANTS FOR CURRENT_USER()` at startup and refuse
to proceed if the connected role has any privilege outside its declared
allow-list — **DESIGNED, not live-verified (no DB)**.

**RECOVERY**: rotate the credential; the attacker's writes (if any)
are still bounded by what INSERT-only, no-DELETE access can do — they
can add forged rows (see T18) but cannot erase or rewrite existing
canonical history.

**RESIDUAL RISK**: a stolen runtime credential CAN still insert forged
`INSERT`-only rows (forged claims, forged observations) — this is why
T18/tamper-evidence (integrity journal) exists as an independent
second wall, not because GRANT absence alone is a complete answer.
**PROOF STATUS: DESIGNED (GRANT SQL + trigger SQL written and unit-
tested against a fake connection) — LIVE ENFORCEMENT BLOCKED (no
credentials to create the role or connect as it).**

## T8 — Direct external connection to MySQL (bypassing YANDI entirely)

**ATTACK**: an attacker on the internet connects straight to
`<host>:3306`, bypassing the application layer.

**BOUNDARY**: OS/network — outside `agent/db/sql/`'s control entirely.

**PREVENTION**: **NOT CURRENTLY SATISFIED on this machine** — see the
"Real environment facts" section above: `*:3306` is listening on all
interfaces, and this is a shared FastPanel-managed instance whose
`bind-address`/firewall YANDI does not own and must not silently change
(mandate §12/§13: don't break someone else's MySQL install). Whether an
external firewall actually blocks inbound 3306 was **not verified** —
unknown, not assumed safe.

**DETECTION**: none from inside this codebase — this is infrastructure,
not application, monitoring.

**RECOVERY**: N/A from application code.

**RESIDUAL RISK**: **HIGH AND UNRESOLVED** on the current shared
instance. This is the single most important open finding in this
threat model. `SECURITY_ARCHITECTURE.md` records this as a required
OWNER DECISION: either (a) provision a dedicated, YANDI-managed local
MySQL/Percona instance where socket-only binding is safe to configure,
or (b) accept a documented DEGRADED SECURITY PROFILE on the shared
instance and rely entirely on strong authentication + TLS + the
account-level/application-level defenses in this document. **5E-S does
NOT resolve this** — it cannot, without credentials and without
authority to reconfigure a shared server. **PROOF STATUS: LIVE FACT
(directly observed `ss -tln` output) — mitigation BLOCKED, requires
owner decision + infrastructure change outside this codebase's
authority.**

## T9 — Stolen datadir (`/var/lib/mysql`)

**ATTACK**: an attacker obtains a copy of the raw datadir files
(backup theft, disk theft, misconfigured snapshot).

**BOUNDARY**: filesystem-level; `drwxr-x--- mysql:mysql` permissions
already correctly restrict the directory to the `mysql` OS user (`ls
-ld /var/lib/mysql` confirmed this session) — that is the FIRST wall,
already present, not created by YANDI.

**PREVENTION (second wall, this pass's design)**: application-level
AES-256-GCM encryption of sensitive `TEXT`/`VARCHAR` payload columns
(question text, answer text, claim text, evidence excerpt, source
content/URI, AI/node provenance text) means the raw datadir bytes for
those columns are ciphertext even without Percona TDE. TDE (third wall,
mandate §22) is **NOT enabled** — BLOCKED, requires real Percona access
to configure a keyring, which this pass does not have.

**DETECTION**: N/A for a passive disk copy (nothing to detect at
YANDI's layer — the theft itself is invisible to it).

**RECOVERY**: N/A — prevention (encryption) is the only lever;
detection lives at the OS/infra layer (disk encryption, access
auditing), not here.

**RESIDUAL RISK**: without TDE, non-sensitive structural data
(IDs, timestamps, enum statuses, FK relationships, table/column names)
remains plaintext in the raw files even when payload columns are
encrypted — an attacker with the datadir can see the SHAPE of YANDI's
memory (how many questions, how many claims per run, timing patterns)
without the CONTENT. **PROOF STATUS: application-layer encryption
DESIGNED + unit-tested (this pass); TDE BLOCKED (no live Percona
admin access).**

## T10 — Stolen backup

**ATTACK**: a `mysqldump`/snapshot backup is stolen from wherever it's
stored.

**BOUNDARY/PREVENTION**: identical to T9 if backups contain the same
encrypted payload columns — but mandate §36 is explicit: **no backup
pipeline exists yet in this codebase** (confirmed: no `mysqldump`,
`xtrabackup`, or backup script found anywhere in `agent/` or `pet/`
during this audit's grep). This threat is therefore currently
**mitigated only by "there is no backup to steal yet"** — not a real
defense, an absence of the attack surface.

**RESIDUAL RISK**: the moment a backup pipeline is added (mandate §36,
explicitly deferred past 5E-S), it MUST NOT be plaintext
(`mysqldump` of encrypted columns is fine — the ciphertext dumps as
ciphertext; a plaintext KEK/DEK backed up alongside the data would
defeat everything). **PROOF STATUS: NOT APPLICABLE — no backup
pipeline exists; requirement recorded for when one is built.**

## T11 — Attacker obtains SELECT-only access

**ATTACK**: attacker compromises or is given `YANDI_READONLY`.

**BOUNDARY**: `YANDI_READONLY` role (DESIGNED).

**PREVENTION**: `YANDI_READONLY` has zero write privilege (no INSERT/
UPDATE/DELETE/CREATE/DROP/ALTER) AND — critically — is never given the
application encryption key. A raw `SELECT * FROM question` through this
account returns ciphertext blobs for sensitive columns, not plaintext.
Decrypted knowledge is only ever produced by YANDI's own read API
(`agent/db/sql/repositories.py`'s `get_*`/`explain_answer`/etc.
functions), which run inside the trusted application process, never
inside the SQL client itself.

**DETECTION**: `security_selfcheck.py`'s grant audit (DESIGNED) flags
if `YANDI_READONLY` ever accumulates a write privilege.

**RECOVERY**: revoke/recreate the account.

**RESIDUAL RISK**: an attacker with SELECT + the encryption key (a much
bigger compromise, see T7/T24) could decrypt everything they can read
— SELECT-only access to ciphertext, by itself, discloses shape/volume/
timing but not content. **PROOF STATUS: DESIGNED (GRANT SQL written) —
LIVE ENFORCEMENT BLOCKED (no DB).**

## T12 — Attacker obtains INSERT at runtime level

**ATTACK**: attacker with the stolen `YANDI_RUNTIME` credential (T7)
inserts forged rows directly (not through the application logic).

**BOUNDARY**: the integrity journal (mandate §24-26, this pass's
design: `agent/db/sql/integrity.py`).

**PREVENTION**: GRANT-level INSERT cannot be prevented for a role that
legitimately needs to INSERT (that's its whole job) — this is why
INSERT-capability alone is NOT the security boundary here; **integrity
verification** is. A row inserted without a valid HMAC chain link
computed from the correct integrity key is **cryptographically
distinguishable** from a legitimately-produced one (see T18).

**DETECTION**: the read path (DESIGNED: `agent/db/sql/integrity.py`'s
verify functions) recomputes the expected HMAC for a row and flags a
mismatch — an attacker who doesn't have the integrity key (which lives
outside SQL, see T7/key hierarchy) cannot produce a row that passes
verification.

**RECOVERY**: quarantine (don't delete — mandate §29's
VALID/INVALID/UNVERIFIED_LEGACY/QUARANTINED framing) the row that fails
verification; do not silently trust it as memory.

**RESIDUAL RISK**: a forged row sits in the table even though it fails
verification (removing it would itself be a DELETE against an
append-only table — see §23) — the mitigation is "never trusted as
memory," not "cannot exist." **PROOF STATUS: DESIGNED + unit-tested
(HMAC chain math, canonical serialization) against a fake connection —
LIVE INSERT-ATTACK TEST BLOCKED (no DB, no live attacker simulation
performed).**

## T13 — Attempted UPDATE of an old QUESTION

**ATTACK/BOUNDARY/PREVENTION**: `YANDI_RUNTIME` has no UPDATE grant on
`question` (DESIGNED); `question` additionally gets a `BEFORE UPDATE`
trigger that unconditionally `SIGNAL`s an error regardless of which
account attempts it (DESIGNED, `agent/db/sql/schema.py`'s
`IMMUTABILITY_TRIGGERS`, this pass) — defense in depth: even a
misconfigured GRANT doesn't bypass the trigger.

**DETECTION**: the trigger's `SIGNAL SQLSTATE '45000'` IS the
detection — the statement fails immediately, visibly, at the point of
attempt.

**RECOVERY**: N/A — nothing to recover, the mutation never happened.

**RESIDUAL RISK**: an account with `SUPER`/`TRIGGER`-bypass privilege
(i.e., DB admin/root) can still disable or drop the trigger first —
mandate §6's own honest framing: "Абсолютного запрета для OS
root/DB root физически обеспечить нельзя... Правильная гарантия: NORMAL
YANDI OPERATION CANNOT ALTER QUESTION HISTORY. ADMIN-LEVEL TAMPERING
MUST BE DETECTABLE" — which the integrity journal (T21) is what
actually detects that case. **PROOF STATUS: DESIGNED (trigger DDL
written, unit-tested for correct SQL shape) — LIVE TRIGGER FIRING
BLOCKED (no DB to fire it against).**

## T14 — Attempted UPDATE of an old ANSWER

Same structure as T13, applied to `answer_version` (never `UPDATE`
`answer_text` — a new fact requires a new `answer_version` row per the
existing, already-implemented `record_answer_version()` design from
Этап 5). **PROOF STATUS: same as T13.**

## T15 — Attempted DELETE of history

**ATTACK**: DELETE a row from any append-only/immutable table
(`question`, `question_occurrence`, `answer_version`,
`answer_assessment`, `claim_occurrence`, `source_observation`,
`evidence_relation`, `belief_assessment_history`, `recheck_event`,
`epistemic_contradiction_observation`, `ai_observation`,
`ai_reported_source`, `run_error`, `claim_family`, `family_member`,
`source_resource`).

**BOUNDARY/PREVENTION**: `YANDI_RUNTIME` has no DELETE grant on any of
these (DESIGNED); each also gets a `BEFORE DELETE` trigger (DESIGNED).
Foreign keys from child tables use `ON DELETE RESTRICT` (default MySQL
behavior when unspecified — explicitly NOT `CASCADE`, verified in
`schema.py`: no `ON DELETE CASCADE` appears anywhere in the current
schema) so even an admin attempting to delete a parent row (e.g. a
`claim_family`) is blocked by referencing children first, an extra
speed bump before the trigger even fires.

**DETECTION/RECOVERY/RESIDUAL RISK**: same reasoning as T13.
**PROOF STATUS: DESIGNED (grants + triggers + FK RESTRICT confirmed by
static regression) — LIVE BLOCKED.**

## T16 — Attempted TRUNCATE

**ATTACK**: `TRUNCATE TABLE question` (bypasses per-row DELETE triggers
in MySQL — TRUNCATE is DDL, not DML, and does NOT fire `BEFORE DELETE`
triggers).

**BOUNDARY/PREVENTION**: this is why GRANT absence is the PRIMARY
defense here, not the trigger (triggers cannot stop TRUNCATE).
`YANDI_RUNTIME` has no `DROP` privilege — in MySQL, `TRUNCATE TABLE`
requires the `DROP` privilege on the table, so a role without `DROP`
cannot `TRUNCATE` either (documented MySQL behavior, not a YANDI
invention — cited in `SECURITY_ARCHITECTURE.md`).

**DETECTION**: `security_selfcheck.py` confirms `YANDI_RUNTIME` lacks
`DROP` at startup (DESIGNED).

**RESIDUAL RISK**: same admin-bypass caveat as T13.
**PROOF STATUS: DESIGNED — LIVE BLOCKED (this MySQL behavior is
documented/well-known, not independently re-verified against THIS
Percona 8.0.46 build in this pass, since no credentials exist to test
with).**

## T17 — Attempted DROP TABLE

Same as T16 — no `DROP` grant for `YANDI_RUNTIME`. **PROOF STATUS:
DESIGNED — LIVE BLOCKED.**

## T18 — Forged observation INSERT

Covered under T12 (the general "stolen INSERT capability" case) — a
`source_observation` row inserted without going through
`shadow_record_claims_and_evidence()`/the integrity journal has no
valid HMAC chain entry and fails verification on read. **PROOF STATUS:
same as T12.**

## T19 — Ciphertext modified in place

**ATTACK**: an attacker with row-level write access (or direct file
access, T9) flips a byte in an encrypted column's ciphertext.

**BOUNDARY/PREVENTION**: AES-256-GCM is an AEAD cipher — the
authentication tag covers the entire ciphertext; ANY bit flip anywhere
in the ciphertext or tag causes decryption to raise
`InvalidTag`/authentication failure, not silently return corrupted
plaintext. Proven this pass by
`agent/db_sql_crypto_regression_test.py`: flip one byte of a real
ciphertext produced by the real encrypt function, assert decrypt raises.

**DETECTION**: the raised exception itself, at read time.

**RECOVERY**: the row's plaintext is unrecoverable from that ciphertext
(by design — that's what "tamper-evident" means); recovery means going
back to an earlier verified state/backup, not "fixing" the ciphertext.

**RESIDUAL RISK**: none for THIS specific attack — this is exactly what
AEAD is designed to catch. **PROOF STATUS: FUNCTIONAL, fully proven
offline (pure cryptography, no DB needed).**

## T20 — Ciphertext swapped between rows/columns

**ATTACK**: attacker copies a valid ciphertext blob from `question A`'s
`raw_text` column into `question B`'s `raw_text` column — both decrypt
successfully with the same key (naive AES-GCM alone does not detect
this), silently corrupting B's data with A's plaintext.

**BOUNDARY/PREVENTION**: AAD (Additional Authenticated Data) binds each
ciphertext to `schema|entity_type|entity_id|field_name|version`
(mandate §18's exact design, implemented this pass in
`agent/db/sql/crypto.py::encrypt_field()`). Decrypting A's ciphertext
with B's AAD (different `entity_id`) fails authentication.

**DETECTION**: the raised exception at decrypt time when AAD doesn't
match the row it's being read for.

**RECOVERY**: same as T19.

**RESIDUAL RISK**: none for THIS attack, GIVEN the AAD is always
constructed correctly from the row's own real identity at both encrypt
and decrypt time — a bug that constructs the WRONG AAD (e.g. using the
wrong `entity_id` variable) would silently reopen this hole; covered by
`agent/db_sql_crypto_regression_test.py`'s explicit
"ciphertext-from-A-decrypted-with-B's-AAD-must-fail" test. **PROOF
STATUS: FUNCTIONAL, fully proven offline.**

## T21 — Deletion of one historical row (targeted, not TRUNCATE)

**ATTACK**: an attacker with row-level DELETE (T7 compromise + a
GRANT-bypass or admin-level access) deletes exactly ONE row from an
append-only table, hoping the gap is unnoticed.

**BOUNDARY/PREVENTION**: this is beyond what GRANTs/triggers alone can
stop if the attacker has admin-level bypass (see T13's residual risk)
— this is precisely what the **integrity hash chain** (mandate §24-26,
DESIGNED this pass: `agent/db/sql/integrity.py`) exists to catch. Each
`integrity_event` row's HMAC covers `previous_event_hash`, so removing
event N breaks the chain: event N+1's stored `previous_event_hash`
no longer matches any present row's `record_hash`.

**DETECTION**: a verification pass over the chain (DESIGNED:
`agent/db/sql/integrity.py::verify_chain()`) finds the discontinuity.

**RECOVERY**: the deleted row's specific content cannot be
cryptographically un-deleted by the chain alone (the chain PROVES
something is missing, it doesn't RESTORE it) — recovery requires a
prior backup/checkpoint (mandate §27/§36, both explicitly deferred past
5E-S).

**RESIDUAL RISK**: **the chain detects, it does not prevent or
restore** — this is stated plainly in `SECURITY_ARCHITECTURE.md` §14,
not oversold. **PROOF STATUS: FUNCTIONAL, hash-chain math and
break-detection proven offline against a fake connection
(`agent/db_sql_integrity_regression_test.py`, this pass) — LIVE
row-deletion-against-a-real-chain BLOCKED (no DB).**

## T22 — Rollback of the entire database to an old (internally valid) snapshot

**ATTACK**: attacker restores an old, internally-consistent backup —
every row's HMAC/chain link is individually valid (it was valid when
made), so the internal hash chain alone cannot tell "this is old" from
"this is current."

**BOUNDARY/PREVENTION**: this is exactly why mandate §27 requires an
**external checkpoint** stored OUTSIDE the database (a monotonic
sequence/hash/timestamp written to a protected local filesystem path
after each completed run) — DESIGNED this pass
(`agent/db/sql/integrity.py`'s checkpoint functions +
`SECURITY_ARCHITECTURE.md`'s recovery-model section), **not yet wired
into any startup path** (that wiring requires deciding where the
protected path lives on THIS machine, which is itself dependent on
knowing whether this stays a shared FastPanel host or becomes a
dedicated instance — see T8's residual risk; deferred, documented, not
silently skipped).

**DETECTION**: comparing DB HEAD (latest `integrity_event`'s hash)
against the external checkpoint at startup — DESIGNED, not yet wired.

**RECOVERY**: refuse to proceed with canonical writes
(`SECURITY_INTEGRITY_BROKEN` state, mandate §28) until a human resolves
which state is authoritative.

**RESIDUAL RISK**: **NOT YET MITIGATED IN THIS PASS** — the primitive
(checkpoint format, comparison logic) is designed and unit-tested, but
wiring it into the real startup sequence is deferred pending the T8
infrastructure decision. Recorded as an open item, not claimed done.
**PROOF STATUS: DESIGNED (checkpoint format + comparison logic
unit-tested) — NOT WIRED, NOT LIVE.**

## T23 — Migration/schema substitution attack

**ATTACK**: an attacker replaces `agent/db/sql/schema.py` or a future
migration file with a version that quietly drops a trigger, widens a
grant, or removes a column, hoping `migrate.py --check` doesn't notice.

**BOUNDARY/PREVENTION**: mandate §39's schema manifest — DESIGNED this
pass as an extension to `migrate.py --check` (`agent/db/sql/
security_selfcheck.py`, checking required tables/triggers/grants exist
and match an expected set) rather than a separate manifest file, to
avoid a second, driftable source of truth.

**DETECTION**: `security_selfcheck.py` run at startup/CI compares live
schema introspection (`information_schema`) against the expected
tables/triggers/grants list from `schema.py` — DESIGNED, LIVE
VERIFICATION BLOCKED (no DB).

**RECOVERY**: refuse startup / flag `SECURITY_INTEGRITY_BROKEN` on
mismatch rather than silently proceeding or silently "repairing" from
a potentially-compromised runtime account (mandate §39: "При mismatch:
не делать silent repair от runtime account").

**RESIDUAL RISK**: this catches DEPLOYED drift, not source-control
tampering before deployment — that's what code review/git history is
for, out of this document's scope. **PROOF STATUS: DESIGNED — LIVE
BLOCKED.**

## T24 — Leaked encryption key (KEK)

**ATTACK**: the KEK (outside SQL, per mandate §20) is exposed via a
misconfigured file permission, a log line, a crash dump, or process
memory inspection.

**BOUNDARY**: `agent/db/sql/keys.py` (this pass) never logs, prints, or
returns the KEK bytes from any function whose return value could reach
a log line — verified by `agent/db_sql_crypto_regression_test.py`
grepping the module for any `print`/`log` call that touches a key
variable.

**PREVENTION**: file-based KEK storage (this environment's realistic
option — see `SECURITY_ARCHITECTURE.md` §11, no systemd-credentials/TPM
available to verify on this machine) requires `0700` directory /
`0600` file permissions, owned by the service account, outside the git
repository and outside any web-served path. DESIGNED and enforced by
`agent/db/sql/keys.py::load_kek()`'s permission check (refuses to load
a key file with group/other-readable permissions) — **file itself does
not exist yet on this machine** (no bootstrap has run), so this is
unit-tested against a temp file, not proven against the real deployed
path.

**DETECTION**: `keys.py::load_kek()` raises if permissions are too
open, rather than silently loading — the "detection" IS the refusal.

**RECOVERY**: rotate the KEK (mandate §38) — re-wrap all DEKs under a
new KEK; DEK-encrypted data itself does not need re-encryption (only
the wrapping does), bounding the blast radius of a KEK rotation.

**RESIDUAL RISK**: mandate's own honest framing (§3, repeated in
`SECURITY_ARCHITECTURE.md`): "Если злоумышленник получил полный OS root
... абсолютная конфиденциальность ключей невозможна." A KEK compromise
via full host compromise (root reading process memory) is not solvable
by application design — the permission check stops the CHEAP version
of this attack (a misconfigured file, an accidental `chmod 644`), not
a determined root-level adversary. **PROOF STATUS: FUNCTIONAL (unit-
tested permission enforcement, temp files) — LIVE deployed-path
verification N/A (no bootstrap has run on this machine).**

## T25 — Lost encryption key (no attacker, just an accident)

**ATTACK**: not an attack — an operational failure (disk failure,
accidental deletion, botched rotation) destroys the only copy of a KEK
or DEK with no attacker involved.

**BOUNDARY**: `SECURITY_ARCHITECTURE.md`'s Key Lifecycle section
(mandate §37).

**PREVENTION**: this pass documents (does not yet implement, since
there is no live deployment to back up) the requirement that key
material backup is part of the SAME recovery model as data backup
(mandate §36/§37) — a key backed up nowhere is a single point of total,
permanent data loss for everything it protects.

**DETECTION**: `keys.py::load_kek()` failing to find/open the key file
IS the detection — fails closed (raises), never falls back to writing
plaintext (mandate §20/§40, tested this pass:
`agent/db_sql_crypto_regression_test.py`'s "missing key -> encrypt
raises, never returns plaintext" case).

**RECOVERY**: **NOT SOLVABLE after the fact without a backup** — this
is stated plainly, not softened. Recorded as a hard operational
requirement for whoever runs 5E-S's bootstrap for real: back up the key
material BEFORE trusting SQL as canonical for anything irreplaceable.

**RESIDUAL RISK**: total, if no key backup discipline exists at
deployment time — this is why `SECURITY_ARCHITECTURE.md` explicitly
refuses to call 5E-S "production ready" without a documented backup
plan (mandate §37, §54's Definition of Done). **PROOF STATUS: fail-
closed behavior FUNCTIONALLY proven (unit test) — backup discipline
itself is a documented REQUIREMENT, not a shipped feature (no backup
pipeline exists, T10).**

## T26 — Application compromise / RCE in the YANDI process itself

**ATTACK**: an attacker achieves remote code execution inside the
Python process running `agent.orchestrator_v2`/`pet.council_chat_
server`.

**BOUNDARY**: this is the boundary EVERYTHING in this document
ultimately reduces to in the worst case — a fully-compromised runtime
process can call any function this codebase exposes, with any
credential/key it holds in memory, exactly as if it were legitimate
code.

**PREVENTION**: capability separation (T7) bounds what the COMPROMISED
RUNTIME's own DB credential can do (no DROP/TRUNCATE/UPDATE/DELETE-
canonical even from inside a fully-owned process) — this is real
mitigation, not theater, because it constrains the SQL PRIVILEGE, which
survives even if the Python code itself is fully controlled. Mandate
§35's future PERSISTENCE SERVICE / IPC split (documented as
FEASIBILITY ONLY this pass, not built — matches the mandate's explicit
"НЕ внедрять автоматически") would further separate "the process an
attacker can RCE into" (PET/web-facing) from "the process holding DB
credentials/keys," but that is future work.

**DETECTION**: none at this layer — RCE detection is an OS/EDR
concern, outside `agent/db/sql/`.

**RECOVERY**: rotate every credential/key the compromised process could
have read; audit the integrity journal for anything it inserted.

**RESIDUAL RISK**: **explicitly acknowledged as not fully solvable by
this pass** — mandate §3's own words: "Не врать в документации, что
криптография решает полный host compromise." Capability separation
reduces blast radius; it does not eliminate it. **PROOF STATUS:
architectural mitigation DESIGNED (grant boundaries) — full RCE
scenario is inherently NOT LIVE-TESTABLE from inside this pass (would
require an actual RCE exploit, out of scope and inappropriate to
build).**

## T27 — Disk exhaustion via unbounded INSERT flood

**ATTACK**: an attacker who can trigger many requests (or has T7-level
INSERT access) floods an append-only table, exhausting disk space.

**BOUNDARY**: this codebase's existing request-level bounds (e.g.
`agent.dependency_recheck.MAX_RECHECKS_PER_CALL`, web-fetch budgets
already established in prior phases) already limit legitimate
request-driven INSERT volume; nothing in 5E-S removes those bounds.

**PREVENTION**: **NOT ADDRESSED IN THIS PASS** — no per-account
row-count/rate quota exists for `YANDI_RUNTIME` in the DESIGNED grant
model; MySQL itself has no built-in per-user storage quota mechanism
without a third-party plugin or filesystem-level quota (out of scope,
infra-level).

**DETECTION**: none built this pass — would need disk-usage monitoring
(infra-level, out of scope for `agent/db/sql/`).

**RECOVERY**: N/A — this is an availability (DoS) concern, and mandate
explicitly scopes this codebase's threat model around confidentiality/
integrity of canonical memory, not DoS resilience infrastructure.

**RESIDUAL RISK**: **open, explicitly unresolved**. Recorded honestly
rather than papered over with an untested "rate limit" claim.
**PROOF STATUS: NOT ADDRESSED — recorded as a known gap, not silently
omitted.**

---

## Summary table

| # | Threat | Proof status |
|---|---|---|
| T1 | SQLi via user text | STATIC + FUNCTIONAL (mock) |
| T2 | SQLi via URL/metadata | STATIC + FUNCTIONAL (mock) |
| T3 | SQLi via NODE text | N/A (no transport exists) |
| T4 | SQLi via AI_CHAT text | N/A (no transport exists) |
| T5 | Malformed JSON/input | STATIC + existing regression |
| T6 | Stored XSS | N/A to this layer (handoff documented) |
| T7 | Stolen runtime credential | DESIGNED — LIVE BLOCKED |
| T8 | Direct external DB connection | **LIVE FACT, UNRESOLVED, owner decision needed** |
| T9 | Stolen datadir | App-crypto DESIGNED; TDE BLOCKED |
| T10 | Stolen backup | N/A — no backup pipeline exists |
| T11 | SELECT-only compromise | DESIGNED — LIVE BLOCKED |
| T12 | Runtime-level forged INSERT | DESIGNED + unit-tested — LIVE BLOCKED |
| T13 | UPDATE old QUESTION | DESIGNED — LIVE BLOCKED |
| T14 | UPDATE old ANSWER | DESIGNED — LIVE BLOCKED |
| T15 | DELETE history | DESIGNED — LIVE BLOCKED |
| T16 | TRUNCATE | DESIGNED — LIVE BLOCKED |
| T17 | DROP TABLE | DESIGNED — LIVE BLOCKED |
| T18 | Forged observation | same as T12 |
| T19 | Ciphertext bit-flip | **FUNCTIONAL, fully proven offline** |
| T20 | Ciphertext swap between rows | **FUNCTIONAL, fully proven offline** |
| T21 | Single-row deletion | FUNCTIONAL (offline) — LIVE BLOCKED |
| T22 | Full-DB rollback | DESIGNED — NOT WIRED |
| T23 | Migration/schema substitution | DESIGNED — LIVE BLOCKED |
| T24 | Leaked KEK | FUNCTIONAL (unit-tested) |
| T25 | Lost key | Fail-closed FUNCTIONAL; backup is a documented requirement |
| T26 | Application RCE | Architectural mitigation only, not fully solvable |
| T27 | Disk exhaustion / DoS | **NOT ADDRESSED, open** |
