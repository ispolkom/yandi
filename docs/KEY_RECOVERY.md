# Key recovery (P1c-1) — the root key, the device, and the recovery password

What is **implemented**, and how the owner uses it. Design and findings: `docs/KEY_CHAIN_AUDIT.md`. Code: `node/key_root/` (library and the
`yandi-keys` tool) and its use in `node/src/web/auth.rs`, `node/src/core/identity.rs`, `node/src/main.rs`.

**P1c-1 protects the node's identity and root key and makes them recoverable. The SQL / personal memory is NOT bound to the root yet (P1c-2), and nothing of it is encrypted.**

## The goal, in two lines

```text
loss of the device  !=  loss of the YANDI identity            a crypto error  !=  "create a new person"
```

## What changed

```text
                 device key (random, this device) ─┐   machine id = a public label, only "which machine", never a key
                                                    ├── unwrap ──▶ ROOT ──HKDF("yandi/identity/v1")──▶ identity file (format v3)
   recovery password ──Argon2id (64 MiB, 3 passes)──┘                  ├─HKDF("yandi/core/v1")────────▶ the key the Node gives the Core (unchanged)
                                                                       └─ (chat store key: derived from the same root as before, unchanged)
```

* **ROOT is the node's existing master key**, not a new one: so the chat store and the Node→Core key keep working with no re-encryption. Until now that key was
  a random value that nothing could re-derive (`docs/KEY_CHAIN_AUDIT.md` F1); now it is wrapped twice and recoverable.
* **`auth.json` version 2** holds the two wrappers (the login hash is carried over). **Identity format 3** encrypts the private keys under the root. Both formats
  say their version in the file, so the code never tries passwords to find out what a file is.
* **The old formats still work** (auth v1 machine-wrapped, identity v2) until you migrate. The node then prints a reminder.

## The rules the code follows (and tests prove)

1. An identity that exists and cannot be opened is an **error**, never a new identity. Wrong or newly set `YANDI_KEY_PASSWORD`, a changed machine id, a damaged,
   truncated, unknown-format or unsafe-permission file: the node stops with a category and **not one byte on disk changes**. Only a truly absent identity may be created,
   and only if no other identity-like file exists in `~/.yandi_keys`, and never over an existing file.
2. The identity, `auth.json` and the device key are replaced only by atomic writes: written beside the old file, read back and checked, then renamed. Originals are
   backed up first (`*.legacy-000`, `*.before-recovery-000`, `device.key.replaced-000`); this code never deletes a backup.
3. A wrong recovery password changes nothing. Recovery checks that the recovered root really opens the **owner's identity** before it writes anything.
4. Secrets are never printed or logged (tests scan the tool's output). Failure categories shown outside: `identity_not_initialized`, `identity_locked`,
   `identity_recovery_required`, `identity_recovery_failed`, `identity_corrupt`, `identity_format_unsupported`, `identity_unreadable`.
5. `~/.yandi_keys` is tightened to 0700 (a directory that is a link, another user's, or not a directory is refused, not chmod-ed); key files are 0600.

## What to do (the owner, once)

```bash
# 0. a copy first (as before): cp -a ~/.yandi_keys ~/yandi_keys_backup_$(date +%F-%H%M%S) && chmod 700 ~/yandi_keys_backup_*
# 1. build the tool
cd ~/yandi/node && cargo build --release -p yandi-key-root        # -> node/target/release/yandi-keys
# 2. look (changes nothing except tightening the directory to 0700)
~/yandi/node/target/release/yandi-keys status
# 3. migrate (asks twice for a NEW recovery password; stop the node first)
~/yandi/node/target/release/yandi-keys migrate
# 4. start the node and check the log: "[auth] Master key loaded (device key)" and "[identity] Identity loaded (node_id: …)" with the SAME node id
```

Choose the recovery password like a passphrase (several words, at least 12 characters). It is **not** the web login password and is asked for only when the
device cannot open the key. Whoever holds a copy of `auth.json` can guess it offline (Argon2id makes each guess cost 64 MiB and about half a second), so it must
be long. It cannot be changed yet, and it cannot be recovered: **write it down**.

After migration the old files remain as `auth.json.legacy-000` and `node_identity_9000.json.legacy-000`. They are protected only by the public machine id: keep
them until you have restarted and checked, then move them to a safe offline place or delete them yourself. The tool never deletes them.

**What to back up:** `auth.json` and `node_identity_9000.json` (both encrypted) and your recovery password. The device key does **not** need a backup — that is the point.

## New machine, reinstall, or a lost device key

```bash
# copy auth.json and node_identity_9000.json into ~/.yandi_keys on the new machine (do NOT copy device.key)
~/yandi/node/target/release/yandi-keys recover        # asks for the recovery password
# the same node id comes back; a new device key is made for this machine; the next start needs no password
```

A directory copied **with** `device.key` to another machine does not open by itself either (the wrapper is bound to the machine id): recovery is required, and it keeps the
old device key as `device.key.replaced-000`.

## The device key: how strong is it? (honest)

Today the only backend is a private file (`device.key`, 0600, in the 0700 key directory). It keeps the key from other users and from copies of the encrypted
files, but **not from anyone who can read your whole home directory** (a program running as you, a copy of the whole directory). An operating-system key store
or a hardware module would be stronger; none is available in this offline build. The `DeviceKeyProvider` interface lets one be added without changing any file format.
`/etc/machine-id` is public and is used only as a label; it is not a secret and adds no entropy.

## `YANDI_KEY_PASSWORD`

Legacy only: it is the passphrase of a legacy (v2) identity when you set it. It no longer can destroy anything: on an existing identity that was not encrypted with it, the
node now stops and changes nothing. In the new format it has no role. Do not set it on a node that has not been migrated.

## Limits

* A fresh install is still created in the legacy format (the identity is made before the first-run web setup); run `yandi-keys migrate` after the first setup. Reworking the first-run flow is later work.
* The web "rebind" page no longer changes keys: it answers that recovery is done on the command line.
* Linux and macOS (Unix). On Windows the node keeps its old code path.
* The SQL / personal memory, Redis and the Core's own storage are **not** encrypted or bound to the root (P1c-2).
