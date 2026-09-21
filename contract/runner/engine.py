"""Execute fixtures against a running core.

Outcomes of one scenario:
  pass            every expectation held
  fail            the system under test did not do what the contract says
  pending         the fixture is data for a chapter that is not testable yet (never counted as pass)
  unsupported     the target does not offer a hook the scenario needs (never a false pass)
  not_applicable  the scenario applies only to some capabilities and this core reports otherwise
  error           the runner could not work (target unreachable at reset, a bug in the runner)
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field

from .checks import check_response, substitute
from .loader import Scenario, Suite
from .wire import Response, send


class TargetUnavailable(Exception):
    """The target cannot be brought to a fresh state (infrastructure problem, not a contract failure)."""


class ProbeUnsupported(Exception):
    """A target offers a hook in general but cannot honestly answer this particular probe: the scenario is 'unsupported', never
    a pass (a probe that could only ever say what the fixture hopes to hear proves nothing)."""


@dataclass
class Result:
    id: str
    status: str
    detail: str = ""
    file: str = ""
    steps: list = field(default_factory=list)


def wrong_key_for(valid_b64: str) -> str:
    """A well-formed key that is different from the valid one."""
    return base64.b64encode(hashlib.sha256(b"wrong:" + base64.b64decode(valid_b64)).digest()).decode()


class Run:
    """State of one scenario execution."""

    def __init__(self, suite: Suite, target, sc: Scenario):
        self.suite, self.target, self.sc = suite, target, sc
        self.ctx = {
            "secret": target.launch_secret,
            "secret.short": target.launch_secret[:-1],
            "key.valid": target.unlock_key,
            "key.wrong": wrong_key_for(target.unlock_key),
            "context": "yandi/core/v1",
            "run": secrets.token_hex(6),
            "contract_version": suite.version,
        }
        self.counter = 0
        self.earlier: dict[str, Response] = {}
        self.log: list[dict] = []

    # ── request construction ─────────────────────────────────────────────────────────────────────────────────
    def auto_key(self) -> str:
        self.counter += 1
        return f"auto-{self.ctx['run']}-{self.counter}"

    def build(self, req: dict) -> tuple[str, str, dict, bytes | None]:
        method = req["method"]
        path = substitute(req["path"], self.ctx)
        headers: dict[str, str] = {}
        auth = req.get("auth", "launch_secret")
        if auth == "launch_secret":
            headers["Authorization"] = f"Bearer {self.ctx['secret']}"
        elif isinstance(auth, dict):
            headers["Authorization"] = substitute(auth["authorization"], self.ctx)
        idem = req.get("idempotency_key", "auto" if method != "GET" else "none")
        if idem == "auto":
            headers["Idempotency-Key"] = self.auto_key()
        elif idem != "none":
            headers["Idempotency-Key"] = substitute(idem, self.ctx)
        body = self.body_bytes(req)
        if body is not None:
            headers["Content-Type"] = "application/json"
        for k, v in req.get("headers", {}).items():
            headers[k] = substitute(v, self.ctx)
        return method, path, headers, body

    def body_bytes(self, req: dict) -> bytes | None:
        if "body_raw" in req:
            return substitute(req["body_raw"], self.ctx).encode("utf-8")
        if "body_hex" in req:
            return bytes.fromhex(req["body_hex"])
        if "body_generated" in req:
            depth = req["body_generated"]["depth"]
            return b"[" * depth + b"]" * depth
        if "body" in req:
            obj = substitute(req["body"], self.ctx)
            if "body_pad_to" in req:
                obj = json.loads(json.dumps(obj))
                obj.setdefault("extensions", {})["pad"] = ""
                base = len(json.dumps(obj, separators=(",", ":")).encode())
                want = req["body_pad_to"]
                if want < base:
                    raise ValueError(f"body_pad_to {want} is smaller than the body itself ({base} bytes)")
                obj["extensions"]["pad"] = "a" * (want - base)
            return json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return None

    def send_req(self, req: dict) -> Response:
        method, path, headers, body = self.build(req)
        return send(self.target.base_url, method, path, headers, body)

    # ── logging (secrets never reach a report) ───────────────────────────────────────────────────────────────
    def redact(self, text: str) -> str:
        for name in ("secret", "key.valid", "key.wrong"):
            text = text.replace(self.ctx[name], f"<{name}>")
        return text

    def note(self, what: str, resp: Response | None = None):
        entry = {"step": what}
        if resp is not None:
            entry["status"] = resp.status if resp.status is not None else resp.error
            entry["body"] = self.redact(resp.text[:160])
        self.log.append(entry)


def _order(scenarios: list[Scenario]) -> list[Scenario]:
    return sorted(scenarios, key=lambda s: bool(s.data.get("destructive")))


def run_suite(suite: Suite, target, only: list[str] | None = None, lenient: bool = False, on_result=None, supervisor=None) -> list[Result]:
    results: list[Result] = []
    for sc in _order(suite.scenarios):
        if only and not any(sc.id.startswith(p) for p in only):
            continue
        res = run_scenario(suite, target, sc, lenient, supervisor)
        results.append(res)
        if on_result:
            on_result(res)
    return results


def run_scenario(suite: Suite, target, sc: Scenario, lenient: bool = False, supervisor=None) -> Result:
    res = Result(id=sc.id, status="pass", file=sc.file)
    if sc.status == "pending":
        res.status, res.detail = "pending", sc.data.get("pending_reason", "")
        return res
    if sc.kind == "supervisor":
        if supervisor is None:
            res.status, res.detail = "unsupported", "no supervisor harness was given (--supervisor-harness)"
            return res
        return supervisor.run(sc, suite.leaks)
    missing = [h for h in sc.requires if h not in target.hooks]
    if missing:
        res.status, res.detail = "unsupported", f"target offers no hook {missing}"
        return res
    try:
        try:
            if not lenient:                    # lenient = no preparation at all: the requests go out exactly as the fixture says
                if hasattr(target, "configure"):
                    target.configure(sc.requires)
                target.reset()
        except TargetUnavailable as exc:
            res.status, res.detail = "error", f"target could not be reset: {exc}"
            return res
        run = Run(suite, target, sc)                # after reset: a target that restarts gets a new launch secret and key each time
        problem = _prepare_given(run, lenient)
        if problem:
            res.status, res.detail = "fail", problem
            res.steps = run.log
            return res
        if not _applicable(run, res, lenient):
            res.steps = run.log
            return res
        for i, step in enumerate(sc.data["steps"], 1):
            problem = _do_step(run, i, step)
            if problem:
                res.status, res.detail = "fail", f"step {i}: {problem}"
                break
    except ProbeUnsupported as exc:
        res.status, res.detail = "unsupported", f"probe not answerable by this target: {exc}"
    except Exception as exc:  # a bug in the runner or a broken fixture must not look like a pass
        res.status, res.detail = "error", f"runner error: {type(exc).__name__}: {exc}"
    res.steps = getattr(locals().get("run"), "log", [])
    return res


def _prepare_given(run: Run, lenient: bool) -> str | None:
    state = run.sc.data["given"]["state"]
    if lenient:
        return None
    h = send(run.target.base_url, "GET", "/v1/health", {}, None)
    run.note("given: health", h)
    problems = _health_problems(run, h, "locked" if state == "locked" else None)
    if state == "locked":
        return f"given state 'locked' is not reachable: {problems[0]}" if problems else None
    if not problems and h.json()["state"] == "ready":
        return None
    if problems:
        return f"given state 'ready' is not reachable: {problems[0]}"
    unlock = {"method": "POST", "path": "/v1/unlock", "idempotency_key": f"prep-{run.ctx['run']}",
              "body": {"key": "${key.valid}", "context": "${context}"}}
    r = run.send_req(unlock)
    run.note("given: unlock", r)
    if r.status != 200:
        return f"given state 'ready' is not reachable: unlock answered {r.status if r.status else r.error}"
    return None


def _applicable(run: Run, res: Result, lenient: bool) -> bool:
    cond = run.sc.data.get("applies_when")
    if not cond or lenient:
        return True
    r = run.send_req({"method": "GET", "path": "/v1/capabilities"})
    run.note("applies_when: capabilities", r)
    try:
        caps = r.json()
        features, level = caps["features"], caps["egress_confinement"]
    except (ValueError, KeyError, TypeError):
        res.status, res.detail = "fail", "capabilities unreadable, so the scenario cannot say whether it applies"
        return False
    if "feature_absent" in cond and cond["feature_absent"] in features:
        res.status, res.detail = "not_applicable", f"core lists feature '{cond['feature_absent']}'"
        return False
    if "egress_confinement_in" in cond and level not in cond["egress_confinement_in"]:
        res.status, res.detail = "not_applicable", f"core reports egress_confinement '{level}'"
        return False
    return True


def _health_problems(run: Run, r: Response, want_state: str | None) -> list[str]:
    if r.error:
        return [f"health gave no answer ({r.error})"]
    exp = {"status": 200, "body_schema": "lifecycle/health"}
    if want_state:
        exp["body_pointer"] = {"/state": want_state}
    return check_response(exp, r, run.ctx, run.suite.schemas, run.suite.leaks, run.earlier)


def _do_step(run: Run, i: int, step: dict) -> str | None:
    sid = step.get("id")
    if "request" in step:
        req = step["request"]
        r = run.send_req(req)
        run.note(f"{req['method']} {req['path']}", r)
        problems = check_response(step["expect"], r, run.ctx, run.suite.schemas, run.suite.leaks, run.earlier)
        if sid:
            run.earlier[sid] = r
        return _join(req, problems)
    if "parallel" in step:
        par = step["parallel"]
        method, path, headers, body = run.build(par["request"])     # one key, one body: eight copies of the same request
        with concurrent.futures.ThreadPoolExecutor(max_workers=par["count"]) as pool:
            futs = [pool.submit(send, run.target.base_url, method, path, headers, body) for _ in range(par["count"])]
            answers = [f.result() for f in futs]
        run.note(f"{par['count']}x {method} {path}", answers[0])
        for n, r in enumerate(answers, 1):
            problems = check_response(par["expect_all"], r, run.ctx, run.suite.schemas, run.suite.leaks, run.earlier)
            if problems:
                return f"copy {n} of {par['count']}: {'; '.join(problems)}"
        if par.get("identical_bodies") and len({a.body for a in answers}) != 1:
            return "the copies did not all get the same answer"
        if sid:
            run.earlier[sid] = answers[0]
        return None
    if "assert_health" in step:
        r = send(run.target.base_url, "GET", "/v1/health", {}, None)
        run.note("GET /v1/health", r)
        problems = _health_problems(run, r, step["assert_health"])
        return f"health: {'; '.join(problems)}" if problems else None
    if "wait_stopped" in step:
        return _wait_stopped(run, step["wait_stopped"])
    if "probe" in step:
        p = step["probe"]
        got = run.target.probe(p["name"], substitute(p.get("args", {}), run.ctx))
        run.note(f"probe {p['name']}")
        want = substitute(p.get("expect", {}), run.ctx)
        wrong = {k: got.get(k) for k, v in want.items() if got.get(k) != v}
        return f"probe {p['name']} answered {wrong}, expected {want}" if wrong else None
    raise ValueError(f"unknown step {sorted(step)}")


def _wait_stopped(run: Run, spec: dict) -> str | None:
    deadline = time.monotonic() + spec["timeout_ms"] / 1000
    allowed = set(spec.get("allowed_health_states", []))
    while time.monotonic() < deadline:
        r = send(run.target.base_url, "GET", "/v1/health", {}, None, timeout=2.0)
        if r.error in ("connection_refused", "connection_reset"):
            run.note("wait_stopped: no answer, the core is gone", r)
            return None
        problems = _health_problems(run, r, None)
        if problems:
            return f"while stopping, health: {problems[0]}"
        state = r.json()["state"]
        if state not in allowed and state != "stopped":
            return f"while stopping, health said '{state}' (only {sorted(allowed) or 'nothing'} allowed)"
        time.sleep(0.05)
    return f"the core still answered after {spec['timeout_ms']} ms"


def _join(req: dict, problems: list[str]) -> str | None:
    return f"{req['method']} {req['path']}: " + "; ".join(problems) if problems else None
