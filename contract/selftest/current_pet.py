"""Runner target: a TEMPORARY copy of today's Python system (the PET web server), for the RED baseline.

Safety, in order of importance:
  * it listens on a random free port, never a port of the live system (wire.LIVE_PORTS);
  * it runs with a scrubbed environment: a throw-away HOME and XDG directories, YANDI_TEST_MODE=1 (the database layer then
    refuses any real database connection, agent/db/sql/connection.py), no inherited YANDI_* or credentials;
  * its Redis address is redirected, before the server module is imported, to a port on which nothing listens, so it cannot
    reach the owner's Redis even if a request tried;
  * it has no hooks: the current system has no lifecycle, no launch secret and no unlock, and the runner does not pretend it has.
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

from contract.runner.wire import LIVE_PORTS, send

REPO = Path(__file__).resolve().parents[2]

_BOOT = """
import sys
sys.path.insert(0, {repo!r})
import pet.shared as shared
shared.REDIS_URL = "redis://127.0.0.1:{dead}"
import pet.council_chat_server as server
import uvicorn
uvicorn.run(server.app, host="127.0.0.1", port={port}, log_level="warning")
"""


def _free_port(avoid: set[int]) -> int:
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port not in LIVE_PORTS and port not in avoid:
            return port


class CurrentPetTarget:
    name = "current Python system (temporary PET instance, test mode)"
    hooks: set = set()

    def __init__(self):
        self.launch_secret = secrets.token_hex(32)               # the current system knows nothing of it
        self.unlock_key = base64.b64encode(secrets.token_bytes(32)).decode()
        port = _free_port(set())
        dead = _free_port({port})
        with socket.socket() as probe:                           # nothing may listen on the dead Redis port
            if probe.connect_ex(("127.0.0.1", dead)) == 0:
                raise RuntimeError("the port chosen as a dead Redis is in use; refusing to start")
        self.tmp = Path(tempfile.mkdtemp(prefix="yandi-contract-pet-"))
        for d in ("home", "data", "config", "state", "cache", "tmp"):
            (self.tmp / d).mkdir()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
            "HOME": str(self.tmp / "home"), "TMPDIR": str(self.tmp / "tmp"),
            "XDG_DATA_HOME": str(self.tmp / "data"), "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_STATE_HOME": str(self.tmp / "state"), "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "YANDI_TEST_MODE": "1", "YANDI_WEB_SETTINGS": str(self.tmp / "web_settings.json"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.log = self.tmp / "server.log"
        self.proc = subprocess.Popen([sys.executable, "-c", _BOOT.format(repo=str(REPO), dead=dead, port=port)],
                                     cwd=str(self.tmp), env=env, stdout=open(self.log, "wb"), stderr=subprocess.STDOUT)
        self.base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("the temporary PET exited at start:\n" + self.log.read_text(errors="replace")[-800:])
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)
        else:
            self.close()
            raise RuntimeError("the temporary PET did not start in time")
        r = send(self.base_url, "GET", "/v1/health", {}, None, timeout=10)
        self.first_contact = {"request": "GET /v1/health", "status": r.status, "error": r.error,
                              "content_type": r.headers.get("content-type"), "body_start": r.text[:160]}

    def reset(self) -> None:                                     # the current system has nothing to reset to
        return None

    def probe(self, name: str, args: dict) -> dict:
        raise NotImplementedError(name)

    def close(self) -> None:
        if getattr(self, "proc", None) and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(getattr(self, "tmp", "/nonexistent"), ignore_errors=True)
