"""
agent/db_sql_security_invariants_regression_test.py — Этап 5E-S (SQL
BASTION), S1: locks the mandate's §2 non-negotiable security invariants
into the documentation, verbatim, so a future edit to SECURITY_
ARCHITECTURE.md/SECURITY_THREAT_MODEL.md can't silently drop one.

This is a documentation-presence test, not a runtime enforcement test
(runtime enforcement — grants/triggers — is proven separately by
agent/db_sql_security_grants_regression_test.py and agent/db_sql_
security_triggers_regression_test.py). Its job is narrower and just as
real: the mandate demanded these exact phrases be "зафиксировать
буквально" — this proves they were.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_security_invariants_regression_test
"""
from __future__ import annotations

from pathlib import Path

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


BASE = Path(__file__).parent / "db" / "sql"
ARCH = (BASE / "SECURITY_ARCHITECTURE.md").read_text(encoding="utf-8")
THREAT = (BASE / "SECURITY_THREAT_MODEL.md").read_text(encoding="utf-8")
ALL_DOCS = ARCH + "\n" + THREAT

check("SECURITY_ARCHITECTURE.md exists and is non-trivial", len(ARCH) > 2000)
check("SECURITY_THREAT_MODEL.md exists and is non-trivial", len(THREAT) > 2000)

REQUIRED_INVARIANTS = [
    "NETWORK INPUT IS DATA, NEVER CODE",
    "PARAMETER DATA != SQL CODE",
    "STORED != TRUSTED",
    "AUTHENTICATED != TRUSTED",
    "ENCRYPTED != SAFE",
    "SIGNED != TRUE",
    "MAC_VALID != CLAIM_TRUE",
    "DATABASE ACCESS != EPISTEMIC AUTHORITY",
    "QUESTION IS IMMUTABLE",
    "QUESTION UPDATE = FORBIDDEN",
    "QUESTION DELETE = FORBIDDEN",
    "ANSWER HISTORY IS IMMUTABLE",
    "OLD ANSWER UPDATE = FORBIDDEN",
    "OLD ANSWER DELETE = FORBIDDEN",
    "NEW ANSWER = NEW ANSWER_VERSION",
    "TRACE HISTORY IS APPEND-ONLY",
    "OBSERVATION HISTORY IS APPEND-ONLY",
    "ASSESSMENT HISTORY IS APPEND-ONLY",
    "RECHECK HISTORY IS APPEND-ONLY",
    "DELETE CANONICAL EPISTEMIC HISTORY = FORBIDDEN",
    "TRUNCATE CANONICAL EPISTEMIC HISTORY = FORBIDDEN",
    "CASCADE DELETE CANONICAL HISTORY = FORBIDDEN",
    "MEMORY REPLAY != NEW PROVENANCE ROOT",
    "DATABASE CRYPTOGRAPHIC INTEGRITY != TRUTH",
    "DB SUPERUSER ACCESS MUST NOT EXIST IN NORMAL RUNTIME",
    "SQL CREDENTIAL != ENCRYPTION KEY",
    "ENCRYPTION KEY MUST NOT LIVE IN THE DATABASE IT DECRYPTS",
    "NO PLAINTEXT FALLBACK AFTER SQL BECOMES CANONICAL",
]

for phrase in REQUIRED_INVARIANTS:
    check(f"invariant present verbatim in security docs: {phrase!r}", phrase in ALL_DOCS)

check(
    f"ALL {len(REQUIRED_INVARIANTS)} mandate §2 invariants are present "
    f"(no partial coverage)",
    all(p in ALL_DOCS for p in REQUIRED_INVARIANTS),
)

# ============================================================
# Threat model: all 27 threats present, each with the 6 required
# analysis dimensions, not just a one-line mention.
# ============================================================

for i in range(1, 28):
    check(f"T{i} is documented in SECURITY_THREAT_MODEL.md", f"## T{i} " in THREAT or f"## T{i}—" in THREAT or f"## T{i} —" in THREAT)

for dim in ("ATTACK", "BOUNDARY", "PREVENTION", "DETECTION", "RECOVERY", "RESIDUAL RISK"):
    check(
        f"threat model uses the required '{dim}' analysis dimension somewhere "
        f"(structural sanity check, not per-threat exhaustive)",
        f"**{dim}" in THREAT,
    )

# ============================================================
# Proof-status honesty: the docs must distinguish PROVEN/DESIGNED/
# BLOCKED rather than claiming uniform completeness (mandate §55).
# ============================================================

for status_word in ("DESIGNED", "BLOCKED", "PROOF STATUS"):
    check(f"proof-status vocabulary '{status_word}' is used in the threat model", status_word in THREAT)

check(
    "the architecture doc explicitly flags the real environment finding "
    "(shared FastPanel-managed instance, *:3306) rather than assuming a "
    "clean managed-local profile",
    "FastPanel" in ARCH and "*:3306" in ARCH,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
