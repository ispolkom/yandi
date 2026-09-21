"""
agent/db_sql_field_protection_regression_test.py — P1c-2: the person's own words are sealed before they reach the database.

Over an in-memory fake SQL that keeps EXACTLY what the repositories send (so "what the database holds" can be inspected), with the real
field_protection module and the real repositories. It proves what the storage layer is meant to guarantee:

    THE DATABASE NEVER HOLDS THE WORDS WHEN PROTECTION IS ON.   NO KEY, NO WRITE, NO READ.   NOTHING FALLS BACK TO PLAINTEXT.
    A SEALED VALUE BELONGS TO ITS ROW AND ITS COLUMN.           A WRONG KEY IS A REFUSAL, NEVER GARBAGE.
    A PLAINTEXT VALUE WHERE A SEALED ONE IS REQUIRED IS REFUSED.   A FORGED MODE RECORD IS REFUSED.   WHAT THE PERSON TYPES CANNOT LOOK SEALED.

Every safety net is then removed on purpose (mutants) and the checks must notice. Real-engine proofs (the migration, triggers, backup and
restore) are in agent/db_sql_field_protection_sql_integration_test.py.

Run: python -m agent.db_sql_field_protection_regression_test
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
CORE_KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
WORDS = "Я боюсь, что сдам работу поздно — «секретное слово»: мандарин-7"
REPLY = "Понимаю. Расскажи подробнее, что тебя беспокоит."
USER = "owner"
NOW = datetime(2026, 9, 22, 12, 0, 0)


def run_checks() -> list:
    """Every check, against whatever the modules currently do. Returns the names of the checks that failed."""
    import agent.db.sql.field_protection as fp
    import agent.db.sql.repositories as repo
    from agent.relationship_apology_matching_regression_test import FakeConnection

    failures: list = []

    def check(name: str, condition: bool) -> None:
        if not condition:
            failures.append(name)

    def raises(exc_type, fn) -> bool:
        try:
            fn()
        except exc_type:
            return True
        except Exception:                                # noqa: BLE001 — the wrong kind of failure is not the right refusal
            return False
        return False

    def record(mode: str, key: bytes = CORE_KEY) -> dict:
        proof_key = fp._hkdf(key, fp.PROOF_INFO)
        return {"mode": mode, "nonce": "ab" * 16, "proof": fp.mode_proof(proof_key, "ab" * 16, mode)}

    def db(mode: str, key: bytes = CORE_KEY) -> "FakeConnection":
        conn = FakeConnection()
        conn.protection_record = None if mode == "off" else record(mode, key)
        fp.forget_mode()
        return conn

    def stored_text(conn) -> str:
        """Everything the fake database was handed, as one string."""
        parts = []
        for rows in (conn.interaction_turns, conn.personal_facts, conn.personal_fact_events, conn.commitment_events):
            parts += [repr(r) for r in rows]
        parts += [repr(v) for v in conn.commitments.values()] + [repr(v) for v in conn.grievances.values()]
        return "\n".join(parts)

    def write_everything(conn) -> None:
        repo.record_interaction_turn(conn, USER, "t1", "client", WORDS, REPLY, created_at=NOW)
        repo.insert_personal_fact(conn, "f1", USER, "family", "боится сдать работу поздно", "affirmed", "current",
                                  WORDS[:30], 0, 30, "t1", NOW)
        repo.insert_personal_fact_event(conn, "f1", USER, "restated", None, WORDS[:30], 0, 30, "t1", NOW)
        repo.record_commitment(conn, "c1", USER, "general", "принесу отчёт " + WORDS, WORDS[:40], None, NOW, "t1")
        repo.record_commitment_event(conn, "c1", USER, "fulfillment_claimed", "user_report", WORDS[:40], NOW, "t1")
        repo.record_grievance(conn, "g1", USER, "insult", "Ты сказал(а): " + WORDS, 0.5, {"note": WORDS}, NOW)

    # ── the primitives ────────────────────────────────────────────────────────────────────────────────────────────
    fp.clear_key()
    check("P1 a key must be 32 bytes", raises(ValueError, lambda: fp.install_key(b"short")))
    fp.install_key(CORE_KEY)
    key_row = {"user_id": USER, "source_turn_id": "t1"}

    off = db("off")
    check("P2 protection off: a value is stored exactly as before", fp.seal(off, "interaction_turn", "user_text", key_row, WORDS) == WORDS)
    check("P3 protection off: it reads back exactly", fp.open_value(off, "interaction_turn", "user_text", key_row, WORDS) == WORDS)
    check("P4 NULL stays NULL in every mode", fp.seal(off, "interaction_turn", "assistant_text", key_row, None) is None
          and fp.open_value(off, "interaction_turn", "assistant_text", key_row, None) is None)
    for typed in ("yp1:not-really-sealed", "yp0:hello", "yp1:", "yp0:yp1:x"):
        stored = fp.seal(off, "interaction_turn", "user_text", key_row, typed)
        check(f"P5 what the person types ({typed!r}) cannot look sealed", not fp.is_sealed(stored)
              and fp.open_value(off, "interaction_turn", "user_text", key_row, stored) == typed)

    on = db("on")
    sealed = fp.seal(on, "interaction_turn", "user_text", key_row, WORDS)
    check("P6 protection on: the stored value is sealed and holds none of the words",
          fp.is_sealed(sealed) and "мандарин" not in sealed and "секретное" not in sealed and WORDS not in sealed)
    check("P7 and it opens back to exactly the words", fp.open_value(on, "interaction_turn", "user_text", key_row, sealed) == WORDS)
    check("P8 sealing twice gives two different values (fresh nonce)", fp.seal(on, "interaction_turn", "user_text", key_row, WORDS) != sealed)
    for text in ("", "x", "я" * 20000, "🙂" * 500, "a\x00b"):
        s2 = fp.seal(on, "interaction_turn", "user_text", key_row, text)
        check(f"P9 round trip of a {len(text)}-character value", fp.open_value(on, "interaction_turn", "user_text", key_row, s2) == text)
    check("P10 a sealed value of one row does not open as another row",
          raises(fp.StorageTampered, lambda: fp.open_value(on, "interaction_turn", "user_text", {"user_id": USER, "source_turn_id": "t2"}, sealed)))
    check("P11 …nor as another person's row",
          raises(fp.StorageTampered, lambda: fp.open_value(on, "interaction_turn", "user_text", {"user_id": "other", "source_turn_id": "t1"}, sealed)))
    check("P12 …nor as another column of the same row",
          raises(fp.StorageTampered, lambda: fp.open_value(on, "interaction_turn", "assistant_text", key_row, sealed)))
    check("P13 an altered value is refused, not opened", raises(
        fp.StorageTampered, lambda: fp.open_value(on, "interaction_turn", "user_text", key_row, sealed[:-2] + ("AA" if sealed[-2:] != "AA" else "BB"))))
    check("P14 a truncated value is refused", raises(fp.StorageTampered, lambda: fp.open_value(on, "interaction_turn", "user_text", key_row, sealed[:12])))
    check("P15 protection on: a plaintext value is refused (somebody wrote around the application)",
          raises(fp.StorageTampered, lambda: fp.open_value(on, "interaction_turn", "user_text", key_row, "plain words")))
    check("P16 a column outside the protected list is refused by name", raises(fp.StorageProtectionError, lambda: fp.seal(on, "interaction_turn", "model", key_row, "x")))

    fp.clear_key()
    fp.forget_mode()
    check("P17 no key, protection on: a write is refused, nothing falls back to plaintext",
          raises(fp.StorageLocked, lambda: fp.seal(on, "interaction_turn", "user_text", key_row, WORDS)))
    check("P18 no key: a sealed value is not readable", raises(fp.StorageLocked, lambda: fp.open_value(on, "interaction_turn", "user_text", key_row, sealed)))
    check("P19 no key, protection off: a sealed value (from an earlier run) is still refused, never shown as text",
          raises(fp.StorageLocked, lambda: fp.open_value(off, "interaction_turn", "user_text", key_row, sealed)))
    fp.install_key(OTHER_KEY)
    check("P20 a wrong key is a refusal, never garbage", raises(fp.StorageTampered, lambda: fp.open_value(off, "interaction_turn", "user_text", key_row, sealed)))
    fp.forget_mode()
    check("P21 a mode record made with another key does not verify", raises(fp.StorageTampered, lambda: fp.read_mode(on)))
    fp.install_key(CORE_KEY)

    forged = db("on")
    forged.protection_record = dict(record("on"), proof=b"\x00" * 32)
    check("P22 a forged mode record is refused by a process that holds the key", raises(fp.StorageTampered, lambda: fp.read_mode(forged)))
    forged.protection_record = dict(record("on"), proof=None)
    check("P23 …also one with no proof at all (an 'off' written by someone without the key cannot switch protection off)",
          raises(fp.StorageTampered, lambda: fp.read_mode(forged)))
    forged.protection_record = {"mode": "sideways", "nonce": "x", "proof": b""}
    check("P24 an unknown mode is refused", raises(fp.StorageTampered, lambda: fp.read_mode(forged)))
    check("P25 a database without the mode table is 'off'", fp.read_mode(_MissingTable()) == "off")
    check("P26 a database that fails for another reason is NOT treated as 'off'", raises(RuntimeError, lambda: fp.read_mode(_BrokenDb())))

    # the mode is read at most once per TTL
    counting = db("on")
    reads = {"n": 0}
    real_read = fp.read_mode

    def counted(conn):
        reads["n"] += 1
        return real_read(conn)
    with patch.object(fp, "read_mode", counted):
        fp.forget_mode()
        for _ in range(5):
            fp.current_mode(counting)
        check("P27 the mode is cached (one read for five uses)", reads["n"] == 1)
        fp.forget_mode()
        fp.current_mode(counting)
        check("P28 …and forgetting it makes the next use look again", reads["n"] == 2)

    # a change of mode reaches a running process within the TTL (nobody has to restart it)
    ttl = db("off")
    fp.forget_mode()
    check("P29 the mode is 'off' at first", fp.current_mode(ttl) == "off")
    ttl.protection_record = record("on")
    check("P30 …a change is not seen at once (the mode is cached)", fp.current_mode(ttl) == "off")
    real_monotonic = fp.time.monotonic
    with patch.object(fp.time, "monotonic", lambda: real_monotonic() + 11):     # the documented promise: a switch reaches a running process within 10 seconds
        check("P31 …but is seen within 11 seconds (nobody has to restart the process)", fp.current_mode(ttl) == "on")
    fp.forget_mode()

    # ── the key tool (the legacy PET obtains its key from it) ────────────────────────────────────────────────────
    import base64
    import os
    import stat
    import tempfile
    tooldir = tempfile.mkdtemp(prefix="yandi-fp-tool-")

    def tool(name: str, body: str) -> str:
        path = os.path.join(tooldir, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, stat.S_IRWXU)
        return path
    good = tool("good", f"echo {base64.b64encode(CORE_KEY).decode()}")
    fp.clear_key()
    fp.install_key_from_tool(good)
    check("K1 the key from the tool is installed (and opens what the same key sealed)", fp.has_key()
          and fp.open_value(db("off"), "interaction_turn", "user_text", key_row, sealed) == WORDS)
    for label, body in (("refuses", "echo 'yandi-keys: recovery_required' >&2; exit 2"), ("prints garbage", "echo not-base64!!"),
                        ("prints a short key", f"echo {base64.b64encode(b'x' * 16).decode()}"), ("prints nothing", "exit 0")):
        fp.clear_key()
        check(f"K2 a tool that {label} is a refusal and no key is installed",
              raises(fp.StorageLocked, lambda: fp.install_key_from_tool(tool("t_" + label.replace(" ", "_"), body))) and not fp.has_key())
    fp.clear_key()
    check("K3 a tool that does not exist is a refusal", raises(fp.StorageLocked, lambda: fp.install_key_from_tool(os.path.join(tooldir, "missing"))) and not fp.has_key())
    fp.install_key(CORE_KEY)

    # ── through the repositories ──────────────────────────────────────────────────────────────────────────────────
    conn = db("on")
    write_everything(conn)
    held = stored_text(conn)
    check("R1 protection on: not one of the person's words reaches the database, in any of the six tables",
          "мандарин" not in held and "секретное" not in held and "Расскажи" not in held and "боится" not in held and "отчёт" not in held)
    check("R2 …what does reach it is sealed", held.count("yp1:") >= 10)
    check("R3 the structure stays readable (ids, kinds, statuses)", "t1" in held and "fulfillment_claimed" in held and "insult" in held)

    turn = repo.list_recent_interaction_turns(conn, USER)[0]
    check("R4 the turn reads back exactly (user and assistant text)", turn["user_text"] == WORDS and turn["assistant_text"] == REPLY)
    check("R5 the immutable user text of a turn reads back", repo.get_interaction_turn_text(conn, USER, "t1") == WORDS)
    fact = repo.list_personal_facts(conn, USER)[0]
    check("R6 the fact reads back exactly", fact["statement"] == "боится сдать работу поздно" and fact["evidence"] == WORDS[:30])
    commitment = repo.get_commitment(conn, "c1")
    check("R7 the promise reads back exactly", commitment["text"] == ("принесу отчёт " + WORDS)[:500] and commitment["evidence"] == WORDS[:40])
    check("R8 …also through the list", repo.list_commitments(conn, USER)[0]["text"] == commitment["text"])
    check("R9 the promise's outcome reads back exactly", repo.list_commitment_events(conn, USER)[0]["evidence"] == WORDS[:40])
    grievance = repo.get_grievance(conn, "g1")
    check("R10 the grievance reads back exactly, its context as a real object", grievance["description"] == "Ты сказал(а): " + WORDS and grievance["context"] == {"note": WORDS})
    check("R11 …also through the active list", repo.list_active_grievances(conn, USER)[0]["description"] == grievance["description"])
    similar = repo.find_similar_open_grievance(conn, USER, ("Ты сказал(а): " + WORDS)[:20] + " (в другой раз)")
    check("R12 a repeated grievance is still recognised by its first characters, on the opened text", similar is not None and similar["id"] == "g1")
    check("R13 …and a different one is not", repo.find_similar_open_grievance(conn, USER, "совсем другое") is None)

    conn_off = db("off")
    write_everything(conn_off)
    plain = stored_text(conn_off)
    check("R14 protection off: exactly the behaviour before P1c-2 (plaintext, no marker)", "yp1:" not in plain and "мандарин" in plain)
    check("R15 …and it reads back", repo.list_personal_facts(conn_off, USER)[0]["evidence"] == WORDS[:30])

    mixed = db("migrating")
    conn_off_rows = conn_off.interaction_turns
    mixed.interaction_turns = list(conn_off_rows)
    check("R16 while migrating, plaintext rows are still read (and new ones are sealed)",
          repo.list_recent_interaction_turns(mixed, USER)[0]["user_text"] == WORDS
          and repo.record_interaction_turn(mixed, USER, "t9", "client", "новое", None, created_at=NOW)
          and fp.is_sealed(mixed.interaction_turns[-1]["user_text"]))

    fp.clear_key()
    fp.forget_mode()
    keyless = db("on")
    check("R17 protection on, process holds no key: a turn is NOT written", raises(fp.StorageLocked, lambda: repo.record_interaction_turn(
        keyless, USER, "t1", "client", WORDS, REPLY, created_at=NOW)) and keyless.interaction_turns == [])
    check("R18 …a fact is NOT written", raises(fp.StorageLocked, lambda: repo.record_commitment(keyless, "c9", USER, "general", "x", "y", None, NOW, "t1")) and not keyless.commitments)
    check("R19 …a grievance is NOT written", raises(fp.StorageLocked, lambda: repo.record_grievance(keyless, "g9", USER, "insult", "x", 0.1, None, NOW)) and not keyless.grievances)
    keyless.interaction_turns = list(conn.interaction_turns)
    check("R20 …and sealed history is not readable without the key", raises(fp.StorageLocked, lambda: repo.list_recent_interaction_turns(keyless, USER)))
    fp.install_key(CORE_KEY)

    swapped = db("on")
    repo.record_interaction_turn(swapped, USER, "a", "client", "первое сообщение", None, created_at=NOW)
    repo.record_interaction_turn(swapped, USER, "b", "client", "второе сообщение", None, created_at=NOW)
    a, b = swapped.interaction_turns
    a["user_text"], b["user_text"] = b["user_text"], a["user_text"]
    check("R21 sealed values swapped between two rows are refused, not shown under the wrong turn",
          raises(fp.StorageTampered, lambda: repo.list_recent_interaction_turns(swapped, USER)))
    injected = db("on")
    repo.record_interaction_turn(injected, USER, "a", "client", "первое сообщение", None, created_at=NOW)
    injected.interaction_turns[0]["user_text"] = "подложенный текст"
    check("R22 a plaintext row slipped into a protected ledger is refused", raises(fp.StorageTampered, lambda: repo.list_recent_interaction_turns(injected, USER)))

    # ── the storage layer is the ONLY way to those tables ─────────────────────────────────────────────────────────
    repo_source = (ROOT / "agent" / "db" / "sql" / "repositories.py").read_text(encoding="utf-8")
    functions = re.split(r"\n(?=def )", repo_source)
    writers = [f for f in functions if re.search(r"INSERT (?:IGNORE )?INTO (?:%s)\b" % "|".join(fp.PROTECTED), f)]
    check("S1 every repository function that INSERTs into a protected table seals (and there are 6 such writers)",
          len(writers) >= 6 and all("fp.seal(" in f for f in writers))
    readers = [f for f in functions if re.search(r"(?:FROM|JOIN) (?:%s)\b" % "|".join(fp.PROTECTED), f) and "SELECT" in f]
    readers = [f for f in readers if not re.search(r"SELECT (?:COUNT|MODE)", f) and "def count_" not in f and "def list_personal_fact_events" not in f]
    check("S2 every repository function that SELECTs from a protected table opens what it reads", readers and all("fp.open_" in f or "fp.open_row" in f for f in readers))
    allowed = {"repositories.py", "schema.py", "protect.py", "field_protection.py", "migrate.py", "bootstrap.py", "live_bootstrap.py",
               "security_grants.py", "security_triggers.py", "security_selfcheck.py"}
    outside = []
    pattern = re.compile(r"(?:FROM|INTO|UPDATE|JOIN)\s+(?:%s)\b" % "|".join(fp.PROTECTED))
    for path in list(ROOT.glob("agent/**/*.py")) + list(ROOT.glob("pet/**/*.py")) + list(ROOT.glob("llm_gateway/**/*.py")):
        if path.name in allowed or path.name.endswith("_test.py") or "test_support" in path.name:
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            outside.append(str(path.relative_to(ROOT)))
    check("S3 no module outside the repository layer runs SQL against a protected table: " + ", ".join(outside), not outside)
    check("S4 every protected column is a column of its table's DDL", _columns_exist(fp))
    return failures


def _columns_exist(fp) -> bool:
    import agent.db.sql.schema as schema
    ddl = dict(schema.ALL_TABLES_IN_ORDER)
    return all(re.search(rf"\b{column}\b", ddl[table]) for table, columns in fp.PROTECTED.items() for column in columns)


class _MissingTable:
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        raise RuntimeError(1146, "Table 'yandi_epistemic.storage_protection_event' doesn't exist")


class _BrokenDb(_MissingTable):
    def execute(self, sql, params=None):
        raise RuntimeError("connection lost")


def mutants() -> list:
    """(label, context manager applying a deliberate defect). Each must make at least one check fail."""
    import contextlib
    import agent.db.sql.field_protection as fp
    import agent.db.sql.repositories as repo

    real_seal, real_open_with, real_entity, real_open_value, real_read_mode = fp.seal, fp.open_with, fp.entity_id, fp.open_value, fp.read_mode

    @contextlib.contextmanager
    def swapped(module, name, replacement):
        original = getattr(module, name)
        setattr(module, name, replacement)
        try:
            yield
        finally:
            setattr(module, name, original)

    def seal_plain(conn, table, column, row_key, value):                 # M1: nothing is sealed
        return value

    def seal_when_keyed(conn, table, column, row_key, value):            # M2: no key -> quietly write plaintext
        return real_seal(conn, table, column, row_key, value) if fp.has_key() else value

    def entity_constant(table, values):                                   # M3: the value is not bound to its row
        return "constant"

    def open_lenient(conn, table, column, row_key, stored):              # M4: plaintext accepted while protection is on
        if stored is not None and not fp.is_sealed(stored):
            return stored
        return real_open_value(conn, table, column, row_key, stored)

    def read_mode_unverified(conn):                                       # M5: the mode record is trusted without checking its proof
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT mode, nonce, proof FROM storage_protection_event ORDER BY event_id DESC LIMIT 1")
                row = cur.fetchone()
        except RuntimeError as exc:
            if exc.args and exc.args[0] == 1146:
                return "off"
            raise
        return row["mode"] if row else "off"

    def open_keyless_garbage(conn, table, column, row_key, stored):      # M6: no key -> return the sealed text as if it were the words
        if fp.is_sealed(stored) and not fp.has_key():
            return stored
        return real_open_value(conn, table, column, row_key, stored)

    def no_escape_seal(conn, table, column, row_key, value):             # M7: a typed "yp1:" is stored as it is typed
        if fp.current_mode(conn) == "off":
            return value
        return real_seal(conn, table, column, row_key, value)

    @contextlib.contextmanager
    def one_writer_forgets():                                             # M8: one writer (the promise's outcome) stores plaintext again
        original = repo.record_commitment_event
        source = original.__code__

        def forgetful(conn, commitment_id, user_id, event_type, source, evidence=None, created_at=None, source_turn_id=None,
                      span_start=None, span_end=None, require_provenance=False):
            with conn.cursor() as cur:
                cur.execute("INSERT IGNORE INTO commitment_event (commitment_id, user_id, event_type, source, evidence, created_at, "
                            "source_turn_id, span_start, span_end) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (commitment_id, user_id, event_type, source, evidence, created_at, source_turn_id, span_start, span_end))
                return cur.rowcount == 1
        repo.record_commitment_event = forgetful
        try:
            yield
        finally:
            repo.record_commitment_event = original

    @contextlib.contextmanager
    def similar_on_sealed():                                              # M9: the grievance match compares the sealed value's first characters
        original = repo.find_similar_open_grievance

        def broken(conn, user_id, description):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM grievance WHERE user_id=%s AND status != 'forgiven' ORDER BY created_at DESC", (user_id,))
                rows = cur.fetchall()
            for row in rows:
                if row["description"][:20] == description[:20]:
                    return row
            return None
        repo.find_similar_open_grievance = broken
        try:
            yield
        finally:
            repo.find_similar_open_grievance = original

    real_from_tool = fp.install_key_from_tool

    def tool_failure_swallowed(tool_path, timeout=30.0):               # M10: a key tool that fails is ignored (the server would run keyless without saying)
        try:
            real_from_tool(tool_path, timeout)
        except fp.StorageLocked:
            return

    return [
        ("M1 nothing is sealed", swapped(fp, "seal", seal_plain)),
        ("M2 a missing key quietly writes plaintext", swapped(fp, "seal", seal_when_keyed)),
        ("M3 a sealed value is not bound to its row", swapped(fp, "entity_id", entity_constant)),
        ("M4 plaintext is accepted while protection is on", swapped(fp, "open_value", open_lenient)),
        ("M5 the mode record is trusted without its proof", swapped(fp, "read_mode", read_mode_unverified)),
        ("M6 without a key a sealed value is handed out as text", swapped(fp, "open_value", open_keyless_garbage)),
        ("M7 what the person types is stored as typed (can look sealed)", swapped(fp, "seal", no_escape_seal)),
        ("M8 one writer forgets to seal", one_writer_forgets()),
        ("M9 the grievance match compares the sealed value", similar_on_sealed()),
        ("M10 a failing key tool is ignored", swapped(fp, "install_key_from_tool", tool_failure_swallowed)),
    ]


def main() -> int:
    print("clean code:")
    failures = run_checks()
    for name in failures:
        print(f"[FAIL] {name}")
    if not failures:
        print("[OK] every check passes")
    bad = bool(failures)
    for label, applied in mutants():
        with applied:
            try:
                caught = run_checks()
            except Exception as exc:                     # a defect that breaks the run itself is noticed too
                caught = [f"the run itself failed: {type(exc).__name__}"]
        print(f"[{'OK' if caught else 'FAIL'}] {label} -> {'caught by ' + str(len(caught)) + ' check(s), e.g. ' + caught[0][:70] if caught else 'NOT CAUGHT'}")
        bad = bad or not caught
    print("RESULT:", "all checks passed" if not bad else "FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
