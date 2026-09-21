"""What the runner talks to.

A target is a core that was started FOR THE TEST, with its own throw-away state, launch secret and unlock key. The runner
never starts, stops or configures anything itself except through a target's `reset`; and it refuses the live system's ports
(wire.LIVE_PORTS) and any non-loopback address.

`ExternalTarget` describes a core that is already listening (a Rust core, a Python core, anything): it has no hooks, and its
`reset` only brings it back to `locked` through the contract's own `lock` request. A target that can start a fresh core per
scenario (the test double in contract/selftest, or a future real core with a test launcher) implements `reset` by restarting
and lists the hooks it offers.
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from .engine import TargetUnavailable
from .loader import load_schemas
from .wire import check_target_url, send


class Target:
    name = "target"
    hooks: set = set()
    base_url = ""
    launch_secret = ""
    unlock_key = ""

    def __init__(self):
        check_target_url(self.base_url) if self.base_url else None

    def reset(self) -> None:          # bring the core to a fresh, locked state
        raise NotImplementedError

    def probe(self, name: str, args: dict) -> dict:
        raise NotImplementedError(f"target '{self.name}' offers no probe '{name}'")

    def close(self) -> None:
        pass


class ExternalTarget(Target):
    """An already running core, described by a JSON file (schemas/scenario/target.schema.json)."""

    def __init__(self, config: dict):
        schemas = load_schemas()
        errors = schemas.errors("scenario/target", config)
        if errors:
            raise ValueError("target description is not valid: " + "; ".join(errors))
        self.name = config["name"]
        self.base_url = config["base_url"].rstrip("/")
        self.launch_secret = config["launch_secret"]
        self.unlock_key = config["unlock_key"]
        self.hooks = set(config.get("hooks", []))
        super().__init__()

    @classmethod
    def from_file(cls, path: str) -> "ExternalTarget":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def reset(self) -> None:
        r = send(self.base_url, "GET", "/v1/health", {}, None, timeout=5.0)
        if r.error:
            raise TargetUnavailable(f"health gave no answer ({r.error})")
        try:
            state = r.json()["state"]
        except (ValueError, KeyError, TypeError):
            raise TargetUnavailable("health did not answer with the contract's body")
        if state == "locked":
            return
        if state != "ready":
            raise TargetUnavailable(f"core is '{state}', not something a test can bring back to locked")
        headers = {"Authorization": f"Bearer {self.launch_secret}", "Idempotency-Key": f"reset-{secrets.token_hex(8)}",
                   "Content-Type": "application/json"}
        r = send(self.base_url, "POST", "/v1/lock", headers, b"{}")
        if r.status != 200:
            raise TargetUnavailable(f"a lock request to bring the core back to 'locked' was answered {r.status if r.status else r.error}")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            h = send(self.base_url, "GET", "/v1/health", {}, None, timeout=2.0)
            if not h.error and h.status == 200 and h.body.strip().startswith(b"{") and json.loads(h.body).get("state") == "locked":
                return
            time.sleep(0.05)
        raise TargetUnavailable("core did not return to 'locked' after a lock request")
