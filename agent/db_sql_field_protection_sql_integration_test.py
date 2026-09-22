"""
agent/db_sql_field_protection_sql_integration_test.py — P1c-2 on a REAL SQL engine (a private, temporary MySQL instance): the personal
ledger is sealed, opened, unsealed, backed up and restored, and the guarantees hold where only a real engine can show them.

Run through scripts/test-sql-temp.sh (needs a mysqld binary; the live database is never contacted). Without the environment it prints SKIP.

What only a real engine can prove:
  * the v18 -> v19 upgrade is additive: rows keep their content, the JSON column becomes text with the same JSON, the wide columns are wide;
  * `seal` leaves NOT ONE of the person's words in any column of the six tables (the raw rows are searched), the content opens back to exactly
    what was there (same measure before and after), and the append-only triggers are back in place afterwards;
  * an interrupted `seal` leaves a state the application still reads and that `seal` finishes; an error in the middle of one table rolls that
    table back completely and puts its trigger back; a write that slips in during the run stops the switch;
  * `unseal` returns exactly the original content;
  * without the key nothing is written and nothing is shown; a wrong key is refused before anything changes; a plaintext row written around
    the application is refused;
  * backup -> destroy -> restore -> open -> same content, in a file that itself holds no plaintext.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


WORDS = "Я боюсь, что сдам работу поздно — «секретное слово»: мандарин-7"
MARKERS = ("мандарин", "секретное", "Расскажи", "боится", "отчёт", "Рекс", "заветный")
CORE_KEY = bytes(range(64, 96))
OTHER_KEY = bytes(range(65, 97))
USER = "owner"
NOW = datetime(2026, 9, 22, 12, 0, 0)


def main() -> int:
    socket = os.environ.get("YANDI_TEST_SQL_SOCKET")
    admin, admin_pw = os.environ.get("YANDI_TEST_SQL_ADMIN"), os.environ.get("YANDI_TEST_SQL_ADMIN_PW")
    if not (socket and admin and admin_pw):
        print("SKIP: set YANDI_TEST_SQL_SOCKET / _ADMIN / _ADMIN_PW (see scripts/test-sql-temp.sh)")
        return 0
    from agent.db.sql.connection import LiveDatabaseRefused, assert_connection_allowed, pymysql_target
    try:
        assert_connection_allowed(socket)
    except LiveDatabaseRefused as e:
        print(f"REFUSED: {e}")
        return 2

    import pymysql
    import pymysql.cursors
    from agent.db.sql import field_protection as fp
    from agent.db.sql import migrate, protect, schema, security_grants, security_triggers
    import agent.db.sql.repositories as repo

    def connect(user, password, autocommit=False):
        assert_connection_allowed(socket)
        return pymysql.connect(**pymysql_target(socket), user=user, password=password, database="yandi_epistemic",
                               cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit, charset="utf8mb4")

    root = connect(admin, admin_pw, autocommit=True)
    quiet = lambda *_: None  # noqa: E731

    def scalar(sql, params=()):
        with root.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchone().values())[0]

    def run(sql, params=()):
        with root.cursor() as cur:
            cur.execute(sql, params)

    def raw_text() -> str:
        """Every value of every row of the six tables, as the database holds it."""
        out = []
        with root.cursor() as cur:
            for table in protect.ORDER:
                cur.execute(f"SELECT * FROM `{table}`")
                out += [repr(row) for row in cur.fetchall()]
        return "\n".join(out)

    def words_in_database() -> bool:
        text = raw_text()
        return any(m in text for m in MARKERS)

    def triggers() -> set:
        with root.cursor() as cur:
            cur.execute("SELECT trigger_name AS n FROM information_schema.triggers WHERE trigger_schema='yandi_epistemic'")
            return {r["n"] for r in cur.fetchall()}

    def raises(exc_types, fn) -> bool:
        try:
            fn()
        except exc_types:
            return True
        except Exception as exc:                          # noqa: BLE001
            print(f"    (the wrong kind of failure: {type(exc).__name__}: {str(exc)[:120]})")
            return False
        return False

    # ── v18 -> v19 upgrade is additive ────────────────────────────────────────────────────────────────────────────
    run("DELETE FROM schema_migrations WHERE version = %s" % schema.SCHEMA_VERSION)
    run("DROP TABLE IF EXISTS storage_protection_event")
    run("SET FOREIGN_KEY_CHECKS=0")
    for table in protect.ORDER:
        run(f"TRUNCATE TABLE `{table}`")
    run("SET FOREIGN_KEY_CHECKS=1")
    run("ALTER TABLE personal_fact MODIFY COLUMN statement VARCHAR(300) NOT NULL, MODIFY COLUMN evidence VARCHAR(500) NOT NULL")
    run("ALTER TABLE grievance MODIFY COLUMN context JSON NULL")
    run("INSERT INTO interaction_turn (user_id, source_turn_id, turn_id_origin, user_text, created_at) "
        "VALUES ('v18_owner', 'turn-v18-0001', 'client', 'row that existed at schema v18', NOW())")
    run("INSERT INTO personal_fact (fact_id, user_id, fact_class, statement, polarity, temporality, evidence, source_turn_id, created_at) "
        "VALUES ('pf_v18', 'v18_owner', 'possession', 'a statement', 'affirmed', 'current', 'an evidence', 'turn-v18-0001', NOW())")
    run("INSERT INTO grievance (id, user_id, event_type, description, severity, status, context, created_at, updated_at) "
        "VALUES ('g_v18', 'v18_owner', 'insult', 'a v18 grievance', 0.4, 'registered', '{\"a\": 1, \"b\": [1, 2]}', NOW(), NOW())")
    check("U0: before the migration the protected columns are NOT wide", scalar(
        "SELECT data_type FROM information_schema.columns WHERE table_schema='yandi_epistemic' AND table_name='personal_fact' AND column_name='evidence'") == "varchar")
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            protect.check_schema(root)
            refused_before = False
        except protect.ProtectError:
            refused_before = True
    check("U1: `seal` refuses to start on a database that has not been upgraded (a ciphertext would not fit)", refused_before)
    os.environ.update({"YANDI_SQL_SOCKET": socket, "YANDI_SQL_USER": admin, "YANDI_SQL_AUTH_MODE": "password", "YANDI_SQL_PASSWORD": admin_pw})
    with contextlib.redirect_stdout(io.StringIO()):
        upgraded = migrate.apply()
    types = {(t, c): scalar("SELECT data_type FROM information_schema.columns WHERE table_schema='yandi_epistemic' AND table_name=%s AND column_name=%s", (t, c))
             for t, cols in fp.PROTECTED.items() for c in cols}
    check("U2: the migration upgrades v18 -> v19, records version 19 and creates the mode table",
          upgraded and scalar("SELECT MAX(version) FROM schema_migrations") == schema.SCHEMA_VERSION == 20
          and scalar("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yandi_epistemic' AND table_name='storage_protection_event'") == 1)
    check("U3: every protected column is now wide text", all(v in ("mediumtext", "text", "longtext") for v in types.values()), repr(types))
    check("U4: the v18 rows are untouched (text, and the JSON is the same JSON)",
          scalar("SELECT evidence FROM personal_fact WHERE fact_id='pf_v18'") == "an evidence"
          and json.loads(scalar("SELECT context FROM grievance WHERE id='g_v18'")) == {"a": 1, "b": [1, 2]})
    with contextlib.redirect_stdout(io.StringIO()):
        again = migrate.apply()
    check("U5: running the migration again changes nothing and fails nothing", again and scalar("SELECT COUNT(*) FROM storage_protection_event") == 0)
    run("SET FOREIGN_KEY_CHECKS=0")
    for table in protect.ORDER:
        run(f"TRUNCATE TABLE `{table}`")
    run("SET FOREIGN_KEY_CHECKS=1")

    # ── the append-only triggers the real installation has ────────────────────────────────────────────────────────
    wanted = {name: ddl for name, ddl in security_triggers.immutability_triggers()
              if any(name.startswith(f"trg_{t}_") for t in protect.ORDER)}
    for name, ddl in wanted.items():
        run(f"DROP TRIGGER IF EXISTS `{name}`")
        run(ddl)
    check("T0: the update/delete triggers of the six tables are installed (as bootstrap does)", set(wanted) <= triggers() and len(wanted) >= 10)

    run("DROP USER IF EXISTS 'yandi_rt_prot'@'localhost'")
    run("CREATE USER 'yandi_rt_prot'@'localhost' IDENTIFIED BY 'rt-temp-only'")
    with root.cursor() as cur:
        for sql, params in security_grants.yandi_runtime_grant_statements("yandi_rt_prot", "localhost"):
            cur.execute(sql, params)
        cur.execute("FLUSH PRIVILEGES")
    rt = connect("yandi_rt_prot", "rt-temp-only")

    def app(fn):
        """One application transaction as the runtime role, the way the chat path does it."""
        try:
            result = fn(rt)
            rt.commit()
            return result
        except BaseException:
            rt.rollback()
            raise

    def seed():
        def go(c):
            repo.record_interaction_turn(c, USER, "t1", "client", WORDS, "Понимаю. Расскажи подробнее, что тебя беспокоит.", model="m", created_at=NOW)
            repo.record_interaction_turn(c, USER, "t2", "client", "заветный второй ответ " + "я" * 20000, None, created_at=NOW)
            repo.insert_personal_fact(c, "f1", USER, "family", "боится сдать работу поздно", "affirmed", "current", WORDS[:30], 0, 30, "t1", NOW)
            repo.insert_personal_fact_event(c, "f1", USER, "restated", None, WORDS[:30], 0, 30, "t2", NOW)
            repo.record_commitment(c, "c1", USER, "general", "принесу отчёт", "принесу отчёт завтра", None, NOW, "t1")
            repo.record_commitment_event(c, "c1", USER, "fulfillment_claimed", "user_report", "отчёт отправил", NOW, "t2")
            repo.record_grievance(c, "g1", USER, "insult", "Ты сказал(а): " + WORDS, 0.5, {"note": "заветный", "n": [1, 2]}, NOW)
            repo.record_grievance(c, "g2", USER, "insult", "yp1:это набрал человек, не шифр", 0.2, None, NOW)
        app(go)

    def read_everything():
        def go(c):
            return {
                "turns": [(t["source_turn_id"], t["user_text"], t["assistant_text"]) for t in reversed(repo.list_recent_interaction_turns(c, USER))],
                "facts": [(f["fact_id"], f["statement"], f["evidence"]) for f in repo.list_personal_facts(c, USER)],
                "commitments": [(x["commitment_id"], x["text"], x["evidence"]) for x in repo.list_commitments(c, USER)],
                "cevents": [(x["event_type"], x["evidence"]) for x in repo.list_commitment_events(c, USER)],
                "g1": (lambda g: (g["description"], g["context"]))(repo.get_grievance(c, "g1")),
                "g2": repo.get_grievance(c, "g2")["description"],
                "active": sorted(g["id"] for g in repo.list_active_grievances(c, USER)),
                "similar": (repo.find_similar_open_grievance(c, USER, ("Ты сказал(а): " + WORDS)[:20] + " again") or {}).get("id"),
            }
        return app(go)

    # ── legacy state: plaintext, exactly what the application wrote before P1c-2 ───────────────────────────────────
    fp.clear_key()
    fp.forget_mode()
    seed()
    before = read_everything()
    check("L1: protection off (no record): the application writes and reads exactly as before", words_in_database() and before["turns"][0][1] == WORDS
          and before["g1"] == ("Ты сказал(а): " + WORDS, {"note": "заветный", "n": [1, 2]}) and before["similar"] == "g1")
    check("L2: a plaintext value that looks sealed (typed by the person) is stored escaped and reads back", before["g2"] == "yp1:это набрал человек, не шифр"
          and scalar("SELECT description FROM grievance WHERE id='g2'").startswith("yp0:"))
    st = protect.status(root)
    check("L3: status (no key needed) counts plaintext values and reports the mode 'off'",
          st["mode"] == "off" and sum(t["plain_values"] for t in st["tables"].values()) > 10 and sum(t["sealed_values"] for t in st["tables"].values()) == 0)

    # ── backup BEFORE anything is changed ─────────────────────────────────────────────────────────────────────────
    work = tempfile.mkdtemp(prefix="yandi-protect-")
    backup_path = os.path.join(work, "before.bak")
    fp.clear_key()
    info_backup = protect.backup(root, CORE_KEY, backup_path, log=quiet)
    blob = open(backup_path, "rb").read()
    check("B1: the backup is a file only its owner can read", stat.S_IMODE(os.stat(backup_path).st_mode) == 0o600)
    check("B2: the backup of PLAINTEXT rows holds no plaintext itself (it is encrypted)", not any(m.encode() in blob for m in MARKERS) and "Ты сказал".encode() not in blob)
    check("B3: it counts what it holds", info_backup["counts"]["interaction_turn"] == 2 and info_backup["counts"]["grievance"] == 2)

    # ── refusals before any change ────────────────────────────────────────────────────────────────────────────────
    fp.clear_key()
    check("R1: a key that is not 32 bytes is refused", raises(ValueError, lambda: protect.seal_all(root, b"short", log=quiet)))
    run("CREATE TRIGGER trg_stranger BEFORE UPDATE ON personal_fact FOR EACH ROW SET NEW.fact_class = NEW.fact_class")
    check("R2: an UPDATE trigger the tool did not create stops it before anything is changed (no mode record, nothing sealed)",
          raises(protect.ProtectError, lambda: protect.seal_all(root, CORE_KEY, log=quiet))
          and scalar("SELECT COUNT(*) FROM storage_protection_event") == 0
          and scalar("SELECT COUNT(*) FROM interaction_turn WHERE user_text LIKE 'yp1:%%'") == 0 and words_in_database())
    run("DROP TRIGGER trg_stranger")

    # ── seal ──────────────────────────────────────────────────────────────────────────────────────────────────────
    before_triggers = triggers()
    result = protect.seal_all(root, CORE_KEY, log=quiet)
    check("S1: `seal` switched protection ON", result["mode"] == "on" and fp.read_mode(root) == "on")
    check("S2: NOT ONE of the person's words remains in any column of the six tables", not words_in_database(), raw_text()[:200])
    check("S3: every stored protected value is sealed", protect.status(root)["mode"] == "on"
          and sum(t["plain_values"] for t in protect.status(root)["tables"].values()) == 0)
    check("S4: the append-only triggers are back, exactly as before", triggers() == before_triggers)
    check("S5: the runtime role still cannot rewrite or delete a sealed row",
          raises(pymysql.err.MySQLError, lambda: app(lambda c: c.cursor().execute("UPDATE personal_fact SET fact_class='x' WHERE fact_id='f1'")))
          and raises(pymysql.err.MySQLError, lambda: app(lambda c: c.cursor().execute("DELETE FROM interaction_turn"))))
    fp.install_key(CORE_KEY)
    fp.forget_mode()
    after = read_everything()
    check("S6: with the key, the application reads back exactly what it had (every table, the JSON, the recognised grievance, the typed 'yp1:')", after == before, repr(after)[:200])
    fp.clear_key()
    fp.forget_mode()
    again = protect.seal_all(root, CORE_KEY, log=quiet)
    check("S7: `seal` on a sealed ledger changes nothing", again["changed"] == 0 and again["digest"] == result["digest"])

    # ── the application under protection ──────────────────────────────────────────────────────────────────────────
    fp.clear_key()
    fp.forget_mode()
    check("A1: no key: the application's write is refused and nothing reaches the tables", raises(fp.StorageLocked, lambda: app(
        lambda c: repo.record_interaction_turn(c, USER, "t3", "client", "заветный третий", None, created_at=NOW))) and scalar("SELECT COUNT(*) FROM interaction_turn") == 2)
    check("A2: no key: the sealed history is not shown", raises(fp.StorageLocked, lambda: read_everything()))
    fp.install_key(OTHER_KEY)
    fp.forget_mode()
    check("A3: a wrong key is refused (the mode record does not verify), never garbage", raises(fp.StorageTampered, lambda: read_everything()))
    check("A4: …and the tool refuses to work with it before changing anything", raises((protect.ProtectError, fp.StorageProtectionError), lambda: protect.unseal_all(root, OTHER_KEY, log=quiet))
          and not words_in_database())
    fp.clear_key()
    check("A4b: …the mode is still 'on' and nothing was opened", protect.status(root)["mode"] == "on" and not words_in_database())
    fp.install_key(CORE_KEY)
    fp.forget_mode()
    app(lambda c: repo.record_interaction_turn(c, USER, "t3", "client", "заветный третий", "ответ", created_at=NOW))
    check("A5: with the key a new turn is written sealed and read back", not words_in_database() and any(t[1] == "заветный третий" for t in read_everything()["turns"]))
    run("INSERT INTO interaction_turn (user_id, source_turn_id, turn_id_origin, user_text, created_at) VALUES (%s, 'planted', 'client', 'подложенный текст', NOW())", (USER,))
    check("A6: a plaintext row planted around the application is refused, not shown", raises(fp.StorageTampered, lambda: read_everything()))
    run("DROP TRIGGER IF EXISTS trg_interaction_turn_no_delete")
    run("DELETE FROM interaction_turn WHERE source_turn_id = 'planted'")
    run(wanted["trg_interaction_turn_no_delete"])
    check("A7: the planted row is gone and the ledger reads again", read_everything()["turns"][0][1] == WORDS)

    # swapping two sealed values inside the database (an admin who tampers): refused
    run("DROP TRIGGER IF EXISTS trg_interaction_turn_no_update")
    a_text, b_text = scalar("SELECT user_text FROM interaction_turn WHERE source_turn_id='t1'"), scalar("SELECT user_text FROM interaction_turn WHERE source_turn_id='t2'")
    run("UPDATE interaction_turn SET user_text=%s WHERE source_turn_id='t1'", (b_text,))
    run("UPDATE interaction_turn SET user_text=%s WHERE source_turn_id='t2'", (a_text,))
    check("A8: two sealed values swapped between rows inside the database are refused, not shown under the wrong turn", raises(fp.StorageTampered, lambda: read_everything()))
    run("UPDATE interaction_turn SET user_text=%s WHERE source_turn_id='t1'", (a_text,))
    run("UPDATE interaction_turn SET user_text=%s WHERE source_turn_id='t2'", (b_text,))
    run(wanted["trg_interaction_turn_no_update"])
    check("A9: put back, it reads again and the trigger is restored", read_everything()["turns"][0][1] == WORDS and triggers() == before_triggers)

    # ── backup of a SEALED ledger, destroy, restore ───────────────────────────────────────────────────────────────
    sealed_backup = os.path.join(work, "sealed.bak")
    fp.clear_key()
    measured = protect.backup(root, CORE_KEY, sealed_backup, log=quiet)
    check("B4: the backup of the sealed ledger records mode 'on'", measured["mode"] == "on")
    fp.clear_key()
    check("B5: restore refuses a NON-empty ledger and touches nothing", raises(protect.ProtectError, lambda: protect.restore(root, CORE_KEY, sealed_backup, log=quiet))
          and scalar("SELECT COUNT(*) FROM interaction_turn") == 3)
    counts_before = {t: scalar(f"SELECT COUNT(*) FROM `{t}`") for t in protect.ORDER}
    digest_before = protect.scan(root, fp._hkdf(CORE_KEY, fp.STORAGE_INFO)).digest
    run("SET FOREIGN_KEY_CHECKS=0")
    for table in protect.ORDER:
        run(f"TRUNCATE TABLE `{table}`")
    run("SET FOREIGN_KEY_CHECKS=1")
    run("TRUNCATE TABLE storage_protection_event")
    check("B6: the ledger is destroyed", all(scalar(f"SELECT COUNT(*) FROM `{t}`") == 0 for t in protect.ORDER))
    check("B7: a wrong key does not open the backup", raises(protect.ProtectError, lambda: protect.restore(root, OTHER_KEY, sealed_backup, log=quiet)))
    tampered = os.path.join(work, "tampered.bak")
    data = bytearray(open(sealed_backup, "rb").read())
    data[100] ^= 0x01
    open(tampered, "wb").write(bytes(data))
    check("B8: a damaged backup is refused", raises(protect.ProtectError, lambda: protect.restore(root, CORE_KEY, tampered, log=quiet)))
    check("B9: nothing was restored by the refusals", all(scalar(f"SELECT COUNT(*) FROM `{t}`") == 0 for t in protect.ORDER))
    restored = protect.restore(root, CORE_KEY, sealed_backup, log=quiet)
    check("B10: restore puts every row back, the same count in every table", {t: scalar(f"SELECT COUNT(*) FROM `{t}`") for t in protect.ORDER} == counts_before)
    check("B11: …the content measures the same as before the destruction", restored["digest"] == digest_before == measured["digest"])
    check("B12: …the mode is 'on' again, verified with the key", fp.read_mode(root) == "on")
    fp.install_key(CORE_KEY)
    fp.forget_mode()
    restored_view = read_everything()
    check("B13: …and the application reads the restored history exactly as before", restored_view["turns"][0][1] == WORDS and restored_view["g1"] == before["g1"]
          and restored_view["g2"] == before["g2"] and restored_view["similar"] == "g1")
    check("B14: still no word in the database", not words_in_database())
    fp.clear_key()
    fp.forget_mode()

    # ── unseal: back to exactly the original ─────────────────────────────────────────────────────────────────────
    fp.clear_key()
    reopened = protect.unseal_all(root, CORE_KEY, log=quiet)
    check("N1: `unseal` returns protection to OFF", reopened["mode"] == "off" and fp.read_mode(root) == "off")
    check("N2: the words are back in the database (rollback is real) and no sealed value remains",
          words_in_database() and sum(t["sealed_values"] for t in protect.status(root)["tables"].values()) == 0)
    check("N3: the triggers are back", triggers() == before_triggers)
    fp.clear_key()
    fp.forget_mode()
    plain_view = read_everything()
    check("N4: the application without any key reads the very same history", plain_view["turns"][0][1] == WORDS and plain_view["g1"] == before["g1"] and plain_view["g2"] == before["g2"])
    check("N5: the person's typed 'yp1:' is stored escaped again", scalar("SELECT description FROM grievance WHERE id='g2'").startswith("yp0:"))

    # ── seal the same data again and compare the digests: the content measure is the same across the whole trip ──
    d_plain = protect.scan(root, fp._hkdf(CORE_KEY, fp.STORAGE_INFO)).digest
    protect.seal_all(root, CORE_KEY, log=quiet)
    fp.clear_key()
    d_sealed = protect.scan(root, fp._hkdf(CORE_KEY, fp.STORAGE_INFO)).digest
    check("N6: the measured content is identical in plaintext form and in sealed form", d_plain == d_sealed)
    protect.unseal_all(root, CORE_KEY, log=quiet)

    # ── an interruption in the middle of `seal` ──────────────────────────────────────────────────────────────────
    real_rewrite = protect._rewrite_table
    calls = {"n": 0}

    def dies_after_two(conn, key, table, *, seal, log):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("power cut")
        return real_rewrite(conn, key, table, seal=seal, log=log)
    with patch.object(protect, "_rewrite_table", dies_after_two):
        check("I1: an interrupted `seal` stops with the error", raises(RuntimeError, lambda: protect.seal_all(root, CORE_KEY, log=quiet)))
    fp.clear_key()
    fp.install_key(CORE_KEY)
    fp.forget_mode()
    check("I2: the mode is 'migrating' (writes are sealed, both forms are read)", fp.read_mode(root) == "migrating")
    check("I3: the tables already done are sealed, the rest are still plaintext (a half-way state)",
          protect.status(root)["tables"]["interaction_turn"]["plain_values"] == 0 and protect.status(root)["tables"]["grievance"]["sealed_values"] == 0)
    check("I4: the application, with the key, reads the whole ledger in this state", read_everything()["turns"][0][1] == WORDS)
    check("I5: every trigger is in place after the interruption", triggers() == before_triggers)
    fp.clear_key()
    fp.forget_mode()
    check("I6: without the key the sealed part is refused, never shown", raises(fp.StorageLocked, lambda: read_everything()))
    finished = protect.seal_all(root, CORE_KEY, log=quiet)
    check("I7: running `seal` again finishes the job: protection ON, no word left, same content", finished["mode"] == "on" and not words_in_database() and finished["digest"] == d_plain)
    protect.unseal_all(root, CORE_KEY, log=quiet)

    # ── an error inside one table rolls THAT table back ──────────────────────────────────────────────────────────
    seals = {"n": 0}
    real_seal_with = fp.seal_with

    def fails_on_seventh(key, table, column, row_key, text):
        seals["n"] += 1
        if seals["n"] == 7:
            raise RuntimeError("disk full")
        return real_seal_with(key, table, column, row_key, text)
    with patch.object(fp, "seal_with", fails_on_seventh):
        check("E1: an error in the middle of a table stops the run", raises(RuntimeError, lambda: protect.seal_all(root, CORE_KEY, log=quiet)))
    fp.clear_key()
    fp.install_key(CORE_KEY)
    fp.forget_mode()
    status_after = protect.status(root)
    failing = status_after["tables"]["personal_fact_event"]                 # the 7th value sealed belongs to the third table
    check("E2: the failing table was rolled back completely (its value is still plaintext), the ones before it stay sealed, every trigger is back",
          failing["sealed_values"] == 0 and failing["plain_values"] == 1
          and status_after["tables"]["interaction_turn"]["plain_values"] == 0 and triggers() == before_triggers)
    check("E3: the application still reads everything", read_everything()["turns"][0][1] == WORDS)
    finished = protect.seal_all(root, CORE_KEY, log=quiet)
    check("E4: a clean run afterwards succeeds", finished["mode"] == "on" and not words_in_database())
    protect.unseal_all(root, CORE_KEY, log=quiet)

    # ── a rewrite that would change the content is refused ────────────────────────────────────────────────────────
    def seals_the_wrong_text(key, table, column, row_key, text):
        return real_seal_with(key, table, column, row_key, text + "!")
    with patch.object(fp, "seal_with", seals_the_wrong_text):
        check("E5: a rewrite whose result is not the content it started from is refused (measured inside the transaction)",
              raises(protect.ProtectError, lambda: protect.seal_all(root, CORE_KEY, log=quiet)))
    fp.clear_key()
    check("E6: …and rolled back: nothing sealed, the triggers are back", protect.status(root)["tables"]["interaction_turn"]["sealed_values"] == 0 and triggers() == before_triggers)
    protect.seal_all(root, CORE_KEY, log=quiet)
    protect.unseal_all(root, CORE_KEY, log=quiet)

    # ── compare-and-set: a row changed while it was being rewritten ───────────────────────────────────────────────
    hits = {"n": 0}

    def edits_under_us(key, table, column, row_key, text):
        if table == "grievance" and column == "description" and hits["n"] == 0:
            hits["n"] = 1
            run("UPDATE grievance SET description='changed under us' WHERE id=%s", (row_key["id"],))
        return real_seal_with(key, table, column, row_key, text)
    with patch.object(fp, "seal_with", edits_under_us):
        check("C1: a row that changed between being read and being written is not overwritten (the table is rolled back)",
              raises(protect.ProtectError, lambda: protect.seal_all(root, CORE_KEY, log=quiet)))
    check("C2: …and the row is as it was", scalar("SELECT description FROM grievance WHERE id='g1'").startswith("Ты сказал"))
    protect.seal_all(root, CORE_KEY, log=quiet)
    protect.unseal_all(root, CORE_KEY, log=quiet)

    # ── a table that was skipped is noticed at the end ────────────────────────────────────────────────────────────
    def skips_grievance(conn, key, table, *, seal, log):
        return 0 if table == "grievance" else real_rewrite(conn, key, table, seal=seal, log=log)
    with patch.object(protect, "_rewrite_table", skips_grievance):
        check("K1: if a table was left in the old form the mode is NOT switched", raises(protect.ProtectError, lambda: protect.seal_all(root, CORE_KEY, log=quiet)))
    fp.clear_key()
    fp.install_key(CORE_KEY)
    fp.forget_mode()
    check("K2: …the mode stays 'migrating'", fp.read_mode(root) == "migrating")
    protect.seal_all(root, CORE_KEY, log=quiet)
    protect.unseal_all(root, CORE_KEY, log=quiet)

    # ── a write that slips in during the run stops the switch ────────────────────────────────────────────────────
    real_scan = protect.scan
    state = {"n": 0}

    def scan_wrapper(conn, key, tables=None):
        if tables is None:                               # the whole-ledger measures: 1 = before, 2 = the final one
            state["n"] += 1
            if state["n"] == 2:                          # a write slips in after the tables were sealed, before the final measure
                run("INSERT INTO interaction_turn (user_id, source_turn_id, turn_id_origin, user_text, created_at) "
                    "VALUES (%s, 'stray', 'client', 'заветный посторонний', NOW())", (USER,))
        return real_scan(conn, key, tables)
    with patch.object(protect, "scan", scan_wrapper):
        refused = raises(protect.ProtectError, lambda: protect.seal_all(root, CORE_KEY, log=quiet))
    fp.clear_key()
    fp.install_key(CORE_KEY)
    fp.forget_mode()
    check("W1: a row written during the run stops it and the mode is NOT switched to 'on'", refused and fp.read_mode(root) == "migrating")
    protect.seal_all(root, CORE_KEY, log=quiet)          # a second run seals the stray row too
    check("W2: a second run completes and seals it", fp.read_mode(root) == "on" and not words_in_database())
    protect.unseal_all(root, CORE_KEY, log=quiet)

    # a stray write by a legitimate (keyed) writer during the run: nothing is plaintext, yet the ledger is not what was measured
    state["n"] = 0

    def scan_wrapper_sealed(conn, key, tables=None):
        if tables is None:
            state["n"] += 1
            if state["n"] == 2:
                app(lambda c: repo.record_interaction_turn(c, USER, "stray-sealed", "client", "заветный законный", None, created_at=NOW))
        return real_scan(conn, key, tables)
    with patch.object(protect, "scan", scan_wrapper_sealed):
        refused_sealed = raises(protect.ProtectError, lambda: protect.seal_all(root, CORE_KEY, log=quiet))
    fp.clear_key()
    fp.install_key(CORE_KEY)
    fp.forget_mode()
    check("W3: a legitimate write during the run (sealed, so nothing is plaintext) still stops the switch: the ledger is not what was measured",
          refused_sealed and fp.read_mode(root) == "migrating")
    protect.seal_all(root, CORE_KEY, log=quiet)
    protect.unseal_all(root, CORE_KEY, log=quiet)

    # ── a value that looks sealed but does not open ──────────────────────────────────────────────────────────────
    run("DROP TRIGGER IF EXISTS trg_personal_fact_no_update")
    run("UPDATE personal_fact SET evidence='yp1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' WHERE fact_id='f1'")
    run(wanted["trg_personal_fact_no_update"])
    check("V1: a value that looks sealed and does not open stops `seal` (it is not silently treated as text)",
          raises(protect.ProtectError, lambda: protect.seal_all(root, CORE_KEY, log=quiet)))
    run("DROP TRIGGER IF EXISTS trg_personal_fact_no_update")
    run("UPDATE personal_fact SET evidence=%s WHERE fact_id='f1'", (WORDS[:30],))
    run(wanted["trg_personal_fact_no_update"])
    fp.clear_key()
    fp.forget_mode()

    # ── restore of the PLAINTEXT backup into a database that is in a different state ─────────────────────────────
    run("SET FOREIGN_KEY_CHECKS=0")
    for table in protect.ORDER:
        run(f"TRUNCATE TABLE `{table}`")
    run("SET FOREIGN_KEY_CHECKS=1")
    run("TRUNCATE TABLE storage_protection_event")
    protect.restore(root, CORE_KEY, backup_path, log=quiet)
    fp.clear_key()
    fp.forget_mode()
    check("B15: the pre-change backup of plaintext rows restores to protection off, and the application reads it without a key",
          read_everything()["turns"][0][1] == WORDS and words_in_database() and fp.read_mode(root) == "off")

    # ── a backup that was altered before it was encrypted (by someone holding the key) is refused ─────────────
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    storage_key = fp._hkdf(CORE_KEY, fp.STORAGE_INFO)
    raw = open(backup_path, "rb").read()
    doc = json.loads(AESGCM(protect._backup_key(storage_key)).decrypt(raw[8:20], raw[20:], protect.BACKUP_AAD))
    doc["tables"]["interaction_turn"]["rows"][0]["user_text"] = "подменённый текст"
    nonce = os.urandom(12)
    forged = os.path.join(work, "forged.bak")
    open(forged, "wb").write(protect.BACKUP_MAGIC + nonce + AESGCM(protect._backup_key(storage_key)).encrypt(
        nonce, json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), protect.BACKUP_AAD))
    run("SET FOREIGN_KEY_CHECKS=0")
    for table in protect.ORDER:
        run(f"TRUNCATE TABLE `{table}`")
    run("SET FOREIGN_KEY_CHECKS=1")
    run("TRUNCATE TABLE storage_protection_event")
    check("B16: a restore whose content is not the content the backup was measured with is refused and rolled back",
          raises(protect.ProtectError, lambda: protect.restore(root, CORE_KEY, forged, log=quiet)) and all(scalar(f"SELECT COUNT(*) FROM `{t}`") == 0 for t in protect.ORDER))

    # a backup that names a column the database does not have (or an injected name) is refused before any SQL is built from it
    doc = json.loads(AESGCM(protect._backup_key(storage_key)).decrypt(raw[8:20], raw[20:], protect.BACKUP_AAD))
    evil = "user_text`) SELECT 1; -- "
    doc["tables"]["interaction_turn"]["columns"].append(evil)
    for row in doc["tables"]["interaction_turn"]["rows"]:
        row[evil] = "x"
    nonce = os.urandom(12)
    injected = os.path.join(work, "injected.bak")
    open(injected, "wb").write(protect.BACKUP_MAGIC + nonce + AESGCM(protect._backup_key(storage_key)).encrypt(
        nonce, json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), protect.BACKUP_AAD))
    check("B17: a backup naming a column that is not a plain identifier of this database is refused, nothing is written",
          raises(protect.ProtectError, lambda: protect.restore(root, CORE_KEY, injected, log=quiet)) and all(scalar(f"SELECT COUNT(*) FROM `{t}`") == 0 for t in protect.ORDER))

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
