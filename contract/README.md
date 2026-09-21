# `contract/` — the Node ⇄ Core contract, in an executable form

Version **1.0-rc1** (`CONTRACT_VERSION`). The prose is `docs/NODE_CORE_CONTRACT.md`; this directory is the same agreement as **data**:
JSON Schemas, scenario fixtures, and a small runner. Nothing here is specific to Python. A Rust core is *done* for a chapter when it passes
that chapter's fixtures, the same files, unchanged.

> Do not port Python to Rust. Implement the contract. **Contract → fixtures → RED → implementation → GREEN.**

This first slice is **P1 only**: lifecycle (`health`, `capabilities`, `unlock`, `lock`, `shutdown`), the launch-secret authentication, idempotency of
lifecycle mutations, strict requests, the canonical error body and its sanitisation, `X-Principal`/`X-Actor`, and the supervisor's expected behaviour
(as data). Conversation, egress, events, knowledge and peers come with P2/P3 and get their own chapters here.

## Layout

```text
contract/
  CONTRACT_VERSION           1.0-rc1 (schemas, fixtures and coverage.json must match it)
  coverage.json              per invariant: what the fixtures prove, and what they cannot prove yet
  schemas/
    common/                  identifiers (idempotency key, key, states, error codes), the canonical error body
    lifecycle/               health, capabilities, unlock / lock / shutdown requests and answers
    scenario/                the meta-schema every fixture file must satisfy; the description of an external target
  scenarios/
    _shared/leak-patterns.json   what an error or a health body must never contain (paths, addresses, backends, traces, env names, credential names)
    p1/*.json                one file per topic: health, capabilities, auth, locked, unlock, lock, shutdown, idempotency,
                             principal_actor, malformed, sanitization, key_material, launch_secret, isolation, state_machine, supervisor
  runner/                    python -m contract.runner … (loader, wire, checks, engine, coverage, report, targets)
  selftest/                  a TEST DOUBLE of the lifecycle chapter, and the target for a temporary copy of today's Python system
  baseline/                  the recorded RED result of the current Python system
  contract_regression_test.py   the contract's own tests (run by scripts/test-core.sh)
```

## Rules the files follow

* **Requests are strict, answers are additive** (I9). Every request schema sets `additionalProperties: false` at every level; the only open
  place is the request's `extensions` object. Answers may grow — with one deliberate exception: `health` is closed (`{"state": …}` and nothing
  else), so an open endpoint cannot leak detail. `contract_regression_test.py` lints exactly this.
* **A fixture typo fails closed.** Fixture files are validated against `schemas/scenario/scenario.schema.json`, which is closed everywhere; a
  misspelt key (`expected_statuz`), an unknown placeholder, an unknown hook, a section or invariant the contract does not have, a duplicate id, or a
  fixture without a trace is an error before any request is sent.
* **Every fixture cites the contract**: `traces: [{"invariant": "I2", "section": "5.2"}]` (section `D` = appendix D, the P1 clarifications).
* **`status: active`** fixtures run; **`pending`** fixtures are data for something that cannot run yet (the supervisor chapter, an open transaction at
  shutdown) and are never reported as passed.
* **`requires: [hook]`** — a fixture that needs something only some targets can give (`restart`, `drain_hold`, `storage_probe`, `memory_probe`,
  `log_probe`, `process_probe`, `runtime_file_probe`, `socket_probe`, `launch_probe`, `os_confinement_probe`, `transaction_probe`). A target
  without the hook gets `unsupported` — never a false pass. This is how "testable now" is kept apart from "needs an implementation hook".
