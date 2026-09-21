# Sealed personal memory (P1c-2) — what is protected, how to turn it on, how to turn it off

What is **implemented and tested**, and what it does not do. Code: `agent/db/sql/field_protection.py` (the layer), `agent/db/sql/protect.py` (the
tool), the wiring in `agent/db/sql/repositories.py`, the Core hook in `pet/core_lifecycle.py`, `yandi-keys core-key` (`node/key_root`).
Proof: `agent/db_sql_field_protection_regression_test.py`, `agent/db_sql_field_protection_sql_integration_test.py` (a real, private MySQL),
`scripts/protect_mutants.py`.

## Status in one paragraph

**The mechanism is built and proven; it is OFF until you run `seal` on your database. The owner sealed his live database on 2026-09-22 (7 turns, 7 grievances; mode on, no plaintext left).** Nothing changes for a database that has not been
sealed: the application writes and reads plaintext exactly as before. Once `seal` has been run, the words of the personal ledger are
stored as AES-256-GCM ciphertext and can be opened only by a process that holds the key derived from the node's root key.
**It protects data at rest** (a stolen disk, a leaked database dump or backup). It does **not** protect against something that already
runs as you on this machine (it can ask the key tool for the key, as the legacy PET does), nor against anyone who can read the process's memory.

## What is sealed

| Table | Sealed columns | Left readable |
|---|---|---|
| `interaction_turn` | `user_text`, `assistant_text` | ids, the turn id, model, adapter, times |
| `personal_fact` | `statement`, `evidence` | ids, class, polarity, temporality, spans |
| `personal_fact_event` | `evidence` | ids, event type, spans |
| `commitment` | `text`, `evidence` | ids, kind, due date |
| `commitment_event` | `evidence` | ids, event type, source |
| `grievance` | `description`, `context` | ids, event type, severity, status, times |

These are the person's own words and what was derived from them. **Not sealed (still plaintext at rest):** the other personal tables
(episodes, self events, biography events, experiences, context instances, decision journal, disagreements, knowledge archive, …), the epistemic
memory, Redis, the legacy SQLite/JSON/JSONL stores. Whether a column is `NULL` (for example "no reply was produced") is also visible, and so
are row counts and times. Extending the list is one line in `PROTECTED`/`ENTITY_KEYS` plus the repository functions of that table.

## How it works

* Each value is sealed as `yp1:` + base64(version byte ‖ nonce ‖ AES-256-GCM ciphertext), with the **table, the column and the row's natural key
  as associated data**: a sealed value moved to another row, another person or another column does not open (it is refused, not shown).
* The key is `HKDF(core key, "yandi/storage/personal/v1")`, the core key being what the node already gives the Core
  (`HKDF(root, "yandi/core/v1")`). Nothing new to back up: **your master password / recovery secret is what gets the data back** (through the root).
* **The database says whether protection is on; the key is the capability.** The newest row of `storage_protection_event` is the mode:
  * **off** (no row): plaintext, as before (a sealed value is refused without the key, never shown as text);
  * **migrating**: writes are sealed, both forms are read (the tool is in the middle of the change; safe to stop and continue);
  * **on**: writes are sealed and **require the key**, reads **require sealed values**. No key → the write is refused and nothing is written (never
    plaintext); a plaintext value found where a sealed one is required is refused (someone wrote around the application).
