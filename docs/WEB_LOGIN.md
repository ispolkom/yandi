# One account, two web pages — the node's page and the assistant's page

What is **implemented and tested**. Code: `node/key_root/src/login.rs` (the one implementation), `yandi-keys login-check | login-reset | setup`
(`node/key_root/src/bin/yandi-keys.rs`), `pet/web_login.py` (the assistant's door), `node/src/web/auth.rs` (the node's door, now delegating to `login.rs`).
Proof: `node/key_root/tests/login.rs`, `pet/pet_web_login_regression_test.py`, mutants L1–L7 (`scripts/key_root_mutants.py`) and M1–M11 (in the Python test).

## What the owner gets

* **One password, one recovery phrase, two pages.** The account is `~/.yandi_keys/auth.json`: the **login password** (Argon2id hash) and the **master password**
  (your recovery phrase, chosen and typed twice by you at first setup). Both web pages use it.
* **Each page asks at the door.** Opening `http://127.0.0.1:9010` (the assistant) now shows the same login form as the node's page (the same file:
  `node/src/web/ui/login.html`, so the look and the behaviour cannot drift). Wrong password → refusal; forgot it → "Забыли пароль? Восстановить по мастер-паролю".
* **Either page can be used alone or both together.** Each has its own session (its own cookie: `yandi_pet_session` here, `yandi_session` on the node — browsers do not
  separate cookies by port, so the names must differ). Logging in to one does not log you in to the other, by design.
* **First run in either place.** If no password exists yet, opening the assistant's page sends the browser to the same setup form as the node's
  (login password twice, master password twice). Whichever page is used first creates the account; the other one then accepts the same password. If the node is
  started later it finds the keys and creates its identity from them.

## How it is built (one implementation, not two)

The password is **never checked in Python**. The assistant's page runs `yandi-keys` (the same Rust code the node uses): `login-check` (password on standard input →
exit 0 / 1), `login-reset` (recovery phrase + new login password → the login hash is replaced, atomically, the previous file kept as
`auth.json.before-login-reset-…`, the keys untouched), `setup` (four typed lines → the keys are created; refuses if keys exist). Secrets travel only on standard input,
never on a command line, and are never printed. If the tool is not built, browsers are **kept out** (fail closed) and the page says what to build.

## Who is checked, and who is not (be clear about this)

* **Checked: any request that comes from a browser** — recognised by the `Sec-Fetch-*` headers that a web page cannot forge. Without a valid session:
  pages go to the login page, API calls get 401, WebSockets are closed.
* **Not checked: programs on this computer** (the agent, the council scripts, `curl`): they send no browser headers and pass as before. A password cannot help them
  yet, because they have no way to type one; they will move behind the node (P3/P4), or get a local service token. **Until then another local user could still
  call the assistant's API with `curl`**; the password protects the *page* (another user's browser, a forwarded port, a shared screen), not the local API from a local program.
* **The Firefox extension** keeps only its own allow-listed paths (unchanged, `pet/local_guard.py`); it needs no session for them.
* The node's page is unchanged in behaviour (its 196 tests pass on the shared code).

## What you have to do once

```bash
cd ~/yandi/node
cargo build --release -p yandi-key-root        # yandi-keys with login-check / login-reset / setup   (required)
cargo build --release                          # the node, now using the shared login code (behaviour unchanged)
cd ~/yandi && ./start.sh                       # the assistant; open http://127.0.0.1:9010 — it asks for the password
```

You already have an account (made in the node's setup): use **the same** login password. The assistant's page prints `[login] вход по паролю: включён` at start.

## Limits

* Sessions live in the assistant's memory: restarting it logs everybody out. "Remember me" lasts 30 days, otherwise 12 hours.
* Attempts are slowed after three mistakes (2 s, 4 s … up to 5 minutes), the same rule as the node; the slow-down is per process, not per address.
* Unix only (the shared code and the tool are Unix-only, like the rest of the key chain).
* The recovery phrase resets the *login* password; it cannot be recovered itself. Lose both and there is no way back except `yandi-keys recover` with the phrase (see `docs/KEY_RECOVERY.md`).
