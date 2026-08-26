"""
tool_system.py — состояние системы (Debian/Linux).
"""
import os
import platform
import subprocess
from pathlib import Path


def os_info() -> dict:
    info = {"platform": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "python": platform.python_version()}
    try:
        info["distro"] = Path("/etc/os-release").read_text().splitlines()[0].replace('PRETTY_NAME=', '').strip('"')
    except Exception:
        pass
    return info


def memory() -> dict:
    try:
        import psutil
        m = psutil.virtual_memory()
        return {"total_gb": round(m.total / 1e9, 1), "used_gb": round(m.used / 1e9, 1),
                "percent": m.percent, "available_gb": round(m.available / 1e9, 1)}
    except ImportError:
        raw = Path("/proc/meminfo").read_text()
        d = {}
        for line in raw.splitlines():
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
        total = int(d.get("MemTotal", "0 kB").split()[0]) * 1024
        free  = int(d.get("MemAvailable", "0 kB").split()[0]) * 1024
        return {"total_gb": round(total / 1e9, 1), "available_gb": round(free / 1e9, 1),
                "used_gb": round((total - free) / 1e9, 1), "percent": round((total - free) / total * 100, 1)}


def disk(path: str = "/") -> dict:
    s = os.statvfs(path)
    total = s.f_frsize * s.f_blocks
    free  = s.f_frsize * s.f_bfree
    return {"path": path, "total_gb": round(total / 1e9, 1),
            "free_gb": round(free / 1e9, 1), "used_gb": round((total - free) / 1e9, 1),
            "percent": round((total - free) / total * 100, 1)}


def cpu() -> dict:
    try:
        import psutil
        return {"percent": psutil.cpu_percent(interval=0.5), "cores": psutil.cpu_count()}
    except ImportError:
        load = os.getloadavg()
        return {"load_1m": load[0], "load_5m": load[1], "load_15m": load[2]}


def processes(name_filter: str = "") -> list[dict]:
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "status", "memory_percent"]):
            if name_filter and name_filter.lower() not in p.info["name"].lower():
                continue
            procs.append(p.info)
        return sorted(procs, key=lambda x: x.get("memory_percent", 0), reverse=True)[:20]
    except ImportError:
        out = subprocess.check_output(["ps", "aux", "--no-header"], text=True)
        lines = [l for l in out.splitlines() if name_filter.lower() in l.lower()] if name_filter else out.splitlines()
        return [{"raw": l[:120]} for l in lines[:20]]


def services(names: list[str] = None) -> dict:
    default = ["redis", "ollama", "nginx", "postgresql"]
    targets = names or default
    result = {}
    for svc in targets:
        try:
            r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
            result[svc] = r.stdout.strip()
        except Exception:
            result[svc] = "unknown"
    return result


def full_report() -> dict:
    return {"os": os_info(), "cpu": cpu(), "memory": memory(),
            "disk": disk("/"), "services": services()}
