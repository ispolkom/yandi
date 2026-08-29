"""
agent/db_sql_temp_password_roundtrip_regression_test.py — DATABASE
BOOTSTRAP V1, eighth Phase B attempt follow-up: full byte-for-byte
roundtrip of the temp-password pipeline

    Percona fresh log event
      -> bash _read_fresh_log_delta / _extract_unique_temp_password
      -> atomic marker write (install-yandi.sh)
      -> agent.db.sql.live_bootstrap.load_and_consume_fresh_init_marker()
         (the REAL production function, imported and called directly)

against a battery of "nasty" password samples exercising punctuation/
symbol edge cases MySQL's generated temp passwords can plausibly
contain, proving the value survives EXACTLY — never printing any
sample value itself, only lengths and their exact string equality
(these are synthetic test fixtures, not real secrets, so asserting
exact equality in test output is fine — this file only avoids ever
treating a REAL captured secret this way).

Live-debugging context (eighth Phase B attempt): live_bootstrap.py's
marker loader used to call plain `.strip()`, which removes ANY
leading/trailing whitespace character, not just a trailing line
ending — flagged as an unproven risk and replaced with a surgical
single-trailing-newline removal. This test proves the full pipeline
(not just that one function in isolation) preserves nasty passwords
byte-for-byte end to end, including through bash's printf/mv-based
atomic marker write and grep -oP's \\S+ extraction.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_temp_password_roundtrip_regression_test
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from agent.db.sql.live_bootstrap import load_and_consume_fresh_init_marker

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


REPO = Path(__file__).parent.parent
INSTALL_SCRIPT = REPO / "deploy" / "install-yandi.sh"
script_text = INSTALL_SCRIPT.read_text(encoding="utf-8")


def _extract_fn(name: str) -> str:
    body = script_text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
    return f"{name}() {{\n" + body + "\n}\n"


_FUNCS_SRC = (
    _extract_fn("_stat_log_boundary")
    + _extract_fn("_read_fresh_log_delta")
    + _extract_fn("_extract_unique_temp_password")
)


def _stat_boundary(path: str):
    snippet = f"""
set -u
die() {{ echo "[die] $*" >&2; exit 1; }}
{_FUNCS_SRC}
_stat_log_boundary {path!r}
"""
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=10)
    parts = result.stdout.split()
    return (parts[0], parts[1]) if len(parts) == 2 else ("", "0")


# ============================================================
# "Nasty" password samples — mandate's own explicit list, covering
# punctuation MySQL's generated temp passwords plausibly contain.
# ============================================================
NASTY_PASSWORDS = [
    "abc!Def#123",
    "A:B:C",
    "!LeadingPunctuation1",
    "TrailingPunctuation1!",
    "has%percent1",
    "has&ampersand1",
    "has$dollarsign1",
    r"has\backslash1",
    "has'singlequote1",
    'has"doublequote1',
    "has`backtick1",
    "EndsWithSpecial$",
    "_Under-Score.Dot,Comma;Semi:Colon",
    "Mixed*(){}[]<>?~|",
]

for pw in NASTY_PASSWORDS:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "mysql-error.log"
        marker_path = Path(tmpdir) / "fresh_init_temp_password"

        # All content flows through FILES written by Python
        # (Path.write_text) — never embedded as literal bash snippet
        # source text — so the nasty characters are never at risk of
        # being lexically re-interpreted as shell syntax BY THE TEST
        # ITSELF (as opposed to by the production code under test,
        # which is exactly what this test verifies is safe).
        log_path.write_text("[Note] baseline before this invocation\n", encoding="utf-8")
        pre_inode, pre_size = _stat_boundary(str(log_path))

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"A temporary password is generated for root@localhost: {pw}\n")

        snippet = f"""
set -u
die() {{ echo "[die] $*" >&2; exit 1; }}
{_FUNCS_SRC}
delta="$(_read_fresh_log_delta {str(log_path)!r} {pre_inode!r} {pre_size!r})" || exit 1
pw="$(_extract_unique_temp_password '' "$delta")" || exit 1
marker_tmp={str(marker_path)!r}".tmp"
( umask 077; printf '%s' "$pw" > "$marker_tmp" )
mv -f "$marker_tmp" {str(marker_path)!r}
"""
        result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=10)
        check(
            f"roundtrip (log-delta source) bash extraction+marker-write "
            f"succeeded for a password shaped like {pw[:3]!r}...",
            result.returncode == 0,
            f"rc={result.returncode} stderr={result.stderr!r}",
        )

        recovered = load_and_consume_fresh_init_marker(str(marker_path))
        check(
            f"roundtrip (log-delta source): password shaped like {pw[:3]!r}... "
            f"survives bash extraction -> atomic marker write -> the REAL "
            f"production load_and_consume_fresh_init_marker() EXACTLY "
            f"(byte-for-byte, length {len(pw)})",
            recovered == pw,
            f"expected_len={len(pw)} actual_len={len(recovered) if recovered is not None else None}",
        )
        check(
            "marker was consumed (deleted) by the real production loader",
            not marker_path.exists(),
        )


# ============================================================
# Same battery, but sourced from the subprocess-output side
# (init_output) rather than the log delta — passed via an ENVIRONMENT
# VARIABLE to the bash subprocess (never embedded as literal snippet
# source text) so nasty shell metacharacters can't be misinterpreted
# by the TEST HARNESS itself either.
# ============================================================
for pw in NASTY_PASSWORDS[:5]:
    marker_snippet = """
set -u
die() { echo "[die] $*" >&2; exit 1; }
""" + _FUNCS_SRC + """
pw="$(_extract_unique_temp_password "$INIT_OUTPUT_FOR_TEST" "")" || exit 1
printf '%s' "$pw"
"""
    result = subprocess.run(
        ["bash", "-c", marker_snippet],
        capture_output=True, text=True, timeout=10,
        env={"INIT_OUTPUT_FOR_TEST": f"A temporary password is generated for root@localhost: {pw}\n", "PATH": "/usr/bin:/bin"},
    )
    check(
        f"roundtrip (subprocess-output source, via env var not inline "
        f"snippet text): password shaped like {pw[:3]!r}... extracted exactly",
        result.returncode == 0 and result.stdout == pw,
        f"rc={result.returncode} match={result.stdout == pw} stderr={result.stderr!r}",
    )


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
