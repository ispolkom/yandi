# Testing

Tests are plain Python modules named `*_regression_test.py` (not pytest). Each prints `OK`/`FAIL`
lines and exits non-zero on failure. Run one with:

```bash
python -m pet.pet_event_provenance_regression_test
```

Run from the repository root; use the interpreter of the environment you installed
`requirements.txt` into. Rust tests for the node live in `node/tests/` and run with `cargo test`
inside `node/`.

## Core suite

```bash
scripts/test-core.sh
YANDI_PYTHON=/path/to/python scripts/test-core.sh
```

The interpreter is `YANDI_PYTHON`, else `./.venv`, else `~/venv`, else `python3`. The script uses
`set -euo pipefail`, prints the tail of each suite, and exits non-zero if any suite fails.

| Area | Suites |
|---|---|
| Gateway | `llm_gateway.client_regression_test`, `remote_backend_`, `llamacpp_backend_`, `intelligence_bridge_`, `secure_store_` |
| Personal chat | `pet.pet_chat_local_regression_test`, `pet.pet_event_extraction_regression_test`, `pet.pet_event_provenance_regression_test`, `pet.pet_relationship_focus_regression_test`, `pet.pet_relationship_state_causality_regression_test`, `pet.pet_commitment_events_regression_test`, `pet.pet_turn_identity_regression_test`, `pet.pet_personal_memory_regression_test`, `pet.pet_turn_transaction_regression_test`, `pet.pet_fact_extraction_regression_test`, `pet.pet_personal_facts_regression_test`, `pet.pet_commitment_verification_regression_test`, `pet.pet_commitment_trust_regression_test`, `pet.pet_extension_regression_test`, `pet.pet_settings_tab_regression_test`, `pet.pet_web_guard_regression_test` |
| Relationship memory | `agent.message_intensity_regression_test`, `agent.relationship_memory_regression_test`, `agent.relationship_apology_matching_regression_test`, `agent.relationship_healing_clock_regression_test`, `agent.relationship_state_regression_test`, `agent.relationship_commitments_regression_test`, `agent.relationship_direct_fulfilment_regression_test`, `agent.relationship_idempotency_regression_test` |
| Epistemic / write-back | `agent.epistemic_canonical_trust_shadow_regression_test`, `agent.writeback_episodic_sql_regression_test` |
| SQL layer | `agent.db_sql_shadow_write_regression_test`, `agent.db_sql_security_injection_regression_test`, `agent.db_sql_test_isolation_regression_test` |
| Core lifecycle (P1a) | `pet.pet_core_lifecycle_regression_test` — launch secret file, check value, lifecycle, concurrency, gate in front of the real application, contract fixtures against the Core as a separate process, mutants M1–M13; see `docs/CORE_LIFECYCLE.md` |
| Node ⇄ Core contract | `contract.contract_regression_test` — the contract's own files (schemas, fixtures, coverage) and proof that the fixtures bite (needs `jsonschema`, listed in `requirements.txt`); see `contract/README.md` |

## Tests never touch the live database

```text
TEST SUITE MUST NEVER WRITE THE LIVE OWNER DATABASE
```

The connection layer's defaults point at the operator's own database, so a test that merely forgets
to isolate itself would write real rows. The rule is therefore enforced in one place,
`agent/db/sql/connection.py`, and does not depend on any test being careful:

- A **test process** is recognised by its entry point (`*_test.py`, `*_proof.py`, `*regression*`,
  pytest/unittest) or by `YANDI_TEST_MODE=1` (set by the scripts).
- In a test process every real database connection is **refused** (`LiveDatabaseRefused`, a
  `SqlUnavailable`, so fail-open callers keep working) unless it targets the socket declared in
  `YANDI_TEST_ISOLATED_SOCKET` (set by `scripts/test-sql-temp.sh` for its throw-away instance) and that
  socket is not the live one. There is no switch that turns the guard off and no fallback to the
  live database. A refusal that could have reached a real database is announced on stderr
  (`[live-db-guard] REFUSED ...`).
- The one check (`assert_connection_allowed`) is also what the integration tests call before opening
  their own connection to the throw-away instance: the declared socket must be the one requested,
  must not resolve (through symlinks) to the live socket, and must be inside the system temporary
  directory. A test that calls `pymysql.connect` without it fails the isolation suite.
- The only exceptions are the two operator tools `db_sql_live_persistence_proof` and
  `db_sql_live_immutability_proof`, which are documented as live tools (not tests), are not part of any
  suite, and are named (and pinned by a test) in `connection.py`.
- Suites that are not about SQL persistence say so with `db_sql_fake_fixtures.no_database()` or use
  an in-memory fake.

