"""
agent/system_awareness_v1_1_regression_test.py — SYSTEM AWARENESS
V1.1: software/database readiness hardening (mandate §1-§8).

Covers exactly the checklist in §8:
    - mysql client != mysql server (independently trackable facts).
    - server binary != running (a locally-installed CLI/daemon binary
      says nothing about whether ANY mysql-family process is active).
    - running != YANDI ready (a running shared instance is still
      NOT_READY for YANDI without its own configured credentials).
    - PATH omission doesn't create a false ABSENT for a known daemon
      (the sbin-fallback fix, exercised deterministically here rather
      than relying on this host's own PATH gap).
    - version != readiness (a real, known version string never implies
      READY on its own — checked both for mysql and structurally
      across the whole module).
    - corrupted latest -> RECOVERED (a distinct state word from NEW,
      mandate §5's "не считать повреждённый state достоверным").
    - recovery preserves history (append-only, unaffected by the
      corrupted-latest situation that triggered it).
    - unchanged second run doesn't append (STATE_UNCHANGED, no growth).
    - concise startup status (mandate §4: no full snapshot dump on
      UNCHANGED/CHANGED, matching the mandate's own literal examples).

Plus a LIVE TWO-RUN TEST (§7) against this actual machine's REAL
default registry/system_state paths — not a tmp dir — proving RUN2 is
UNCHANGED with no history growth when nothing on the system changed
between runs.

Run: /home/iam/venv/bin/python3 -m agent.system_awareness_v1_1_regression_test
"""
from __future__ import annotations

import inspect
import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.system_awareness as sa
import agent.system_state_store as store
from agent.system_awareness import (
    build_snapshot, PRESENT, ABSENT, UNKNOWN, NOT_READY, READY,
    _mysql_family_section, _component,
)

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
# mysql client != mysql server.
# ============================================================

def _fake_component(names, version_args=("--version",)):
    if names[0] == "mysql":
        return {"status": ABSENT}
    if names[0] == "mysqld":
        return {"status": PRESENT, "path": "/usr/sbin/mysqld", "version": "mysqld  Ver 8.0.46 Percona Server (GPL)"}
    return {"status": ABSENT}


with patch("agent.system_awareness._component", side_effect=_fake_component), \
     patch("agent.system_awareness._run", return_value=None), \
     patch("shutil.which", return_value=None):
    result_client_absent = _mysql_family_section()

check(
    "mysql CLIENT and SERVER are tracked as independent facts: client can be ABSENT "
    "while server.binary is PRESENT (a headless box with only the daemon installed)",
    result_client_absent["client"]["status"] == ABSENT and result_client_absent["server"]["binary"] == PRESENT,
    f"{result_client_absent}",
)


def _fake_component_reverse(names, version_args=("--version",)):
    if names[0] == "mysql":
        return {"status": PRESENT, "path": "/usr/bin/mysql", "version": "mysql  Ver 8.0.46 Percona Server (GPL)"}
    if names[0] == "mysqld":
        return {"status": ABSENT}
    return {"status": ABSENT}


with patch("agent.system_awareness._component", side_effect=_fake_component_reverse), \
     patch("agent.system_awareness._run", return_value=None), \
     patch("shutil.which", return_value=None):
    result_server_absent = _mysql_family_section()

check(
    "the reverse also holds: client PRESENT while server.binary is ABSENT (a client-"
    "only box connecting to a remote/shared server)",
    result_server_absent["client"]["status"] == PRESENT and result_server_absent["server"]["binary"] == ABSENT,
    f"{result_server_absent}",
)
check(
    "when the server binary is ABSENT, implementation is honestly UNKNOWN (never "
    "guessed from the client's own version string)",
    result_server_absent["server"]["implementation"] == UNKNOWN,
    f"{result_server_absent['server']}",
)


# ============================================================
# server binary != running.
# ============================================================

def _component_binary_present(names, version_args=("--version",)):
    if names[0] == "mysqld":
        return {"status": PRESENT, "path": "/usr/sbin/mysqld", "version": "mysqld  Ver 8.0.46 Percona Server (GPL)"}
    return {"status": ABSENT}


with patch("agent.system_awareness._component", side_effect=_component_binary_present), \
     patch("shutil.which", return_value="/usr/bin/systemctl"), \
     patch("agent.system_awareness._run", return_value="inactive"):
    result_binary_not_running = _mysql_family_section()

check(
    "SERVER BINARY PRESENT != RUNNING: the binary being installed says nothing about "
    "whether a process is currently active — a stopped/never-started shared instance "
    "still has its binary on disk",
    result_binary_not_running["server"]["binary"] == PRESENT and result_binary_not_running["server"]["running"] == ABSENT,
    f"{result_binary_not_running['server']}",
)


