#!/usr/bin/env python3
"""Mutation check of the sealed personal ledger (agent/db/sql/field_protection.py, protect.py, the repository wiring, the Core hook).

Each mutant is a copy of the source tree with ONE deliberate defect; the tests must then fail. A mutant that survives means a test suite
that would not notice that defect in the real code. The unmodified copy (the control) must pass first.

Two suites judge every mutant, the fast one first:
  * agent.db_sql_field_protection_regression_test   (fake SQL, milliseconds)
  * agent.db_sql_field_protection_sql_integration_test  through scripts/test-sql-temp.sh   (a real, private MySQL; about a minute)
  * pet.pet_core_lifecycle_regression_test          (only for the Core hook)

Run: YANDI_PYTHON=/path/to/python python scripts/protect_mutants.py [--only F2,T5]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = os.environ.get("YANDI_PYTHON") or sys.executable

FP = "agent/db/sql/field_protection.py"
PR = "agent/db/sql/protect.py"
RP = "agent/db/sql/repositories.py"
CL = "pet/core_lifecycle.py"

REG = "agent.db_sql_field_protection_regression_test"
SQL = "agent.db_sql_field_protection_sql_integration_test"
LIFE = "pet.pet_core_lifecycle_regression_test"

MUTANTS = [
    # ── the layer itself ──
    ("F1", "what the person types can look sealed (no escape)", {FP: [("        return ESCAPED_PREFIX + value if value.startswith((SEALED_PREFIX, ESCAPED_PREFIX)) else value\n", "        return value\n")]}, [REG]),
    ("F2", "plaintext is accepted while protection is on", {FP: [("    if current_mode(conn) == MODE_ON:\n        raise StorageTampered(f\"a stored value of {table}.{column} is not sealed although protection is on\")", "    if False:\n        raise StorageTampered(\"\")")]}, [REG]),
    ("F3", "no key, protection on: the write is quietly plaintext", {FP: [("        raise StorageLocked(\"storage protection is on and this process holds no key: nothing was written\")", "        return value")]}, [REG]),
    ("F4", "a sealed value is not bound to its row", {FP: [("        return \"|\".join(str(values[k]) for k in keys)", "        return \"x\"")]}, [REG]),
    ("F5", "the mode record is trusted without its proof", {FP: [("        if not proof or not hmac.compare_digest(bytes(proof), expected):", "        if False:")]}, [REG]),
    ("F6", "any database error is read as 'protection off'", {FP: [("        if _is_missing_table(exc):\n            return MODE_OFF\n        raise", "        return MODE_OFF")]}, [REG]),
    ("F7", "the mode is cached for ever", {FP: [("MODE_TTL_SECONDS = 10.0", "MODE_TTL_SECONDS = 1e12")]}, [REG]),
    ("F8", "a key tool that fails is ignored", {FP: [("        raise StorageLocked(\"the key tool refused: \" + (reason[0][:120] if reason else f\"exit {proc.returncode}\"))", "        return")]}, [REG]),
    ("F9", "a wrong key is not an error (the value is handed out as it is)", {FP: [("    except (InvalidTag, binascii.Error, ValueError, UnicodeDecodeError):\n        raise StorageTampered(f\"a stored value of {table}.{column} does not open (wrong key, moved or altered)\") from None", "    except (InvalidTag, binascii.Error, ValueError, UnicodeDecodeError):\n        return stored")]}, [REG]),
    # ── the wiring ──
    ("Q1", "a promise is written unsealed", {RP: [("fp.seal(conn, \"commitment\", \"text\", key, text[:500])", "text[:500]")]}, [REG]),
    ("Q2", "the history is read without opening", {RP: [("        return [fp.open_row(conn, \"interaction_turn\", dict(r)) for r in cur.fetchall()]", "        return [dict(r) for r in cur.fetchall()]")]}, [REG]),
    ("Q3", "the assistant's reply is written unsealed", {RP: [("    assistant_stored = fp.seal(conn, \"interaction_turn\", \"assistant_text\", key,\n                               None if assistant_text is None else assistant_text[:INTERACTION_TEXT_CAP])", "    assistant_stored = None if assistant_text is None else assistant_text[:INTERACTION_TEXT_CAP]")]}, [REG]),
    ("Q4", "the grievance context is written unsealed", {RP: [("    context_text = fp.seal(conn, \"grievance\", \"context\", key, json.dumps(context) if context is not None else None)", "    context_text = json.dumps(context) if context is not None else None")]}, [REG]),
    ("Q5", "the Core does not hand its key to the storage layer", {CL: [("        field_protection.install_key(key)          # P1c-2:", "        pass                                       # P1c-2:")]}, [LIFE]),
    ("Q6", "lock leaves the storage key in memory", {CL: [("    def _forget_key(self) -> None:\n        field_protection.clear_key()\n", "    def _forget_key(self) -> None:\n")]}, [LIFE]),
    # ── the tool ──
    ("T1", "an unknown UPDATE trigger is not looked for before changing anything", {PR: [("        _preflight(conn)\n", "")]}, [SQL]),
    ("T2", "the append-only trigger is not put back", {PR: [("    finally:\n        if had_trigger:\n            with conn.cursor() as cur:\n                cur.execute(_reject_update_trigger(table))", "    finally:\n        if False:\n            with conn.cursor() as cur:\n                cur.execute(_reject_update_trigger(table))")]}, [SQL]),
    ("T3", "a row that changed under the tool is overwritten", {PR: [("    guards = \" AND \".join(f\"{_ident(c)} <=> %s\" for c in columns)", "    guards = \" AND \".join(f\"({_ident(c)} <=> %s OR 1=1)\" for c in columns)")]}, [SQL]),
    ("T4", "one table's content is not measured inside its transaction", {PR: [("        after = scan(conn, key, [table])\n        if after.digest != before.digest:", "        after = scan(conn, key, [table])\n        if False:")]}, [SQL]),
    ("T5", "a table left in the old form is not noticed at the end", {PR: [("        if (after.total(after.plain) if seal else after.total(after.sealed)):", "        if False:")]}, [SQL]),
    ("T6", "the final whole-ledger measure is skipped", {PR: [("        if after.digest != before.digest:\n            raise ProtectError(\"the content of the ledger is not what it was before: the mode was NOT switched\")", "        if False:\n            raise ProtectError(\"\")")]}, [SQL]),
    ("T7", "the backup is not encrypted", {PR: [("    data = BACKUP_MAGIC + nonce + AESGCM(_backup_key(key)).encrypt(nonce, blob, BACKUP_AAD)", "    data = BACKUP_MAGIC + nonce + blob")]}, [SQL]),
    ("T8", "restore writes into a ledger that is not empty", {PR: [("                if cur.fetchone()[\"n\"]:", "                if False:")]}, [SQL]),
    ("T9", "restore does not check the content it restored", {PR: [("            if got.digest != doc[\"digest\"]:", "            if False:")]}, [SQL]),
    ("T10", "unseal does not escape a typed 'yp1:'", {PR: [("                    updates[column] = fp.ESCAPED_PREFIX + text if text.startswith((fp.SEALED_PREFIX, fp.ESCAPED_PREFIX)) else text", "                    updates[column] = text")]}, [SQL]),
    ("T11", "a value that looks sealed and does not open is treated as text", {PR: [("            raise ProtectError(f\"a value of {table}.{column} (row {row[PRIMARY_KEY[table]]}) looks sealed but does not open with this key\") from None", "            return \"plain\", stored")]}, [SQL]),
    ("T12", "a database that has not been upgraded is not refused", {PR: [("            if cols.get(column) not in WIDE_TYPES:", "            if False:")]}, [SQL]),
    ("T13", "the 'migrating' step is skipped", {PR: [("        if mode != fp.MODE_MIGRATING:\n            set_mode(conn, fp.MODE_MIGRATING)\n", "")]}, [SQL]),
    ("T14", "the mode is switched before the ledger is checked", {PR: [("        changed = 0\n        for table in (ORDER if seal else reversed(ORDER)):", "        set_mode(conn, fp.MODE_ON if seal else fp.MODE_OFF)\n        changed = 0\n        for table in (ORDER if seal else reversed(ORDER)):")]}, [SQL]),
]


def run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    env = dict(os.environ, YANDI_TEST_MODE="1", YANDI_PYTHON=PYTHON)
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def suite(cwd: Path, name: str) -> int:
    if name == SQL:
        env_cmd = ["bash", "scripts/test-sql-temp.sh", name]
        code, out = run(env_cmd, cwd, 600)
        return code if "SKIP" not in out else 3
    code, _ = run([PYTHON, "-m", name], cwd, 400)
    return code


def build_copy(edits: dict) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="yandi-protect-mutant-"))
    for sub in ("agent", "pet", "llm_gateway", "scripts", "contract", "docs"):
        shutil.copytree(ROOT / sub, tmp / sub, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for rel, pairs in edits.items():
        path = tmp / rel
        src = path.read_text(encoding="utf-8")
        for old, new in pairs:
            if src.count(old) != 1:
                raise SystemExit(f"mutant anchor not found exactly once in {rel}: {old[:70]!r} ({src.count(old)}x)")
            src = src.replace(old, new)
        path.write_text(src, encoding="utf-8")
    return tmp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    only = {x for x in args.only.split(",") if x}

    for mid, _, edits, _ in MUTANTS:                      # every anchor must exist before anything runs
        if only and mid not in only:
            continue
        shutil.rmtree(build_copy(edits), ignore_errors=True)

    control = build_copy({})
    try:
        for name in (REG, SQL, LIFE):
            code = suite(control, name)
            print(f"control {name}: {'pass' if code == 0 else 'FAIL (' + str(code) + ')'}", flush=True)
            if code != 0:
                return 2
    finally:
        shutil.rmtree(control, ignore_errors=True)

    survived = []
    for mid, label, edits, suites in MUTANTS:
        if only and mid not in only:
            continue
        tree = build_copy(edits)
        try:
            caught_by = None
            for name in suites:
                if suite(tree, name) != 0:
                    caught_by = name
                    break
            print(f"{mid:4} {'CAUGHT  ' if caught_by else 'SURVIVED'} {label}" + (f"   [{caught_by.split('.')[-1]}]" if caught_by else ""), flush=True)
            if not caught_by:
                survived.append(mid)
        finally:
            shutil.rmtree(tree, ignore_errors=True)
    print("all mutants caught" if not survived else f"SURVIVED: {', '.join(survived)}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
