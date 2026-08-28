"""
agent/db/sql/security_triggers.py — Этап 5E-S S2: immutability defense-
in-depth via BEFORE UPDATE/DELETE triggers (mandate §6/§7/§23).

DESIGNED, NOT EXECUTED against any live server in this pass — see
SECURITY_ARCHITECTURE.md §0/§9. These triggers are a SECOND wall behind
the GRANT model (security_grants.py): even if a role were ever
misconfigured with an UPDATE/DELETE grant it shouldn't have, the
trigger still rejects the statement at the database level, for EVERY
account including one with a bug in its GRANT setup — not just the
intended runtime role.

What triggers CANNOT do (documented honestly, mandate §6's own
framing): stop a MySQL account with the TRIGGER-bypass-capable
privilege set (essentially SUPER/root) from dropping the trigger first.
"NORMAL YANDI OPERATION CANNOT ALTER QUESTION HISTORY. ADMIN-LEVEL
TAMPERING MUST BE DETECTABLE." — the second half of that sentence is
the integrity journal's (integrity.py) job, not this file's.
"""
from __future__ import annotations

from typing import List, Tuple

from agent.db.sql.schema import ALL_TABLES_IN_ORDER, TABLE_CLASSIFICATION

_ALL_TABLES = [n for n, _ in ALL_TABLES_IN_ORDER]

# Class A/B (+ D's own narrower rule below): UPDATE/DELETE unconditionally
# rejected. Class C (belief, semantic_edge): UPDATE is legitimate (that's
# what a projection is for) — only DELETE is blocked (nothing in this
# codebase ever deletes a belief/edge; §34: make the wrong thing
# impossible, not just undocumented).
_FULLY_IMMUTABLE_TABLES = [n for n in _ALL_TABLES if TABLE_CLASSIFICATION.get(n) in ("A", "B")]
_PROJECTION_TABLES = [n for n in _ALL_TABLES if TABLE_CLASSIFICATION.get(n) == "C"]
_NARROW_MUTABLE_TABLES = [n for n in _ALL_TABLES if TABLE_CLASSIFICATION.get(n) == "D"]


def _reject_update_trigger(table: str) -> str:
    trigger_name = f"trg_{table}_no_update"
    return (
        f"CREATE TRIGGER {trigger_name}\n"
        f"BEFORE UPDATE ON `{table}`\n"
        f"FOR EACH ROW\n"
        f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        f"'{table} is immutable (YANDI SQL BASTION): UPDATE forbidden';"
    )


def _reject_delete_trigger(table: str) -> str:
    trigger_name = f"trg_{table}_no_delete"
    return (
        f"CREATE TRIGGER {trigger_name}\n"
        f"BEFORE DELETE ON `{table}`\n"
        f"FOR EACH ROW\n"
        f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        f"'{table} is immutable (YANDI SQL BASTION): DELETE forbidden';"
    )


def _verification_run_update_guard() -> str:
    """
    Mandate §6's "verification_run stays D, not fully immutable" decision
    (SECURITY_ARCHITECTURE.md §6): the ONLY legitimate UPDATE is a
    status transition FROM 'running' TO exactly one of the three
    terminal states, and the identity/history-defining columns
    (run_id, occurrence_id, started_at) may never change. Anything else
    — including a second transition attempt on an already-terminal row
    — is rejected.
    """
    return (
        "CREATE TRIGGER trg_verification_run_guard_update\n"
        "BEFORE UPDATE ON `verification_run`\n"
        "FOR EACH ROW\n"
        "BEGIN\n"
        "  IF NEW.run_id <> OLD.run_id OR NEW.occurrence_id <> OLD.occurrence_id "
        "OR NEW.started_at <> OLD.started_at THEN\n"
        "    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'verification_run: run_id/occurrence_id/started_at are immutable';\n"
        "  END IF;\n"
        "  IF OLD.status <> 'running' THEN\n"
        "    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'verification_run: status is already terminal, no further transition allowed';\n"
        "  END IF;\n"
        "  IF NEW.status NOT IN ('completed', 'aborted', 'failed') THEN\n"
        "    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'verification_run: only running -> a terminal status is an allowed transition';\n"
        "  END IF;\n"
        "END"
    )


def immutability_triggers() -> List[Tuple[str, str]]:
    """Returns [(trigger_name, ddl), ...] for every table this pass
    protects. `agent/db/sql/bootstrap.py` is the intended (not yet
    executed) caller — one CREATE TRIGGER per statement, matching
    MySQL's requirement that DDL not be batched with MULTI_STATEMENTS
    (mandate §15: "MULTI_STATEMENTS не включать")."""
    out: List[Tuple[str, str]] = []
    for table in _FULLY_IMMUTABLE_TABLES:
        out.append((f"trg_{table}_no_update", _reject_update_trigger(table)))
        out.append((f"trg_{table}_no_delete", _reject_delete_trigger(table)))
    for table in _PROJECTION_TABLES:
        out.append((f"trg_{table}_no_delete", _reject_delete_trigger(table)))
    for table in _NARROW_MUTABLE_TABLES:
        if table == "verification_run":
            out.append(("trg_verification_run_guard_update", _verification_run_update_guard()))
            out.append((f"trg_{table}_no_delete", _reject_delete_trigger(table)))
        else:
            # Any FUTURE class-D table without a bespoke guard defaults
            # to fully immutable rather than silently unprotected —
            # never add a table to TABLE_CLASSIFICATION's "D" bucket
            # without also writing its own guard function above.
            out.append((f"trg_{table}_no_update", _reject_update_trigger(table)))
            out.append((f"trg_{table}_no_delete", _reject_delete_trigger(table)))
    return out
