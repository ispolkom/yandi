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

    LIVE PENTEST FINDING (owner-authorized, this pass) fixed here: the
    original version of this trigger validated identity + the one-shot
    transition, but nothing else about what that ONE legitimate UPDATE
    was allowed to say. Live-confirmed exploitable by yandi_runtime
    itself (its own ordinary, already-granted UPDATE privilege — no
    privilege escalation involved at all):

      1. final_answer_id could be pointed at ANY existing answer_version
         row, including one belonging to a COMPLETELY DIFFERENT
         question — the FK only proves the target answer_id exists
         somewhere, never that it's THIS run's own question's answer.
         A forged run then makes explain_answer()/get_current_answer()
         present another question's claims/evidence as if they justified
         THIS one's delivered text.
      2. pipeline_version/web_enabled/validation_enabled/schema_version
         were never protected at all — freely rewritable during the
         same single legitimate transition, silently falsifying the
         provenance fields anything downstream (Decision Ledger,
         reputation-by-code-version analysis) would otherwise treat as
         a trustworthy audit trail.

    Both closed below without weakening the original one-shot-transition
    guarantee: pipeline_version/web_enabled/validation_enabled/
    schema_version join run_id/occurrence_id/started_at as write-once
    (set only by start_run()'s own INSERT, never touched again — MySQL's
    NULL-safe `<=>` used so a legitimate NULL->NULL "unchanged" reading
    is never mistaken for a real change); final_answer_id, when set, is
    verified via a fresh SELECT to belong to the SAME question as this
    run's own occurrence_id.
    """
    return (
        "CREATE TRIGGER trg_verification_run_guard_update\n"
        "BEFORE UPDATE ON `verification_run`\n"
        "FOR EACH ROW\n"
        "BEGIN\n"
        "  DECLARE v_run_question_id BIGINT;\n"
        "  DECLARE v_answer_question_id BIGINT;\n"
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
        "  IF NOT (NEW.pipeline_version <=> OLD.pipeline_version) "
        "OR NOT (NEW.web_enabled <=> OLD.web_enabled) "
        "OR NOT (NEW.validation_enabled <=> OLD.validation_enabled) "
        "OR NOT (NEW.schema_version <=> OLD.schema_version) THEN\n"
        "    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'verification_run: pipeline_version/web_enabled/validation_enabled/schema_version "
        "are write-once, set only at start_run() time';\n"
        "  END IF;\n"
        "  IF NEW.final_answer_id IS NOT NULL THEN\n"
        "    SELECT question_id INTO v_run_question_id FROM question_occurrence "
        "WHERE occurrence_id = NEW.occurrence_id;\n"
        "    SELECT question_id INTO v_answer_question_id FROM answer_version "
        "WHERE answer_id = NEW.final_answer_id;\n"
        "    IF v_answer_question_id IS NULL OR v_answer_question_id <> v_run_question_id THEN\n"
        "      SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'verification_run: final_answer_id must belong to THIS run''s own question';\n"
        "    END IF;\n"
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