* The mode record carries an HMAC made with another key derived from the same root: a process that holds the key refuses a forged or foreign record.
* A process reads the mode at most every 10 seconds, so a switch reaches running processes without a restart (they start refusing / sealing within 10 s).
* What a person types cannot pose as a sealed value: while protection is off, a typed `yp1:…` is stored escaped (`yp0:…`) and read back unescaped.
* The Core installs the key when it is **unlocked** and forgets it when **locked** (`pet/core_lifecycle.py`).
* The legacy PET (`./start.sh`, not unlocked by the node) gets the key from the node's key tool when started by `./start.sh` (it uses the key tool automatically when it is built; `YANDI_PROTECTED_STORAGE=0` turns that off):
  it runs `yandi-keys core-key` (the tool opens the root with this machine's device key; it refuses to print into a terminal), holds the key in memory
  and stops loudly if it cannot get it. Without the tool (or with `YANDI_PROTECTED_STORAGE=0`), on a sealed database, the PET starts but says clearly that it can neither read nor write the personal memory.

## Turn it on (the owner)

Everything runs on your machine; nothing here touches anything until you type it. **Stop the node, the Core and the PET first** (the tool proves at the
end that nothing changed under it, but it does not lock the application out).

```bash
cd ~/yandi
git pull                                                     # the code
cd node && cargo build --release -p yandi-key-root           # yandi-keys with `core-key`;  cargo build --release for the node as before
cd ..
KEYS=~/yandi/node/target/release/yandi-keys
# 1. the schema (v19: the wide columns and the mode table). Same rights as for the earlier migrations (DDL):
sudo YANDI_SQL_USER=root YANDI_SQL_PASSWORD='...' ~/venv/bin/python3 -m agent.db.sql.migrate
# 2. look (no key needed): mode, and how many values are plaintext
sudo YANDI_SQL_USER=root YANDI_SQL_PASSWORD='...' ~/venv/bin/python3 -m agent.db.sql.protect status
# 3. an encrypted backup of the ledger BEFORE changing anything (keep the file):
$KEYS core-key | sudo -E YANDI_SQL_USER=root YANDI_SQL_PASSWORD='...' ~/venv/bin/python3 -m agent.db.sql.protect backup --out ~/personal-before-seal.bak
# 4. seal (needs UPDATE and TRIGGER rights: an administrator, like the migration):
$KEYS core-key | sudo -E YANDI_SQL_USER=root YANDI_SQL_PASSWORD='...' ~/venv/bin/python3 -m agent.db.sql.protect seal
# 5. start the PET as usual:  ./start.sh   (it fetches the key by itself)
```

(The exact way you reach the database as an administrator is the same as for `migrate`; if your setup uses the socket login instead of a password, use it the same way.
The key travels only through the pipe.) Step 4 prints what it did per table and ends with "the content is identical to what it was".

To go back: `… protect unseal` (the same checks in the other direction). To restore a backup into an empty ledger: `… protect restore --from FILE`.

## What the tool guarantees (all proven on a real engine)

* Before anything changes it **measures** the plaintext content (a SHA-256 over table, row, column, text). Each table is rewritten in **one transaction that
  also opens what it wrote and compares the measure**; a mismatch rolls that table back. Only when the whole ledger opens back to exactly the measured
  content does the mode become **on**. An interruption leaves **migrating** (the application still works); run `seal` again to finish.
* A row that changes while it is being rewritten is never overwritten (compare-and-set); a write that slips in during the run stops the switch.
* The append-only triggers (`trg_<table>_no_update`) are dropped for the rewrite and **put back in every case**, success or failure. An UPDATE trigger the tool did
  not create stops it **before** any change.
* A value that looks sealed but does not open stops it (it is not silently treated as text). A wrong key is refused before anything changes.
* `backup` writes one file (0600) that is **itself encrypted** (a backup of plaintext rows is not plaintext on disk); `restore` accepts only **empty** tables,
  measures the restored content against the backup's, and rolls back on a difference. Proven: backup → destroy → restore → open → identical content.

## Honest limits

* **Off until you seal.** Until then the personal ledger is plaintext exactly as before.
* Data at rest only. A process that runs as you can obtain the key (the same key tool the legacy PET uses). While the PET runs, the key is in its memory.
* **Legacy PET (`./start.sh`) and the council scripts still reach the database without the node**; with protection **on** they need the key too (the PET gets it
  via `YANDI_PROTECTED_STORAGE=1`); the council scripts do not touch these tables. System-wide "only the node calls the Core" is still not enforced (P3/P4).
* The device key is a private file (see `docs/KEY_RECOVERY.md`): the strength of the sealed data is the strength of that key plus your recovery secret.
* Only the six tables above. The other personal tables and the epistemic memory are not sealed yet.
* The tool needs an administrator connection (UPDATE + TRIGGER rights); the runtime role can neither rewrite nor delete these rows, by design.
* The tool was proven on a private test MySQL, **not** on your live database, which nobody but you can reach. Take the backup (step 3) first.
* The schema step (`migrate`) changes column types (`MODIFY … MEDIUMTEXT`, and `grievance.context` from JSON to text) on tables that hold your data; it is additive
  in content (proven on rows written at v18) but it is a schema change: run it with the node and the PET stopped.
