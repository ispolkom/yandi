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
| Personal chat | `pet.pet_chat_local_regression_test`, `pet.pet_event_provenance_regression_test`, `pet.pet_relationship_focus_regression_test` |
| Relationship memory | `agent.message_intensity_regression_test`, `agent.relationship_memory_regression_test`, `agent.relationship_apology_matching_regression_test`, `agent.relationship_healing_clock_regression_test` |
| Epistemic / write-back | `agent.epistemic_canonical_trust_shadow_regression_test`, `agent.writeback_episodic_sql_regression_test` |
| SQL layer | `agent.db_sql_shadow_write_regression_test`, `agent.db_sql_security_injection_regression_test` |

## Deterministic vs. environment-dependent

- **Deterministic (the core suite):** the model is mocked at the gateway/HTTP boundary and SQL is an
  in-memory fake. No model, database, network or Redis is required.
- **Need a local model:** anything that loads real weights or calls a live server. Model
  behaviour (for example how often a model reports a genuine insult) is measured with separate
  experiments and is not a regression test.
- **Need a live SQL instance:** the `db_sql_live_*` and ownership/bootstrap proofs, which check a
  real dedicated database instance. Do not run these against a database holding data you care about
  unless you know what they do.

## Test data

Tests use synthetic users, synthetic grievances and mock fixtures. Do not add real conversation
history, real memory contents or credentials to test files.

## Adding tests

A change to relationship memory, the gateway, or the provenance guard should come with a test that
fails without the change. For matching or state behaviour, prefer a counterfactual: same input,
different state, different outcome.
