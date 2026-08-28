# YANDI SQL — Security Architecture (Этап 5E-S: SQL BASTION)

Read `SECURITY_THREAT_MODEL.md` alongside this file — that document is
the adversarial/attack-scenario half; this one is the design/reference
half. Neither replaces the other.

**Status at the top, honestly**: this document describes a MIX of code
that exists and is unit-tested today, and design that is written down
so a later pass (with real Percona credentials) can implement it
without re-deriving the reasoning. Every section below is tagged
`[LIVE]`, `[DESIGNED]`, or `[BLOCKED]` — see §0.

## 0. Proof-status legend (used throughout this file and the tests)

- **PROVEN (live)** — verified against a real, running Percona server.
- **PROVEN (static/mock)** — verified by reading the actual DDL/code, or
  by running real Python functions against a `FakeConnection` that
  records exactly what SQL/params were sent, without a real server.
- **DESIGNED** — written (code or SQL), reasoned through, but not yet
  exercised against anything live because no SQL credentials exist in
  this environment (see `MIGRATION_STATUS.md` — unchanged fact this
  pass).
- **BLOCKED** — cannot be done at all without something this pass does
  not have (credentials, ownership of a shared server, an actual
  attacker).

As of this pass: **PROVEN (live)** applies to nothing new in 5E-S — the
whole SQL layer remains shadow-only/unconfigured in this environment,
exactly as `MIGRATION_STATUS.md` already states. Everything below that
sounds like a strong guarantee is a **DESIGNED** or **PROVEN
(static/mock)** guarantee unless explicitly marked otherwise.

## 0.1 Non-negotiable security invariants (mandate §2, verbatim)

These are fixed points. Every design decision in this document and
every trigger/grant this pass writes exists to make these true, not
merely aspirational:

    NETWORK INPUT IS DATA, NEVER CODE.
    PARAMETER DATA != SQL CODE.
    STORED != TRUSTED.
    AUTHENTICATED != TRUSTED.
    ENCRYPTED != SAFE.
    SIGNED != TRUE.
    MAC_VALID != CLAIM_TRUE.
    DATABASE ACCESS != EPISTEMIC AUTHORITY.
    QUESTION IS IMMUTABLE.
    QUESTION UPDATE = FORBIDDEN.
    QUESTION DELETE = FORBIDDEN.
    ANSWER HISTORY IS IMMUTABLE.
    OLD ANSWER UPDATE = FORBIDDEN.
    OLD ANSWER DELETE = FORBIDDEN.
    NEW ANSWER = NEW ANSWER_VERSION.
    TRACE HISTORY IS APPEND-ONLY.
    OBSERVATION HISTORY IS APPEND-ONLY.
    ASSESSMENT HISTORY IS APPEND-ONLY.
    RECHECK HISTORY IS APPEND-ONLY.
    DELETE CANONICAL EPISTEMIC HISTORY = FORBIDDEN.
    TRUNCATE CANONICAL EPISTEMIC HISTORY = FORBIDDEN.
    CASCADE DELETE CANONICAL HISTORY = FORBIDDEN.
    MEMORY REPLAY != NEW PROVENANCE ROOT.
    DATABASE CRYPTOGRAPHIC INTEGRITY != TRUTH.
    DB SUPERUSER ACCESS MUST NOT EXIST IN NORMAL RUNTIME.
    SQL CREDENTIAL != ENCRYPTION KEY.
    ENCRYPTION KEY MUST NOT LIVE IN THE DATABASE IT DECRYPTS.
    NO PLAINTEXT FALLBACK AFTER SQL BECOMES CANONICAL.

`agent/db_sql_security_invariants_regression_test.py` greps this exact
block (and its mirror in `SECURITY_THREAT_MODEL.md`) so no future edit
can silently drop one.

## 1. Core principle: logical owner, not permanent superuser

> LOGICAL OWNER != PERMANENT SQL SUPERUSER

YANDI is the single logical writer of its own epistemic memory — that
was already true before 5E-S and does not change. What changes is that
this is now made **physically** true through capability separation
(§3), not left as an informal convention resting on "well, nothing else
connects to this database." A single leaked runtime credential (T7 in
the threat model) must not be equivalent to root on the whole database.

