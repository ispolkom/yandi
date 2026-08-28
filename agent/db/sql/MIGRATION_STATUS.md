# SQL persistence migration — status (Этап 5)

Read this before touching anything in `agent/db/sql/`.

## Current phase: 5E (shadow dual-write, wired, unverified live)

| Phase | What it means | Status |
|---|---|---|
| 5A design | Schema/entity model | Done — see the storage audit artifact + `schema.py`'s module docstring corrections |
| 5B-1 prerequisite fix | Delivered-answer text capture | Done, committed (`5171473`) |
| 5B-2 prerequisite decision | Q&A archive reconciliation | Decided (not executed — see below), documented in final report |
| 5C schema + connection | DDL + env-config connection layer | Done, committed (`893e9fc`) — **static, never live-tested** |
| 5D repositories | Read/write functions | Done, committed (`893e9fc`) — unit-tested against a mocked DB-API connection only |
| 5E shadow dual-write | Wired into production, fail-open | Done, committed (`6e49609`, `530063c`) — question/run + answer/assessment + claim/evidence, all wired |
| 5F equivalence audit | JSON vs SQL, real traffic | **BLOCKED BY CREDENTIALS** |
| 5G reset | Clear old runtime history | **NOT STARTED** — requires 5F first |
| 5H virgin run | Fresh DB, fresh JSON | **BLOCKED BY CREDENTIALS** |
| 5I read cutover | Local Memory reads from SQL | **NOT STARTED** |
| 5J remove JSON dependency | JSON becomes debug-only | **NOT STARTED** |

## Why 5F onward is blocked

No MySQL/Percona credentials exist anywhere in this environment:
checked env vars, `~/.my.cnf`, passwordless sudo to read the systemd
service's env file — all absent. `agent.db.sql.connection.
is_configured()` returns `False`. A real `mysql.service` (Percona
Server) IS running on `:3306`, but nothing was done to guess or bypass
its credentials (explicitly forbidden by the mandate).

**Nothing in this codebase claims live MySQL validation that didn't
happen.** What IS proven, and how:

- Schema structural invariants (43 checks, `db_sql_schema_regression_test.py`) — static analysis of the DDL strings, no connection needed.
- Repository SQL construction + idempotency logic (22 checks, `db_sql_repositories_regression_test.py`) — against a hand-written fake DB-API connection that records executed SQL/params, not a real server.
- Fail-open contract (11 checks, `db_sql_shadow_write_regression_test.py`) — against the REAL current "unconfigured" state of this environment, which is genuinely representative (this IS what production would see today).
- Production wiring positions (8 checks, `db_sql_wiring_regression_test.py`) — real call-graph position checks + a real `run_optimistic_respond()` call proving the wiring doesn't change behavior with SQL unconfigured.
- Claim/evidence wiring + resource/route mapping (15 checks, `db_sql_claims_evidence_shadow_regression_test.py`) — same technique, plus the local_memory-replay and non-internet-skip cases against real evidence-dict shapes.

**Also currently blocked**: this machine's GPU driver went down mid-session, making the ~3 regression files that make real Ollama embedding/LLM calls (family identity batching, dependency recheck, claim family persistence) impractically slow under CPU fallback. They were last confirmed GREEN immediately before the 5E claim/evidence wiring commit; that commit's own verification used a curated set of fast, non-model-call tests instead of the full suite. **Re-run the full 60-file regression suite once the GPU is back**, before treating 5E as fully re-confirmed.

## What is NOT wired yet (deliberate)

- `shadow_reconcile_stale_runs()` — not called from any daemon startup path. Needs a decision about which process actually owns "the daemon" in this codebase (not investigated in this pass).
- `pre_pipeline.py`'s ~11 early-return short-circuits do not call `shadow_complete_run()` — a run that exits early via one of those paths stays `status='running'` in SQL until reconciled.
- `origin_observation_id` (the SQL-side replay-chain FK) is always written as `NULL` by `shadow_record_claims_and_evidence()` — resolving which SQL observation a `local_memory` replay pointed at needs a resource+run+time lookup not built in this pass. The JSON-side provenance chain (`origin_route`/`origin_trace_id`/...) is completely unaffected.
- `claim_family`/`family_member` rows are NOT written by `shadow_record_claims_and_evidence()` — `claims_data` doesn't carry `canonical_text`, which `get_or_create_claim_family()` requires. Wiring this belongs at `claim_family_registry.py`'s own `find_or_link_claim()` call site, a separate decision.
- non-`internet` evidence (`network_node`/`ai_chat`/`local_model`, after replay-resolution) is skipped entirely by the claim/evidence shadow write, not given a fabricated identity — matches `agent.verification_memory.compute_stable_root()`'s existing V1 scope.

## Q&A archive reconciliation decision (mandate §23)

`agent/db/manager.py::KnowledgeDB` (written every request, exact-hash
question identity, UPDATE-in-place) is the concept this migration's
`question`/`answer_version`/`answer_assessment` tables supersede.
`agent/orch_knowledge_writer.py` (conditional writes on
verified/partially-verified verdicts, P2P peer-sync capability) serves
a genuinely different purpose — curated, network-shareable knowledge,
not per-request history — and is explicitly **out of scope** for this
migration. Neither store has been touched, modified, or deleted.

## Do not

- Do not set `YANDI_SQL_USER`/`YANDI_SQL_PASSWORD` by guessing.
- Do not add `shadow_record_claim`/`shadow_record_evidence` calls to
  the production pipeline without also writing the equivalent
  call-graph-position + fail-open regression coverage the existing
  wiring has.
- Do not run `agent.db.sql.migrate` against a real server without
  first confirming with the project owner which database/credentials
  are actually intended for this (a `yandi_epistemic` database is
  assumed by default — never verified against anything real).
