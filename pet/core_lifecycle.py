"""The Core's P1 lifecycle boundary: the Node ⇄ Core contract (docs/NODE_CORE_CONTRACT.md, 1.0-rc1) for `/v1/health`,
`/v1/capabilities`, `/v1/unlock`, `/v1/lock`, `/v1/shutdown`.

What this is
    A pure ASGI layer that sits OUTSIDE the existing application. It answers `/v1/*` itself and decides, for everything else,
    whether the application may be reached at all: while the core is locked (or draining) every other HTTP request and every
    WebSocket is refused, so the application does not serve anything until the Node has unlocked it. It is started only in
    "core mode" (pet/core_main.py); the ordinary PET start (start.sh) does not use it and behaves exactly as before.

What this is NOT (said plainly, see docs/CORE_LIFECYCLE.md)
    * `unlock` proves the Node-derived key against a check value and keeps it in memory. The SQL/personal storage is NOT yet
      encrypted with that key: lifecycle unlock is implemented, storage encryption is not bound to the Node-derived key.
    * The gate covers the HTTP/WebSocket surface of the process. It does not stop another process (a script, a second PET)
      that talks to the database or Redis directly.
    * Memory zeroisation is best effort: Python cannot prove that no copy of the key survives in the interpreter.

Rules kept here (contract sections in brackets)
    * only `GET /v1/health` is open; everything else needs `Authorization: Bearer <launch secret>`, compared in constant time [4]
    * order of checks: authentication → Idempotency-Key → body size → parse and validation → idempotency lookup → state → execute [D.4]
    * requests are strict: an unknown field, a duplicate JSON key, NaN, invalid UTF-8, a wrong type are `invalid_payload` [I9, I10]
    * every error is the canonical body and carries nothing internal; detail goes to the local log, never a secret [I8]
    * lifecycle idempotency records live in memory for the life of the process; for `unlock` only a keyed digest of the body is kept,
      never the key [D.5]
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agent.db.sql import field_protection

CONTRACT_VERSION = "1.0-rc1"
IMPLEMENTATION_NAME = "yandi-python-core"
IMPLEMENTATION_VERSION = "p1-lifecycle"
UNLOCK_CONTEXT = "yandi/core/v1"

MAX_BODY_BYTES = 64 * 1024
DEFAULT_DRAIN_MS = 5000
MAX_DRAIN_MS = 300_000

ENV_SECRET_FILE = "YANDI_CORE_SECRET_FILE"
ENV_STATE_DIR = "YANDI_CORE_STATE_DIR"
ENV_PORT_FILE = "YANDI_CORE_PORT_FILE"
ENV_HOLD_DRAIN_FILE = "YANDI_CORE_TEST_HOLD_DRAIN_FILE"       # honoured only when YANDI_TEST_MODE=1

_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
_IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SECRET_RE = re.compile(r"^[0-9a-f]{64}$")

_CHECK_PLAINTEXT = b"yandi core check value v1"
_CHECK_INFO = b"yandi/core/v1/check-value"
_CHECK_FILE = "check-value.json"

# What a running core may honestly report about how far the operating system confines its own sockets (contract 1.1). Nothing
# in P1 confines the core: it must reach the local model, MySQL and Redis, and per-program firewall rules need privileges.
EGRESS_CONFINEMENT = "none"


def _log_line(message: str) -> None:
    """Lifecycle events go to the local log at once (a supervisor reading the pipe must see them, and so must a test)."""
    print(message, flush=True)


class LaunchSecretError(Exception):
    """The launch secret file is missing or unsafe. The message is safe to print; it never names the secret."""


# ── launch secret ─────────────────────────────────────────────────────────────────────────────────────────────────

def load_launch_secret(path: str) -> str:
    """Read the per-launch secret from the runtime file the Node prepared. Fail closed: a file that anybody but the owner
    can read, that is not a regular file, or that does not hold 32 random bytes in hex is refused."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise LaunchSecretError("the launch secret file cannot be opened")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LaunchSecretError("the launch secret file is not a regular file")
        if st.st_uid != os.getuid():
            raise LaunchSecretError("the launch secret file is not owned by the user running the core")
        if st.st_mode & 0o077:
            raise LaunchSecretError("the launch secret file is readable by others (it must be mode 0600)")
        if st.st_size > 256:
            raise LaunchSecretError("the launch secret file has an unexpected size")
        data = os.read(fd, 300)
    finally:
        os.close(fd)
    secret = data.decode("ascii", "replace").strip()
    if not _SECRET_RE.match(secret):
        raise LaunchSecretError("the launch secret file does not hold 32 random bytes in hex")
    return secret