This is NOT a violation of "one YANDI owner" — see §3.4's explicit
framing.

## 2. Real environment this design targets

Checked directly this pass (see `SECURITY_THREAT_MODEL.md`'s "Real
environment facts" for the commands run):

- Percona Server **8.0.46-37** — NOT 8.4. Any keyring/TDE guidance in
  this document targets 8.0's component keyring
  (`component_keyring_file`, available in Percona 8.0 since the
  component-keyring backport), not 8.4-specific mechanisms the original
  mandate speculated about.
- This is a **shared, FastPanel-managed** instance
  (`/etc/mysql/my.cnf.fastpanel/99-fastpanel.cnf`), listening on
  `*:3306`. **This is the single most consequential fact for this
  design** — see §4.
- No YANDI SQL credentials, no passwordless local admin path
  (`sudo -n mysql` fails). Bootstrap (§9) is therefore **DESIGNED, not
  executed**, in this pass.

## 3. Trust zones

```
ZONE 0  UNTRUSTED         web pages, P2P nodes, AI chat responses,
                          user text, browser input
ZONE 1  YANDI CORE        parsing, epistemic classification, NLI,
                          claim/evidence extraction (unchanged by 5E-S)
ZONE 2  PERSISTENCE       agent/db/sql/repositories.py (sole SQL
        BOUNDARY          entry point) + crypto.py + integrity.py
ZONE 3  SQL RUNTIME       YANDI_RUNTIME role — restricted capability
ZONE 4  MIGRATION/        YANDI_BOOTSTRAP / YANDI_MIGRATOR — elevated,
        BOOTSTRAP         temporary, local-only, never network-reachable
ZONE 5  KEY MATERIAL      KEK file, outside SQL, outside git
ZONE 6  READ-ONLY         YANDI_READONLY role — SELECT only, no key
```

**Zone crossing rule**: nothing in Zone 0 ever reaches Zone 3 directly
— it always passes through Zone 1 (classification/validation) and Zone
2 (parameterized repository calls + encryption + integrity stamping)
first. No NODE/PET/WEB/browser component holds a Zone 3, 4, or 5
credential — confirmed today by the T1-T4 injection audit (zero
non-`agent/db/sql` code imports `pymysql`) and by `pet/*.py` never
importing `agent.db.sql.keys`/`agent.db.sql.connection` (grepped this
pass).

## 4. MANAGED vs EXTERNAL profile — REQUIRED OWNER DECISION

Mandate §12 asks which profile applies. **Verified answer for THIS
machine: EXTERNAL/SHARED.** This is not a guess — see
`SECURITY_THREAT_MODEL.md`'s T8 entry.

Consequence: 5E-S **does not** attempt to:
- change `bind-address` or add `skip-networking` to the shared
  `my.cnf` (could break other FastPanel-hosted sites on this box);
- configure a global keyring component (also global server state);
- assume YANDI can create its own OS-level MySQL service account.

What 5E-S **can** still do on a shared instance (and does, as design +
code this pass):
- create a dedicated `yandi_epistemic` database + dedicated
  `YANDI_RUNTIME`/`YANDI_READONLY`/`YANDI_MIGRATOR` accounts scoped to
  only that database (standard, non-disruptive multi-tenant MySQL
  practice);
- application-level AES-256-GCM encryption of sensitive columns
  (doesn't require server-level config at all);
- application-level integrity journal (same);
- require TLS on the connection even though the instance is shared
  (`connection.py` can request `ssl=` — **not yet added**, tracked as
  an open item since no test server with a real cert exists to verify
  against).

**This is recorded as an explicit, required decision for the human
owner before any bootstrap runs for real**: either provision a
dedicated local MySQL/Percona instance for YANDI (full profile, socket-
only, TDE), or formally accept the degraded profile above on the shared
instance. 5E-S does not choose this silently.

## 5. Table classification

| Table | Class | Why |
|---|---|---|
| `question` | **A** immutable identity | one row per canonical question, never updated |
| `question_occurrence` | **A** immutable, append | one row per literal ask, never updated |
| `claim_family` | **A** immutable identity | `canonical_text` write-once (`INSERT IGNORE` only, confirmed — no `UPDATE` anywhere in `repositories.py`) |
| `source_resource` | **A** immutable identity | found-or-created (`SELECT` then `INSERT`), never `UPDATE`d |
| `schema_migrations` | **A** immutable log | `INSERT IGNORE` only |
| `answer_version` | **B** versioned/append | new row per version; `record_answer_version()` never updates an existing row |
| `answer_assessment` | **B** append-only | always `INSERT` |
| `family_member` | **B** append-only link | `INSERT IGNORE` |
| `claim_occurrence` | **B** append-only | always `INSERT`, run-scoped |
| `source_observation` | **B** append-only | always `INSERT` |
| `evidence_relation` | **B** append-only | always `INSERT` |
| `belief_assessment_history` | **B** append-only | always `INSERT` |
| `recheck_event` | **B** append-only | always `INSERT` (this pass's own fix for the JSON overwrite bug) |
| `epistemic_contradiction_observation` | **B** append-only | always `INSERT` (unwired, but designed append-only) |
| `ai_observation` / `ai_reported_source` | **B** append-only | unwired stub, designed append-only |
| `run_error` | **B** append-only, minimal | always `INSERT`, 6 columns only |
| `belief` | **C** derived projection | `upsert_belief()`'s `ON DUPLICATE KEY UPDATE` — current derived state, fully rebuildable from `belief_assessment_history` |
| `semantic_edge` | **C** derived projection | designed as mutable-upsert (`observation_count++`) per its own schema comment; unwired |
| `verification_run` | **D** operational, narrowly mutable | `status` transitions `running`→`completed`/`aborted`/`failed` exactly once, each transition gated by `WHERE status='running'` — see §6 for why this stays D instead of migrating to a B-style event log in 5E-S |
| *(future)* `integrity_event` | **E** security metadata | this pass's new table, see §14 |
| *(future)* `integrity_checkpoint` | **E** security metadata | this pass's new concept, see §14 — file-based in this pass, not a table (see §14) |
| *(future)* `key_metadata` | **E** security metadata | DEK wrapping metadata (`key_id`/`key_version`), see §10 |

**Projection is disposable, history is not**: `belief` and
`semantic_edge` could both be dropped and rebuilt from `belief_
assessment_history`/the claim-graph shadow's own recomputation — that
rebuildability is exactly what makes it safe for them to be mutable.

## 6. `verification_run` — deliberately NOT migrated to append-only this pass

Mandate §9 asks to consider migrating `complete_run()`'s `UPDATE
verification_run SET status='completed', ...` to an append-only
`verification_run_event` table (`STARTED`/`COMPLETED`/`FAILED`/
`ABANDONED` rows, current state = latest event).

**Decision this pass: documented, not executed.** Reasoning (mandate's
own instruction: "Если полный переход сейчас слишком рискован, не
ломай pipeline вслепую"):

1. `verification_run`'s mutability is already NARROW, not general —
   exactly one column (`status`, plus `completed_at`/`final_answer_id`/
   `failed_stage`/`error_class` set together with it) transitions along
   exactly one path (`running` → one of three terminal states), each
   transition is idempotent-guarded (`WHERE run_id=%s AND
   status='running'` — a second `complete_run()` call for an already-
   completed run is a silent no-op, not a double-transition).
2. Migrating this now means touching `shadow_complete_run()`,
   `shadow_fail_run()`, `reconcile_stale_running_runs()`, and every
   READ function that currently does `SELECT * FROM verification_run
   WHERE run_id=...` (`get_verification_runs`, `explain_answer`) to
   instead compute "current state" from `MAX(event_id)` — a real,
   non-trivial change to code that's already proven correct by this
   session's live 539s run, right before a security-focused pass. Doing
   it now risks conflating two different kinds of change in one review.
3. **This IS still covered by the immutability defense-in-depth**:
   `YANDI_RUNTIME`'s grant for `verification_run` is `SELECT, INSERT,
   UPDATE` (the ONLY table in the schema where `YANDI_RUNTIME` gets
   `UPDATE` at all — see §7's grant table) — and a `BEFORE UPDATE`
   trigger (DESIGNED, `security_triggers.py`) restricts what the
   `UPDATE` is allowed to touch: it rejects any attempted `UPDATE` that
   would change `run_id`, `occurrence_id`, or `started_at` (the
   identity/history-defining columns), and rejects any transition
   that isn't `running` → a terminal status. This gets narrow-mutable
   protection now without the full event-log migration.

**Recorded as future work** (not started): `verification_run_event`
per mandate §9's own sketch, at the point a maintainer decides the
migration risk is worth it — likely alongside 5F/5I when the read path
is being touched anyway.

## 7. Database role matrix

All four roles are **DESIGNED** (SQL written in
`agent/db/sql/security_grants.py`, this pass) — **not provisioned on
any live server** (no credentials).

| Role | Scope | Has | Does NOT have |
|---|---|---|---|
| `YANDI_BOOTSTRAP` | local only, temporary | `CREATE DATABASE`, `CREATE USER`, `GRANT OPTION`, DDL on `yandi_epistemic.*` | permanent existence — revoked/dropped after bootstrap completes |
| `YANDI_MIGRATOR` | local only | DDL (`CREATE`/`ALTER TABLE`) on `yandi_epistemic.*` only | `CREATE USER`, `GRANT OPTION`, any privilege on any other schema, `SUPER`/`FILE`/`PROCESS` |
| `YANDI_RUNTIME` | network-reachable (the actual app) | `SELECT` on all `yandi_epistemic.*`; `INSERT` on every table; `UPDATE` on **only** `verification_run` (narrow, trigger-guarded, §6) and the two **C**-class projections (`belief`, `semantic_edge`) | `DELETE` (any table), `UPDATE` on any **A**/**B** table, `DROP`, `TRUNCATE`, `ALTER`, `CREATE`, `CREATE USER`, `GRANT OPTION`, `SUPER`, `FILE`, `PROCESS`, `EXECUTE` on any privileged routine |
| `YANDI_READONLY` | local, human-operator use | `SELECT` on all `yandi_epistemic.*` (ciphertext, not the encryption key) | any write privilege whatsoever; no encryption key access — see §11 |

Every privilege granted has a one-line justification (mandate §10.3:
"Каждый privilege должен быть объяснён") — see the docstring in
`security_grants.py` for the full table, reproduced in summary above.

## 8. Repository is the sole SQL owner (capability surface)

Confirmed this pass by reading every `def ` in `agent/db/sql/
repositories.py` (31 public functions): **zero** generic
`update_any()`/`delete_any()`/`execute_raw()`/`run_sql()` exist. Every
function is domain-specific (`record_question()`-shaped names per
mandate §34). `agent/db_sql_security_capability_surface_regression_
test.py` (this pass) locks this in — it fails if a future change adds
a generic CRUD escape hatch, and separately fails if any function name
matching `update_question`/`delete_question`/`delete_answer`/
`update_answer_version`-shaped patterns appears anywhere in the module
(mandate §6/§33: no such functions should ever exist).

## 9. Zero-config bootstrap — DESIGNED, not executed

`agent/db/sql/bootstrap.py` (this pass) implements the flow from
mandate §11 as idempotent Python functions, each individually unit-
tested against a `FakeConnection` for idempotency logic (calling twice
produces the same end state, not duplicates). **Real execution against
the actual shared Percona instance is explicitly NOT attempted in this
pass** — see §4's required owner decision first, and because no
`YANDI_BOOTSTRAP`-capable credential exists in this environment (no
passwordless local admin path — verified, see threat model).

Flow implemented (as idempotent functions, unit-tested, not live-run):
`ensure_database()` → `ensure_roles()` → `apply_schema()` (delegates to
existing `migrate.py`, not duplicated) → `apply_immutability_triggers()`
→ `verify_grants()` → `run_security_smoke_test()`. Key generation
(`keys.py::generate_kek()`) is a separate, explicit, one-time operator
action — NOT auto-run by `bootstrap.py` (mandate §37: key generation
needs a human aware a backup obligation now exists).

## 10. Encryption design

**Cipher**: AES-256-GCM (`cryptography` library's
`AESGCM` — a mature, audited AEAD implementation; **not
hand-rolled**, per mandate §17). Confirmed available in this
environment's venv (`agent/db/sql/crypto.py` imports
`cryptography.hazmat.primitives.ciphers.aead.AESGCM`).

**Nonce**: fresh 12-byte CSPRNG (`os.urandom(12)`) per encryption call
— never reused with the same key (proven this pass: `agent/
db_sql_crypto_regression_test.py` encrypts the same plaintext twice and
asserts the two ciphertexts AND the two nonces differ).

**AAD** binds ciphertext to its row per mandate §18's exact design:
`f"YANDI|{entity_type}|{entity_id}|{field_name}|v{version}"` — e.g.
`"YANDI|question|42|raw_text|v1"`. Swapping ciphertext between rows/
fields/entities changes the AAD, which AES-GCM authenticates as part of
the tag — mismatched AAD fails decryption (T20, proven offline).

**Key hierarchy** (mandate §19): `KEK` (outside SQL entirely, §11) wraps
per-data-class `DEK`s (`DEK_question`, `DEK_answer`, `DEK_trace_
evidence`, ... — one per the data-classification groups in §12).
Wrapped DEKs (`AESGCM(KEK).encrypt(...)` of the raw DEK bytes) ARE
permitted to live in SQL (`key_metadata` table, class **E**) — the KEK
itself is never stored there. `key_id`/`key_version` travel alongside
each ciphertext (a small metadata prefix, not mixed into the ciphertext
bytes) so rotation (§13) can identify which DEK version to unwrap with.

## 11. Key storage

**MASTER/KEK location, this environment**: no `systemd-creds`
verified available, no TPM verified available on this machine (neither
checked further — out of scope to provision new host infrastructure in
this pass). **Fallback per mandate §20**: a dedicated file,
`0700` directory, `0400`/`0600` file, owned by the service account,
outside the git repository (`~/.yandi/keys/kek.bin`-shaped path,
exact location left to the operator via an env var —
`agent/db/sql/keys.py::KEK_PATH_ENV = "YANDI_KEK_PATH"`, no hardcoded
default that could accidentally end up inside the repo).

`keys.py::load_kek()` **refuses to load** a key file with
group/other-readable permissions (`os.stat(...).st_mode & 0o077`) —
fails closed with a clear error rather than silently trusting a loosely
-permissioned file. Unit-tested this pass with a real temp file whose
mode is deliberately set to `0o644`.

**Never**: git, `schema.py`, `.env` in the repo, the database itself,
debug traces, logs, CLI args — `agent/db_sql_crypto_regression_test.py`
greps `keys.py`/`crypto.py` for any `print`/`log(` call that could leak
key bytes, and confirms `keys.py` is not imported by anything under
`pet/` (the untrusted-adjacent process).

## 12. Data classification (what gets encrypted)

Per mandate §16 — encrypted (application-level, this pass's `crypto.py`
is capable of; **not yet wired into `repositories.py`'s INSERT calls in
this pass** — see §21's scope note):

`question.raw_text`/`anonymized_text`, `answer_version.answer_text`,
`claim_occurrence.claim_text`, `source_observation.content_excerpt`,
`source_resource.canonical_uri`, `ai_observation.answer_excerpt`,
`ai_reported_source.reported_name`/`reported_uri`.

**Not encrypted** (structural, low-sensitivity, needed for indexing/
joins in plaintext): opaque IDs, timestamps, FK columns, ENUM statuses,
version numbers, hashes that are themselves already one-way (
`canonical_hash`, `uri_hash`, `answer_hash`).

**`canonical_uri` gets the two-column treatment mandate §16 suggests**:
encrypted `canonical_uri` (confidentiality) + `uri_hash` (already
exists, unkeyed SHA-256 — see §13 for why this stays a stable
provenance identity and is NOT "upgraded" to a blind index).

## 13. Blind index vs content hash — NOT the same thing

**Critical distinction this pass keeps explicit** (mandate §21):
`question.canonical_hash`/`source_resource.uri_hash` (unkeyed SHA-256)
are **existing epistemic provenance identifiers** — `CONTENT_HASH`,
used for cross-run identity ("is this the same question/resource"),
not secrecy. They are **not touched** by this pass.

A **new, separate** concept — `BLIND_INDEX` — is what mandate §21 asks
for as a SECRET exact-match lookup that resists offline dictionary
attack on a stolen, encrypted database: `HMAC-SHA256(index_key,
"question:v1:" + normalized_text)`. Implemented this pass in
`agent/db/sql/crypto.py::blind_index()`, with domain separation proven
by test: the same normalized string under `"question:v1:"` and
`"resource:v1:"` namespaces produces different HMACs. **Not yet wired
into `repositories.py`'s `resolve_question()`** (which still uses
`canonical_hash` for its existing find-or-create lookup) — wiring a
SECOND identity column onto an already-proven-correct function is
scoped as a follow-up once the DEK/KEK infrastructure this pass adds
is actually deployed (§4's decision gates that), to avoid mixing an
encryption-readiness change with a working function's logic in the
same commit.

## 14. Tamper-evident integrity journal

**DESIGNED + unit-tested this pass** (`agent/db/sql/integrity.py`),
**not wired into any production write path** (mirrors §13's "primitive
built, integration deferred" pattern — deliberately, to keep this
commit reviewable).

**Canonical serialization** (mandate §25): `canonicalize_record()` — a
versioned (`integrity_format_version = 1`), deterministic byte
representation: UTF-8, sorted-key JSON (`sort_keys=True,
separators=(",", ":")`), explicit `null` for missing fields, integer
microsecond timestamps (never a raw Python `float`/`repr()`), version
field always present. Same logical record → byte-identical output on
every call (proven: `agent/db_sql_integrity_regression_test.py`).

**Hash chain** (mandate §26): `event_hash = HMAC-SHA256(K_integrity,
format_version || entity_type || entity_id || payload_hash ||
previous_event_hash)`. `K_integrity` is a DEDICATED key, separate from
any encryption DEK/KEK (mandate §19's "independent purposes" rule) —
`keys.py::derive_integrity_key()` derives it from the KEK via HKDF with
a distinct `info` label, so rotating the encryption KEK and the
integrity key are independent operations, without needing to store yet
another top-level secret.

**Concurrency note** (mandate §26's own caution against over-
engineering): a single GLOBAL chain across all tables would serialize
every canonical write through one lock — not attempted. Design instead:
**one chain per entity_type** (`question`, `answer_version`, ...) —
bounded contention, no Merkle-tree framework, matches "YANDI сейчас не
high-frequency trading system."

**Verification**: `verify_chain(events)` walks a sequence and confirms
each `event_hash` matches recomputation AND each `previous_event_hash`
points at the actually-preceding event's real hash — proven this pass
to detect: a modified payload, a deleted middle event (T21), and a
reordered/reference-corrupted event (`agent/db_sql_integrity_
regression_test.py`, three dedicated cases).

## 15. Rollback detection (external checkpoint) — DESIGNED, NOT WIRED

Mandate §27. `integrity.py::make_checkpoint()`/`verify_checkpoint()`
implement the comparison logic (`DB HEAD hash` vs. `externally stored
checkpoint hash+monotonic sequence`) and are unit-tested for the three
outcomes (DB ahead — fine; DB equal — fine; **DB behind a previously-
confirmed checkpoint — ROLLBACK SUSPECTED**, must not proceed silently).
**Not wired into any startup path** — the protected external path
(mandate's `/var/lib/yandi/integrity/...` suggestion) depends on the
§4 MANAGED vs EXTERNAL decision (a shared host may not give YANDI a
writable path outside its own home directory with the needed
guarantees) — recorded as blocked on that decision, not silently
dropped.

## 16. Backup / recovery

**Mandate §36's `BACKUP → destroy → RESTORE → decrypt → integrity
verify → same semantic history` test is NOT performed this pass** — no
backup pipeline exists in this codebase (confirmed by grep, see threat
model T10), and building one is explicitly listed as a *later* stage
(mandate's own §48 ordering puts "S10 — BACKUP/RECOVERY" after the
crypto/integrity layers, which is what this pass focuses on). Recorded
as a hard gate: **5E-S is not "production ready" without this test
existing and passing** — `SECURITY_ARCHITECTURE.md` says so explicitly
rather than implying completeness.

## 17. Fail-open (now) vs fail-closed (after cutover) — UNCHANGED THIS PASS

Mandate §40 is explicit that shadow-phase fail-open (SQL unavailable →
skip the shadow write, JSON stays canonical) must be **preserved**
until cutover — this pass does not touch `shadow_write.py`'s `_shadow()`
fail-open wrapper's semantics. What IS added: `crypto.py`'s own
functions fail **closed** at the unit level regardless of the outer
shadow/fail-open context — `encrypt_field()` raises (never returns
plaintext, never returns a placeholder) if the KEK/DEK can't be loaded
(mandate §20/§40, T25). This is a property of the crypto primitive
itself, proven in isolation; it does not change `shadow_write.py`'s
overall contract because the crypto layer is not called from any
production path yet (§12's wiring is deferred).

## 18. Legacy plaintext stores — untouched, flagged

`agent/db/manager.py` (SQLite `KnowledgeDB`), `agent/verification_
memory.py` (SQLite claim index), `agent/orch_reputation.py`,
`agent/orch_ledger.py`, `agent/orch_knowledge_writer.py`,
`agent/knowledge_graph.py`, `agent/dashboard.py`, and every
`registry/*.json`/trace JSONL file remain **plaintext**, **untouched**
this pass — per mandate §41, they stay until a proven 5F cutover, and
"encrypting SQL while the same answer sits in plaintext JSON" is
recorded explicitly as a known, currently-true security debt (not
hidden) — full-disk/host security is what actually protects those
today, not this pass's work.

## 19. AI / NETWORK_NODE stubs — untouched

Per mandate §42: `agent/db_sql_ai_provenance_schema_regression_test.py`
and `agent/db_sql_network_node_provenance_regression_test.py` (both
prior-session work) are re-run as part of this pass's full regression
sweep and remain green, unmodified. This pass's encrypted-field/
integrity-journal design is capable of covering `ai_observation`/
`ai_reported_source` and any future `node_observation` the same way it
covers existing tables (same `entity_type`/`entity_id` AAD scheme) —
noted as a design capability, not implemented (no writers exist for
those tables at all, encrypted or not).

## 20. Trust badge bug — untouched

Per mandate §43 — `agent/answer_delivery_persistence_regression_
test.py`'s documented case D (stale visible Trust badge) is not
touched by this pass, on purpose.

## 21. What is explicitly deferred out of this pass (honest scope cut)

To keep 5E-S reviewable and avoid mixing "new security primitives" with
"rewiring working production code," the following are **designed/
built as standalone, unit-tested modules this pass, but NOT wired into
`repositories.py`'s existing INSERT/read functions**:

- Encrypting the classified fields in §12 at actual write time.
- Blind-index-based lookup replacing/augmenting `resolve_question()`.
- Integrity-journal writes alongside every canonical INSERT.
- Rollback-checkpoint writes/verification at startup.
- Any of §7's roles actually being `CREATE ROLE`'d on a live server.
- Immutability triggers actually being `CREATE TRIGGER`'d on a live
  server.

This is a deliberate two-step: **this pass proves the primitives are
individually correct** (crypto math, chain math, grant SQL shape,
bootstrap idempotency logic) against fakes/temp files/pure functions;
**wiring them into the live write path is the next 5E-S continuation**,
gated on the §4 owner decision (which server this actually deploys
against) and on real credentials existing to test GRANT/trigger
enforcement live (mandate §55's own PROVEN/BLOCKED distinction).
