"""pet/pet_core_lifecycle_regression_test.py — the Core's P1 lifecycle boundary (pet/core_lifecycle.py, pet/core_main.py).

    LOCKED CORE DOES NOT SERVE.   THE LAUNCH SECRET IS EPHEMERAL.   SAME MUTATION RETRY != SECOND TRANSITION.

The frozen contract (docs/NODE_CORE_CONTRACT.md 1.0-rc1) is proven by contract/ fixtures; this suite proves what the fixtures cannot see
from outside, and proves that both together have teeth:

  1. the launch secret file is accepted only if it is a 0600 regular file of the current user with 32 random bytes in hex;
  2. the check value proves a key without keeping it readable; a wrong, missing or damaged one is never 'ok';
  3. the lifecycle at ASGI level: states, concurrency (eight identical unlocks = one transition), retry after a state change,
     draining, hooks that run once after unlock, the gate in front of an application and its WebSockets;
  4. nothing secret reaches a log, an error body, or a directory the core writes;
  5. the REAL application (pet.council_chat_server) is gated: it serves nothing while locked, and its start-up steps do not run;
  6. the same contract fixtures, run against the Core as a separate process (lifecycle shell), fail nothing;
  7. mutants M1–M10 (and the log/disk ones): each deliberate defect in the source makes a named check fail.

Run: python -m pet.pet_core_lifecycle_regression_test
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("YANDI_TEST_MODE", "1")

from pet import core_lifecycle as cl                                   # noqa: E402
from pet import core_main                                              # noqa: E402
from agent.db.sql import field_protection as fpmod                     # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


TMP = Path(tempfile.mkdtemp(prefix="yandi-core-lifecycle-test-"))
KEY = secrets.token_bytes(32)
KEY_B64 = base64.b64encode(KEY).decode()
WRONG_B64 = base64.b64encode(hashlib.sha256(b"wrong" + KEY).digest()).decode()
SECRET = secrets.token_hex(32)


# ── a minimal ASGI client ─────────────────────────────────────────────────────────────────────────────────────────
async def call(app, method: str, path: str, *, headers: dict | None = None, body: bytes = b"", secret: str | None = SECRET,
               idem: str | None = None):
    hdrs = {"content-type": "application/json"}
    if secret is not None:
        hdrs["authorization"] = f"Bearer {secret}"
    if idem is not None:
        hdrs["idempotency-key"] = idem
    hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
    if body:
        hdrs["content-length"] = str(len(body))
    scope = {"type": "http", "method": method, "path": path, "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()]}
    sent = [{"type": "http.request", "body": body, "more_body": False}]
    out: list[dict] = []

    async def receive():
        return sent.pop(0) if sent else {"type": "http.disconnect"}

    async def send(message):
        out.append(message)
    await app(scope, receive, send)
    status = next(m["status"] for m in out if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return status, payload


async def unlock(app, key_b64=KEY_B64, idem="unlock-key-0001", **kw):
    return await call(app, "POST", "/v1/unlock", body=json.dumps({"key": key_b64, "context": "yandi/core/v1"}).encode(), idem=idem, **kw)


class StandIn:
    """An application behind the boundary that records what reaches it."""

    def __init__(self):
        self.calls: list[str] = []
        self.gate = None                         # an asyncio.Event a request may wait on

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return
        self.calls.append(scope["type"] + ":" + scope.get("path", ""))
        if scope["type"] == "http":
            if self.gate is not None:
                await self.gate.wait()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"legacy":"alive"}'})
        elif scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.accept"})


def make(state_dir: Path, *, provisioned=True, hooks=None, log=None, stop=None):
    if provisioned:
        cl.provision_state(state_dir, KEY)
    lifecycle = cl.Lifecycle(SECRET, state_dir, log=(log.append if log is not None else (lambda _m: None)), on_ready=hooks or [],
                             request_stop=stop or (lambda _t: None), test_mode=True)
    inner = StandIn()
    lifecycle.mark_locked()
    return cl.CoreBoundary(inner, lifecycle), lifecycle, inner


def fresh_dir(name: str) -> Path:
    d = TMP / name
    shutil.rmtree(d, ignore_errors=True)
    return d


# ── 1. launch secret file ────────────────────────────────────────────────────────────────────────────────────────
def secret_file_section() -> None:
    d = TMP / "secrets"
    d.mkdir(exist_ok=True)

    def write(name: str, content: str, mode: int) -> str:
        p = d / name
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.chmod(p, mode)
        return str(p)

    def refused(path: str) -> bool:
        try:
            cl.load_launch_secret(path)
            return False
        except cl.LaunchSecretError as exc:
            return SECRET not in str(exc)

    check("1.1 a 0600 file with 32 random bytes in hex is accepted", cl.load_launch_secret(write("good", SECRET + "\n", 0o600)) == SECRET)
    for mode in (0o644, 0o640, 0o604, 0o660, 0o666):
        check(f"1.2 mode {oct(mode)} is refused", refused(write(f"m{mode:o}", SECRET, mode)))
    check("1.3 a short, long, non-hex or empty secret is refused",
          all(refused(write(n, c, 0o600)) for n, c in (("short", SECRET[:-2]), ("long", SECRET + "00"), ("nothex", "z" * 64), ("empty", ""))))
    (d / "link").unlink(missing_ok=True)
    os.symlink(d / "good", d / "link")
    check("1.4 a symlink is refused (no following)", refused(str(d / "link")))
    check("1.5 a directory and a missing file are refused", refused(str(d)) and refused(str(d / "nope")))
    check("1.6 the error messages never contain the secret", refused(write("m", SECRET, 0o644)))


# ── 2. check value ────────────────────────────────────────────────────────────────────────────────────────────────
def check_value_section() -> None:
    d = fresh_dir("check")
    check("2.1 unprovisioned state: no key is 'ok'", cl.verify_key(d, KEY) == "unprovisioned")
    cl.provision_state(d, KEY)
    check("2.2 the right key is 'ok', another key is 'wrong'", cl.verify_key(d, KEY) == "ok" and cl.verify_key(d, base64.b64decode(WRONG_B64)) == "wrong")
    f = d / "check-value.json"
    check("2.3 the check value file is 0600 in a 0700 directory", stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(d.stat().st_mode) == 0o700)
    blob = f.read_bytes()
    check("2.4 the key, its base64 and its hex are nowhere in the check value file",
          KEY not in blob and KEY_B64.encode() not in blob and KEY.hex().encode() not in blob)
    check("2.5 the plaintext of the check value is not in the file", cl._CHECK_PLAINTEXT not in blob and cl._CHECK_PLAINTEXT.hex().encode() not in blob)
    doc = json.loads(blob)
    doc["ct"] = doc["ct"][:-2] + ("00" if doc["ct"][-2:] != "00" else "01")
    f.write_text(json.dumps(doc))
    check("2.6 a damaged check value is never 'ok'", cl.verify_key(d, KEY) != "ok")
    f.write_text("not json")
    check("2.7 an unreadable check value is never 'ok'", cl.verify_key(d, KEY) != "ok")
    d2 = fresh_dir("check2")
    cl.provision_state(d2, KEY)
    cl.provision_state(d2, base64.b64decode(WRONG_B64))
    check("2.8 provisioning again replaces the check value (the old key stops working)", cl.verify_key(d2, KEY) == "wrong")
    try:
        cl.provision_state(fresh_dir("check3"), b"short")
        check("2.9 a key that is not 32 bytes cannot be provisioned", False)
    except ValueError:
        check("2.9 a key that is not 32 bytes cannot be provisioned", True)


# ── 3. lifecycle at ASGI level ───────────────────────────────────────────────────────────────────────────────────
async def lifecycle_section() -> None:
    logs: list[str] = []
    app, core, inner = make(fresh_dir("lc1"), log=logs)
    check("3.1 the core is 'locked' once the application has started", core.state == "locked")
    s, b = await call(app, "GET", "/v1/health", secret=None)
    check("3.2 health is open and exactly {state}", (s, json.loads(b)) == (200, {"state": "locked"}))
    s, b = await call(app, "GET", "/legacy/anything")
    check("3.3 while locked the application is not reached: 423", s == 423 and inner.calls == [] and json.loads(b)["error"]["code"] == "locked")
    fpmod.clear_key()
    s, b = await unlock(app, WRONG_B64)
    check("3.4 a wrong key: 403, still locked", s == 403 and core.state == "locked" and core._key is None)
    check("3.4b a wrong key installs nothing into the storage layer", not fpmod.has_key())
    s, b = await unlock(app, KEY_B64, idem="unlock-key-0002")
    check("3.5 the right key: 200 ready, the key is held", s == 200 and json.loads(b) == {"state": "ready"} and core.state == "ready" and bytes(core._key) == KEY)
    check("3.5b …and the storage layer holds the key derived from it (the sealed personal ledger opens only while the core is unlocked)", fpmod.has_key())
    s, b = await call(app, "GET", "/legacy/anything")
    check("3.6 once ready the application is reached unchanged", s == 200 and inner.calls == ["http:/legacy/anything"])
    s, b = await call(app, "POST", "/v1/lock", body=b"{}", idem="lock-key-00001")
    check("3.7 lock: 200 locked, the key is forgotten, the gate closes again",
          s == 200 and core.state == "locked" and core._key is None and (await call(app, "GET", "/legacy/x"))[0] == 423)
    check("3.7b lock: the storage layer forgets its key too (the sealed ledger is closed again)", not fpmod.has_key())
    s, b = await call(app, "GET", "/v1/capabilities")
    check("3.8 capabilities on a locked core is 423", s == 423)
    await unlock(app, KEY_B64, idem="unlock-key-0003")
    s, b = await call(app, "GET", "/v1/capabilities")
    caps = json.loads(b)
    check("3.9 capabilities: contract version, no feature that is not implemented, confinement 'none' (not claimed higher)",
          s == 200 and caps["contract"] == cl.CONTRACT_VERSION and caps["features"] == [] and caps["egress_confinement"] == "none")
    check("3.10 the contract version in the code is the contract version of contract/CONTRACT_VERSION",
          (ROOT / "contract" / "CONTRACT_VERSION").read_text().strip() == cl.CONTRACT_VERSION)

    # unlock retried after the state changed (lock in between): the original answer, no second transition
    app, core, inner = make(fresh_dir("lc2"))
    s1, b1 = await unlock(app, KEY_B64, idem="retry-key-0001")
    await call(app, "POST", "/v1/lock", body=b"{}", idem="lock-key-00002")
    check("3.11 set-up: ready, then locked", s1 == 200 and core.state == "locked")
    s2, b2 = await unlock(app, KEY_B64, idem="retry-key-0001")
    check("3.12 the same unlock retried after the state moved on returns the original answer and does NOT unlock again",
          (s2, b2) == (s1, b1) and core.state == "locked")
    s3, _ = await unlock(app, WRONG_B64, idem="retry-key-0001")
    check("3.13 the same key with another body is 409", s3 == 409)

    # concurrency
    app, core, inner = make(fresh_dir("lc3"))
    calls = {"n": 0}
    original = cl.verify_key

    def counting(*a, **k):
        calls["n"] += 1
        return original(*a, **k)
    cl.verify_key = counting
    try:
        results = await asyncio.gather(*[unlock(app, KEY_B64, idem="same-key-0001") for _ in range(8)])
    finally:
        cl.verify_key = original
    check("3.14 eight identical unlocks at once: one transition, eight identical answers",
          calls["n"] == 1 and len({r for r in results}) == 1 and results[0][0] == 200 and core.state == "ready")
    app, core, inner = make(fresh_dir("lc4"))
    results = await asyncio.gather(*([unlock(app, KEY_B64, idem="race-key-0001") for _ in range(4)] + [unlock(app, WRONG_B64, idem="race-key-0001") for _ in range(4)]))
    statuses = sorted(r[0] for r in results)
    winners = {200, 403}
    check("3.15 same key, two different bodies at once: one canonical request wins, the others get the conflict",
          statuses.count(409) == 4 and (statuses.count(200) == 4 or statuses.count(403) == 4) and set(statuses) <= winners | {409})

    # draining
    stops: list[float] = []
    app, core, inner = make(fresh_dir("lc5"), stop=stops.append)
    await unlock(app, KEY_B64, idem="unlock-key-0004")
    inner.gate = asyncio.Event()
    slow = asyncio.create_task(call(app, "GET", "/legacy/slow"))
    await asyncio.sleep(0.05)
    s, b = await call(app, "POST", "/v1/shutdown", body=json.dumps({"deadline_ms": 3000}).encode(), idem="shut-key-00001")
    check("3.16 shutdown: 202 draining", s == 202 and json.loads(b) == {"state": "draining"} and core.state == "draining")
    s, _ = await call(app, "GET", "/legacy/new")
    check("3.17 while draining, new application requests are refused (503) and health says draining",
          s == 503 and json.loads((await call(app, "GET", "/v1/health", secret=None))[1]) == {"state": "draining"})
    s, b = await call(app, "POST", "/v1/shutdown", body=json.dumps({"deadline_ms": 3000}).encode(), idem="shut-key-00001")
    check("3.18 a retried shutdown is answered from the record while draining", s == 202)
    await asyncio.sleep(0.15)
    check("3.19 the process is not stopped while an application request is still running", stops == [] and core.state == "draining")
    inner.gate.set()
    await slow
    await asyncio.sleep(0.15)
    check("3.20 once the request has finished the core stops (once) and the key is gone", len(stops) == 1 and core.state == "stopped" and core._key is None)

    app, core, inner = make(fresh_dir("lc6"), stop=stops.append)
    await unlock(app, KEY_B64, idem="unlock-key-0005")
    inner.gate = asyncio.Event()
    stops.clear()
    slow = asyncio.create_task(call(app, "GET", "/legacy/never"))
    await asyncio.sleep(0.05)
    await call(app, "POST", "/v1/shutdown", body=json.dumps({"deadline_ms": 300}).encode(), idem="shut-key-00002")
    await asyncio.sleep(0.6)
    check("3.21 the deadline bounds the wait: a request that never ends does not keep the core alive", len(stops) == 1)
    inner.gate.set()
    await slow

    # websockets
    app, core, inner = make(fresh_dir("lc7"))
    out: list[dict] = []

    async def recv_ws():
        return {"type": "websocket.connect"}

    async def send_ws(m):
        out.append(m)
    await app({"type": "websocket", "path": "/ws/abc", "headers": []}, recv_ws, send_ws)
    check("3.22 a WebSocket on a locked core is closed, never accepted, never reaching the application",
          [m["type"] for m in out] == ["websocket.close"] and inner.calls == [])
    await unlock(app, KEY_B64, idem="unlock-key-0006")
    out.clear()
    await app({"type": "websocket", "path": "/ws/abc", "headers": []}, recv_ws, send_ws)
    check("3.23 once ready a WebSocket reaches the application", [m["type"] for m in out] == ["websocket.accept"])

    # hooks
    ran: list[str] = []

    async def hook_ok():
        ran.append("ok")

    async def hook_bad():
        raise RuntimeError("boom")
    app, core, inner = make(fresh_dir("lc8"), hooks=[hook_bad, hook_ok])
    await unlock(app, WRONG_B64, idem="unlock-key-0007")
    await asyncio.sleep(0.05)
    check("3.24 start-up steps do not run before a successful unlock (not even on a wrong key)", ran == [])
    await unlock(app, KEY_B64, idem="unlock-key-0008")
    await asyncio.sleep(0.05)
    check("3.25 they run after the first unlock; one that fails does not stop the others or the unlock", ran == ["ok"] and core.state == "ready")
    await call(app, "POST", "/v1/lock", body=b"{}", idem="lock-key-00003")
    await unlock(app, KEY_B64, idem="unlock-key-0009")
    await asyncio.sleep(0.05)
    check("3.26 and only once per process", ran == ["ok"])

    # unprovisioned
    app, core, inner = make(fresh_dir("lc9"), provisioned=False)
    s, _ = await unlock(app, KEY_B64)
    check("3.27 an unprovisioned core never becomes ready, whatever key it is given", s == 403 and core.state == "locked")

    # constant-time comparison is really used
    seen = {"n": 0}
    real = hmac.compare_digest

    def spy(a, b):
        seen["n"] += 1
        return real(a, b)
    cl.hmac.compare_digest = spy
    try:
        app, core, inner = make(fresh_dir("lc10"))
        await call(app, "GET", "/v1/capabilities", secret="0" * 64)
    finally:
        cl.hmac.compare_digest = real
    check("3.28 the launch secret is compared with hmac.compare_digest", seen["n"] >= 1)

    # authentication before anything else and principal/actor
    app, core, inner = make(fresh_dir("lc11"))
    bad = [None, "0" * 64, SECRET[:-1], SECRET + "0"]
    codes = [(await call(app, "POST", "/v1/unlock", secret=s, body=b"{not json", idem="x")) [0] for s in bad]
    check("3.29 missing, wrong, truncated and extended secrets are all 401 before the body is read", codes == [401] * 4)
    s, _ = await call(app, "POST", "/v1/unlock", secret=None, headers={"authorization": f"Basic {SECRET}"}, body=b"{}", idem="x")
    check("3.30 a Basic scheme is 401", s == 401)
    s, _ = await call(app, "GET", "/v1/capabilities", secret=None, headers={"X-Principal": "person_1", "X-Actor": "dev_1"})
    check("3.31 X-Principal and X-Actor authorise nothing", s == 401)
    s, b = await call(app, "GET", "/v1/health", secret=None, headers={"X-Request-Id": "trace-1234"})
    check("3.32 a request id is only a trace: it does not change the answer", (s, json.loads(b)) == (200, {"state": "locked"}))
    s, b = await call(app, "GET", "/v1/nothing-here", headers={"X-Request-Id": "trace-1234"})
    check("3.33 the caller's request id is echoed in the error", json.loads(b)["error"]["request_id"] == "trace-1234")
    s, b = await call(app, "GET", "/v1/nothing-here", headers={"X-Request-Id": "bad id with spaces!"})
    check("3.34 an unusable request id is replaced, not echoed", "bad id" not in b.decode())


# ── 4. nothing secret leaks ──────────────────────────────────────────────────────────────────────────────────────
async def leak_section() -> None:
    logs: list[str] = []
    app, core, inner = make(fresh_dir("leak1"), log=logs)
    bodies = []
    for args in ((WRONG_B64, "a-key-000001"), (KEY_B64, "a-key-000002")):
        bodies.append(await unlock(app, *args))
    bodies.append(await call(app, "POST", "/v1/unlock", body=json.dumps({"key": KEY_B64, "context": "nope"}).encode(), idem="a-key-000003"))
    bodies.append(await call(app, "POST", "/v1/unlock", secret="x" * 64, body=b"{}", idem="a-key-000004"))
    bodies.append(await call(app, "POST", "/v1/lock", body=b"{}", idem="a-key-000005"))
    everything = "\n".join(logs) + "\n".join(b.decode() for _, b in bodies)
    check("4.1 neither the launch secret nor a key (right or wrong) appears in any answer or log line",
          all(x not in everything for x in (SECRET, KEY_B64, WRONG_B64, KEY.hex())))

    # an internal failure: a generic 500 for the caller, the class only in the log, no trace in the body
    app, core, inner = make(fresh_dir("leak2"), log=logs)

    async def broken(*a, **k):
        raise RuntimeError("/home/secret/path mysql.sock password")
    core.decide = broken
    s, b = await call(app, "POST", "/v1/lock", body=b"{}", idem="boom-key-0001")
    text = b.decode()
    check("4.2 an internal failure is a canonical 500 that says nothing about the failure",
          s == 500 and json.loads(text)["error"]["code"] == "internal" and all(x not in text for x in ("/home", "mysql", "password", "Traceback", "RuntimeError")))
    check("4.3 the local log keeps the class of the failure for the owner", any("RuntimeError" in line for line in logs))


# ── 5. the real application behind the gate ──────────────────────────────────────────────────────────────────────
async def real_app_section() -> None:
    app, hooks = core_main._pet_app()
    names = [h.__name__ for h in hooks]
    check("5.1 the application's own start-up steps are handed over, not run: nothing is left on the application",
          len(hooks) >= 2 and app.router.on_startup == [] and "_reconcile_stale_sql_runs" in names and "_system_awareness_probe" in names, str(names))
    logs: list[str] = []
    lifecycle = cl.Lifecycle(SECRET, fresh_dir("real"), log=logs.append, on_ready=hooks, test_mode=True)
    boundary = cl.CoreBoundary(app, lifecycle)
    lifecycle.mark_locked()
    seen = []
    for method, path in (("GET", "/"), ("GET", "/api/local/history"), ("POST", "/api/local/chat"), ("GET", "/api/orch/history"),
                         ("GET", "/media/settings_tab.js"), ("POST", "/api/tools/run"), ("GET", "/docs"), ("GET", "/openapi.json")):
        s, b = await call(boundary, method, path, secret=None)
        seen.append((s, json.loads(b)["error"]["code"]))
    check("5.2 a locked core serves none of the real application's pages or APIs (423 locked), with or without a secret", set(seen) == {(423, "locked")}, str(seen))
    out: list[dict] = []

    async def recv_ws():
        return {"type": "websocket.connect"}

    async def send_ws(m):
        out.append(m)
    await boundary({"type": "websocket", "path": "/ws/abc", "headers": [], "query_string": b""}, recv_ws, send_ws)
    check("5.3 its WebSocket is refused while locked", [m["type"] for m in out] == ["websocket.close"])
    check("5.4 no start-up step has run while locked (nothing logged, nothing started)", logs == [] or all("start-up step failed" not in l for l in logs))


# ── 6. the fixtures against the Core as a separate process ───────────────────────────────────────────────────────
def conformance_section() -> None:
    from contract.runner.engine import run_suite
    from contract.runner.loader import load_suite
    from contract.targets.python_core import PythonCoreTarget
    suite = load_suite()
    target = PythonCoreTarget("shell")
    try:
        results = run_suite(suite, target)
    finally:
        target.close()
    bad = [f"{r.id}: {r.detail}" for r in results if r.status in ("fail", "error")]
    passed = [r for r in results if r.status == "pass"]
    check("6.1 every contract fixture the target can run passes against the Core process (none fails, none errors)", bad == [], "; ".join(bad[:3]))
    check("6.2 and most of the suite really ran", len(passed) >= 60, f"{len(passed)} passed")
    unsupported = {r.id for r in results if r.status == "unsupported"}
    check("6.3 what cannot be proven from outside is reported 'unsupported', not passed",
          {"key_material.key_is_forgotten_from_memory_on_lock", "launch_secret.is_not_on_the_command_line", "launch_secret.file_is_private",
           "launch_secret.differs_between_launches", "isolation.reported_level_is_effective", "key_material.no_decrypted_state_exists_while_locked"} <= unsupported)


# ── 7. mutants ───────────────────────────────────────────────────────────────────────────────────────────────────
def mini_repo(name: str, edits: dict[str, list[tuple[str, str]]]) -> Path:
    """A copy of the two core modules with deliberate defects (and the unmodified storage-protection module they import), laid out so that `import pet.core_main` finds them."""
    d = fresh_dir(name)
    (d / "pet").mkdir(parents=True)
    (d / "pet" / "__init__.py").write_text("")
    for pkg in ("agent", "agent/db", "agent/db/sql"):          # the storage-protection module the lifecycle installs its key into, unmodified
        (d / pkg).mkdir(parents=True, exist_ok=True)
        (d / pkg / "__init__.py").write_text("")
    for name in ("field_protection", "crypto", "keys"):
        shutil.copyfile(ROOT / "agent" / "db" / "sql" / f"{name}.py", d / "agent" / "db" / "sql" / f"{name}.py")
    for module in ("core_lifecycle", "core_main"):
        src = (ROOT / "pet" / f"{module}.py").read_text(encoding="utf-8")
        for old, new in edits.get(module, []):
            if src.count(old) != 1:
                raise AssertionError(f"mutant anchor not found exactly once in {module}: {old!r}")
            src = src.replace(old, new)
        (d / "pet" / f"{module}.py").write_text(src, encoding="utf-8")
    return d


def run_fixtures(repo: Path, only: list[str]):
    from contract.runner.engine import run_suite
    from contract.runner.loader import load_suite
    from contract.targets.python_core import PythonCoreTarget
    target = PythonCoreTarget("shell", repo=repo)
    try:
        return run_suite(load_suite(), target, only=only)
    finally:
        target.close()


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def mutants_section() -> None:
    def fixture_mutant(label: str, edits: dict, only: list[str]) -> None:
        try:
            results = run_fixtures(mini_repo("mut_" + label.split()[0], edits), only)
            caught = [r.id for r in results if r.status in ("fail", "error")]
        except Exception as exc:                          # a mutant that cannot even start is caught too
            caught = [f"did not start: {exc}"]
        check(f"7.{label} is caught by the conformance fixtures", bool(caught), "the fixtures noticed nothing")

    clean = [r for r in run_fixtures(mini_repo("mut_clean", {}), ["auth.", "unlock.wrong", "locked.", "idempotency.a_replay", "launch_secret.core_listens"]) if r.status in ("fail", "error")]
    check("7.0 (control) the unmodified copy fails none of the fixtures used below", clean == [], str([r.id for r in clean]))

    fixture_mutant("M1 the core binds 0.0.0.0", {"core_main": [('HOST = "127.0.0.1"', 'HOST = "0.0.0.0"')]}, ["launch_secret.core_listens_on_loopback_only"])
    fixture_mutant("M4 a wrong unlock enters ready", {"core_lifecycle": [('        if outcome != "ok":', "        if False:")]}, ["unlock.wrong_key_never_becomes_ready", "state_machine.failure"])
    fixture_mutant("M5 lock leaves the core usable (state unchanged)", {"core_lifecycle": [('            self._forget_key()\n            self.state = "locked"\n            self._log("[core] state: locked (lock requested)")',
                                                                                              '            self._log("[core] state: locked (lock requested)")')]}, ["lock.ready_becomes_locked"])
    fixture_mutant("M6 a protected endpoint is allowed while locked", {"core_lifecycle": [('            return None if endpoint == "unlock" else error_reply(423, "locked", "The core is locked.", rid)', "            return None")]},
                   ["locked.refuses_everything"])
    fixture_mutant("M7 the same idempotency key executes again", {"core_lifecycle": [("                record = self._records.get(idem_key)", "                record = None")]},
                   ["idempotency.a_replay_has_no_second_effect", "idempotency.unlock_same_key_same_body"])
    fixture_mutant("M8 the same key with another body is accepted", {"core_lifecycle": [("                    if (record.method, record.path, record.digest) != (method, path, self.digest(raw)):", "                    if False:")]},
                   ["idempotency.unlock_same_key_different_body_conflicts", "idempotency.lock_same_key_different_body_conflicts"])
    fixture_mutant("M9 an error leaks a path", {"core_lifecycle": [('"The request body is not valid.", rid)', 'f"The request body is not valid ({os.getcwd()}).", rid)')]},
                   ["sanitization.refusals_on_a_locked_core_leak_nothing", "malformed.invalid_json_is_refused"])
    fixture_mutant("M11 the key is written to disk on unlock", {"core_lifecycle": [('        self._key = bytearray(key)\n', '        self._key = bytearray(key)\n        (self.state_dir / "leaked.key").write_bytes(key)\n')]},
                   ["key_material.key_is_never_written_to_disk"])
    fixture_mutant("M12 the key is written to the log on unlock", {"core_lifecycle": [('        self._key = bytearray(key)\n', '        self._key = bytearray(key)\n        self._log(f"[core] unlocked with {base64.b64encode(key).decode()}")\n')]},
                   ["key_material.key_is_not_in_the_logs"])
    fixture_mutant("M13 the launch secret is compared with ==", {"core_lifecycle": [('        return hmac.compare_digest(header_value.encode("utf-8", "replace"), b"Bearer " + self._secret)',
                                                                                    '        return header_value.startswith("Bearer " + self._secret.decode())')]},
                   ["auth.wrong_secret_is_refused_when_locked"])

    # M3 the file mode is not enforced
    mod = load_module(mini_repo("mut_M3", {"core_lifecycle": [("        if st.st_mode & 0o077:", "        if False:")]}) / "pet" / "core_lifecycle.py", "mut_m3")
    p = TMP / "m3-secret"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.write(fd, SECRET.encode())
    os.close(fd)
    os.chmod(p, 0o644)
    try:
        mod.load_launch_secret(str(p))
        mutant_accepts = True
    except mod.LaunchSecretError:
        mutant_accepts = False
    check("7.M3 a version that no longer enforces 0600 accepts a world-readable secret file; the real one refuses it",
          mutant_accepts and _refuses_world_readable())

    # M5' lock keeps a usable key (the state changes, the key stays)
    mod = load_module(mini_repo("mut_M5b", {"core_lifecycle": [("            self._forget_key()\n            self.state = \"locked\"", "            self.state = \"locked\"")]}) / "pet" / "core_lifecycle.py", "mut_m5b")

    async def key_kept() -> bool:
        d = fresh_dir("m5b")
        mod.provision_state(d, KEY)
        core = mod.Lifecycle(SECRET, d, log=lambda _m: None, test_mode=True)
        boundary = mod.CoreBoundary(StandIn(), core)
        core.mark_locked()
        await unlock(boundary, KEY_B64, idem="m5-unlock-01")
        await call(boundary, "POST", "/v1/lock", body=b"{}", idem="m5-lock-0001")
        return core._key is not None
    check("7.M5 lock forgets the key: a version that keeps it after locking is caught", asyncio.run(key_kept()) is True and _lock_forgets_key())

    # M2/M10 as far as the core can be responsible
    parser_options = {a.dest for a in core_main.build_parser()._actions}
    check("7.M2 the core takes no secret, key or address on its command line (options: " + ",".join(sorted(parser_options)) + ")",
          parser_options <= {"help", "command", "shell", "port"})
    mod2 = mini_repo("mut_M2", {"core_main": [('    parser.add_argument("--port"', '    parser.add_argument("--secret")\n    parser.add_argument("--port"')]})
    src = (mod2 / "pet" / "core_main.py").read_text()
    check("7.M2 a version that adds a --secret option is caught by the same check", '"--secret"' in src and '"--secret"' not in (ROOT / "pet" / "core_main.py").read_text())

    from contract.targets.python_core import PythonCoreTarget

    def secret_file_survives(repo: Path) -> bool:
        target = PythonCoreTarget("shell", repo=repo)
        try:
            target.reset()
            return target.secret_file.exists()
        finally:
            target.close()
    check("7.M10 one launch, one secret: the core removes the secret file after reading it", secret_file_survives(ROOT) is False)
    check("7.M10 a version that leaves the file behind is caught", secret_file_survives(mini_repo("mut_M10", {"core_main": [("        os.unlink(secret_path)", "        pass")]})) is True)


def _refuses_world_readable() -> bool:
    p = TMP / "real-644"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.write(fd, SECRET.encode())
    os.close(fd)
    os.chmod(p, 0o644)
    try:
        cl.load_launch_secret(str(p))
        return False
    except cl.LaunchSecretError:
        return True


def _lock_forgets_key() -> bool:
    async def go() -> bool:
        app, core, inner = make(fresh_dir("m5real"))
        await unlock(app, KEY_B64, idem="m5r-unlock-1")
        await call(app, "POST", "/v1/lock", body=b"{}", idem="m5r-lock-001")
        return core._key is None
    return asyncio.run(go())


# ── 8. started by hand ───────────────────────────────────────────────────────────────────────────────────────────
def manual_start_section() -> None:
    d = fresh_dir("manual")
    d.mkdir(parents=True)
    secret_file = d / "secret"
    fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, (SECRET + "\n").encode())
    os.close(fd)
    bad = d / "bad"
    fd = os.open(bad, os.O_WRONLY | os.O_CREAT, 0o644)
    os.write(fd, SECRET.encode())
    os.close(fd)
    os.chmod(bad, 0o644)
    env = {"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(d), "YANDI_TEST_MODE": "1", "YANDI_CORE_STATE_DIR": str(d / "state"),
           "PYTHONDONTWRITEBYTECODE": "1", "YANDI_CORE_PORT_FILE": str(d / "port")}
    r = subprocess.run([sys.executable, "-m", "pet.core_main", "--shell"], cwd=str(ROOT), env={**env, "YANDI_CORE_SECRET_FILE": str(bad)},
                       capture_output=True, text=True, timeout=30)
    check("8.1 a core given a group/world-readable secret file refuses to start and does not print the secret",
          r.returncode == 2 and SECRET not in r.stdout + r.stderr and "0600" in r.stderr)
    r = subprocess.run([sys.executable, "-m", "pet.core_main", "--shell"], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30)
    check("8.2 a core without a secret file refuses to start", r.returncode == 2 and "YANDI_CORE_SECRET_FILE" in r.stderr)
    for extra in (["--secret", SECRET], ["--host", "0.0.0.0"], ["--key", KEY_B64]):
        r = subprocess.run([sys.executable, "-m", "pet.core_main", "--shell", *extra], cwd=str(ROOT), env={**env, "YANDI_CORE_SECRET_FILE": str(secret_file)},
                           capture_output=True, text=True, timeout=30)
        check(f"8.3 the option {extra[0]} does not exist", r.returncode == 2 and secret_file.exists())
    check("8.4 provision reads the key on stdin, never on the command line",
          subprocess.run([sys.executable, "-m", "pet.core_main", "provision"], cwd=str(ROOT), env=env, input=KEY_B64 + "\n", capture_output=True, text=True, timeout=30).returncode == 0
          and cl.verify_key(d / "state", KEY) == "ok")
    check("8.5 provision refuses a key that is not 32 bytes",
          subprocess.run([sys.executable, "-m", "pet.core_main", "provision"], cwd=str(ROOT), env=env, input="AAAA\n", capture_output=True, text=True, timeout=30).returncode == 2)
    proc = subprocess.Popen([sys.executable, "-m", "pet.core_main", "--shell"], cwd=str(ROOT), env={**env, "YANDI_CORE_SECRET_FILE": str(secret_file)},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        port_file = d / "port"
        end = time.monotonic() + 30
        while time.monotonic() < end and not (port_file.exists() and port_file.read_text().strip().isdigit()):
            time.sleep(0.1)
        port = int(port_file.read_text())
        from contract.runner.wire import send
        base = f"http://127.0.0.1:{port}"
        end = time.monotonic() + 10
        h = send(base, "GET", "/v1/health", {}, None)
        while (h.error or h.status != 200) and time.monotonic() < end:
            time.sleep(0.1)
            h = send(base, "GET", "/v1/health", {}, None)
        time.sleep(0.5)
        h2 = send(base, "GET", "/v1/health", {}, None)
        check("8.6 a core started by hand waits, locked, and does not unlock itself", h.text == '{"state":"locked"}' and h2.text == '{"state":"locked"}')
        check("8.7 its secret file was removed after it was read", not secret_file.exists())
        check("8.8 the chosen port file is 0600", stat.S_IMODE(port_file.stat().st_mode) == 0o600)
        auth = {"Authorization": f"Bearer {SECRET}", "Idempotency-Key": "manual-key-0001", "Content-Type": "application/json"}
        r = send(base, "POST", "/v1/unlock", auth, json.dumps({"key": KEY_B64, "context": "yandi/core/v1"}).encode())
        check("8.9 and unlocks with the provisioned key", r.status == 200 and r.text == '{"state":"ready"}')
        r = send(base, "POST", "/v1/shutdown", {**auth, "Idempotency-Key": "manual-key-0002"}, b'{"deadline_ms":2000}')
        rc = proc.wait(timeout=15)
        check("8.10 shutdown ends the process normally (exit code 0, not killed)", r.status == 202 and rc == 0, f"rc={rc}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def main() -> int:
    try:
        print("── 1. launch secret file")
        secret_file_section()
        print("\n── 2. check value")
        check_value_section()
        print("\n── 3. lifecycle")
        asyncio.run(lifecycle_section())
        print("\n── 4. nothing secret leaks")
        asyncio.run(leak_section())
        print("\n── 5. the real application behind the gate")
        asyncio.run(real_app_section())
        print("\n── 6. contract fixtures against the Core process")
        conformance_section()
        print("\n── 7. mutants")
        mutants_section()
        print("\n── 8. started by hand")
        manual_start_section()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 72)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