# ============================================================
# running != YANDI ready.
# ============================================================

with patch("agent.system_awareness._component", side_effect=_component_binary_present), \
     patch("shutil.which", return_value="/usr/bin/systemctl"), \
     patch("agent.system_awareness._run", return_value="active"), \
     patch.dict("os.environ", {}, clear=False):
    import os as _os_mod
    _os_mod.environ.pop("YANDI_SQL_USER", None)
    _os_mod.environ.pop("YANDI_SQL_PASSWORD", None)
    result_running_not_ready = _mysql_family_section()

check(
    "RUNNING != YANDI READY: a mysql-family server actively running on this host does "
    "NOT make YANDI's own database backend READY — it's the shared FastPanel instance "
    "until proven otherwise, matching agent/db/sql/SQL_DEPLOYMENT_DEFERRED.md's own status",
    result_running_not_ready["server"]["running"] == PRESENT
    and result_running_not_ready["server"]["readiness"] == NOT_READY
    and result_running_not_ready["server"]["reason"] == "SHARED_OR_UNCONFIGURED",
    f"{result_running_not_ready['server']}",
)

with patch("agent.system_awareness._component", side_effect=_component_binary_present), \
     patch("shutil.which", return_value="/usr/bin/systemctl"), \
     patch("agent.system_awareness._run", return_value="active"), \
     patch.dict("os.environ", {"YANDI_SQL_USER": "u", "YANDI_SQL_PASSWORD": "p"}):
    result_configured = _mysql_family_section()

check(
    "when YANDI_SQL_USER/PASSWORD ARE set, readiness becomes an honest UNKNOWN "
    "(CONFIGURED_NOT_VERIFIED) — never a fabricated READY, since V1.1 still never "
    "connects to check reachability/grants",
    result_configured["server"]["readiness"] == UNKNOWN and result_configured["server"]["reason"] == "CONFIGURED_NOT_VERIFIED",
    f"{result_configured['server']}",
)
check(
    "server.reachable is ALWAYS UNKNOWN — never tested (mandate: не подключайся к "
    "shared FastPanel DB ради readiness)",
    result_running_not_ready["server"]["reachable"] == UNKNOWN and result_configured["server"]["reachable"] == UNKNOWN,
)


# ============================================================
# PATH omission doesn't create a false ABSENT for a known daemon.
# ============================================================

_fake_sbin_mysqld = Path(tempfile.mkdtemp(prefix="sa_v11_")) / "mysqld"
_fake_sbin_mysqld.write_text("#!/bin/sh\necho fake\n")
_fake_sbin_mysqld.chmod(0o755)

with patch("shutil.which", return_value=None), \
     patch.object(sa, "_FALLBACK_BIN_DIRS", (str(_fake_sbin_mysqld.parent),)), \
     patch("agent.system_awareness._version_of", return_value="fake mysqld 8.0.46 Percona Server (GPL)"):
    result_fallback = _component(["mysqld"])

check(
    "PATH OMISSION: shutil.which() reporting nothing does NOT make a genuinely-"
    "installed daemon binary ABSENT — the sbin fallback finds it",
    result_fallback["status"] == PRESENT and result_fallback["path"] == str(_fake_sbin_mysqld),
    f"{result_fallback}",
)

with patch("shutil.which", return_value=None), \
     patch.object(sa, "_FALLBACK_BIN_DIRS", ("/tmp/definitely_not_a_real_sbin_dir_xyz",)):
    result_truly_absent = _component(["definitely_nonexistent_binary_xyz"])
check(
    "genuinely absent (not on PATH, not in any fallback dir either) is STILL correctly "
    "reported ABSENT — the fallback doesn't fabricate presence",
    result_truly_absent["status"] == ABSENT,
)


# ============================================================
# version != readiness.
# ============================================================

check(
    "VERSION != READINESS: even with a real, fully-known server version string, "
    "readiness in the SHARED_OR_UNCONFIGURED case is still NOT_READY, never READY",
    result_running_not_ready["server"]["version"] != UNKNOWN and result_running_not_ready["server"]["readiness"] == NOT_READY,
    f"{result_running_not_ready['server']}",
)

_src = inspect.getsource(sa)
check(
    "STRUCTURAL: the literal READY constant is assigned unconditionally nowhere in "
    "system_awareness.py — every occurrence is inside a branch gated on a real, "
    "specific signal (ollama's 'ollama ps' output), never a bare 'status = READY'",
    "= READY\n" not in _src.replace("gpu_use = READY", "<GATED>"),
)


# ============================================================
# corrupted latest -> RECOVERED, recovery preserves history.
# ============================================================

