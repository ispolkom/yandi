"""Runner target: the Python Core, started as a separate process for the test, exactly as a supervisor would start it.

Everything a test needs is throw-away: a temporary directory holds the launch secret file (0600), the state directory (with a
check value provisioned for a fresh random key), the chosen port and the log. The process gets a scrubbed environment
(no inherited YANDI_* or credentials), YANDI_TEST_MODE=1, and — for the real application — a Redis address that nothing listens on.

Modes
    shell  `python -m pet.core_main --shell`: the lifecycle boundary with a tiny stand-in application (fast; every fixture)
    real   `python -m pet.core_main`: the real PET application behind the boundary (the conformance proof that counts)

Hooks it offers, and why each is honest
    restart      a fresh process per scenario (or a lock request, when the process is alive and only unlocked)
    drain_hold   the core's test-mode-only switch that keeps `draining` open until a file is removed (YANDI_TEST_MODE=1 only)
    socket_probe the listening sockets of the child, read from /proc: what the core itself bound
    log_probe    what the core printed
    storage_probe (needles only) the bytes of the core's own state and runtime directories
Hooks it does NOT offer, because a Python test harness would only be testing itself: process_probe, runtime_file_probe, launch_probe
(the launcher is the Node's job, P1b), memory_probe (Python cannot show that no copy of the key survives), os_confinement_probe
(the core reports egress_confinement "none"), transaction_probe. The `decrypted_state` storage scan is refused as unsupported.
"""
from __future__ import annotations

import base64
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from contract.runner.engine import ProbeUnsupported, TargetUnavailable
from contract.runner.wire import LIVE_PORTS, send
from contract.selftest.current_pet import _free_port

REPO = Path(__file__).resolve().parents[2]

_REAL_BOOT = """
import sys
sys.path.insert(0, {repo!r})
import pet.shared as shared
shared.REDIS_URL = "redis://127.0.0.1:{dead}"
from pet.core_main import main
raise SystemExit(main([]))
"""
_SHELL_BOOT = """
import sys
sys.path.insert(0, {repo!r})
from pet.core_main import main
raise SystemExit(main(["--shell"]))
"""


