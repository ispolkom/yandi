# SQL persistence migration — status (Этап 5)

Read this before touching anything in `agent/db/sql/`. **Read
`SQL_DEPLOYMENT_DEFERRED.md` FIRST** if you're about to touch
`agent/db/sql/security_*.py`, `bootstrap.py`, `deploy/`, or start
thinking about 5F — it's the 30-second version of "what's designed,
what needs owner-sudo, what not to touch," so this file doesn't need
to repeat it in full below.

## Canonical status

| Stage | What | Status |
|---|---|---|
| 5E | SQL shadow wiring | DONE |
| 5E-S | SQL Bastion (offline design + regression) | DONE |
| 5E-S2 | Dedicated DB design/audit | DONE |
| 5E-S2-LIVE | Privileged deployment | BLOCKED / DEFERRED |
| 5F | JSON↔SQL equivalence | NOT STARTED |
| RESET | Clearing old runtime history | FORBIDDEN |

**The SQL Bastion work is deliberately shelved here** — the project
owner's own words: "SQL-бастион теперь можно спокойно положить на
полку до момента, когда реально понадобится canonical cutover." The
next SQL-related task is not "finish the installer" — see
`SQL_DEPLOYMENT_DEFERRED.md` §3 for the explicit do-not-touch list.

## Current phase: 5E-S2 (DEDICATED DATABASE APPLIANCE) — design/audit only, NOT deployed

Sequence: 5E → 5E-S SQL BASTION → **5E-S2 dedicated appliance
(design phase, current)** → 5F equivalence → 5G reset → 5H virgin run
→ 5I read cutover → 5J remove JSON dependency.

**CRITICAL DECISION (owner, this stage)**: the shared FastPanel Percona
instance found during 5E-S must NEVER be used for YANDI — not
modified, not connected to with guessed/obtained credentials, not
touched at all. YANDI needs its OWN dedicated local instance.

This stage produced a full non-privileged design (`agent/db/sql/
DEDICATED_INSTANCE_DESIGN.md`, `agent/db/sql/DISK_CAPACITY_REPORT.md`,
`agent/db/sql/storage_policy.py`, `deploy/yandi-db.service`, `deploy/
install-yandi.sh`) but **deployed nothing** — this session has no
passwordless sudo (`sudo -n` fails), and per explicit instruction did
not request, obtain, or work around that. No `yandi-db` OS user, no
`/var/lib/yandi`/`/etc/yandi`/`/run/yandi` paths, no systemd unit, no
dedicated mysqld process exist anywhere on this host. See `DEDICATED_
INSTANCE_DESIGN.md` §L for the exact list of what remains unverified
until an owner-authorized `sudo ./deploy/install-yandi.sh` run
happens — including the fact that `install-yandi.sh`'s own DB-level
hand-off (`run_python_bootstrap()`) is a deliberate stub that refuses
to proceed, because the auth-mechanism decision (§H: `auth_socket` vs.
a generated secret file) has not been made yet.

## Prior phase: 5E-S (SQL BASTION) — primitives built + unit-tested, wiring + live enforcement NOT DONE

New sequence (mandate: 5E → 5E-S SQL BASTION → 5F equivalence → 5G
reset → 5H virgin run → 5I read cutover → 5J remove JSON dependency).
**5F must not start until 5E-S is accepted** — see
`SECURITY_ARCHITECTURE.md`/`SECURITY_THREAT_MODEL.md` for the full
design, and the session's final report for the honest PROVEN/DESIGNED/
BLOCKED breakdown. One-line summary: table classification, GRANT/
trigger design, AES-256-GCM crypto layer, HMAC integrity journal, and
bootstrap logic are all written and unit-tested against fakes; NONE of
it is wired into `repositories.py`'s live write path yet, and NONE of
it has been proven against a real server (still no SQL credentials in
this environment; the actual Percona instance turned out to be a
SHARED, FastPanel-managed host listening on `*:3306`, not a dedicated
YANDI instance — a required owner decision, `SECURITY_ARCHITECTURE.md`
§4, that 5E-S deliberately does not make unilaterally).

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

**GPU note (resolved)**: the GPU driver outage mentioned in an earlier version of this file has recovered — `nvidia-smi` confirms the card is back and healthy. The full regression suite (66 files as of this pass, all Ollama-calling tests included) has been re-run green multiple times since, and a full, uncached, real-web live run (`process()` called directly, ~540s wall clock, 24 claims, real retrieval/NLI/synthesis) completed with zero exceptions and the SQL shadow-write wiring firing correctly (`record_question_and_run` → `record_claim_family` ×24 → `record_claims_and_evidence` → `complete_run`, all `SKIPPED (SQL unavailable)` as expected, fail-open, no crash).

## AI SELF-REPORTED PROVENANCE (schema-only architectural stub)

`ai_observation`/`ai_reported_source` tables exist in `schema.py` as a
**forward-looking stub only** — no AI-chat transport, no provider API
calls, no browser automation, no `AI_CHAT` route activation, no Trust
change, no independence counting, and (deliberately) no repository or
shadow-write functions for these two tables exist anywhere yet. INSPECT
conclusion: the existing `source_resource`/`source_observation` model
cannot hold this without breaking its own semantics (a provider+model
has no URI to dedupe on; one AI answer can report many sources at
once, which the one-observation-one-resource shape can't express
without fabricating resources for strings the model merely claimed) —
hence two small, new, additive tables rather than an extension.
`ai_reported_source` carries **no FK to `source_resource`** — a
self-reported source name/URI is never wired as proven identity.
See `agent/db_sql_ai_provenance_schema_regression_test.py` for the
regression proving this stays true, and `schema.py`'s own comment
block for the full rationale (search for "SELF-REPORTED PROVENANCE").

## NETWORK_NODE PROVENANCE (documentation-only extension contract)

No new tables. INSPECT conclusion (mandate's own explicit ask): the
schema is extensible enough for now — a future `NODE_OBSERVATION`/
`NODE_REPORTED_SOURCE`/`PROVENANCE_RESOLUTION` design is documented in
`schema.py` (search "NETWORK_NODE PROVENANCE — extension contract"),
structurally mirroring `AI_OBSERVATION`, answering all 10 of the
mandate's own SQL_ARCHITECTURE_CHECK questions inline. One real
structural fix: `source_resource.node_id` was `VARCHAR(40)` — too
narrow for this codebase's actual node identity
(`node/src/core/identity.rs::NodeIdentity::node_id()`, a real
Ed25519/X25519-derived 32-byte `HashId`, hex-encoded to exactly 64
chars) — widened to `VARCHAR(64)` before any writer exists. Also
documented: a DIFFERENT, already-active, unrelated "node_id" concept
(`agent/orch_federation.py`/`agent/orch_reputation.py`'s short labels
for local background-validation council members, e.g.
`"local-qwen14b-a"`) must not be confused with the future P2P identity.
`NETWORK_NODE_PROVENANCE_INVARIANTS` in `schema.py` holds the 9
required invariants verbatim, for future code to assert against. See
`agent/db_sql_network_node_provenance_regression_test.py`.

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
