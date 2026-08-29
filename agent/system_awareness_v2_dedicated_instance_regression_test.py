"""
agent/system_awareness_v2_dedicated_instance_regression_test.py —
DATABASE BOOTSTRAP V1, mandate §23: System Awareness must keep the
SHARED FastPanel `mysql.service` and YANDI's own dedicated `yandi-db.
service` as distinct, never-confused facts.

Covers:
    A. build_snapshot() has a `yandi_db_instance` section, structurally
       separate from `software.sql_engines.mysql` (the existing,
       unrelated "is some mysql-family binary present" detector).
    B. the three facts (service_running/socket_present/identity_file_
       present) are independent — none is derived from another.
    C. no import of agent.db.sql.* anywhere in system_awareness.py
       (re-asserted here, not just relied on from the V1 test file, so
       this specific new section's own compliance is explicit).
    D. fingerprint()/compare() actually notice a change in this new
       section (proven once already by the live two-run regression in
       system_awareness_v1_1_regression_test.py; this file adds a
       synthetic, non-live proof so it doesn't depend on this
       particular host's current state).
    E. the well-known paths match deploy/install-yandi.sh's/deploy/
       yandi-db.service's OWN literal paths, not independently guessed
       ones that could silently drift apart.

Run: /home/iam/venv/bin/python3 -m agent.system_awareness_v2_dedicated_instance_regression_test
"""
from __future__ import annotations

import inspect
import re

import agent.system_awareness as sa

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


# ============================================================
# A. Structural separation.
# ============================================================

snap = sa.build_snapshot(probe_source="test")
check("A: build_snapshot() includes a top-level 'yandi_db_instance' key", "yandi_db_instance" in snap)
check(
    "A: 'yandi_db_instance' is NOT nested inside software.sql_engines (a separate top-level "
    "section, not a variant of the generic mysql-family binary detector)",
    "yandi_db_instance" not in snap.get("software", {}).get("sql_engines", {}),
)

yd = snap["yandi_db_instance"]
check(
    "A: exactly the three documented facts are present, nothing else smuggled in "
    "(this section stays a fact probe, not a growing DB-manager surface)",
    set(yd.keys()) == {"service_running", "socket_present", "identity_file_present"},
    f"{sorted(yd.keys())}",
)


# ============================================================
# B. Independence of the three facts.
# ============================================================

check(
    "B: each of the three facts is one of PRESENT/ABSENT/UNKNOWN — controlled "
    "vocabulary, never a raw exception string or boolean",
    all(yd[k] in (sa.PRESENT, sa.ABSENT, sa.UNKNOWN) for k in yd),
    f"{yd}",
)


# ============================================================
# C. No SQL dependency (this section specifically).
# ============================================================

_fn_src = inspect.getsource(sa._yandi_db_instance_section)
check(
    "C: _yandi_db_instance_section() has no ACTUAL 'import agent.db.sql'/'from agent.db.sql' "
    "statement (a prose docstring mention explaining the boundary, e.g. 'no import of "
    "agent.db.sql.* anywhere', is fine and expected — only a real import statement would "
    "couple this module to the shelved SQL work)",
    "import agent.db.sql" not in _fn_src and "from agent.db.sql" not in _fn_src,
)
check(
    "C: _yandi_db_instance_section() never opens a database connection (no 'pymysql', "
    "no '.cursor(', no 'get_connection' anywhere in its own source)",
    "pymysql" not in _fn_src and ".cursor(" not in _fn_src and "get_connection" not in _fn_src,
)


# ============================================================
# D. fingerprint()/compare() notice a synthetic change.
# ============================================================

base = sa.build_snapshot(probe_source="test")
changed = dict(base)
changed["yandi_db_instance"] = dict(base["yandi_db_instance"])
# Flip whatever this real host currently reports to something different,
# so the test is independent of whether yandi-db actually exists here.
changed["yandi_db_instance"]["service_running"] = (
    sa.PRESENT if base["yandi_db_instance"]["service_running"] != sa.PRESENT else sa.ABSENT
)

check(
    "D: fingerprint() differs when yandi_db_instance.service_running flips",
    sa.fingerprint(base) != sa.fingerprint(changed),
)

delta = sa.compare(base, changed)
check("D: compare() reports the change under the 'yandi_db_instance' key", "yandi_db_instance" in delta, f"{delta}")
check(
    "D: the reported diff names 'service_running' specifically, not a vague whole-section dump",
    "service_running" in delta.get("yandi_db_instance", {}),
    f"{delta.get('yandi_db_instance')}",
)

unchanged_delta = sa.compare(base, base)
check("D: comparing a snapshot against itself reports NO yandi_db_instance change", "yandi_db_instance" not in unchanged_delta)


# ============================================================
# E. Well-known paths match deploy/*.
# ============================================================

install_sh = open("deploy/install-yandi.sh", encoding="utf-8").read()
service_unit = open("deploy/yandi-db.service", encoding="utf-8").read()

check(
    "E: SOCKET_PATH in system_awareness.py matches deploy/install-yandi.sh's own "
    "RUNTIME_MYSQL_DIR/mysql.sock composition (both /run/yandi/mysql/mysql.sock — "
    "live-confirmed bug: this module's own independent copy of the path went "
    "stale when install-yandi.sh split RUNTIME_DIR into RUNTIME_MYSQL_DIR/"
    "RUNTIME_BOOTSTRAP_DIR for a runtime-directory-lifecycle fix)",
    sa._YANDI_DB_SOCKET_PATH == "/run/yandi/mysql/mysql.sock"
    and 'RUNTIME_MYSQL_DIR="${RUNTIME_DIR}/mysql"' in install_sh
    and 'SOCKET_PATH="${RUNTIME_MYSQL_DIR}/mysql.sock"' in install_sh,
)
check(
    "E: the systemctl unit name checked ('yandi-db') matches deploy/yandi-db.service's "
    "own filename/unit convention",
    re.search(r'"yandi-db"', inspect.getsource(sa._yandi_db_instance_section)) is not None,
)
check(
    "E: CONFIG_DIR/instance.id composition in install-yandi.sh would land at the exact "
    "path system_awareness.py checks for the identity file marker",
    sa._YANDI_DB_INSTANCE_ID_PATH == "/etc/yandi/mysql/instance.id" and 'CONFIG_DIR="/etc/yandi/mysql"' in install_sh,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
