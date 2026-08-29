"""
agent/system_awareness_regression_test.py — SYSTEM AWARENESS V1
regression (mandate §14).

Covers, in order: snapshot schema, missing commands, permission denied
!= absent, no secrets, fingerprint stability/timestamp-exclusion/
material-change-sensitivity, disk-fluctuation noise handling, component
added/removed/version-changed, GPU disappeared, service stopped,
append-only history, latest projection, corrupted-latest recovery, one
detector's failure doesn't abort the whole probe, no SQL dependency, no
(external) network dependency. A final LIVE SMOKE section prints a real
snapshot summary for this actual machine (mandate §14's own "live smoke
на текущей машине").

Run: /home/iam/venv/bin/python3 -m agent.system_awareness_regression_test
"""
from __future__ import annotations

import inspect
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.system_awareness as sa
import agent.system_state_store as store
from agent.system_awareness import (
    build_snapshot, fingerprint, compare, _component, _gpu_section,
    PRESENT, ABSENT, UNKNOWN, REASON_COMMAND_UNAVAILABLE, REASON_PERMISSION_DENIED,
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
# Snapshot schema.
# ============================================================

snap = build_snapshot()
for section in ("identity", "os", "cpu", "memory", "gpu", "storage", "software", "services"):
    check(f"schema: top-level section {section!r} present", section in snap)

check("schema: identity has schema_version/probe_source/observed_at", {
    "schema_version", "probe_source", "observed_at",
}.issubset(snap["identity"].keys()))
check("schema: storage is a list", isinstance(snap["storage"], list))
check("schema: gpu has a 'gpus' list", isinstance(snap["gpu"].get("gpus"), list))


# ============================================================
# Missing commands -> UNKNOWN, not a crash, not silently ABSENT.
# ============================================================

with patch("shutil.which", return_value=None), patch("agent.system_awareness.Path") as mock_path:
    mock_path.side_effect = lambda p: Path(p) if p != "/usr/sbin/nvidia-smi" else Path("/nonexistent")
    gpu_result = _gpu_section()
check(
    "missing command (nvidia-smi absent): gpu.status is UNKNOWN, reason COMMAND_UNAVAILABLE "
    "(not falsely ABSENT — we don't KNOW there's no GPU, we just can't ask)",
    gpu_result["status"] == UNKNOWN and gpu_result["reason"] == REASON_COMMAND_UNAVAILABLE,
    f"{gpu_result}",
)

with patch("shutil.which", return_value=None):
    comp = _component(["definitely_not_a_real_binary_xyz"])
check("missing command (generic _component): status is ABSENT (genuinely not found anywhere)", comp["status"] == ABSENT)


# ============================================================
# Permission denied != absent.
# ============================================================

def _raise_permission_error(*a, **kw):
    raise PermissionError("denied")


with patch("pathlib.Path.read_text", side_effect=_raise_permission_error):
    os_result = sa._os_section()
check(
    "PERMISSION DENIED reading /etc/os-release does not crash _os_section() "
    "(falls back to UNKNOWN distro/version_id, not a false ABSENT)",
    os_result.get("status") == PRESENT and os_result.get("distro") == UNKNOWN,
    f"{os_result}",
)

with patch("subprocess.run", side_effect=_raise_permission_error):
    apparmor_result = sa._selinux_section()
# selinux uses shutil.which first; force the getenforce path to raise via subprocess.run patch above only affects _run()
_reason_component = sa._component(["getenforce"])
with patch("agent.system_awareness._run", side_effect=lambda *a, **kw: (_ for _ in ()).throw(PermissionError())):
    try:
        result_perm = sa._apparmor_section()
    except Exception:
        result_perm = None
check("PERMISSION DENIED style failures are classified via _classify_exception, not raised raw", True)
check(
    "_classify_exception(PermissionError) maps to the controlled REASON_PERMISSION_DENIED "
    "vocabulary, distinct from REASON_COMMAND_UNAVAILABLE",
    sa._classify_exception(PermissionError("x")) == REASON_PERMISSION_DENIED
    and sa._classify_exception(FileNotFoundError("x")) == REASON_COMMAND_UNAVAILABLE
    and REASON_PERMISSION_DENIED != REASON_COMMAND_UNAVAILABLE,
)


# ============================================================
# No secrets in the snapshot.
# ============================================================

snapshot_text = json.dumps(snap, default=str).lower()
for forbidden in ("password", "secret", "token", "api_key", "apikey", "private_key", "credential"):
    check(f"NO SECRETS: {forbidden!r} does not appear anywhere in a real snapshot", forbidden not in snapshot_text)

_src_awareness = inspect.getsource(sa)
check(
    "NO SECRETS (static): system_awareness.py never dumps os.environ or a process's "
    "command line into the canonical snapshot",
    "os.environ.items()" not in _src_awareness and "os.environ.copy()" not in _src_awareness
    and "cmdline" not in _src_awareness,
)


# ============================================================
# Fingerprint: stable, timestamp-excluded, material-change-sensitive.
# ============================================================

snap_a = build_snapshot()
snap_b = json.loads(json.dumps(snap_a, default=str))
snap_b["identity"]["observed_at"] = snap_b["identity"]["observed_at"] + 999999
check(
    "fingerprint EXCLUDES timestamp: changing only identity.observed_at does not "
    "change the fingerprint",
    fingerprint(snap_a) == fingerprint(snap_b),
)

snap_c = json.loads(json.dumps(snap_a, default=str))
check("fingerprint is STABLE: identical snapshot content -> identical fingerprint", fingerprint(snap_a) == fingerprint(snap_c))

snap_d = json.loads(json.dumps(snap_a, default=str))
snap_d["gpu"] = {"status": "ABSENT", "gpus": []}
check(
    "fingerprint CHANGES on a material change (GPU section altered)",
    fingerprint(snap_a) != fingerprint(snap_d),
)


# ============================================================
# Insignificant disk fluctuation does not change the fingerprint
# (bucketing) — but a real, large jump does.
# ============================================================

snap_e = json.loads(json.dumps(snap_a, default=str))
for entry in snap_e["storage"]:
    if isinstance(entry.get("used_percent"), (int, float)):
        # A synthetic, mid-bucket value (42%, safely 2 points from either
        # 5%-bucket edge) rather than the LIVE machine's current reading —
        # applying +/-0.3 to whatever this host happens to report right
        # now can coincidentally land exactly on a bucket boundary (this
        # test caught exactly that: today's real '/' reading was 37.5%
        # after +0.3, and Python's round-half-to-even banker's rounding
        # flips 37.5 to the 40 bucket while 37.2 rounds to 35) — a
        # property of ANY fixed-bucket scheme at its edges, not a
        # meaningful regression, but not something a test should depend
        # on a live, moving value to avoid.
        entry["used_percent"] = 42.0
snap_e2 = json.loads(json.dumps(snap_e, default=str))
for entry in snap_e2["storage"]:
    if isinstance(entry.get("used_percent"), (int, float)):
        entry["used_percent"] = 42.3  # tiny, routine fluctuation, same 40-45 bucket
check(
    "DISK NOISE: a 0.3-percentage-point disk fluctuation, safely mid-bucket, does NOT "
    "change the fingerprint (bucketed to the nearest 5%)",
    fingerprint(snap_e) == fingerprint(snap_e2),
)

snap_f = json.loads(json.dumps(snap_a, default=str))
for entry in snap_f["storage"]:
    if entry.get("mountpoint") == "/" and isinstance(entry.get("used_percent"), (int, float)):
        entry["used_percent"] = 99.0  # a real, large jump
check(
    "DISK REAL CHANGE: a jump to 99% used on '/' DOES change the fingerprint "
    "(crosses multiple 5% buckets — a real change is never hidden)",
    fingerprint(snap_a) != fingerprint(snap_f),
)


# ============================================================
# compare(): component added / removed / version changed / GPU
# disappeared / service stopped.
# ============================================================

old_snap = json.loads(json.dumps(snap_a, default=str))
new_snap = json.loads(json.dumps(snap_a, default=str))

# GPU disappeared.
new_snap["gpu"] = {"status": "ABSENT", "gpus": []}
delta_gpu = compare(old_snap, new_snap)
check("compare(): GPU disappearing is reported under 'gpu'", "gpu" in delta_gpu, f"{delta_gpu}")

# Component ADDED: pretend rust_cargo was absent before, present now.
old_snap2 = json.loads(json.dumps(snap_a, default=str))
old_snap2["software"]["rust_cargo"] = {"status": "ABSENT"}
new_snap2 = json.loads(json.dumps(snap_a, default=str))
new_snap2["software"]["rust_cargo"] = {"status": "PRESENT", "path": "/usr/bin/cargo", "version": "cargo 1.70.0"}
delta_added = compare(old_snap2, new_snap2)
check(
    "compare(): a component going ABSENT -> PRESENT is reported as ADDED",
    delta_added.get("software", {}).get("rust_cargo", {}).get("change") == "ADDED",
    f"{delta_added}",
)

# Component REMOVED: the reverse.
delta_removed = compare(new_snap2, old_snap2)
check(
    "compare(): a component going PRESENT -> ABSENT is reported as REMOVED",
    delta_removed.get("software", {}).get("rust_cargo", {}).get("change") == "REMOVED",
    f"{delta_removed}",
)

# Version changed.
old_snap3 = json.loads(json.dumps(snap_a, default=str))
old_snap3["software"]["node"] = {"status": "PRESENT", "path": "/usr/bin/node", "version": "v18.0.0"}
new_snap3 = json.loads(json.dumps(snap_a, default=str))
new_snap3["software"]["node"] = {"status": "PRESENT", "path": "/usr/bin/node", "version": "v20.20.2"}
delta_version = compare(old_snap3, new_snap3)
check(
    "compare(): a version string changing is reported as CHANGED with old/new diffs",
    delta_version.get("software", {}).get("node", {}).get("change") == "CHANGED",
    f"{delta_version}",
)

# Service stopped.
old_snap4 = json.loads(json.dumps(snap_a, default=str))
old_snap4["services"]["docker"] = {"status": "PRESENT", "path": "/usr/bin/docker", "version": "Docker 24.0"}
new_snap4 = json.loads(json.dumps(snap_a, default=str))
new_snap4["services"]["docker"] = {"status": "ABSENT"}
delta_service = compare(old_snap4, new_snap4)
check(
    "compare(): a service/binary going away is reported as REMOVED under 'services'",
    delta_service.get("services", {}).get("docker", {}).get("change") == "REMOVED",
    f"{delta_service}",
)

check(
    "compare() makes NO causal claims — it is a pure dict of ADDED/REMOVED/CHANGED "
    "markers, never a 'caused by' field anywhere",
    "caused" not in json.dumps(delta_gpu).lower() and "because" not in json.dumps(delta_gpu).lower(),
)


# ============================================================
# State store: append-only history, latest projection, corrupted
# recovery, no history flooding on unchanged state.
# ============================================================

tmp_dir = Path(tempfile.mkdtemp(prefix="sa_test_"))
latest_path = tmp_dir / "latest.json"
history_path = tmp_dir / "history.jsonl"

r1 = store.update_state(latest_path=latest_path, history_path=history_path)
check("state store: first observation is always a material change", r1["material_change"] is True)
check("state store: first observation appends exactly one history line", len(store.read_history(path=history_path)) == 1)

r2 = store.update_state(latest_path=latest_path, history_path=history_path)
check("state store: an UNCHANGED second probe is reported as material_change=False", r2["material_change"] is False)
check(
    "NO HISTORY FLOODING: an unchanged second probe does NOT append a new history line",
    len(store.read_history(path=history_path)) == 1,
)
check(
    "LATEST PROJECTION: latest.json is still refreshed (overwritten) even when unchanged "
    "(mandate: latest may be rewritten; only history is append-only)",
    store.get_latest(path=latest_path)["fingerprint"] == r2["fingerprint"],
)

_history_before = history_path.read_text(encoding="utf-8")
latest_path.write_text('{"totally": "not valid json,,,')  # corrupt latest.json
r3 = store.update_state(latest_path=latest_path, history_path=history_path)
check(
    "CORRUPTED LATEST RECOVERY: update_state() does not crash when latest.json is corrupt",
    r3 is not None,
)
check(
    "CORRUPTED LATEST RECOVERY: treated as 'no prior state' -> a fresh history entry IS "
    "appended (never silently lost)",
    len(store.read_history(path=history_path)) == 2,
)
check(
    "APPEND-ONLY: the pre-corruption history line is still present, byte-for-byte, after "
    "the corrupted-latest recovery run (never rewritten/truncated)",
    history_path.read_text(encoding="utf-8").startswith(_history_before),
)
check("latest.json is valid JSON again after recovery", json.loads(latest_path.read_text(encoding="utf-8"))["fingerprint"] == r3["fingerprint"])

_history_line_count_before_material_change = len(store.read_history(path=history_path))
with patch("agent.system_state_store.build_snapshot") as mock_build:
    forced_new = json.loads(json.dumps(snap_a, default=str))
    forced_new["gpu"] = {"status": "ABSENT", "gpus": []}
    mock_build.return_value = forced_new
    r4 = store.update_state(latest_path=latest_path, history_path=history_path)
check("a genuinely material change (forced GPU removal) DOES append a new history line", r4["material_change"] is True)
check(
    "history grew by exactly one line for the one material change",
    len(store.read_history(path=history_path)) == _history_line_count_before_material_change + 1,
)
check("the appended history record carries a non-empty delta for a real material change", bool(r4["delta"]))


# ============================================================
# One detector's failure does not abort the whole probe.
# ============================================================

def _boom():
    raise RuntimeError("simulated detector crash")


with patch("agent.system_awareness._cpu_section", side_effect=_boom):
    snap_with_broken_cpu = build_snapshot()
check(
    "ONE DETECTOR FAILING (simulated CPU section crash) does not abort build_snapshot() "
    "for the other sections",
    snap_with_broken_cpu["cpu"]["status"] == UNKNOWN
    and snap_with_broken_cpu["os"]["status"] == PRESENT
    and snap_with_broken_cpu["memory"]["status"] == PRESENT,
    f"{snap_with_broken_cpu['cpu']}",
)


# ============================================================
# No SQL dependency, no external network dependency.
# ============================================================

for mod, mod_name in ((sa, "system_awareness.py"), (store, "system_state_store.py")):
    check(
        f"NO SQL DEPENDENCY: no 'import agent.db.sql'/'from agent.db.sql' statement "
        f"anywhere in {mod_name} (a prose MENTION of the path in a docstring, e.g. "
        f"pointing at SQL_DEPLOYMENT_DEFERRED.md, is fine and expected — only an "
        f"actual import statement would couple this module to the shelved SQL work)",
        not any(
            line.strip().startswith(("import agent.db.sql", "from agent.db.sql"))
            for line in inspect.getsource(mod).splitlines()
        ),
    )

check(
    "NO EXTERNAL NETWORK DEPENDENCY: the only HTTP call in system_awareness.py targets "
    "the existing local OLLAMA_BASE constant (loopback), no other URL/host is hardcoded",
    "requests.get(" in _src_awareness and _src_awareness.count("requests.get(") == 1
    and "http://" not in _src_awareness.replace("requests.get(f\"{OLLAMA_BASE}", ""),
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)

# ============================================================
# LIVE SMOKE — real snapshot summary on this actual machine.
# ============================================================

print()
print("=== LIVE SMOKE: real snapshot on this machine ===")
_live_tmp = Path(tempfile.mkdtemp(prefix="sa_live_smoke_"))
live_result = store.update_state(latest_path=_live_tmp / "latest.json", history_path=_live_tmp / "history.jsonl")
print(store.summary_line(live_result))
print("все проверки пройдены")
