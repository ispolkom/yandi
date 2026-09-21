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
| Personal chat | `pet.pet_chat_local_regression_test`, `pet.pet_event_extraction_regression_test`, `pet.pet_event_provenance_regression_test`, `pet.pet_relationship_focus_regression_test`, `pet.pet_relationship_state_causality_regression_test`, `pet.pet_commitment_events_regression_test`, `pet.pet_turn_identity_regression_test`, `pet.pet_personal_memory_regression_test`, `pet.pet_turn_transaction_regression_test`, `pet.pet_fact_extraction_regression_test`, `pet.pet_personal_facts_regression_test`, `pet.pet_commitment_verification_regression_test`, `pet.pet_commitment_trust_regression_test`, `pet.pet_extension_regression_test` |
| Relationship memory | `agent.message_intensity_regression_test`, `agent.relationship_memory_regression_test`, `agent.relationship_apology_matching_regression_test`, `agent.relationship_healing_clock_regression_test`, `agent.relationship_state_regression_test`, `agent.relationship_commitments_regression_test`, `agent.relationship_direct_fulfilment_regression_test`, `agent.relationship_idempotency_regression_test` |
| Epistemic / write-back | `agent.epistemic_canonical_trust_shadow_regression_test`, `agent.writeback_episodic_sql_regression_test` |
| SQL layer | `agent.db_sql_shadow_write_regression_test`, `agent.db_sql_security_injection_regression_test`, `agent.db_sql_test_isolation_regression_test` |

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
