"""
agent/system_awareness.py — SYSTEM AWARENESS V1: OBSERVE -> NORMALIZE.

AUDIT this module is based on (no second/third hardware detector
written where one already existed):

    - agent/tools/tool_system.py already exposes os_info()/memory()/
      disk()/cpu()/services() as LLM-callable AGENT TOOLS (registered
      in agent/tools/__init__.py as "system.*") for AD-HOC diagnostic
      QUERIES during a conversation ("what's my disk usage?"). That is
      a DIFFERENT layer from this module: on-demand, human-facing,
      pre-rounded (GB to 1 decimal) output — not a persisted, byte-
      precise, fingerprintable snapshot. This module reuses tool_
      system.py's OWN technique (try psutil, fall back to /proc
      parsing) rather than inventing a third one, and calls tool_
      system.services() directly, unchanged, for the services section
      (identical need, no reason to reimplement).
    - node/src/util/sysmon.rs is the Rust NODE's "hardware detector"
      the audit was asked to find — it computes a `NodePower` (Low/
      Medium/High) classification from CPU core count + RAM total,
      purely to tune P2P network capacity (max connections, bandwidth,
      timeouts). It has NO GPU/disk/software/service detection, is
      never persisted to disk, and solves a genuinely different
      problem (network capacity, not general system awareness). V1
      does NOT bridge Rust<->Python for this — see the module docstring
      of agent/system_state_store.py for the `probe_source` field this
      leaves room for ("node_probe", not built this pass).
    - No existing GPU/nvidia-smi/CUDA detection exists anywhere in
      Python (grepped exhaustively — every "GPU" match in agent/ was
      prose/comments, not code).
    - No existing systemd/AppArmor/SELinux/Docker presence detection
      exists in Python (only shell-command ALLOW-LISTS in tool_shell.py/
      policy.py, which is a different concern — permitting a command,
      not detecting a component).
    - agent.orch_config.OLLAMA_BASE is the existing canonical Ollama
      base URL constant — reused here, not a second hardcoded
      "127.0.0.1:11434".

SCOPE (mandate, repeated so it isn't silently expanded later):
    NO apt install, NO OS changes, NO sudo, NO service management, NO
    auto-repair, NO SQL dependency (agent/db/sql/* is NOT imported
    anywhere in this module — SQL Bastion work is shelved, see
    agent/db/sql/SQL_DEPLOYMENT_DEFERRED.md, and this module does not
    reopen it), NO installer development. GPU detection is NVIDIA-only
    in V1 (nvidia-smi) — AMD/Intel GPU detection is out of scope,
    reported as UNKNOWN/COMMAND_UNAVAILABLE, not guessed. "network/
    listening ports" (mentioned in the audit instruction) is
    deliberately NOT part of the V1 snapshot schema below — the
    mandate's own §3 schema doesn't list a network section, and
    capturing listening-port data is exactly the kind of scope
    expansion mandate §9 (privacy) warns against; left as a documented
    V2 candidate, not built here.

FACT vs CAPABILITY vs READINESS (mandate §4) — this module produces
FACTS and, where genuinely cheap and safe, CAPABILITY inferences. It
deliberately does NOT compute READINESS judgments that would require
inspecting another subsystem's actual behavior (e.g. "is Ollama
actually using the GPU for inference right now") — those need live,
task-specific evidence this module has no business fabricating from a
binary's mere presence. Where the mandate's own examples ask for this
distinction, the returned dict says so explicitly (see `ollama.gpu_use`
below, which is UNKNOWN unless `ollama ps` currently shows a loaded
model's processor split).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

# ── Status vocabulary (mandate §5) ──────────────────────────────────────
PRESENT = "PRESENT"
ABSENT = "ABSENT"
UNKNOWN = "UNKNOWN"
RUNNING = "RUNNING"
REACHABLE = "REACHABLE"
NOT_REACHABLE = "NOT_REACHABLE"
COMPATIBLE = "COMPATIBLE"
READY = "READY"
NOT_READY = "NOT_READY"

# Controlled vocabulary for `reason` when status isn't a plain success —
# never a raw exception message/traceback in the canonical snapshot
# (mandate §10: no secret traceback payload).
REASON_COMMAND_UNAVAILABLE = "COMMAND_UNAVAILABLE"
REASON_PERMISSION_DENIED = "PERMISSION_DENIED"
REASON_TIMEOUT = "TIMEOUT"
REASON_PARSE_ERROR = "PARSE_ERROR"
REASON_DRIVER_NOT_LOADED = "DRIVER_NOT_LOADED"
REASON_UNREACHABLE = "UNREACHABLE"
REASON_UNEXPECTED_ERROR = "UNEXPECTED_ERROR"

_SUBPROCESS_TIMEOUT_S = 3.0
_HTTP_TIMEOUT_S = 2.0


def _classify_exception(exc: Exception) -> str:
    """Maps a real exception to a controlled-vocabulary reason string —
    never returns str(exc) or a traceback (mandate §10)."""
    if isinstance(exc, FileNotFoundError):
        return REASON_COMMAND_UNAVAILABLE
    if isinstance(exc, PermissionError):
        return REASON_PERMISSION_DENIED
    if isinstance(exc, subprocess.TimeoutExpired):
        return REASON_TIMEOUT
    return REASON_UNEXPECTED_ERROR


def _run(cmd: List[str], timeout: float = _SUBPROCESS_TIMEOUT_S) -> Optional[str]:
    """Runs a read-only command, returns stripped stdout on success, None
    on any failure — never raises, never leaks a traceback into a
    caller's return dict (mandate §10: one detector failing must not
    abort the whole probe, and must not leak diagnostics beyond a
    controlled reason code)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception:
        return None


