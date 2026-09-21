# Core lifecycle boundary (P1a) — how the Python Core is started, locked and unlocked

This describes what is **implemented**, not the contract. The contract is `docs/NODE_CORE_CONTRACT.md` (1.0-rc1, frozen); its executable form is
`contract/`. Code: `pet/core_lifecycle.py` (the lifecycle and the ASGI boundary), `pet/core_main.py` (the process entry point).
Tests: `pet/pet_core_lifecycle_regression_test.py` and `python -m contract.runner run --target python-core`.

## Status in one paragraph

**Lifecycle unlock is implemented. Storage encryption is NOT yet bound to the Node-derived key.** The core, started in *core mode*, is a separate
process on `127.0.0.1`, locked until `POST /v1/unlock`; while locked it serves nothing of the real application (HTTP and WebSocket). What is
still true of today's system: the SQL/personal storage is encrypted with the old automatic key in `~/.local/share/yandi/keys`, so "the memory is
protected by the master password" must not be said yet; and the gate covers this process only (see *What still goes around the gate*).
Classification: **PARTIAL** — a real, tested boundary in front of the real application, with the storage binding and the other entrances open.

## Starting a core

The Node does this in P1b. By hand (development), exactly the same path:

```bash
# once, out of band: give the core the check value for a key (the key is read from stdin, never from a command line)
echo "<base64 of 32 bytes>" | python -m pet.core_main provision            # state dir: $YANDI_CORE_STATE_DIR or ~/.local/share/yandi/core

# every launch: a NEW secret in a NEW 0600 file; the file's path goes in the environment, the secret never does
umask 077; python - <<'PY' > /run/user/$UID/yandi-core-secret
import secrets; print(secrets.token_hex(32))
PY
YANDI_CORE_SECRET_FILE=/run/user/$UID/yandi-core-secret YANDI_CORE_PORT_FILE=/run/user/$UID/yandi-core-port python -m pet.core_main
```

The core prints `listening on 127.0.0.1:<port>, locked until unlocked` and waits. It never unlocks itself.

| | |
|---|---|
| Bind | `127.0.0.1` only; there is no option to change it. `--port 0` (default) lets the system choose; the port is written to `$YANDI_CORE_PORT_FILE` (0600). |
| Secret | `$YANDI_CORE_SECRET_FILE` must be a regular file of the current user, mode 0600, holding 32 random bytes in hex; anything else and the core refuses to start (exit 2). It is **read once and the file is removed**, so nothing the application later starts can read it from disk. There is no `--secret` option and no secret in the environment. |
| Modes | `python -m pet.core_main` — the real application (PET) behind the boundary. `--shell` — a tiny stand-in application (fast conformance runs; nothing else). |
| Unchanged | `start.sh` / `python -m pet.council_chat_server` do not use any of this and behave as before. |

## Lifecycle commands (contract 1.0-rc1, appendix D)

`GET /v1/health` (open, exactly `{"state": …}`) · `GET /v1/capabilities` · `POST /v1/unlock {"key": <base64 of 32 bytes>, "context": "yandi/core/v1"}` ·
`POST /v1/lock {}` · `POST /v1/shutdown {"deadline_ms": …}`. Everything except health needs `Authorization: Bearer <launch secret>` (constant-time
comparison) and, for POST, an `Idempotency-Key`. Errors are the canonical body and carry nothing internal; detail goes to the local log (never a secret).

* **locked** — only health and unlock answer; every other request to this process is `423`, every WebSocket is closed. The application's own start-up
  steps (`_reconcile_stale_sql_runs`, `_system_awareness_probe`: they write to the database and the system-state store) are **not run at start**;
  they are handed to the lifecycle and run once, after the first successful unlock. Their code is unchanged.
* **unlock** — the key is checked against a check value (AES-GCM under an HKDF-derived key; the file `check-value.json`, 0600 in a 0700 directory, holds
  ciphertext only). A wrong key, a wrong context, a malformed key, or a core with no check value (never provisioned) stays `locked`; nothing is loaded.
  The key is then held in memory only.
* **lock** — the key is overwritten and dropped (best effort: Python cannot prove that no other copy survives in the interpreter), the state is `locked`,
  the gate closes. Requests already running finish; a later `unlock` needs the key again.
* **shutdown** — `draining`: the application stops receiving requests (`503`), health says `draining`; running requests get up to `deadline_ms` (default
  5 s), then the process ends normally (exit code 0). A retried shutdown is answered from its record.
* **idempotency** — `unlock`/`lock`/`shutdown` records live in memory for the life of the process (never on disk; for `unlock` only a keyed digest of the
  body). Same key + same body: the original answer, no second transition, even after the state has moved on. Same key + another body: `409`. Eight identical
  requests at once give one transition.

`capabilities` reports `features: []` (only lifecycle exists behind `/v1`; no conversation, verification or knowledge), `egress_confinement: "none"` (nothing
confines the core's own sockets: it must reach the local model, MySQL and Redis; a firewall level needs privileges — not claimed) and the storage schema
version of the database layer.

## What still goes around the gate (why this is PARTIAL)

1. **Other entrances to the same data.** The ordinary PET (`start.sh`, port 9010) is not in core mode and serves as before; scripts and daemons that talk to
   Redis or the database directly (`pet/council_chat_listen.py`, `pet/council_gpt_auto.py`, `pet/council_claude_auto.py`, `agent/council_*`) are separate
   processes the lifecycle knows nothing about.
2. **Storage keys.** The Node-derived key is proven and held, but nothing encrypts with it yet: the SQL layer still uses its own automatic key. Binding
   storage to the derived key is a migration of its own, before the web UI moves onto `/v1` (P3/P4).
3. **Work started before a lock.** A request that started a background thread or task (for example the validation log writer in the council server) is not
   stopped by `lock`.
4. **Key residency.** Zeroisation is best effort; the suite cannot and does not prove memory contents.
5. **First key.** The contract's `unlock` cannot name a new key, so the check value is created out of band (`provision`). The Node's first-run setup will call it.
6. **Launcher properties** — a secret that never appears in a command line, a runtime file created 0600 by the launcher, a different secret per launch — are the
   Node's (P1b). The core only refuses an unsafe file and removes the file it read.

## Proof

`python -m contract.runner run --target python-core` runs the frozen fixtures against this core as a separate process in front of the real PET application:
66 pass, 0 fail, 6 unsupported (what a Python harness cannot honestly show: memory, the launcher, OS confinement, decrypted application state), 7 pending
(supervisor, open transaction). The same fixtures against today's system without core mode: 59 fail, 0 pass (`contract/baseline/`).