# ── check value (how a key is proven without keeping anything readable) ─────────────────────────────────────────

def _check_key(derived_key: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_CHECK_INFO).derive(derived_key)


def provision_state(state_dir: str | Path, derived_key: bytes) -> None:
    """Create the check value for a Node-derived key. Done once, out of band, when the Node sets the core up (never over the
    network API: the contract's `unlock` request has no way to name a new key)."""
    if len(derived_key) != 32:
        raise ValueError("the derived key must be 32 bytes")
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    nonce = secrets.token_bytes(12)
    blob = AESGCM(_check_key(derived_key)).encrypt(nonce, _CHECK_PLAINTEXT, _CHECK_INFO)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".check-")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            json.dump({"v": 1, "nonce": nonce.hex(), "ct": blob.hex()}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, directory / _CHECK_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def verify_key(state_dir: str | Path, derived_key: bytes) -> str:
    """'ok', 'wrong' or 'unprovisioned'. A wrong key and a missing or damaged check value are told apart only in the local
    log, never to the caller."""
    path = Path(state_dir) / _CHECK_FILE
    try:
        doc = json.loads(path.read_text(encoding="ascii"))
        nonce, blob = bytes.fromhex(doc["nonce"]), bytes.fromhex(doc["ct"])
    except FileNotFoundError:
        return "unprovisioned"
    except (OSError, ValueError, KeyError, TypeError):
        return "unprovisioned"
    try:
        ok = AESGCM(_check_key(derived_key)).decrypt(nonce, blob, _CHECK_INFO) == _CHECK_PLAINTEXT
    except InvalidTag:
        return "wrong"
    return "ok" if ok else "wrong"


def default_state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "yandi" / "core"


# ── canonical answers ─────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Reply:
    status: int
    body: dict

    def encode(self) -> bytes:
        return json.dumps(self.body, separators=(",", ":"), ensure_ascii=True).encode("ascii")


_RETRYABLE = {429, 502, 503}


def error_reply(status: int, code: str, message: str, request_id: str) -> Reply:
    return Reply(status, {"error": {"code": code, "message": message, "retryable": status in _RETRYABLE, "request_id": request_id}})


def parse_request_body(raw: bytes) -> dict:
    """Strict JSON object: UTF-8, no duplicate keys, no NaN/Infinity, nothing after the value. Raises ValueError."""
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(items)

    def constant(_name):
        raise ValueError("non-finite number")
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, RecursionError) as exc:
        raise ValueError(type(exc).__name__) from None
    if not isinstance(doc, dict):
        raise ValueError("not an object")
    return doc


def _validate(endpoint: str, doc: dict) -> Optional[str]:
    allowed = {"unlock": {"key", "context", "extensions"}, "lock": {"extensions"}, "shutdown": {"deadline_ms", "extensions"}}[endpoint]
    if set(doc) - allowed:
        return "The request contains a field that is not allowed."
    if "extensions" in doc and not isinstance(doc["extensions"], dict):
        return "The field 'extensions' must be an object."
    if endpoint == "unlock":
        key, context = doc.get("key"), doc.get("context")
        if not isinstance(key, str) or not isinstance(context, str):
            return "The fields 'key' and 'context' are required."
        if context != UNLOCK_CONTEXT:
            return "The context is not supported."
        if _decode_key(key) is None:
            return "The key is not valid."
    if endpoint == "shutdown" and "deadline_ms" in doc:
        d = doc["deadline_ms"]
        if isinstance(d, bool) or not isinstance(d, int) or d < 1:
            return "The deadline must be a positive integer."
    return None