def _run_with_reason(cmd: List[str], timeout: float = _SUBPROCESS_TIMEOUT_S):
    """Like _run() but returns (stdout_or_None, reason_or_None) so a
    caller can distinguish COMMAND_UNAVAILABLE from a non-zero exit."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None, (result.stdout + result.stderr).strip()[:200] or None
        return result.stdout.strip(), None
    except Exception as e:
        return None, _classify_exception(e)


# ============================================================
# identity
# ============================================================

def _identity_section(probe_source: str) -> Dict[str, Any]:
    machine_id = None
    try:
        machine_id = Path("/etc/machine-id").read_text().strip() or None
    except Exception:
        pass  # absent/unreadable -> stays None, not an error worth surfacing here

    return {
        "hostname": platform.node(),
        "machine_id": machine_id,
        "probe_source": probe_source,
        "schema_version": SCHEMA_VERSION,
        "observed_at": time.time(),
    }


# ============================================================
# os
# ============================================================

_DESKTOP_MANAGER_SERVICES = ("gdm", "gdm3", "lightdm", "sddm", "xdm")


def _detect_desktop_or_headless() -> Dict[str, Any]:
    """Conservative: only claims "desktop" when a real, checkable signal
    exists (this process's own DISPLAY/WAYLAND_DISPLAY env, or a display-
    manager service reported active by systemd) — never a guess from
    something weaker. UNKNOWN when neither signal is available (mandate
    §5: unknown is normal)."""
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return {"value": "desktop", "basis": "DISPLAY_ENV"}

    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {"value": UNKNOWN, "basis": None, "reason": REASON_COMMAND_UNAVAILABLE}

    for svc in _DESKTOP_MANAGER_SERVICES:
        out = _run([systemctl, "is-active", svc], timeout=2.0)
        if out == "active":
            return {"value": "desktop", "basis": f"SERVICE:{svc}"}

    return {"value": "headless", "basis": "NO_DISPLAY_ENV_NO_DM_SERVICE"}


def _os_section() -> Dict[str, Any]:
    try:
        distro_pretty = None
        version_id = None
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    distro_pretty = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("VERSION_ID="):
                    version_id = line.split("=", 1)[1].strip().strip('"')
        except Exception:
            pass

        desktop_info = _detect_desktop_or_headless()

        return {
            "status": PRESENT,
            "distro": distro_pretty or UNKNOWN,
            "version_id": version_id or UNKNOWN,
            "kernel": platform.release(),
            "arch": platform.machine(),
            "desktop_or_headless": desktop_info["value"],
        }
    except Exception as e:
        return {"status": UNKNOWN, "reason": _classify_exception(e)}


# ============================================================
# cpu
# ============================================================

def _physical_cores() -> Optional[int]:
    try:
        import psutil
        return psutil.cpu_count(logical=False)
    except ImportError:
        pass
    try:
        text = Path("/proc/cpuinfo").read_text()
        for line in text.splitlines():
            if line.startswith("cpu cores"):
                return int(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return None


def _cpu_section() -> Dict[str, Any]:
    try:
        model = UNKNOWN
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass

        logical = os.cpu_count()
        physical = _physical_cores()

        return {
            "status": PRESENT,
            "model": model,
            "physical_cores": physical if physical is not None else UNKNOWN,
            "logical_cores": logical if logical is not None else UNKNOWN,
        }
    except Exception as e:
        return {"status": UNKNOWN, "reason": _classify_exception(e)}


# ============================================================
# memory
# ============================================================

def _memory_section() -> Dict[str, Any]:
    try:
        try:
            import psutil
            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            return {
                "status": PRESENT,
                "total_bytes": int(vm.total),
                "available_bytes": int(vm.available),
                "swap_total_bytes": int(sw.total),
                "swap_available_bytes": int(sw.free),
            }
        except ImportError:
            pass

        raw = Path("/proc/meminfo").read_text()
        d: Dict[str, int] = {}
        for line in raw.splitlines():
            key, _, val = line.partition(":")
            parts = val.strip().split()
            if parts and parts[0].isdigit():
                d[key.strip()] = int(parts[0]) * 1024  # kB -> bytes

        return {
            "status": PRESENT,
            "total_bytes": d.get("MemTotal", 0),
            "available_bytes": d.get("MemAvailable", d.get("MemFree", 0)),
            "swap_total_bytes": d.get("SwapTotal", 0),
            "swap_available_bytes": d.get("SwapFree", 0),
        }
    except Exception as e:
        return {"status": UNKNOWN, "reason": _classify_exception(e)}


# ============================================================
# gpu[] — NVIDIA only in V1 (mandate: no scope explosion; AMD/Intel
# GPU detection is a documented V2 gap, not silently claimed absent).
# ============================================================

def _gpu_section() -> Dict[str, Any]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return {
            "status": UNKNOWN,
            "reason": REASON_COMMAND_UNAVAILABLE,
            "note": "nvidia-smi not found — NVIDIA GPU presence genuinely unknown; "
                    "non-NVIDIA GPU detection is out of V1 scope",
            "gpus": [],
        }

    out, reason = _run_with_reason([
        nvidia_smi, "--query-gpu=name,driver_version,memory.total,memory.used,compute_cap",
        "--format=csv,noheader,nounits",
    ], timeout=5.0)

    if out is None:
        # nvidia-smi EXISTS but failed — the classic case is "driver
        # installed but kernel module not loaded" (e.g. after a kernel
        # update, before reboot). Distinguished from COMMAND_UNAVAILABLE
        # on purpose: this is stronger evidence NVIDIA hardware exists.
        if reason and "couldn't communicate" in reason.lower():
            return {"status": UNKNOWN, "reason": REASON_DRIVER_NOT_LOADED, "gpus": []}
        return {"status": UNKNOWN, "reason": REASON_UNEXPECTED_ERROR, "gpus": []}

    if not out:
        return {"status": ABSENT, "gpus": []}

    gpus = []
    try:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            name, driver_version, vram_total_mib, vram_used_mib, compute_cap = parts[:5]
            gpus.append({
                "vendor": "NVIDIA",
                "model": name,
                "vram_total_bytes": int(float(vram_total_mib)) * 1024 * 1024 if vram_total_mib.replace(".", "", 1).isdigit() else UNKNOWN,
                "driver_installed": True,
                "driver_loaded": True,
                "driver_version": driver_version,
                "compute_capability": compute_cap if compute_cap and compute_cap != "[N/A]" else UNKNOWN,
            })
    except Exception:
        return {"status": UNKNOWN, "reason": REASON_PARSE_ERROR, "gpus": []}

    return {"status": PRESENT if gpus else ABSENT, "gpus": gpus}


# ============================================================
# storage[]
# ============================================================

_PSEUDO_FS_TYPES = {
    "proc", "sysfs", "cgroup", "cgroup2", "devtmpfs", "devpts", "tmpfs",
    "securityfs", "pstore", "bpf", "autofs", "mqueue", "hugetlbfs",
    "debugfs", "tracefs", "configfs", "fusectl", "binfmt_misc", "efivarfs",
    "overlay",
}


def _storage_section() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    try:
        mounts_text = Path("/proc/mounts").read_text()
    except Exception as e:
        return [{"status": UNKNOWN, "reason": _classify_exception(e)}]

    seen_mountpoints = set()
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mountpoint, fstype = fields[0], fields[1], fields[2]
        options = fields[3] if len(fields) > 3 else ""

        if fstype in _PSEUDO_FS_TYPES:
            continue
        if not device.startswith("/dev/") and fstype != "zfs":
            continue
        if mountpoint in seen_mountpoints:
            continue
        seen_mountpoints.add(mountpoint)

        entry: Dict[str, Any] = {
            "device": device, "mountpoint": mountpoint, "filesystem": fstype,
            "readonly": "ro" in options.split(","),
        }
        try:
            st = os.statvfs(mountpoint)
            total = st.f_frsize * st.f_blocks
            free = st.f_frsize * st.f_bfree
            available = st.f_frsize * st.f_bavail
            entry.update({
                "status": PRESENT,
                "total_bytes": total,
                "free_bytes": free,
                "available_bytes": available,
                "used_percent": round((total - free) / total * 100, 1) if total else 0.0,
                "inode_total": st.f_files or UNKNOWN,
                "inode_free": st.f_ffree or UNKNOWN,
            })
        except Exception as e:
            entry.update({"status": UNKNOWN, "reason": _classify_exception(e)})
        entries.append(entry)

    return entries


# ============================================================
# software/components
# ============================================================

def _version_of(binary: str, args: List[str] = ("--version",)) -> Optional[str]:
    out = _run([binary, *args], timeout=2.0)
    if not out:
        return None
    return out.splitlines()[0][:200]


# Server-side daemon binaries (mysqld, etc.) conventionally live in
# sbin directories that are NOT on a normal (non-root) user's PATH —
# `shutil.which()` alone reports a real, installed binary as ABSENT in
# that case, a false negative this codebase already had direct proof
# of this session (agent/db/sql/* work confirmed /usr/sbin/mysqld
# exists on this exact host while `which mysqld` finds nothing for the
# `iam` user). Checked as a fallback, never instead of PATH.
_FALLBACK_BIN_DIRS = ("/usr/sbin", "/usr/local/sbin", "/sbin")


def _component(binary_names: List[str], version_args: List[str] = ("--version",)) -> Dict[str, Any]:
    for name in binary_names:
        path = shutil.which(name)
        if not path:
            for bin_dir in _FALLBACK_BIN_DIRS:
                candidate = Path(bin_dir) / name
                if candidate.exists() and os.access(candidate, os.X_OK):
                    path = str(candidate)
                    break
        if path:
            version = _version_of(path, list(version_args))
            return {"status": PRESENT, "path": path, "version": version or UNKNOWN}
    return {"status": ABSENT}


def _ollama_section() -> Dict[str, Any]:
    from agent.orch_config import OLLAMA_BASE  # reuse the existing canonical constant

    binary = _component(["ollama"])
    result: Dict[str, Any] = {"binary": binary}

    try:
        import requests
        resp = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=_HTTP_TIMEOUT_S)
        if resp.status_code == 200:
            models = [m.get("name") for m in resp.json().get("models", []) if m.get("name")]
            result["api_status"] = REACHABLE
            result["models"] = models
        else:
            result["api_status"] = NOT_REACHABLE
    except Exception:
        result["api_status"] = NOT_REACHABLE

    # GPU-use readiness: only ever asserted when `ollama ps` gives a
    # concrete, current answer (a loaded model's processor split) —
    # UNKNOWN otherwise, never inferred from binary/GPU presence alone
    # (mandate §4's own explicit "не делай вывод READY только по факту
    # наличия бинарника").
    gpu_use = UNKNOWN
    if binary["status"] == PRESENT:
        ps_out = _run([binary["path"], "ps"], timeout=3.0)
        if ps_out:
            lower = ps_out.lower()
            if "100% gpu" in lower:
                gpu_use = READY
            elif "gpu" in lower and "cpu" in lower:
                gpu_use = "PARTIAL"
            elif "100% cpu" in lower:
                gpu_use = NOT_READY
    result["gpu_use"] = gpu_use

    return result


def _classify_mysql_implementation(version_text: Optional[str]) -> str:
    """CLIENT and SERVER binaries of the mysql family are shipped by
    three different projects that all answer to similar names — a
    version string is the only cheap way to tell them apart, and V1.1's
    own audit found `mysql --version`/`mysqld --version` already
    include the vendor name in plain text (e.g. "... Percona Server
    (GPL) ...") on this exact host."""
    if not version_text or version_text == UNKNOWN:
        return UNKNOWN
    lower = version_text.lower()
    if "percona" in lower:
        return "PERCONA"
    if "mariadb" in lower:
        return "MARIADB"
    if "mysql" in lower:
        return "MYSQL"
    return UNKNOWN


def _mysql_family_section() -> Dict[str, Any]:
    """
    V1.1 fix (mandate: "убедись, что detection НЕ смешивает: mysql
    client installed / mysqld server binary installed / Percona package
    installed / server process running / server reachable / server
    usable by YANDI — это разные факты"). Each is now its own field,
    never collapsed into one status:

        client.status         — the `mysql` CLI binary, independent of
                                 any server.
        server.binary          — the `mysqld` binary (V1's sbin-
                                 fallback fix, unchanged).
        server.implementation  — PERCONA/MYSQL/MARIADB/UNKNOWN, parsed
                                 from the server binary's OWN version
                                 string, never guessed from the client's.
        server.running         — is SOME mysql-family systemd service
                                 active right now (`systemctl is-active
                                 mysql`) — a real signal, but says
                                 nothing about WHICH instance (this
                                 codebase's shared FastPanel one is the
                                 only one that has ever run on this
                                 host — see agent/db/sql/
                                 SQL_DEPLOYMENT_DEFERRED.md).
        server.reachable       — ALWAYS UNKNOWN in V1.1, on purpose:
                                 determining this would require actually
                                 opening a connection, which is exactly
                                 the "не подключайся к shared FastPanel
                                 DB ради readiness" the mandate forbids.
        server.readiness       — YANDI's OWN usability judgment, never
                                 inferred from binary/running alone.
        server.reason          — controlled vocabulary explaining the
                                 readiness verdict.

    `yandi_sql_configured` below checks the SAME two env var names
    agent.db.sql.connection.is_configured() checks — re-checked
    directly here (NOT by importing agent.db.sql, preserving the
    module's own "no SQL dependency" boundary from V1) because it is
    the one fact that distinguishes "some mysql server exists on this
    host" from "YANDI itself has been given credentials for one" —
    without it, "server running" would be misread as "YANDI's database
    is ready", exactly the FACT/READINESS conflation this task exists
    to fix.
    """
    client = _component(["mysql"])
    server_binary = _component(["mysqld"], version_args=["--version"])

    server_version = server_binary.get("version") if server_binary.get("status") == PRESENT else None
    implementation = _classify_mysql_implementation(server_version)

    running = UNKNOWN
    systemctl = shutil.which("systemctl")
    if systemctl:
        svc_state = _run([systemctl, "is-active", "mysql"], timeout=2.0)
        if svc_state == "active":
            running = PRESENT
        elif svc_state in ("inactive", "failed", "unknown", "activating", "deactivating", ""):
            running = ABSENT
        # any other/no output (e.g. systemctl itself errored) -> stays UNKNOWN

    reachable = UNKNOWN  # deliberately never tested, see docstring

    yandi_sql_configured = bool(os.environ.get("YANDI_SQL_USER")) and bool(os.environ.get("YANDI_SQL_PASSWORD"))

    if yandi_sql_configured:
        # Configured != verified — V1.1 still does not connect to check
        # this, so it stays an honest UNKNOWN rather than a fabricated READY.
        readiness, reason = UNKNOWN, "CONFIGURED_NOT_VERIFIED"
    elif running == PRESENT:
        readiness, reason = NOT_READY, "SHARED_OR_UNCONFIGURED"
    else:
        readiness, reason = NOT_READY, "NOT_CONFIGURED"

    return {
        "client": client,
        "server": {
            "binary": server_binary.get("status", ABSENT),
            "implementation": implementation,
            "version": server_version or UNKNOWN,
            "running": running,
            "reachable": reachable,
            "readiness": readiness,
            "reason": reason,
        },
    }


def _sql_engines_section() -> Dict[str, Any]:
    """Presence/version of SQL CLIENT/SERVER binaries ONLY — no
    connection attempt, no import of agent.db.sql.* anywhere (mandate:
    "SYSTEM STATE V1 НЕ ЗАВИСИТ ОТ SQL", and 5E-S/5E-S2 stay shelved,
    see agent/db/sql/SQL_DEPLOYMENT_DEFERRED.md). This is the FACT
    layer only (mandate §4's own worked example: "Percona package
    8.0.46 installed" is a FACT, not a READINESS claim about YANDI's
    own database backend) — except `mysql.server.readiness`, which IS
    a deliberate, narrow READINESS judgment (see _mysql_family_section()
    docstring for why that one field earns the exception)."""
    return {
        "mysql": _mysql_family_section(),
        "postgresql_client": _component(["psql"]),
        "sqlite3": _component(["sqlite3"]),
    }


def _software_section() -> Dict[str, Any]:
    return {
        "python": {
            "status": PRESENT,
            "version": platform.python_version(),
            "executable": sys.executable,
            "in_virtualenv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
        },
        "pip": _component(["pip", "pip3"]),
        "rust_cargo": _component(["cargo"]),
        "rustc": _component(["rustc"]),
        "node": _component(["node"]),
        "npm": _component(["npm"]),
        "ollama": _ollama_section(),
        "sql_engines": _sql_engines_section(),
    }


# ============================================================
# services/capabilities
# ============================================================

def _apparmor_section() -> Dict[str, Any]:
    try:
        enabled_flag = Path("/sys/module/apparmor/parameters/enabled").read_text().strip()
        return {"status": PRESENT if enabled_flag == "Y" else "DISABLED"}
    except FileNotFoundError:
        return {"status": ABSENT}
    except Exception as e:
        return {"status": UNKNOWN, "reason": _classify_exception(e)}


def _selinux_section() -> Dict[str, Any]:
    getenforce = shutil.which("getenforce")
    if not getenforce:
        if Path("/sys/fs/selinux").exists():
            return {"status": PRESENT, "mode": UNKNOWN}
        return {"status": ABSENT}
    out = _run([getenforce], timeout=2.0)
    if out is None:
        return {"status": UNKNOWN, "reason": REASON_UNEXPECTED_ERROR}
    return {"status": PRESENT, "mode": out}


def _services_section() -> Dict[str, Any]:
    from agent.tools.tool_system import services as tool_system_services  # reuse verbatim

    systemd = _component(["systemctl"], version_args=["--version"])
    docker = _component(["docker"])  # presence only — never a daemon-dependent call (mandate §11: cheap probe only)

    relevant = tool_system_services(["redis", "ollama", "nginx", "mysql", "postgresql", "docker"])

    return {
        "systemd": systemd,
        "apparmor": _apparmor_section(),
        "selinux": _selinux_section(),
        "docker": docker,
        "running_services": relevant,
    }


# ============================================================
# Snapshot assembly
# ============================================================

def build_snapshot(probe_source: str = "agent_local_probe") -> Dict[str, Any]:
    """Assembles the full SystemSnapshot (mandate §3). Each section is
    independently wrapped so one detector's failure never aborts the
    whole probe (mandate §10) — a section that itself raises becomes
    {"status": UNKNOWN, "reason": UNEXPECTED_ERROR} rather than
    propagating."""
    def _safe(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return {"status": UNKNOWN, "reason": _classify_exception(e)}

    return {
        "identity": _safe(_identity_section, probe_source),
        "os": _safe(_os_section),
        "cpu": _safe(_cpu_section),
        "memory": _safe(_memory_section),
        "gpu": _safe(_gpu_section),
        "storage": _safe(_storage_section),
        "software": _safe(_software_section),
        "services": _safe(_services_section),
    }


# ============================================================
# Fingerprinting (mandate §6) — deterministic, timestamp-excluded.
# ============================================================

def _bucket_percent(value: float, bucket_size: int = 5) -> int:
    """Rounds a percentage to the nearest `bucket_size` — the disk-
    space-noise control mandate §7 asks for: a used_percent value that
    fluctuates by a few tenths of a percent between polls does not
    change its 5%-bucket, so it does not change the fingerprint; a
    used_percent that crosses a bucket boundary (a REAL, material
    change) does."""
    return int(round(value / bucket_size) * bucket_size)


def _component_view(c: Dict[str, Any]) -> Dict[str, Any]:
    """Shared by _material_view() and _sql_engines_material_view() —
    reduces any component dict to just its {status, version}, the two
    fields that matter for materiality (mandate §3: FACT != READINESS
    — a component's `path`/other diagnostic fields never participate
    in the fingerprint)."""
    if not isinstance(c, dict):
        return {"status": UNKNOWN}
    return {"status": c.get("status"), "version": c.get("version")}


def _sql_engines_material_view(sql_engines: Dict[str, Any]) -> Dict[str, Any]:
    """V1.1: `sql_engines.mysql` is a nested {client, server} structure,
    not a flat component dict — a plain _component_view() call on it
    would silently produce {"status": None, "version": None} (mandate
    §3's own "version != readiness" caution applies here too: getting
    this wrong wouldn't crash, it would just quietly stop tracking
    mysql-family changes in the fingerprint/delta, a worse failure mode
    than a loud one). `server.reachable` is deliberately excluded —
    it is ALWAYS UNKNOWN by design (never tested), so including it
    would add a field with zero variance, not a real material signal."""
    out: Dict[str, Any] = {}
    for name, value in sql_engines.items():
        if name == "mysql" and isinstance(value, dict):
            client = value.get("client", {})
            server = value.get("server", {})
            out["mysql"] = {
                "client": _component_view(client),
                "server_binary": server.get("binary"),
                "server_implementation": server.get("implementation"),
                "server_version": server.get("version"),
                "server_running": server.get("running"),
                "server_readiness": server.get("readiness"),
            }
        else:
            out[name] = _component_view(value if isinstance(value, dict) else {})
    return out


def _material_view(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Strips volatile, non-material fields (mandate §6: "Не включай
    timestamp в fingerprint", §7: bucket disk usage) before hashing.
    Deliberately keeps this a PURE FUNCTION of the snapshot — no I/O,
    fully unit-testable."""
    ident = snapshot.get("identity", {})
    os_ = snapshot.get("os", {})
    cpu = snapshot.get("cpu", {})
    mem = snapshot.get("memory", {})
    gpu = snapshot.get("gpu", {})
    storage = snapshot.get("storage", [])
    software = snapshot.get("software", {})
    services = snapshot.get("services", {})

    material_storage = []
    for entry in storage:
        if not isinstance(entry, dict) or "mountpoint" not in entry:
            continue
        material_storage.append({
            "device": entry.get("device"),
            "mountpoint": entry.get("mountpoint"),
            "filesystem": entry.get("filesystem"),
            "readonly": entry.get("readonly"),
            "used_percent_bucket": (
                _bucket_percent(entry["used_percent"]) if isinstance(entry.get("used_percent"), (int, float)) else None
            ),
        })

    return {
        "schema_version": ident.get("schema_version"),
        "os": {
            "distro": os_.get("distro"), "version_id": os_.get("version_id"),
            "kernel": os_.get("kernel"), "arch": os_.get("arch"),
        },
        "cpu": {"model": cpu.get("model"), "physical_cores": cpu.get("physical_cores")},
        "memory_total_bytes": mem.get("total_bytes"),
        "gpu": {
            "status": gpu.get("status"),
            "gpus": [
                {"vendor": g.get("vendor"), "model": g.get("model"), "driver_version": g.get("driver_version")}
                for g in gpu.get("gpus", []) if isinstance(g, dict)
            ],
        },
        "storage": sorted(material_storage, key=lambda e: e.get("mountpoint") or ""),
        "software": {
            "python_version": software.get("python", {}).get("version"),
            "rust_cargo": _component_view(software.get("rust_cargo", {})),
            "node": _component_view(software.get("node", {})),
            "ollama_binary": _component_view(software.get("ollama", {}).get("binary", {})),
            "sql_engines": _sql_engines_material_view(software.get("sql_engines") or {}),
        },
        "services": {
            "systemd": _component_view(services.get("systemd", {})),
            "apparmor": {"status": services.get("apparmor", {}).get("status")},
            "selinux": {"status": services.get("selinux", {}).get("status")},
            "docker": _component_view(services.get("docker", {})),
        },
    }


def fingerprint(snapshot: Dict[str, Any]) -> str:
    """SHA-256 over a deterministic (sorted-key, fixed-separator) JSON
    serialization of the MATERIAL view — same snapshot content always
    produces the same fingerprint; timestamp/observed_at never
    participates."""
    material = _material_view(snapshot)
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================
# Delta / change detection (mandate §8) — no causal inference, just
# ADDED/REMOVED/CHANGED/UNCHANGED.
# ============================================================

def _diff_scalar(old_val, new_val) -> Optional[Dict[str, Any]]:
    if old_val == new_val:
        return None
    return {"change": "CHANGED", "old": old_val, "new": new_val}


def _diff_component_dict(old: Dict[str, Any], new: Dict[str, Any], keys=("status", "version")) -> Optional[Dict[str, Any]]:
    if not isinstance(old, dict):
        old = {}
    if not isinstance(new, dict):
        new = {}
    old_status = old.get("status")
    new_status = new.get("status")
    if old_status in (None, ABSENT, UNKNOWN) and new_status == PRESENT:
        return {"change": "ADDED", "new": {k: new.get(k) for k in keys}}
    if old_status == PRESENT and new_status in (ABSENT, UNKNOWN):
        return {"change": "REMOVED", "old": {k: old.get(k) for k in keys}}
    diffs = {k: (old.get(k), new.get(k)) for k in keys if old.get(k) != new.get(k)}
    if diffs:
        return {"change": "CHANGED", "diffs": diffs}
    return None


def compare(old_snapshot: Dict[str, Any], new_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Structural comparison — ADDED/REMOVED/CHANGED/UNCHANGED per
    section, never a causal claim (mandate §8: "kernel changed before
    GPU failure" != "kernel caused GPU failure" — this function
    produces only the first half of that sentence)."""
    result: Dict[str, Any] = {}

    old_mat = _material_view(old_snapshot)
    new_mat = _material_view(new_snapshot)

    for field in ("os",):
        d = {}
        for sub in old_mat[field].keys() | new_mat[field].keys():
            diff = _diff_scalar(old_mat[field].get(sub), new_mat[field].get(sub))
            if diff:
                d[sub] = diff
        if d:
            result["os"] = d

    cpu_diff = {}
    for sub in ("model", "physical_cores"):
        diff = _diff_scalar(old_mat["cpu"].get(sub), new_mat["cpu"].get(sub))
        if diff:
            cpu_diff[sub] = diff
    if cpu_diff:
        result["cpu"] = cpu_diff

    mem_diff = _diff_scalar(old_mat.get("memory_total_bytes"), new_mat.get("memory_total_bytes"))
    if mem_diff:
        result["memory_total_bytes"] = mem_diff

    if old_mat["gpu"]["status"] != new_mat["gpu"]["status"]:
        result.setdefault("gpu", {})["status"] = {
            "change": "CHANGED", "old": old_mat["gpu"]["status"], "new": new_mat["gpu"]["status"],
        }
    old_gpus = {(g.get("vendor"), g.get("model")): g for g in old_mat["gpu"]["gpus"]}
    new_gpus = {(g.get("vendor"), g.get("model")): g for g in new_mat["gpu"]["gpus"]}
    gpu_items = {}
    for key in old_gpus.keys() - new_gpus.keys():
        gpu_items[str(key)] = {"change": "REMOVED", "old": old_gpus[key]}
    for key in new_gpus.keys() - old_gpus.keys():
        gpu_items[str(key)] = {"change": "ADDED", "new": new_gpus[key]}
    for key in old_gpus.keys() & new_gpus.keys():
        if old_gpus[key] != new_gpus[key]:
            gpu_items[str(key)] = {"change": "CHANGED", "old": old_gpus[key], "new": new_gpus[key]}
    if gpu_items:
        result.setdefault("gpu", {})["devices"] = gpu_items

    old_storage = {e["mountpoint"]: e for e in old_mat["storage"]}
    new_storage = {e["mountpoint"]: e for e in new_mat["storage"]}
    storage_diff = {}
    for mp in old_storage.keys() - new_storage.keys():
        storage_diff[mp] = {"change": "REMOVED", "old": old_storage[mp]}
    for mp in new_storage.keys() - old_storage.keys():
        storage_diff[mp] = {"change": "ADDED", "new": new_storage[mp]}
    for mp in old_storage.keys() & new_storage.keys():
        if old_storage[mp] != new_storage[mp]:
            storage_diff[mp] = {"change": "CHANGED", "old": old_storage[mp], "new": new_storage[mp]}
    if storage_diff:
        result["storage"] = storage_diff

    software_diff = {}
    for name in old_mat["software"].keys() | new_mat["software"].keys():
        old_v, new_v = old_mat["software"].get(name), new_mat["software"].get(name)
        if name == "sql_engines":
            sub_diff = {}
            for engine in (old_v or {}).keys() | (new_v or {}).keys():
                old_engine = (old_v or {}).get(engine, {})
                new_engine = (new_v or {}).get(engine, {})
                if engine == "mysql":
                    # A compound facts bundle (client/server_binary/
                    # server_implementation/server_version/
                    # server_running/server_readiness) — never
                    # collapsed into ADDED/REMOVED semantics that would
                    # only make sense for a single status transition;
                    # a plain field-by-field diff instead (mandate §3:
                    # keep "server running" and "server ready" as
                    # DISTINCT, separately-diffable facts, never merged).
                    field_diffs = {}
                    for field in old_engine.keys() | new_engine.keys():
                        fd = _diff_scalar(old_engine.get(field), new_engine.get(field))
                        if fd:
                            field_diffs[field] = fd
                    if field_diffs:
                        sub_diff[engine] = {"change": "CHANGED", "diffs": field_diffs}
                    continue
                d = _diff_component_dict(old_engine, new_engine)
                if d:
                    sub_diff[engine] = d
            if sub_diff:
                software_diff[name] = sub_diff
            continue
        if name == "python_version":
            d = _diff_scalar(old_v, new_v)
            if d:
                software_diff[name] = d
            continue
        d = _diff_component_dict(old_v or {}, new_v or {})
        if d:
            software_diff[name] = d
    if software_diff:
        result["software"] = software_diff

    services_diff = {}
    for name in old_mat["services"].keys() | new_mat["services"].keys():
        d = _diff_component_dict(old_mat["services"].get(name, {}), new_mat["services"].get(name, {}), keys=("status",))
        if d:
            services_diff[name] = d
    if services_diff:
        result["services"] = services_diff

    return result
