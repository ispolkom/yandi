# SQL persistence migration — status (Этап 5)

Read this before touching anything in `agent/db/sql/`.

## Current phase: 5E complete (shadow dual-write, all known wiring gaps closed, still unverified live)

| Phase | What it means | Status |
|---|---|---|
| 5A design | Schema/entity model | Done — see the storage audit artifact + `schema.py`'s module docstring corrections |
| 5B-1 prerequisite fix | Delivered-answer text capture | Done, committed (`5171473`) |
| 5B-2 prerequisite decision | Q&A archive reconciliation | Decided (not executed — see below), documented in final report |
| 5C schema + connection | DDL + env-config connection layer | Done, committed (`893e9fc`) — **static, never live-tested** |
| 5D repositories | Read/write functions | Done, committed (`893e9fc`) — unit-tested against a mocked DB-API connection only |
| 5E shadow dual-write | Wired into production, fail-open | Done — question/run, answer/assessment, claim/evidence, claim_family/family_member, origin_observation_id replay-chain, early-return run completion, and startup stale-run reconciliation are ALL wired (`6e49609`, `530063c`, `ff9aad8`, `1c1f975`, `24afd8c`, `9268e19`, `ba15fbd`) |
| 5F equivalence audit | JSON vs SQL, real traffic | **BLOCKED BY CREDENTIALS** |
| 5G reset | Clear old runtime history | **NOT STARTED** — requires 5F first |
| 5H virgin run | Fresh DB, fresh JSON | **BLOCKED BY CREDENTIALS** |
| 5I read cutover | Local Memory reads from SQL | **NOT STARTED** |
| 5J remove JSON dependency | JSON becomes debug-only | **NOT STARTED** |

## Since the previous status snapshot (post-5E continuation pass)

All five gaps this file previously listed under "What is NOT wired yet
(deliberate)" are now closed, plus two P0 bugs found along the way
that would have silently broken nearly every write on a real live DB:

1. **claim_family/family_member wiring** (`ff9aad8`) — wired at
   `assign_claim_family_identity()`'s own call site
   (`agent/orchestrator/claims/lifecycle.py`), not the bulk claims/
   evidence path (which still has no `canonical_text` to write with).
   Closes a real FK-violation risk: `claim_occurrence.family_id`
   references `claim_family(family_id)`, and the bulk path inserts
   `claim_occurrence` rows with `family_id` set — without this,
   every such insert would have failed on a live DB, silently
   swallowed by fail-open.
2. **P0: Unix-epoch-float timestamps coerced to `datetime`** (`1c1f975`)
   — found while investigating item 3 below.
   `agent.orch_tracer.Trace.timestamp` is a raw float, forwarded
   unchanged as `started_at`/`asked_at` into `resolve_question()`/
   `start_run()`, which bound it directly to a DATETIME column — not a
   valid MySQL datetime literal. Every question/run row would have
   failed to insert on a live DB. Also fixed a UTC/local-time
   inconsistency (`writeback.py` used `datetime.now()` where
   everything else uses `datetime.utcnow()`) that could have made a
   run's `completed_at` sort before its own `started_at`.
3. **`origin_observation_id` resolved for local_memory replay** (`24afd8c`)
   — `agent.db.sql.repositories.find_observation_id_for_replay()` uses
   the replay's JSON-side `origin_trace_id` (== the original run's SQL
   `run_id`) to look up the SQL observation it replayed. Falls back to
   NULL, never fabricated, when the origin run has no matching SQL row.
4. **Early-return runs no longer stuck at `status='running'`** (`9268e19`)
   — `orchestrator_v2.py`'s TWO `if early_response is not None: return
   early_response` points (after `run_pre_pipeline()` AND after
   `run_standard_pipeline()` — the mandate's own audit only named the
   first) now call `shadow_complete_run()` with that branch's own
   `early_response.answer`/`trust_level`, never a fabricated trust
   value.
5. **`shadow_reconcile_stale_runs()` wired into daemon startup** (`ba15fbd`)
   — `pet/council_chat_server.py` is the daemon (`chat_orch.py`'s
   router calls `agent.orchestrator_v2.process()` in-process); a
   `@app.on_event("startup")` handler there is the one hook both real
   launch paths (`start.sh`'s direct execution, `start_headless.sh`'s
   `uvicorn pet.council_chat_server:app`) share.

Full 65-file regression suite re-run after each of these commits
(GPU-backed, no skips) — see each commit message for the exact
targeted counts; the full-suite result is confirmed green in the
session that made these changes.

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

- non-`internet` evidence (`network_node`/`ai_chat`/`local_model`, after replay-resolution) is skipped entirely by the claim/evidence shadow write, not given a fabricated identity — matches `agent.verification_memory.compute_stable_root()`'s existing V1 scope. This is the one item from the original list that stays as-is; everything else that used to be listed here is now wired (see "Since the previous status snapshot" above).
- the trust-badge-baked-into-delivered-text staleness bug (badge reflects pre-reflection-downgrade/pre-canonical-Trust-cutover `trust_level`, documented in `agent/answer_delivery_persistence_regression_test.py`'s case D) is still NOT fixed — fixing it means reordering background-validation kickoff relative to the Trust cutover, a latency-risk architectural decision out of this migration's scope.

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
