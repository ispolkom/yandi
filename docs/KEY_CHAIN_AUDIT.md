# Key chain audit (P1c, part 1 — read-only)

Date 2026-09-21. **No code was changed for this audit.** It answers, from the code and the design documents, what is really secret in YANDI today,
what an attacker with only files from the disk gets, and what survives a backup, a new machine or a reinstall. Things that can only be seen on the
owner's machine are listed at the end as commands to run. Every statement below has a file reference; where I inferred instead of observing, it says so.

## The chain as it really is

```text
master password ──Argon2id(pw, RANDOM salt that is never stored)──▶ master_key (32 bytes)          node/src/web/auth.rs:363-365
master_key ──AES-GCM under Argon2id("YANDI_MACHINE:" + /etc/machine-id, salt stored beside it)──▶ ~/.yandi_keys/auth.json   auth.rs:250-290
   at every node start, on the same machine: decrypted automatically, no password                                          auth.rs:306-345
master_key ──HKDF(salt=node_id, info="yandi-chat-v2")──▶ chat storage key                                                  communication/storage.rs:65-76
master_key ──HKDF("yandi/core/v1")──▶ Core key ──▶ unlock gate + check value (P1a/P1b)                                     core_supervisor/src/keys.rs
identity private keys ──Argon2id(env YANDI_KEY_PASSWORD, else "YANDI:" + machine-id + node address)                       core/identity.rs:92-111   (master_key NOT used)
node model/API config (SQLite) ──KEK file ~/.local/share/yandi/keys/node_kek.bin (0600, created silently on first use)    llm_gateway/secure_store.py:63-123
personal / epistemic memory (MySQL) ──▶ NOTHING: plain text at rest; crypto primitives exist but are not wired            agent/db/sql/SECURITY_ARCHITECTURE.md §12, §21; docs/KNOWN_ISSUES.md
```

## Findings