`scripts/test-core.sh` additionally fails if a core suite even *attempts* a connection, and, when the
database is reachable, fingerprints it (per-table row counts and auto-increment counters, read-only,
`scripts/live_db_fingerprint.py`) before and after and fails on any change.
`scripts/test-all.sh` does the same for every `*_test.py` in the repository and lists the suites that
attempted a connection. `agent/db_sql_test_isolation_regression_test.py` proves the guard, including
mutants (guard removed, the live socket declared "isolated").

## Deterministic vs. environment-dependent

- **Deterministic (the core suite):** the model is mocked at the gateway/HTTP boundary and SQL is an
  in-memory fake. No model, database, network or Redis is required.
- **Need a local model:** anything that loads real weights or calls a live server. Model
  behaviour (for example how often the model finds a genuine insult in a message) is measured with
  a separate live benchmark and is not a regression test; the regression tests script the model and
  check the protocol around it.
- **Need a live SQL instance:** the `db_sql_live_*` proofs, which check a real dedicated database
  instance and write tagged rows on purpose. They are operator tools, not tests: do not run them
  against a database holding data you care about unless you know what they do.

## SQL integration tests (real engine, throw-away instance)

```bash
scripts/test-sql-temp.sh                                   # every suite
scripts/test-sql-temp.sh agent.commitment_verification_sql_integration_test   # only the named ones
```

Starts a private MySQL-compatible instance in a temporary directory (own unix socket, no network
port, removed on exit; where the environment cannot create unix sockets, a loopback TCP port instead,
reached through the test-only `tcp:127.0.0.1:<port>` target that `agent/db/sql/connection.py` accepts only
from a test process, only for the declared port, never 3306), applies the project's own schema migration to it, and runs
`agent/relationship_idempotency_sql_integration_test.py`,
`agent/personal_memory_sql_integration_test.py`, `agent/turn_atomicity_sql_integration_test.py` and
`agent/personal_facts_sql_integration_test.py` (foreign keys, append-only grants, facts inside the turn's
transaction, corrections, a fact 400 turns back) and `agent/commitment_verification_sql_integration_test.py`
(the v17 -> v18 migration, the pre-v18 database, the runtime role unable to rewrite the ledger, the verified
event and the trust transition in one transaction with faults injected after each step, retry after commit,
8 concurrent deliveries, two different turns verifying at the same moment (a deterministic interleaving of
two transactions), a restart in a new process with another model, trust farming, mutants)
(fault injection at exact points of the persistence phase, retry after rollback / after a lost reply,
8 concurrent deliveries, transaction-ownership mutants): migration idempotency and additivity (v14 -> current,
v15 -> v16), the unique-key claims under real concurrency, rollback semantics, the least-privilege
runtime role and the append-only ledgers, and personal-memory recall on a fresh connection. It needs a `mysqld` binary and a non-root user, skips otherwise, and never
contacts the project's live database. It is not part of `scripts/test-core.sh`.

## Test data

Tests use synthetic users, synthetic grievances and mock fixtures. Do not add real conversation
history, real memory contents or credentials to test files.

## Adding tests

A change to relationship memory, the gateway, or the provenance guard should come with a test that
fails without the change. For matching or state behaviour, prefer a counterfactual: same input,
different state, different outcome.

## Real-model benchmark of commitment verification

`python -m pet.bench_commitment_verification --model <logical model> [--model ...]` runs the whole
verification protocol against a synthetic corpus (real deliveries, self-reports, external actions,
hypotheticals, quotations, other people, an injection, ambiguous deliverables) on the models you name and
counts, separately, direct fulfilments verified, **false verifications (must be 0)**, ambiguous deliverables
matched anyway, wrong-target matches and the in_chat / external classification. It needs no database and
writes nothing. `--scripted` is a plumbing self-check with a stand-in that is not a model.

## Web UI settings tab

`pet.pet_settings_tab_regression_test` (core suite) covers the settings file (one Voice, any number of advisors, the API
key never written), the guard that keeps other websites' pages away from the settings and the disk browser, the folder
browser and the model-file check, and the page wiring. `python -m pet.settings_tab_firefox_e2e` (needs `firefox-esr`,
`redis-server` and `pip install marionette_driver`) starts the REAL server with a throw-away Redis and drives the tab in a
real headless Firefox: first-run placement, the browse window on another disk, apply, the tab moving to the end and staying
closed after a reload, and a page from another origin being refused. It refuses to run when ports 9010 or 6379 are in use.

## The server is closed to other websites