* **`applies_when`** — the fixture applies only to some capabilities (e.g. "a core that does not list `conversation`"; "a core that reports
  `firewall` or `namespace`"). It never demands a particular level or feature: a partial implementation is valid.

## Fixture format (short)

```jsonc
{ "id": "unlock.wrong_key_never_becomes_ready", "kind": "http", "status": "active",
  "title": "…", "traces": [{"invariant": "I2", "section": "5.2"}],
  "given": {"state": "locked"},                       // a fresh core is brought to this state before the steps
  "steps": [
    { "request": {"method": "POST", "path": "/v1/unlock", "body": {"key": "${key.wrong}", "context": "${context}"}},
      "expect":  {"status": 403, "error_code": "forbidden", "must_not_contain": ["@leaks", "${key.valid}"]} },
    { "assert_health": "locked" } ] }
```

Steps: `request`+`expect`, `parallel` (N identical copies at once), `assert_health`, `wait_stopped`, `probe`. Requests default to
`Authorization: Bearer ${secret}` and, for mutations, a fresh valid `Idempotency-Key`; `auth` and `idempotency_key` override that. Bodies are JSON
(`body`), literal text (`body_raw`), bytes (`body_hex`), padded to an exact size (`body_pad_to`) or generated (`body_generated`).
Placeholders: `${secret}`, `${secret.short}`, `${key.valid}`, `${key.wrong}`, `${context}`, `${run}`, `${contract_version}`. In `must_not_contain`,
`@leaks` (all groups) or `@leaks:paths` etc. expands to the patterns in `_shared/leak-patterns.json`.

## Running it

```bash
python -m contract.runner validate          # offline: schemas, fixtures, leak patterns, coverage declaration
python -m contract.runner coverage          # which invariant is covered / partially covered / not yet testable
python -m contract.runner list              # every fixture, its status and hooks
python -m contract.contract_regression_test # the contract's own tests (also part of scripts/test-core.sh)

# against a core that YOU started for the test (any language): describe it in a JSON file
python -m contract.runner run --target-file my-core.json
# {"name": "rust-core dev", "base_url": "http://127.0.0.1:41235", "launch_secret": "<the secret it was launched with>",
#  "unlock_key": "<base64 of the 32-byte key that unlocks its throw-away state>", "hooks": []}

python -m contract.runner run --target reference      # the in-repo test double: proves the fixtures can be satisfied
python -m contract.runner run --target current-pet --lenient --json contract/baseline/red-p1-current-pet.lenient.json   # the RED baseline
```

`--lenient` skips the "given state" checks, so a system that has no lifecycle at all still has every fixture's own assertions executed (and failing).
Exit code: `0` = no failure, `1` = a fixture failed or the runner could not work, `2` = the contract's own files are invalid.

### Safety — the suite never touches the live system

* the runner **refuses** the ports of the live system (PET `9010`, node `9999`, AI-RPC `18082`, bridge `18083`, Redis, MySQL, Ollama …) and any
  non-loopback address, whatever a fixture or an operator says (`runner/wire.py`);
* the only target that starts anything (`selftest/current_pet.py`) runs a **temporary** copy with a scrubbed environment, a throw-away home,
  `YANDI_TEST_MODE=1` (the database layer refuses real connections) and its Redis address redirected to a port where nothing listens;
* with no test core, the offline checks (`validate`, the regression test) need nothing at all;
* reports never contain the launch secret or a key (they are replaced by `<secret>`, `<key.valid>`, `<key.wrong>`).

## The test double

`selftest/reference_double.py` is **not** the core and not a prototype of it: it is a ~300-line program written from appendix D, used to prove two
things about the fixtures — they can all be satisfied by an implementation that follows the contract, and each of 38 deliberately broken variants
("faults": accepts a wrong key, leaks a traceback, ignores an unknown field, applies the state before the idempotency record, …) makes a *named* fixture fail.

## RED baseline (recorded 2026-09-21)

Today's Python system (a temporary PET instance) against the P1 fixtures: **59 fail, 0 pass, 13 unsupported (hooks it cannot offer), 7 pending** —
in both modes (`baseline/red-p1-current-pet.strict.json`, `…lenient.json`). That is the expected result: the current system has no `/v1`, no launch
secret, no lock and no unlock (`GET /v1/health` answers `404 {"detail":"Not Found"}`), so every fixture fails on its own assertions. Nothing was fixed to
make it green; P1 implementation is the next step and its goal is to turn these results green, chapter by chapter.

## Changing the contract

`1.0-rc1` takes only clarifications and additive corrections, unless a conformance contradiction proves an invariant wrong (see the document's freeze
rule). An additive change is `1.0-rc2`: change `CONTRACT_VERSION`, the `$id`/`x-contract-version` of the schemas, and `contract_version` in the fixtures, in
one commit — `validate` refuses a mixture.

## Not covered yet (and said so)

`coverage.json` is checked against the fixtures: no invariant is declared covered while something remains. In particular OS-level isolation of the
core's network (I3), the key being absent from disk/memory/logs (I2), the secret's handling by the launcher (I1), and everything about conversations,
egress and memory (I4, I5, I7, I11 in full, I12) wait for their hooks or their chapters.