**F1 — The master password cannot recover the master key (critical for the "one master password, recovery on the node" idea).**
`master_salt` is generated at setup and used once; it is not written to `auth.json`, nor anywhere (`grep master_salt`: only lines 363-365 and 443-445).
So `master_key` is, in effect, a random 256-bit value that only ever exists (a) in RAM and (b) as the machine-encrypted blob in `auth.json`. `rebind_to_machine`
generates a **new** random salt and therefore a **different** master key from the same password (auth.rs:443-445); its comment ("we trust the user … identity
decryption fails") does not hold: the password proves nothing and restores nothing. Consequence, by construction (not tested): after a rebind, the chat store
that was encrypted under the old master key cannot be read. The password today only distinguishes nothing at all: login uses a separate Argon2id hash
(`login_hash`) and guards the web UI only.

**F2 — At rest, the master key is protected by a public value.** The key that encrypts it is derived from `/etc/machine-id` (readable by every local user,
not a secret) and a salt stored next to the ciphertext. Anyone who has `auth.json` and the machine id recovers the master key with one Argon2id computation;
no secret of the user is involved. It protects against nothing that has the same file set: a stolen disk (the machine id is on it), a backup, another local
user who can read the file, malware running as the same user. (`auth.json` is set to 0600 at setup, which limits *other users* only.)

**F3 — The identity private keys do not use the master key.** `node/AUTH_IMPL_PLAN.md` says `master_key → HKDF("identity") → identity_key`; the code
(`identity.rs:99-111`) uses `YANDI_KEY_PASSWORD` if the user set that environment variable, otherwise `"YANDI:" + machine-id + first 16 bytes of the node
address` — both public. Only the environment password is a real secret, and it is optional.

**F4 — Chat storage is exactly as strong as F2.** With a master key: `HKDF-SHA256(master_key, salt=node_id, info="yandi-chat-v2")`; without one (legacy path):
the raw node id ("weak — node_id is public", comment at storage.rs:66).

**F5 — `node/src/core/crypto_storage.rs` derives its key from the node id alone** (`SHA-256(node_id ‖ "yandi-storage-key")`), a public value. I found no caller
outside the file (`grep "crypto_storage::"`); confirm before relying on either conclusion.

**F6 — The personal and epistemic memory in MySQL is not encrypted at all.** `agent/db/sql/crypto.py` (AES-GCM with AAD, blind index) and `keys.py` (KEK/DEK
hierarchy) exist and are unit-tested, but the code says "nothing in production calls them yet" (`crypto.py` docstring; `SECURITY_ARCHITECTURE.md` §12, §21), and
`docs/KNOWN_ISSUES.md` says the personal tables are "unencrypted at rest". What protects them today: a dedicated database instance, a unix socket, a
least-privilege role, and the Linux user's file permissions. **My earlier statement, written in `docs/CORE_LIFECYCLE.md`, `docs/CORE_SUPERVISION.md` and
`docs/KNOWN_ISSUES.md`, that "the SQL layer still uses its own automatic key" was wrong and has been corrected**: the only thing encrypted with the file
`~/.local/share/yandi/keys/node_kek.bin` is the node's model/API configuration store (F7). There is no SQL storage key to "bind" the Node-derived key to;
encryption of the memory has to be *introduced*.

**F7 — The node configuration store (models, API references) has a real, if modest, key hierarchy.** `secure_store.py`: AES-GCM fields, blind index, hash-chain
journal, rollback checkpoint. Its KEK is a random 32-byte file created silently on first use, single copy ("loss of the file = loss of the settings"), 0600, in the
same user directory as the data; `llm_gateway/harden_key.sh` can make it immutable (needs root, optional). It protects against reading the database file
without the key file; not against a backup that contains both, nor against the same user.

**F8 — There is no backup or restore path, and no test of it.** `SECURITY_ARCHITECTURE.md` §16: "5E-S is not production ready without this test". Nothing
records what must be backed up (auth.json, node_kek.bin, the identity file, the MySQL data) or in what order; the recovery of the memory on a new machine is
untested and, for the master key, impossible by F1.

**F9 — The P1b Core key is sound mechanically and inherits F1/F2.** HKDF from the master key with the contract's context, checked against an encrypted check
value, held in memory only. But its root is the master key of F1/F2, so unlocking the Core is as strong as reading two public-ish files, and it protects no data (F6).

**F10 — Other plaintext stores** (already documented in `SECURITY_ARCHITECTURE.md` §18): the legacy SQLite `KnowledgeDB`, `registry/*.json`, trace JSONL files,
and Redis (council chat lists). Full-disk protection is all that covers them.

**F11 — A failed identity load silently replaces the node's identity.** `NodeIdentity::load_or_create` (`identity.rs:423-441`): if the saved identity cannot be
decrypted (the machine id changed after a reinstall or a hardware move, `YANDI_KEY_PASSWORD` was set or changed, the file is damaged), it prints one line, creates a
**new** identity and **overwrites the saved file**; the old one is not kept. The node id, the keys and every pairing that depends on them are lost with no
error and no backup. This makes F2/F3 worse: the protection is public, *and* a change of it destroys the data instead of stopping. **Do not set
`YANDI_KEY_PASSWORD` on a node whose identity already exists** without a migration: the existing file was encrypted with the machine-id fallback and would not load.

## Observed on the owner's machine (2026-09-21)

* `~/.yandi_keys/auth.json` 0600 (2026-06-28), `node_identity_9000.json` 0600, but the **directory `~/.yandi_keys` is 0755** (files inside are 0600, so other users
  see the names but not the contents; it should be 0700).
* `/etc/machine-id` is `-r--r--r--`: world-readable, as expected (F2).
* `YANDI_KEY_PASSWORD` is **not set**, so the identity private keys are protected only by public values (F3, in effect).
* `~/.local/share/yandi/keys/node_kek.bin` 0600 (2026-09-13, 32 bytes) with `node_config_chain_tip.json`, in a 0700 directory (F7).
* `~/.local/share/yandi/core/check-value.json` (0600, 137 bytes) was created by the first managed-Core run (P1b), as designed.
* Not seen from here: any backup of these files or of the database.

## What is secret today, and what is recoverable from disk files alone

| Store | Protected by | Real user secret involved? | Recoverable from the disk files alone? | After backup → new machine | After machine-id change / OS reinstall |
|---|---|---|---|---|---|
| `auth.json` master key | public machine-id (F2) | no | **yes** | opens only on the same machine-id; otherwise "rebind" | rebind makes a **different** key (F1) |
| Login to the web UI | Argon2id of the login password | yes | no (needs the password) | works | works |
| Identity private keys | env password, else public values (F3); a failed load replaces them (F11) | only if `YANDI_KEY_PASSWORD` is set (it is **not** on the owner's machine) | **yes** unless the env password is set | opens only with the same machine-id (fallback) | needs the env password or fails |
| Chat store | master key (F4) | no | **yes** | unreadable on another machine-id | unreadable after a rebind (F1) |
| Node model/API config | `node_kek.bin` (F7) | no | yes, if the backup has both files | opens if both files are restored | unaffected by the machine id |
| MySQL personal/epistemic memory | nothing at rest (F6) | no | **yes, plain text** | as the database backup | unaffected |
| Legacy SQLite/JSON/JSONL/Redis | nothing (F10) | no | yes, plain text | as the files | unaffected |

## What the design protects against today, and what it does not

| Threat | Protected? |
|---|---|
| A website or another local user talking to the web servers | yes (deny-by-default guard, launch secret between node and Core) |
| Another Linux user reading the owner's files | partly: file modes 0600/0700; nothing cryptographic that survives root or a shared backup |
| Stolen disk, stolen or leaked backup | **no** for the master key, chats, identity (without the env password) and memory; partly for the node config (only if the KEK file was not copied along) |
| Malware running as the same user | **no** (it can read every key file and the machine id; the master key is in the node's memory anyway) |
| Loss of the machine / reinstall | **no recovery path**: the master password cannot restore the master key (F1) |
| An attacker who can only reach the Core's HTTP port | yes since P1a/P1b (launch secret, loopback, gate) — a different question from the data |

## Decisions the owner has to make (plain language; not code)

1. **Where does the user's secret enter?** Today nowhere at boot. Options: (a) a password typed at every node start, from which the key that opens the
   master key is derived (strongest; the node cannot restart unattended, a Core crash is still fine because the key stays in the node's memory); (b) the
   operating system's key store or a hardware module opens it (no typing, protects against a stolen disk but not against the logged-in user);
   (c) both: a device key for convenience plus the password for recovery. This is a product decision: security against convenience.
2. **Recovery.** A real recovery needs either the password-derived key wrapped properly (store the salt; the random master key encrypted under a key
   derived from the password) or a printed recovery code, and a rehearsed restore on a clean machine. Today none exists (F1, F8).
3. **What must be encrypted first.** The chat store and identity already have code that uses a master key; the personal memory has none. Which columns,
   and whether old plain-text rows are migrated or left (`docs/KNOWN_ISSUES.md`: "Old episodes are not adopted").
4. **Legacy plaintext stores** (F10): migrate, encrypt the disk instead, or accept.

## Recommendation (engineer's; nothing is implemented)

Do **not** bind the memory to the current master key: it would add a cipher whose key sits next to the data (F2). Split the next work into three reviewable
steps, each with its own tests and a restore rehearsal:

* **P1c-1 — a real key root.** Separate a *device key* (may be a public-id-derived or OS-store-held value; protects only "this copy of the files on this machine")
  from the *user master key*. Persist the KDF salt; make the master key recoverable from the password (or a recovery code); rebind must restore the **same**
  master key; migrate the existing `auth.json`, identity and chat store in a way that is tested to be lossless. Tests first (restore on a clean directory,
  wrong password, changed machine id, tampered files), including mutants of F1/F2 themselves.
* **P1c-2 — encrypt the personal memory** with data keys derived from the Core key (`HKDF(master, "yandi/core/v1")` → per-table DEKs, AAD as already designed
  in `crypto.py`), wired into the repositories, with the backup → destroy → restore → decrypt → integrity check that the design says is a gate. **Built (2026-09-22): `docs/STORAGE_PROTECTION.md`.**
* **P1c-3 — bind lock/unlock to that**: while locked, the persistent personal state cannot be decrypted (today it is merely not served).

## To check on the owner's machine (only the owner can see these)

```bash
ls -la ~/.yandi_keys ~/.local/share/yandi/keys ~/.local/share/yandi/core 2>&1
stat -c '%A %U %n' ~/.yandi_keys/auth.json ~/.local/share/yandi/keys/node_kek.bin 2>&1
stat -c '%A %n' /etc/machine-id                    # expected: -r--r--r--  (world-readable, so not a secret)
echo "${YANDI_KEY_PASSWORD:+YANDI_KEY_PASSWORD is set}"   # prints only whether the identity password is in the environment
```

Also: is there any backup of `~/.yandi_keys`, `~/.local/share/yandi` and the MySQL data directory, and where? Is the MySQL data directory on an encrypted disk?

## Corrections to earlier statements

* "The Node-derived key does not encrypt the SQL/personal storage because the SQL layer still uses its own automatic key" → the SQL/personal memory is **not
  application-encrypted at all**; the automatic KEK belongs to the node's configuration store (F6, F7).
* "The master password is the recovery path" (design intent) → in the code it is not (F1).
* "The node's master key is machine-bound so an attacker needs the machine" → the binding is to a public identifier; it is obfuscation, not protection (F2).

## Status after P1c-1 (2026-09-22)

| Finding | Now |
|---|---|
| F1 master password cannot recover the key | **Fixed for a migrated directory**: the existing master key is the ROOT, wrapped by a device key and by an Argon2id recovery password; `rebind` no longer makes a different key (it now refuses and points to `yandi-keys recover`) |
| F2 master key protected by a public value | Migrated: protected by a random device key (a private file — see the honest limit in `docs/KEY_RECOVERY.md`) and by the recovery password; the machine id is only a label. **Not migrated yet on the owner's machine** until `yandi-keys migrate` is run |
| F3 identity does not use the master key | Migrated: identity format 3 under `HKDF(root, "yandi/identity/v1")` |
| F11 a failed load replaces the identity | **Fixed** (also for legacy directories): fail closed, nothing written; `setup_auth` no longer overwrites an existing `auth.json` |
| F4 chat store as weak as F2 | Same root as before, so as strong as the device key / recovery password after migration |
| F6, F7, F8, F10 | **Unchanged**: SQL memory still not encrypted, no backup pipeline for it, legacy plaintext stores (P1c-2 and later) |

## Status after P1c-2 (2026-09-22)

| Finding | Now |
|---|---|
| F6 personal memory not encrypted | **Mechanism built and proven, OFF until the owner runs `seal`**, six tables (`interaction_turn`, `personal_fact`, `personal_fact_event`, `commitment`, `commitment_event`, `grievance`): AES-256-GCM per value, bound to table/column/row, key from the root via the Core key, mode recorded in the database with an HMAC, migration with per-table transactions and a content measure, encrypted backup and restore (backup → destroy → restore → open → identical content, on a real engine). See `docs/STORAGE_PROTECTION.md` |
| F8 no backup pipeline for the SQL memory | Partly: `protect backup/restore` for these six tables (encrypted file). No general backup of the whole database |
| P1c-3 lock closes the data | For a sealed database: yes (the Core forgets the storage key on lock; the legacy PET holds it while it runs). Not for tables outside the six |
| Other tables, Redis, legacy stores (F10) | **Unchanged**: plaintext at rest |