`pet.pet_web_guard_regression_test` (core suite) checks the "deny by default" rule (`pet/local_guard.py`) for HTTP and WebSocket:
foreign origins, cross-site requests, DNS rebinding, duplicate headers, the extension on its own paths only, path tricks, CORS,
security headers, with eight mutants. `python -m pet.web_guard_firefox_e2e` (needs `firefox-esr`, `redis-server`,
`websockets` and `pip install marionette_driver`) starts the real server with a throw-away Redis, installs the extension package
in a real headless Firefox and opens a page of another origin: the own page and the extension keep working, the other page can
read nothing, run no tool, open no WebSocket, and even a "simple" request that needs no preflight has no effect (checked in Redis).
With the rule switched off the same scenario fails on four points.

## Conformance suite for the Node ⇄ Core contract

`contract/` holds the language-neutral, executable form of `docs/NODE_CORE_CONTRACT.md` (JSON Schemas, scenario fixtures, a runner).
`python -m contract.runner validate` and `python -m contract.contract_regression_test` are offline and part of the core suite. Running the
fixtures against a core (`python -m contract.runner run --target-file …`) needs a core started *for the test* with throw-away state; the runner
refuses the ports of the live system. The recorded baseline of today's Python system is `contract/baseline/red-p1-current-pet.json` (expected: RED,
the current system implements none of the P1 lifecycle). Details in `contract/README.md`.

## Node core supervisor (Rust, P1b)

`scripts/test-node-supervisor.sh` runs, offline: `cargo fmt --check` and `cargo test` for `node/core_supervisor` (unit tests and supervision tests against a stand-in
core, `tests/support/fake_core.py`, needing only `python3`), the tests against the **real** Python core (`--ignored`, need `YANDI_TEST_PYTHON` = a python with the
core's requirements), the six supervisor scenarios of the contract through the Rust harness (`python -m contract.runner run --target none --only supervisor.
--supervisor-harness …`), and the mutation check (`scripts/supervisor_mutants.py`: 14 deliberate defects, each must fail a named test). None of it touches the live
system: temporary directories, loopback ports chosen by the system, processes it spawned itself.

## Node key root (Rust, P1c-1)

`scripts/test-key-root.sh` runs, offline: `cargo fmt --check` and `cargo test` for `node/key_root` (the root document with its device and recovery wrappers, the identity formats and
the load policy, atomic writes, migration with rollback, recovery on a "new machine", and the `yandi-keys` tool run as a subprocess whose output is scanned for secrets), a check of the whole
node, and the mutation check (`scripts/key_root_mutants.py`, 23 deliberate defects: a failed decrypt that creates or overwrites an identity, a wrong password that leaves a trace, the machine id
used as a key, a weak hash for the recovery password, a printed password, a migration that cannot restore or keep backups, recovery that returns another identity, …). Everything uses temporary
directories and a fake machine id: **the owner's real `~/.yandi_keys` is never touched.**
The node's own tests (`cargo test --lib`, 196) cover the first-run web setup with the person's typed passwords (repeat mismatch, too short, never overwrites, restart, recovery on another "machine" with the typed master password, login-page reset) and the setup-only server (only `/setup` is served, other hosts refused, it stops after success); the mutation script also holds web mutants W1–W3.

## Sealed personal memory (P1c-2)

Three layers, all offline and none touching the live database:

* `python -m agent.db_sql_field_protection_regression_test` — the storage layer and the repository wiring over an in-memory fake SQL that keeps exactly what
  the repositories send (so "what the database holds" can be searched for the person's words): every mode, no key / wrong key / forged mode record, swapped and
  planted values, a typed `yp1:`, the key tool, and structural guards (every writer of a protected table seals, every reader opens, no module outside the repository
  layer runs SQL against those tables). It ends with ten in-process mutants of its own. Part of `scripts/test-core.sh` / `test-all.sh`.
* `scripts/test-sql-temp.sh agent.db_sql_field_protection_sql_integration_test` — on a real, private MySQL: the v18 → v19 upgrade, `seal` (raw rows searched for the
  words: none left), `unseal`, an interruption, an error mid-table, a row changed under the tool, a stray write, a skipped table, a planted plaintext row, swapped sealed values,
  and backup → destroy → restore → identical content (with wrong-key, damaged and forged backups refused). Part of `scripts/test-sql-temp.sh`.
* `python scripts/protect_mutants.py` — 29 deliberate defects in the layer, the wiring, the Core hook and the tool, each of which must fail one of the suites above
  (takes tens of minutes: many of them run the real-engine suite). Needs `YANDI_PYTHON` (a python with `cryptography` and `PyMySQL`).

The Rust side: `yandi-keys core-key` is covered by `cargo test -p yandi-key-root` (prints exactly the key the node gives the Core, one line, nothing on stderr; refuses a terminal).