def _decode_key(text: str) -> Optional[bytes]:
    if not _KEY_RE.match(text):
        return None
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == 32 else None


# ── the lifecycle itself ──────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class _Record:
    method: str
    path: str
    digest: bytes
    reply: Reply


class Lifecycle:
    """State, key and idempotency records of one core process. All decisions are made under one asyncio lock, so eight
    identical requests at once give one transition and eight identical answers."""

    def __init__(self, secret: str, state_dir: str | Path, *, log: Callable[[str], None] = _log_line,
                 on_ready: Optional[list[Callable[[], Awaitable[None]]]] = None,
                 request_stop: Optional[Callable[[float], None]] = None, test_mode: bool = False):
        if not _SECRET_RE.match(secret):
            raise ValueError("the launch secret must be 32 random bytes in hex")
        self._secret = secret.encode("ascii")
        self.state_dir = Path(state_dir)
        self.state = "starting"
        self._key: Optional[bytearray] = None
        self._records: dict[str, _Record] = {}
        self._salt = secrets.token_bytes(32)              # keys the digests so that a digest of an unlock body proves nothing
        self._lock = asyncio.Lock()
        self._in_flight = 0
        self._drain_timeout: Optional[float] = None
        self._log = log
        self._on_ready = list(on_ready or [])
        self._ready_hooks_done = False
        self._request_stop = request_stop or (lambda _timeout: None)
        self._test_mode = test_mode

    # ── used by the ASGI layer ────────────────────────────────────────────────────────────────────────────────
    def authorised(self, header_value: str) -> bool:
        return hmac.compare_digest(header_value.encode("utf-8", "replace"), b"Bearer " + self._secret)

    def digest(self, raw: bytes) -> bytes:
        return hmac.new(self._salt, raw, hashlib.sha256).digest()

    def mark_locked(self) -> None:
        if self.state == "starting":
            self.state = "locked"
            self._log("[core] state: locked")

    def enter(self) -> None:
        self._in_flight += 1

    def leave(self) -> None:
        self._in_flight -= 1

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def capabilities(self) -> dict:
        try:
            from agent.db.sql.schema import SCHEMA_VERSION as storage_schema
        except Exception:                                # a shell without the database layer still answers honestly
            storage_schema = 0
        return {"contract": CONTRACT_VERSION,
                "implementation": {"name": IMPLEMENTATION_NAME, "version": IMPLEMENTATION_VERSION},
                "features": [],                          # only lifecycle exists behind /v1 in P1: no conversation, verification, knowledge
                "limits": {"request_body_bytes": MAX_BODY_BYTES},
                "storage_schema": int(storage_schema) if isinstance(storage_schema, int) else 0,
                "egress_confinement": EGRESS_CONFINEMENT}

    # ── the decision, after authentication, header, size and validation are done ────────────────────────────
    async def decide(self, endpoint: str, method: str, path: str, idem_key: Optional[str], raw: bytes, doc: dict, rid: str) -> Reply:
        async with self._lock:
            if idem_key is not None:
                record = self._records.get(idem_key)
                if record is not None:
                    if (record.method, record.path, record.digest) != (method, path, self.digest(raw)):
                        return error_reply(409, "idempotency_conflict", "This key was already used for a different request.", rid)
                    return record.reply
            refusal = self._state_refusal(endpoint, rid)
            if refusal is not None:
                return refusal
            reply = self._execute(endpoint, doc, rid)
            if idem_key is not None and reply.status < 300:
                self._records[idem_key] = _Record(method, path, self.digest(raw), reply)
            return reply

    def _state_refusal(self, endpoint: str, rid: str) -> Optional[Reply]:
        if self.state == "locked":
            return None if endpoint == "unlock" else error_reply(423, "locked", "The core is locked.", rid)
        if self.state in ("draining", "stopped"):
            return error_reply(503, "unavailable", "The core is shutting down.", rid)
        if self.state != "ready" and endpoint != "unlock":
            return error_reply(503, "unavailable", "The core is not ready.", rid)
        return None

    def _execute(self, endpoint: str, doc: dict, rid: str) -> Reply:
        if endpoint == "capabilities":
            return Reply(200, self.capabilities())
        if endpoint == "unlock":
            return self._unlock(_decode_key(doc["key"]) or b"", rid)
        if endpoint == "lock":
            self._forget_key()
            self.state = "locked"
            self._log("[core] state: locked (lock requested)")
            return Reply(200, {"state": "locked"})
        if endpoint == "shutdown":
            deadline_ms = min(int(doc.get("deadline_ms", DEFAULT_DRAIN_MS)), MAX_DRAIN_MS)
            self.state = "draining"
            self._log("[core] state: draining (shutdown requested)")
            self._drain_timeout = deadline_ms / 1000       # the drain starts once this answer has been sent
            return Reply(202, {"state": "draining"})
        raise AssertionError(endpoint)

    def _unlock(self, key: bytes, rid: str) -> Reply:
        refused = error_reply(403, "forbidden", "The key was not accepted.", rid)
        if self.state == "ready":                        # not fixed by the contract yet: only the key already held is accepted
            return Reply(200, {"state": "ready"}) if self._key is not None and hmac.compare_digest(bytes(self._key), key) else refused
        outcome = verify_key(self.state_dir, key)
        if outcome != "ok":
            self._log(f"[core] unlock refused ({outcome})")
            return refused
        self._key = bytearray(key)
        field_protection.install_key(key)          # P1c-2: the sealed personal ledger opens only while this process is unlocked
        self.state = "ready"
        self._log("[core] state: ready")
        if not self._ready_hooks_done:
            self._ready_hooks_done = True
            asyncio.get_running_loop().create_task(self._run_ready_hooks())
        return Reply(200, {"state": "ready"})

    def _forget_key(self) -> None:
        field_protection.clear_key()
        if self._key is not None:
            for i in range(len(self._key)):              # best effort: Python cannot prove no other copy survives
                self._key[i] = 0
            self._key = None

    async def _run_ready_hooks(self) -> None:
        for hook in self._on_ready:
            try:
                await hook()
            except Exception as exc:                     # a hook is fail-open, exactly as it was at startup
                self._log(f"[core] a start-up step failed after unlock: {type(exc).__name__}")

    def start_pending_drain(self) -> None:
        if self._drain_timeout is not None:
            timeout, self._drain_timeout = self._drain_timeout, None
            asyncio.get_running_loop().create_task(self._drain(timeout))

    async def _drain(self, timeout: float) -> None:
        """Let the application's own requests and sockets finish (bounded by the deadline), then stop the process."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        hold = os.environ.get(ENV_HOLD_DRAIN_FILE) if self._test_mode else None
        while loop.time() < end and (self._in_flight > 0 or (hold and os.path.exists(hold))):
            await asyncio.sleep(0.02)
        self._forget_key()
        self.state = "stopped"
        self._log("[core] state: stopped")
        self._request_stop(max(0.0, end - loop.time()))


# ── the ASGI layer ────────────────────────────────────────────────────────────────────────────────────────────────

_HEADERS = [(b"content-type", b"application/json; charset=utf-8"), (b"cache-control", b"no-store")]


class CoreBoundary:
    """ASGI middleware: answers /v1/*, gates everything else on the lifecycle state, and passes the ready application through."""

    def __init__(self, app, lifecycle: Lifecycle):
        self.app, self.core = app, lifecycle

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            await self._lifespan(scope, receive, send)
        elif kind == "http" and (scope["path"] == "/v1" or scope["path"].startswith("/v1/")):
            await self._v1(scope, receive, send)
        elif kind in ("http", "websocket"):
            await self._legacy(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    # ── lifespan: the application's own start-up runs; only then is the core 'locked' ─────────────────────────────
    async def _lifespan(self, scope, receive, send):
        async def watching(message):
            if message["type"] == "lifespan.startup.complete":
                self.core.mark_locked()
            await send(message)
        await self.app(scope, receive, watching)

    # ── everything that is not /v1 ────────────────────────────────────────────────────────────────────────────
    async def _legacy(self, scope, receive, send):
        if self.core.state != "ready":
            if scope["type"] == "websocket":
                await receive()                          # the connect message
                await send({"type": "websocket.close", "code": 1013})
                return
            code, status = ("locked", 423) if self.core.state in ("locked", "starting") else ("unavailable", 503)
            message = "The core is locked." if status == 423 else "The core is shutting down."
            await self._send(send, error_reply(status, code, message, "req_" + secrets.token_hex(6)))
            return
        self.core.enter()
        try:
            await self.app(scope, receive, send)
        finally:
            self.core.leave()

    # ── /v1 ───────────────────────────────────────────────────────────────────────────────────────────────────
    async def _send(self, send, reply: Reply):
        payload = reply.encode()
        await send({"type": "http.response.start", "status": reply.status,
                    "headers": _HEADERS + [(b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})

    async def _v1(self, scope, receive, send):
        core = self.core
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        rid = headers.get("x-request-id", "")
        rid = rid if _REQUEST_ID_RE.match(rid) else "req_" + secrets.token_hex(6)
        method, path = scope["method"], scope["path"]
        try:
            await self._send(send, await self._v1_reply(method, path, headers, receive, rid))
        except Exception as exc:
            core._log(f"[core] internal error on {method} {path}: {type(exc).__name__}\n{traceback.format_exc()}")
            await self._send(send, error_reply(500, "internal", "The core could not handle the request.", rid))
        core.start_pending_drain()

    async def _v1_reply(self, method, path, headers, receive, rid) -> Reply:
        core = self.core
        if method == "GET" and path == "/v1/health":
            return Reply(200, {"state": core.state if core.state != "stopped" else "draining"})
        if not core.authorised(headers.get("authorization", "")):
            return error_reply(401, "unauthorized", "Authentication is required.", rid)
        routes = {("GET", "/v1/capabilities"): "capabilities", ("POST", "/v1/unlock"): "unlock",
                  ("POST", "/v1/lock"): "lock", ("POST", "/v1/shutdown"): "shutdown"}
        endpoint = routes.get((method, path))
        if endpoint is None:
            return error_reply(404, "not_found", "There is no such endpoint.", rid)
        idem_key, raw, doc = None, b"", {}
        if method == "POST":
            idem_key = headers.get("idempotency-key")
            if idem_key is None or not _IDEM_RE.match(idem_key):
                return error_reply(400, "invalid_payload", "A valid Idempotency-Key header is required.", rid)
            declared = headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                return error_reply(413, "payload_too_large", "The request body is too large.", rid)
            raw = await _read_body(receive)
            if raw is None:
                return error_reply(413, "payload_too_large", "The request body is too large.", rid)
            try:
                doc = parse_request_body(raw)
            except ValueError:
                return error_reply(400, "invalid_payload", "The request body is not valid.", rid)
            problem = _validate(endpoint, doc)
            if problem:
                return error_reply(400, "invalid_payload", problem, rid)
        return await core.decide(endpoint, method, path, idem_key, raw, doc, rid)


async def _read_body(receive) -> Optional[bytes]:
    """The request body, or None when it is over the limit (reading stops at the limit; nothing more is buffered)."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def in_test_mode() -> bool:
    return os.environ.get("YANDI_TEST_MODE") == "1"