class PythonCoreTarget:
    hooks = {"restart", "drain_hold", "socket_probe", "log_probe", "storage_probe"}

    def __init__(self, mode: str = "shell", startup_timeout: float = 90.0, repo: Path | None = None):
        if mode not in ("shell", "real"):
            raise ValueError(mode)
        self.mode = mode
        self.repo = Path(repo) if repo else REPO          # a copy with a deliberate defect, for the mutant tests
        self.name = f"python core ({'real PET application' if mode == 'real' else 'lifecycle shell'}, separate process)"
        self.startup_timeout = startup_timeout
        self.proc: subprocess.Popen | None = None
        self.tmp: Path | None = None
        self.base_url = ""
        self.launch_secret = ""
        self.unlock_key = ""
        self.hold_drain = False
        self.launches = 0

    # ── lifecycle of the process ─────────────────────────────────────────────────────────────────────────────
    def configure(self, requires) -> None:
        wanted = "drain_hold" in requires
        if wanted != self.hold_drain:
            self.hold_drain = wanted
            self._stop()                                  # the switch is read at start: a different setting needs a new process

    def reset(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            h = send(self.base_url, "GET", "/v1/health", {}, None, timeout=5.0)
            state = h.json().get("state") if h.status == 200 and not h.error else None
            if state == "locked":
                return
            if state == "ready" and self._lock():
                return
        self._stop()
        self._start()

    def _lock(self) -> bool:
        headers = {"Authorization": f"Bearer {self.launch_secret}", "Idempotency-Key": f"reset-{secrets.token_hex(8)}",
                   "Content-Type": "application/json"}
        r = send(self.base_url, "POST", "/v1/lock", headers, b"{}", timeout=5.0)
        return r.status == 200

    def _start(self) -> None:
        from pet.core_lifecycle import provision_state
        self.launches += 1
        tmp = self.tmp = Path(tempfile.mkdtemp(prefix="yandi-contract-core-"))
        os.chmod(tmp, 0o700)
        for d in ("home", "data", "config", "state", "cache", "tmp", "state-dir", "run"):
            (tmp / d).mkdir()
        key = secrets.token_bytes(32)
        self.unlock_key = base64.b64encode(key).decode()
        provision_state(tmp / "state-dir", key)
        self.launch_secret = secrets.token_hex(32)
        secret_file = self.secret_file = tmp / "run" / "launch-secret"
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(self.launch_secret + "\n")
        port_file = tmp / "run" / "port"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(tmp / "home"), "TMPDIR": str(tmp / "tmp"),
            "XDG_DATA_HOME": str(tmp / "data"), "XDG_CONFIG_HOME": str(tmp / "config"), "XDG_STATE_HOME": str(tmp / "state"),
            "XDG_CACHE_HOME": str(tmp / "cache"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "YANDI_TEST_MODE": "1",
            "YANDI_WEB_SETTINGS": str(tmp / "web_settings.json"),
            "YANDI_CORE_SECRET_FILE": str(secret_file), "YANDI_CORE_STATE_DIR": str(tmp / "state-dir"), "YANDI_CORE_PORT_FILE": str(port_file),
        }
        if self.hold_drain:
            self.hold_file = tmp / "run" / "hold-drain"
            self.hold_file.write_text("hold")
            env["YANDI_CORE_TEST_HOLD_DRAIN_FILE"] = str(self.hold_file)
        boot = (_SHELL_BOOT if self.mode == "shell" else _REAL_BOOT).format(repo=str(self.repo), dead=_free_port(set()))
        self.log_path = tmp / "core.log"
        self.proc = subprocess.Popen([sys.executable, "-c", boot], cwd=str(tmp), env=env, stdout=open(self.log_path, "wb"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise TargetUnavailable("the core exited at start: " + self.log_path.read_text(errors="replace")[-300:])
            if port_file.exists() and port_file.read_text().strip().isdigit():
                port = int(port_file.read_text().strip())
                if port in LIVE_PORTS:
                    raise TargetUnavailable("the system chose a port of the live system; refusing")
                self.base_url = f"http://127.0.0.1:{port}"
                h = send(self.base_url, "GET", "/v1/health", {}, None, timeout=3.0)
                if not h.error and h.status == 200 and h.text.strip() == '{"state":"locked"}':
                    return
            time.sleep(0.1)
        raise TargetUnavailable("the core did not report 'locked' in time")

    def _stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None

    def close(self) -> None:
        self._stop()

    # ── probes ───────────────────────────────────────────────────────────────────────────────────────────────
    def probe(self, name: str, args: dict) -> dict:
        if name == "release_drain":
            Path(self.hold_file).unlink(missing_ok=True)
            return {}
        if name == "listen_addresses":
            addrs = self._listening()
            return {"addresses": addrs, "all_loopback": bool(addrs) and all(a.startswith("127.") or a == "::1" for a in addrs)}
        if name == "log_scan":
            text = self.log_path.read_text(errors="replace")
            return {"found": any(n and n in text for n in args.get("needles", []))}
        if name == "storage_scan":
            if args.get("scan"):
                raise ProbeUnsupported("a scan for decrypted state needs to know what the application keeps; this harness only sees the core's own files")
            return {"found": self._scan_files(args.get("needles", []))}
        raise ProbeUnsupported(name)

    def _listening(self) -> list[str]:
        pid = self.proc.pid
        inodes = set()
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
        found = []
        for table, v6 in (("tcp", False), ("tcp6", True)):
            for line in Path(f"/proc/{pid}/net/{table}").read_text().splitlines()[1:]:
                parts = line.split()
                if parts[3] != "0A" or parts[9] not in inodes:         # 0A = LISTEN
                    continue
                host_hex = parts[1].split(":")[0]
                raw = bytes.fromhex(host_hex)
                if v6:
                    b = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
                    found.append(socket.inet_ntop(socket.AF_INET6, b))
                else:
                    found.append(socket.inet_ntoa(raw[::-1]))
        return found

    def _scan_files(self, needles: list[str]) -> bool:
        forms: list[bytes] = []
        for n in needles:
            forms.append(n.encode())
            try:
                raw = base64.b64decode(n, validate=True)
                forms += [raw, raw.hex().encode()] if len(raw) == 32 else []
            except ValueError:
                pass
        for path in self.tmp.rglob("*"):
            if path.is_file() and path != self.log_path:
                data = path.read_bytes()
                if any(f and f in data for f in forms):
                    return True
        return False