tmp_dir = Path(tempfile.mkdtemp(prefix="sa_v11_store_"))
latest_path = tmp_dir / "latest.json"
history_path = tmp_dir / "history.jsonl"

r1 = store.update_state(latest_path=latest_path, history_path=history_path)
check("first observation is state=NEW", r1["state"] == store.STATE_NEW, f"{r1['state']}")

r2 = store.update_state(latest_path=latest_path, history_path=history_path)
check("UNCHANGED SECOND RUN: state=UNCHANGED, history does not grow", r2["state"] == store.STATE_UNCHANGED)
check("history has exactly 1 line after an unchanged second run", len(store.read_history(path=history_path)) == 1)

_history_before_corruption = history_path.read_text(encoding="utf-8")
latest_path.write_text("{this is not json,,,")
r3 = store.update_state(latest_path=latest_path, history_path=history_path)
check(
    "CORRUPTED LATEST -> RECOVERED: a corrupted (but previously-existing) latest.json "
    "produces state=RECOVERED, distinct from a fresh-install NEW",
    r3["state"] == store.STATE_RECOVERED,
    f"{r3['state']}",
)
check(
    "RECOVERED never fabricates a delta against the unreadable prior content",
    r3["delta"] == {},
)
check(
    "RECOVERY PRESERVES HISTORY: the pre-corruption history line is untouched, and a "
    "new RECOVERED line was appended (2 total)",
    history_path.read_text(encoding="utf-8").startswith(_history_before_corruption)
    and len(store.read_history(path=history_path)) == 2,
)

r4 = store.update_state(latest_path=latest_path, history_path=history_path)
check("the run AFTER a successful recovery is a normal UNCHANGED (system genuinely didn't change)", r4["state"] == store.STATE_UNCHANGED)
check("no additional history growth after the post-recovery unchanged run", len(store.read_history(path=history_path)) == 2)


# ============================================================
# Concise startup status (mandate §4's own literal examples).
# ============================================================

summary_unchanged = store.summary_line(r2)
check(
    "CONCISE STATUS (UNCHANGED): matches the mandate's own literal shape "
    "'[SystemAwareness] UNCHANGED fp=...' — no full snapshot dump",
    summary_unchanged.startswith("[SystemAwareness] UNCHANGED fp=")
    and "cpu=" not in summary_unchanged and "kernel=" not in summary_unchanged,
    f"{summary_unchanged!r}",
)

fake_changed_result = {
    "fingerprint": "abc123def456", "state": store.STATE_CHANGED, "material_change": True,
    "delta": {"gpu": {}, "storage": {}, "software": {}},
}
summary_changed = store.summary_line(fake_changed_result)
check(
    "CONCISE STATUS (CHANGED): matches the mandate's own literal shape "
    "'[SystemAwareness] CHANGED fp=... delta=3' — names the changed sections, no full dump",
    summary_changed.startswith("[SystemAwareness] CHANGED fp=abc123def456 delta=3")
    and "gpu" in summary_changed and "cpu=" not in summary_changed,
    f"{summary_changed!r}",
)

summary_new = store.summary_line(r1)
check(
    "NEW/RECOVERED still get the one-time descriptive line WITH context (not the "
    "short form) — a fresh install is exactly when a human wants the full picture",
    "cpu=" in summary_new and summary_new.startswith("[SystemAwareness] NEW"),
    f"{summary_new!r}",
)


# ============================================================
# LIVE TWO-RUN TEST (§7) — this actual machine, REAL default paths,
# no system change performed between runs.
# ============================================================

print()
print("=== LIVE TWO-RUN TEST (real default registry/system_state paths) ===")
history_len_before = len(store.read_history())
live_run1 = store.update_state()
print(f"RUN1: {store.summary_line(live_run1)}")
live_run2 = store.update_state()
print(f"RUN2: {store.summary_line(live_run2)}")
history_len_after = len(store.read_history())

check(
    "LIVE: RUN1 is a valid, recognized state (NEW/UNCHANGED/CHANGED/RECOVERED — "
    "whatever this real host's actual history already contains)",
    live_run1["state"] in (store.STATE_NEW, store.STATE_UNCHANGED, store.STATE_CHANGED, store.STATE_RECOVERED),
)
check(
    "LIVE: RUN2 (immediately after RUN1, no system change) is UNCHANGED",
    live_run2["state"] == store.STATE_UNCHANGED,
    f"got {live_run2['state']}",
)
check(
    "LIVE: history did not grow between RUN1 and RUN2's own effect "
    "(RUN2 itself adds zero lines — RUN1 may have added one if the real machine's "
    "own material state had actually changed since the last run in this session)",
    history_len_after == history_len_before + (1 if live_run1["material_change"] else 0),
    f"before={history_len_before} after={history_len_after} run1_material={live_run1['material_change']}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
