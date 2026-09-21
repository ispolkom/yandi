# Core supervision (P1b) — how the node owns the Python Core

What is **implemented**, not the contract (`docs/NODE_CORE_CONTRACT.md`, 1.0-rc1, frozen). Code: `node/core_supervisor/` (a small crate: process
ownership, runtime files, key derivation, lifecycle client, supervisor) and `node/src/managed_core.rs` (the node's opt-in wiring). The Core's side
is described in `docs/CORE_LIFECYCLE.md`. Proof: `scripts/test-node-supervisor.sh`.

## Status in one paragraph

**P1b — the node's supervisor — is implemented and tested for Linux/Unix.** It is **off by default** (`YANDI_MANAGED_CORE=1` turns it on) because nothing
consumes the Core over `/v1` yet; the web UI moves onto it in P3/P4. What is still true: **the legacy PET (`./start.sh`, :9010) is still a second way in to
the same data and is not gated by the node**, so the system-wide "the Core has exactly one caller: the node" is **not enforced**. The Node-derived key
encrypts the personal memory only where the owner has run `python -m agent.db.sql.protect seal` (P1c-2, `docs/STORAGE_PROTECTION.md`; off by default, six tables), and its root (the node's master key) is recoverable from disk files without any user secret (`docs/KEY_CHAIN_AUDIT.md`). See *What still goes around the node*.

## What the node does

```text
start   →  generate a fresh 32-byte launch secret, create <runtime>/core-<nodepid>-<rand>/ (0700), write launch-secret (0600)
        →  spawn the Python Core directly (no shell), own process group, environment names only the FILE PATHS
        →  wait for the port file the child writes; accept the port only if the child itself listens there, on loopback, and nowhere else
        →  wait for GET /v1/health = locked (a core that starts ready is refused)
        →  wait for the master key (the node's auth state) — never invent one
        →  first run: provision the Core's check value out of band (key on stdin), then POST /v1/unlock with HKDF(master, "yandi/core/v1")
        →  wait for health = ready;  then watch the child
exit    →  new process, NEW launch secret, NEW directory, unlocked again; bounded exponential backoff; after the limit, stop and report
stop    →  POST /v1/shutdown, wait, TERM the owned group, KILL only if TERM was ignored; remove the runtime artifacts
```

| Topic | Behaviour |
|---|---|
| Ownership | The supervisor holds the child handle it spawned. It never scans ports, attaches to an existing Core, or signals a process it did not spawn. A port that its child does not own (a port file pointing elsewhere, a Core that also listens beyond loopback) is refused **before** any request is sent. Ownership is proven from `/proc` on Linux; elsewhere only the private port file is available (documented limit). |
| Launch secret | 32 random bytes (hex), new every launch, never derived. Path in `YANDI_CORE_SECRET_FILE`; never in argv, the process title, a log or an error. The Core reads it once and removes the file. |
| Runtime directory | `$XDG_RUNTIME_DIR/yandi-core` (else `/tmp/yandi-core-<uid>`), 0700, owned by the user, not a symlink; each launch directory 0700, the secret file 0600, created exclusively without following links. Removed after every launch and at shutdown. At start only leftovers of a **dead** node (`core-<pid>-…` of a pid that does not exist, our uid, mode 0700) are removed — nothing is ever killed for cleanup. |
| Keys | The master key never enters the Core. The Core gets `HKDF-SHA256(master, info="yandi/core/v1")`, checked against a vector computed with the Python core's own library. Derived and temporary buffers are wiped on drop (best effort). |
| Production unlock | Not automatic with a made-up key: the supervisor waits for the node's master key. On this machine the node decrypts its master key at start (`load_auth_state`, machine-bound), so the Core is unlocked right after it comes up; on first run or rebind it stays locked until setup finishes. A wrong key (`403`) is final: the Core is stopped and `core_unlock_refused` is reported; restarting cannot cure it. |
| Crash | The node keeps running. Every failure is a category: `core_start_failed`, `core_exited`, `core_restart_exhausted`, `core_unlock_refused` — never a traceback, a path or a socket. The core's own output goes to a local log (`~/.local/share/yandi/core.log`, 0600). |
| Restart | Backoff `initial · 2^n`, capped (defaults: 0.5 s, cap 30 s). `restart_limit` automatic restarts inside `failure_window` (defaults: 5 in 300 s); the next failure stops the restarting. These are implementation settings, not part of the contract. |
| Node killed | `PR_SET_PDEATHSIG` (SIGTERM) on the child: a Core does not outlive its node even if the node is SIGKILLed (Linux). |
| Windows / macOS | Not yet: the crate is Unix-only (process groups, `/proc`, PDEATHSIG). |

## Turning it on (development and production of the contract path)

```bash
# once: the node has its master key (web UI setup) — the check value for the Core is created by the node itself
YANDI_MANAGED_CORE=1 YANDI_CORE_PYTHON=~/venv/bin/python YANDI_CORE_ROOT=~/yandi ./node/target/release/yandi
```

`YANDI_CORE_STATE_DIR` (default `~/.local/share/yandi/core`) is where the Core keeps its check value. A core can still be started by hand
(`docs/CORE_LIFECYCLE.md`); it waits, locked.

## Canonical path and legacy path

* **Canonical production path (the contract's):** node → supervised Core (`YANDI_MANAGED_CORE=1`) → `/v1`.
* **Legacy / development path:** `./start.sh` → PET :9010 with its own web UI and its own access to the database and Redis. It stays until the UI moves onto
  the node (P3/P4). It is *not* a second production entrance to build on.

## What still goes around the node

1. The legacy PET on :9010 (and the council scripts/daemons that talk to Redis or the database directly) reach the same cognition and data without the node.
   **System-wide one-caller enforcement: NO** until P3/P4 retire them.
2. Storage encryption is bound to the Node-derived key **only after `protect seal`** (P1c-2, `docs/STORAGE_PROTECTION.md`; six personal tables; off by default): on a sealed database `lock` also closes the data (the key is forgotten), on an unsealed one it is an execution gate only.
3. The node's master key is decrypted automatically on this machine (machine-bound), so an attacker who is already the same user on the same machine
   can obtain it; the Core's lock protects against a Core process that is not running, not against that.
4. Egress confinement is `none`; all outbound traffic through the node is P2.
