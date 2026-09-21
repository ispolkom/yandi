"""A TEST DOUBLE of the lifecycle chapter of the contract. It is not the core, not a prototype of the core, and nothing imports it
outside the contract's own tests.

Why it exists: a fixture suite that no implementation can satisfy is worthless, and a suite that cannot tell a broken
implementation from a good one is worse. The double is written from the contract text (appendix D) independently of the JSON
schemas; the fixtures pass against it (they are satisfiable) and every deliberately broken variant ("fault") makes at least one
named fixture fail (they bite). See contract/contract_regression_test.py.

Standard library only. Deliberately simple: it buffers a whole request body before deciding, which a real core must not do.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from contract.runner.wire import LIVE_PORTS

LIMIT = 65536
KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
CONTEXT = "yandi/core/v1"
CONTRACT = "1.0-rc1"

FAULTS = {
    "no_auth_capabilities", "no_auth_lock", "prefix_auth", "wrong_key_ready", "wrong_key_partial", "locked_serves_capabilities",
    "locked_serves_shutdown", "lock_noop", "unknown_fields_ok", "extensions_bypass", "dup_keys_ok", "no_size_limit", "limit_high",
    "limit_low", "leak_traceback", "echo_key", "health_extra", "idem_ignored", "idem_conflict_ignored", "no_idem_required",
    "state_before_replay", "principal_grants", "absent_feature_ok", "shutdown_stays_ready", "wrong_context_ok", "loose_key_check",
    "error_shape_broken", "caps_no_confinement", "caps_old_contract", "plain_404", "echo_secret_401", "nan_ok", "deadline_loose",
    "health_needs_auth", "validate_before_auth", "leak_path", "non_json_content_type", "lock_ready_after_wrong_key",
}


class Core:
    """State and rules. One instance per test scenario."""

    def __init__(self, secret: str, key_b64: str, faults=frozenset(), hold_drain: bool = False):
        self.secret = secret
        self.key = base64.b64decode(key_b64)
        self.faults = set(faults)
        unknown = self.faults - FAULTS
        if unknown:
            raise ValueError(f"unknown fault {sorted(unknown)}")
        self.hold_drain = hold_drain
        self.state = "locked"
        self.partial = False
        self.records: dict[str, tuple] = {}        # idempotency key -> (method, path, body digest, status, body)
        self.mutex = threading.RLock()
        self.on_stopped = lambda: None
        self.drain_released = threading.Event()
        if not hold_drain:
            self.drain_released.set()

    # ── helpers ───────────────────────────────────────────────────────────────────────────────────────────
    def has(self, fault: str) -> bool:
        return fault in self.faults

    def error(self, status: int, code: str, message: str, request_id: str):
        body = {"error": {"code": code, "message": message, "retryable": status in (429, 502, 503), "request_id": request_id}}
        if self.has("error_shape_broken"):
            del body["error"]["retryable"]
        return status, body

    def authorised(self, headers: dict) -> bool:
        got = headers.get("authorization", "")
        want = f"Bearer {self.secret}"
        if self.has("principal_grants") and headers.get("x-principal"):
            return True
        if self.has("prefix_auth"):
            return got.startswith(want)
        return hmac.compare_digest(got.encode(), want.encode())

    def parse(self, raw: bytes):
        """Strict JSON: UTF-8, no duplicate keys, no NaN/Infinity, top level an object."""
        def pairs(items):
            keys = [k for k, _ in items]
            if len(keys) != len(set(keys)) and not self.has("dup_keys_ok"):
                raise ValueError("duplicate key")
            return dict(items)

        def constant(name):
            if self.has("nan_ok"):
                return 0
            raise ValueError("constant")
        text = raw.decode("utf-8")
        doc = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(doc, dict):
            raise ValueError("not an object")
        return doc

    def validate(self, endpoint: str, doc: dict) -> str | None:
        allowed = {"unlock": {"key", "context", "extensions"}, "lock": {"extensions"}, "shutdown": {"deadline_ms", "extensions"}}[endpoint]
        if not self.has("unknown_fields_ok") and set(doc) - allowed:
            return "The request has a field that is not allowed."
        if "extensions" in doc and not isinstance(doc["extensions"], dict):
            return "The field 'extensions' must be an object."
        if endpoint == "unlock":
            key, context = doc.get("key"), doc.get("context")
            if not isinstance(key, str) or not isinstance(context, str):
                return "The fields 'key' and 'context' are required strings."
            if context != CONTEXT and not self.has("wrong_context_ok"):
                return "The context is not supported."
            if not KEY_RE.match(key):
                if self.has("loose_key_check") and self.loose_decode(key) is not None:
                    return None
                return "The key is not valid."
        if endpoint == "shutdown" and "deadline_ms" in doc:
            d = doc["deadline_ms"]
            if self.has("deadline_loose"):
                return None
            if isinstance(d, bool) or not isinstance(d, int) or d < 1:
                return "The deadline must be a positive integer."
        return None

    @staticmethod
    def loose_decode(key: str):
        try:
            raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
        except (binascii.Error, ValueError):
            return None
        return raw if len(raw) == 32 else None

    # ── the request pipeline (appendix D.4) ───────────────────────────────────────────────────────────────
    def handle(self, method: str, target: str, headers: dict, raw: bytes, oversized: bool):
        rid = "req_" + secrets.token_hex(6)
        path = target.split("?", 1)[0]
        if self.has("leak_path") and path == "/v1/no-such-endpoint":
            return self.error(404, "not_found", "No handler for /home/yandi/core/router.py", rid)

        if method == "GET" and path == "/v1/health":
            if self.has("health_needs_auth") and not self.authorised(headers):
                return self.error(401, "unauthorized", "Authentication is required.", rid)
            body = {"state": self.state}
            if self.has("health_extra"):
                body["version"] = "0.0.1"
            return 200, body

        if self.has("validate_before_auth") and method == "POST" and len(raw) > LIMIT:
            return self.error(413, "payload_too_large", "The request body is too large.", rid)
        if not self.authorised(headers) and not (self.has("no_auth_capabilities") and path == "/v1/capabilities") \
                and not (self.has("no_auth_lock") and path == "/v1/lock"):
            message = "Authentication is required."
            if self.has("echo_secret_401"):
                message = f"Authentication failed for '{headers.get('authorization', '')}'."
            return self.error(401, "unauthorized", message, rid)

        routes = {("GET", "/v1/capabilities"): "capabilities", ("POST", "/v1/unlock"): "unlock",
                  ("POST", "/v1/lock"): "lock", ("POST", "/v1/shutdown"): "shutdown"}
        endpoint = routes.get((method, path))
        if self.has("absent_feature_ok") and method == "POST" and path == "/v1/conversations":
            return 200, {"conversation_id": "conv_fake0001"}
        if endpoint is None:
            return self.error(404, "not_found", "There is no such endpoint.", rid)

        body_doc: dict = {}
        key = headers.get("idempotency-key")
        if endpoint != "capabilities":
            if key is None or not IDEM_RE.match(key):
                if not (key is None and self.has("no_idem_required")):
                    return self.error(400, "invalid_payload", "A valid Idempotency-Key header is required.", rid)
            limit = LIMIT + 100 if self.has("limit_high") else LIMIT - 1 if self.has("limit_low") else LIMIT
            if (len(raw) > limit or oversized) and not self.has("no_size_limit"):
                return self.error(413, "payload_too_large", "The request body is too large.", rid)
            try:
                body_doc = self.parse(raw)
            except RecursionError:
                if self.has("leak_traceback"):
                    return self.error(500, "internal", 'Traceback (most recent call last): File "/srv/core/app.py", line 3 RecursionError', rid)
                return self.error(400, "invalid_payload", "The request body is not valid.", rid)
            except (ValueError, UnicodeDecodeError) as exc:
                if self.has("leak_traceback"):
                    return self.error(500, "internal", "".join(traceback.format_exception_only(type(exc), exc)), rid)
                return self.error(400, "invalid_payload", "The request body is not valid JSON.", rid)
            problem = self.validate(endpoint, body_doc)
            if problem:
                if self.has("echo_key") and endpoint == "unlock":
                    problem += f" Received: {json.dumps(body_doc)}"
                return self.error(400, "invalid_payload", problem, rid)

        with self.mutex:
            digest = hashlib.sha256(raw).hexdigest()
            if not self.has("state_before_replay"):
                early = self.replay(method, path, key, digest, rid)
                if early:
                    return early
            refused = self.state_refusal(endpoint, rid)
            if refused:
                return refused
            if self.has("state_before_replay"):
                early = self.replay(method, path, key, digest, rid)
                if early:
                    return early
            status, body = self.execute(endpoint, body_doc, rid)
            if key is not None and status < 300 and not self.has("idem_ignored"):
                self.records[key] = (method, path, digest, status, body)
            return status, body

    def replay(self, method, path, key, digest, rid):
        if key is None or self.has("idem_ignored") or key not in self.records:
            return None
        m, p, d, status, body = self.records[key]
        if (m, p, d) != (method, path, digest):
            if self.has("idem_conflict_ignored"):
                return status, body
            return self.error(409, "idempotency_conflict", "This key was already used for a different request.", rid)
        return status, body

    def state_refusal(self, endpoint, rid):
        if self.state == "locked":
            if endpoint == "unlock":
                return None
            if endpoint == "capabilities" and (self.has("locked_serves_capabilities") or self.partial):
                return None
            if endpoint == "shutdown" and self.has("locked_serves_shutdown"):
                return None
            return self.error(423, "locked", "The core is locked.", rid)
        if self.state == "draining":
            return self.error(503, "unavailable", "The core is shutting down.", rid)
        return None

    def execute(self, endpoint, doc, rid):
        if endpoint == "capabilities":
            caps = {"contract": "1.0" if self.has("caps_old_contract") else CONTRACT,
                    "implementation": {"name": "contract-test-double", "version": "0"},
                    "features": [], "limits": {"body_bytes": LIMIT}, "storage_schema": 0, "egress_confinement": "none",
                    "note": "additive fields must be tolerated"}
            if self.has("caps_no_confinement"):
                del caps["egress_confinement"]
            return 200, caps
        if endpoint == "unlock":
            raw = self.loose_decode(doc["key"]) if self.has("loose_key_check") and not KEY_RE.match(doc["key"]) else base64.b64decode(doc["key"])
            ok = raw is not None and hmac.compare_digest(raw, self.key)
            if not ok and self.has("extensions_bypass") and isinstance(doc.get("extensions"), dict) and doc["extensions"].get("force"):
                ok = True
            if not ok and self.has("wrong_key_ready"):
                ok = True
            if not ok:
                if self.has("wrong_key_partial"):
                    self.partial = True
                if self.has("lock_ready_after_wrong_key"):
                    self.state = "ready"
                return self.error(403, "forbidden", "The key was not accepted.", rid)
            self.state = "ready"
            self.partial = False
            return 200, {"state": "ready", "note": "additive fields must be tolerated"}
        if endpoint == "lock":
            if not self.has("lock_noop"):
                self.state = "locked"
                self.partial = False
            return 200, {"state": "locked", "note": "additive fields must be tolerated"}
        if endpoint == "shutdown":
            if not self.has("shutdown_stays_ready"):
                self.state = "draining"
                threading.Thread(target=self._drain, daemon=True).start()
            return 202, {"state": "draining", "note": "additive fields must be tolerated"}
        raise AssertionError(endpoint)

    def _drain(self):
        self.drain_released.wait(30)
        time.sleep(0.15)
        self.state = "stopped"
        self.on_stopped()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    core: Core

    def version_string(self):
        return "core"

    def log_message(self, *args):
        pass

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        oversized = length > 8 * 1024 * 1024
        raw = self.rfile.read(min(length, 8 * 1024 * 1024)) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        status, body = self.core.handle(self.command, self.path, headers, raw, oversized)
        if self.core.has("plain_404") and status == 404:
            payload, ctype = b"<html>Not Found</html>", "text/html"
        else:
            payload = json.dumps(body, separators=(",", ":")).encode()
            ctype = "text/plain" if self.core.has("non_json_content_type") else "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = _serve


class ReferenceTarget:
    """Runner target: a fresh double per scenario (hook 'restart'), able to hold the drain open (hook 'drain_hold')."""

    name = "reference double (test double of the lifecycle chapter)"
    hooks = {"restart", "drain_hold"}

    def __init__(self, faults=(), hold_drain_offered: bool = True):
        self.faults = frozenset(faults)
        self.launch_secret = secrets.token_hex(32)
        self.unlock_key = base64.b64encode(secrets.token_bytes(32)).decode()
        self.hooks = set(self.hooks) if hold_drain_offered else {"restart"}
        self.server: ThreadingHTTPServer | None = None
        self.core: Core | None = None
        self.base_url = ""
        self.hold_drain = False

    def configure(self, requires) -> None:
        """Called by the runner before reset(): hold a shutdown in 'draining' only for the scenarios that ask for it."""
        self.hold_drain = "drain_hold" in requires

    def reset(self):
        self.close()
        while True:
            handler = type("Handler", (_Handler,), {})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            if server.server_address[1] not in LIVE_PORTS:
                break
            server.server_close()
        core = Core(self.launch_secret, self.unlock_key, self.faults, hold_drain=self.hold_drain)

        def stopped():                                            # a stopped core no longer listens at all
            server.shutdown()
            server.server_close()
        core.on_stopped = lambda: threading.Thread(target=stopped, daemon=True).start()
        handler.core = core
        self.server, self.core = server, core
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def probe(self, name: str, args: dict) -> dict:
        if name == "release_drain":
            self.core.drain_released.set()
            return {}
        raise NotImplementedError(name)

    def close(self):
        if self.server is not None:
            server, self.server = self.server, None
            self.core.drain_released.set()
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
