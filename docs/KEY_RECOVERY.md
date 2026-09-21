# Key recovery (P1c-1) — the root key, the device, and the recovery code

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
   recovery code ─────Argon2id (64 MiB, 3 passes)────┘                  ├─HKDF("yandi/core/v1")────────▶ the key the Node gives the Core (unchanged)
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

## The recovery code (what you write down)

A password chosen by a person and typed with no echo is a poor secret for something that must never be lost: one wrong layout or caps-lock and it "does not fit",
with nothing to say why. So **the system makes the secret**: 120 random bits, shown once as `XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-CCCC` (no look-alike letters; the last group
is a check). You write it down, then **type it back** before anything is saved; case, spaces and dashes do not matter; a typo is reported as a typo ("does not pass its
check"), not as "wrong". It is used exactly like the earlier password (Argon2id over its canonical form), so no file format changed, and an old chosen password still works
where one was set. It is **not** the web login password and cannot be recovered: write it on paper, keep it away from the key files.

## What to do (the owner)

```bash
cd ~/yandi/node && cargo build --release -p yandi-key-root          # -> target/release/yandi-keys   (cargo build alone does NOT build the tool)
./target/release/yandi-keys status                                   # looks; only tightens the directory to 0700

# not migrated yet: makes and shows a recovery code, asks you to type it back, then migrates (stop the node first)
./target/release/yandi-keys migrate

# already migrated with a password you cannot reproduce: replace it by a recovery code (the DEVICE opens the key; node id, identity and chats are untouched)
./target/release/yandi-keys new-recovery-code
```

Then rehearse on a copy (this is what proves the code you wrote down works):

```bash
rm -rf ~/recovery_test && mkdir -p ~/recovery_test/.yandi_keys
cp ~/.yandi_keys/auth.json ~/.yandi_keys/node_identity_9000.json ~/recovery_test/.yandi_keys/ && chmod 700 ~/recovery_test ~/recovery_test/.yandi_keys
./target/release/yandi-keys recover --dir ~/recovery_test/.yandi_keys     # type the code (shown as you type; --hidden hides it)
rm -rf ~/recovery_test
```

`migrate --own-password` still lets you choose a password (typed with no echo); the code is the recommended way.

After migration the old files remain as `auth.json.legacy-000` and `node_identity_9000.json.legacy-000`, protected only by the public machine id: keep them until you have
restarted and checked, then move them somewhere safe or delete them yourself. The tool never deletes them. `new-recovery-code` also keeps the previous `auth.json` as
`auth.json.before-new-code-000`.

**What to back up:** `auth.json` and `node_identity_9000.json` (both encrypted) and the recovery code on paper. The device key does **not** need a backup — that is the point.

## Forgot the web login password

On the login page: **"Забыли пароль? Восстановить по коду"** → the recovery code and a new login password (at least 8 characters). The code is checked against the recovery
wrapper (Argon2id, throttled like the login), the new password is stored in `auth.json` atomically (the previous file is kept as `auth.json.before-login-reset-000`), and every
existing session is ended. The keys, the identity and the chats are not touched. Not available for a key directory that is not migrated yet.

## New machine, reinstall, or a lost device key

```bash
# copy auth.json and node_identity_9000.json into ~/.yandi_keys on the new machine (do NOT copy device.key)
~/yandi/node/target/release/yandi-keys recover        # asks for the recovery code
# the same node id comes back; a new device key is made for this machine; the next start needs no code
```

A directory copied **with** `device.key` to another machine does not open by itself either (the wrapper is bound to the machine id): recovery is required, and it keeps the
old device key as `device.key.replaced-000`. (Recovery from the login page on a machine whose node cannot start is not built: the node stops at start with the instruction to run this command.)

**A clean slate** (a NEW identity, losing the node id and the chat history) is possible but not needed to fix a lost recovery secret: move `~/.yandi_keys` aside
(`mv ~/.yandi_keys ~/.yandi_keys.old`, never delete it), start the node, complete the web setup, then run `yandi-keys migrate` to get a recovery code.

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
* The web "rebind" page no longer changes keys: it answers that recovery is done on the command line. The login page has "Forgot password → recovery code" for the web password.
* Linux and macOS (Unix). On Windows the node keeps its old code path.
* The SQL / personal memory, Redis and the Core's own storage are **not** encrypted or bound to the root (P1c-2).
