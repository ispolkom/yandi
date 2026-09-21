"""contract/contract_regression_test.py — the contract's own files are sound, and the fixtures bite.

    SCHEMAS · FIXTURES · COVERAGE · RUNNER SAFETY · A SUITE THAT CANNOT PASS BY ACCIDENT

Offline and hermetic: no live system, no database, no network beyond throw-away loopback servers on random ports.

  1. the schemas load, obey the strictness policy (requests closed, answers additive, health closed) and accept/refuse a corpus;
  2. every fixture file is valid against the meta-schema; a typo (`expected_statuz`), an unknown placeholder, a wrong section or
     invariant, a duplicate id or a missing trace is refused before a request is sent;
  3. the fixtures are language-neutral data (no Python, no live port, no product name in them);
  4. coverage.json cannot claim more than the fixtures deliver;
  5. the test double (contract/selftest/reference_double.py) satisfies every fixture that needs no hook;
  6. every deliberately broken double ("fault", 38 of them) makes a NAMED fixture fail — the suite has teeth;
  7. a system that implements nothing (404 to everything, or 200 {} to everything) passes no fixture — the baseline is RED;
  8. the runner refuses the live system's ports and non-loopback addresses, and no report contains a secret.

Run: python -m contract.contract_regression_test
"""
from __future__ import annotations

import copy
import http.server
import json
import re
import sys
import threading
from pathlib import Path

from contract.runner import coverage
from contract.runner.engine import run_suite
from contract.runner.loader import (ROOT, ContractError, lint_context, lint_scenario_doc, lint_schema_policy, load_schemas, load_suite)
from contract.runner.targets import ExternalTarget
from contract.runner.wire import LIVE_PORTS, RefusedTarget, check_target_url
from contract.selftest.reference_double import FAULTS, ReferenceTarget

REPO = ROOT.parent
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
CTX = "yandi/core/v1"


# ── 1. schemas ─────────────────────────────────────────────────────────────────────────────────────────────────
def schemas_section() -> None:
    schemas = load_schemas()
    check("1.1 every schema loads, has a matching $id and version, and is a valid Draft 2020-12 schema", len(schemas.by_name) >= 12)
    problems = lint_schema_policy(schemas)
    check("1.2 policy: requests closed at every level, answers additive, health closed", problems == [], "; ".join(problems))

    def ok(name, doc): return schemas.errors(name, doc) == []

    check("1.3 unlock request: valid", ok("lifecycle/unlock.request", {"key": KEY, "context": CTX}))
    check("1.4 unlock request: extensions accepted", ok("lifecycle/unlock.request", {"key": KEY, "context": CTX, "extensions": {"a": [1]}}))
    for label, doc in {
        "unknown field": {"key": KEY, "context": CTX, "admin": True},
        "missing key": {"context": CTX}, "missing context": {"key": KEY},
        "wrong context": {"key": KEY, "context": "yandi/core/v2"},
        "short key": {"key": "AAAAAAAAAAAAAAAAAAAAAA==", "context": CTX},
        "url-safe key": {"key": "-" * 43 + "=", "context": CTX},
        "unpadded key": {"key": "A" * 43, "context": CTX}, "number key": {"key": 5, "context": CTX},
        "extensions not an object": {"key": KEY, "context": CTX, "extensions": "x"},
    }.items():
        check(f"1.5 unlock request refused: {label}", not ok("lifecycle/unlock.request", doc))
    check("1.6 lock request: empty object and extensions valid", ok("lifecycle/lock.request", {}) and ok("lifecycle/lock.request", {"extensions": {}}))
    check("1.7 lock request: unknown field refused", not ok("lifecycle/lock.request", {"reason": "x"}))
    check("1.8 shutdown request: deadline optional and positive integer", ok("lifecycle/shutdown.request", {}) and ok("lifecycle/shutdown.request", {"deadline_ms": 5000}))
    for label, doc in {"zero": {"deadline_ms": 0}, "negative": {"deadline_ms": -1}, "string": {"deadline_ms": "5"}, "fraction": {"deadline_ms": 1.5}, "unknown": {"now": True}}.items():
        check(f"1.9 shutdown request refused: {label}", not ok("lifecycle/shutdown.request", doc))
    check("1.10 health: exactly {state}", ok("lifecycle/health", {"state": "locked"}) and ok("lifecycle/health", {"state": "draining"}))
    check("1.11 health: any extra field refused (open endpoint stays minimal)", not ok("lifecycle/health", {"state": "ready", "version": "1"}))
    check("1.12 health: unknown state and missing state refused", not ok("lifecycle/health", {"state": "happy"}) and not ok("lifecycle/health", {}))
    caps = {"contract": "1.0-rc1", "implementation": {"name": "x", "version": "1"}, "features": [], "limits": {}, "storage_schema": 0, "egress_confinement": "none"}
    check("1.13 capabilities: a partial implementation (no features) is valid", ok("lifecycle/capabilities", caps))
    check("1.14 capabilities: additive fields tolerated", ok("lifecycle/capabilities", {**caps, "future": {"x": 1}, "limits": {"a": {"b": 1}}}))
    check("1.15 capabilities: each level of confinement valid, others refused",
          all(ok("lifecycle/capabilities", {**caps, "egress_confinement": v}) for v in ("none", "firewall", "namespace"))
          and not ok("lifecycle/capabilities", {**caps, "egress_confinement": "sandbox"}))
    for field in caps:
        check(f"1.16 capabilities: '{field}' is required", not ok("lifecycle/capabilities", {k: v for k, v in caps.items() if k != field}))
    check("1.17 capabilities: duplicate features refused", not ok("lifecycle/capabilities", {**caps, "features": ["conversation", "conversation"]}))
    check("1.18 unlock/lock/shutdown answers: the state, additive fields tolerated",
          ok("lifecycle/unlock.response", {"state": "ready", "x": 1}) and ok("lifecycle/lock.response", {"state": "locked"})
          and ok("lifecycle/shutdown.response", {"state": "draining"}) and not ok("lifecycle/unlock.response", {"state": "locked"}))
    err = {"error": {"code": "locked", "message": "The core is locked.", "retryable": False, "request_id": "req_1"}}
    check("1.19 error body: canonical shape valid, extra fields tolerated", ok("common/error", err) and ok("common/error", {**err, "x": 1}))
    for label, doc in {"no retryable": {"error": {k: v for k, v in err["error"].items() if k != "retryable"}},
                       "unknown code": {"error": {**err["error"], "code": "boom"}}, "flat": err["error"],
                       "empty message": {"error": {**err["error"], "message": ""}}}.items():
        check(f"1.20 error body refused: {label}", not ok("common/error", doc))
    for code in ("invalid_payload", "unauthorized", "forbidden", "not_found", "idempotency_conflict", "payload_too_large", "locked",
                 "rate_limited", "backend_error", "unavailable", "internal"):
        check(f"1.21 appendix A code accepted: {code}", ok("common/error", {"error": {**err["error"], "code": code}}))
    idem = schemas.by_name["common/identifiers"]["$defs"]["idempotency_key"]["pattern"]
    check("1.22 idempotency key pattern is [A-Za-z0-9_-]{8,64}", bool(re.match(idem, "abcdefgh")) and not re.match(idem, "abcdefg") and not re.match(idem, "a" * 65) and not re.match(idem, "bad key!!"))
    # a schema that drifts from the policy is caught
    broken = copy.deepcopy(schemas)
    broken.by_name["lifecycle/unlock.request"] = {**schemas.by_name["lifecycle/unlock.request"], "additionalProperties": True}
    check("1.23 policy lint catches a request schema opened up", lint_schema_policy(broken) != [])
    broken = copy.deepcopy(schemas)
    broken.by_name["lifecycle/health"] = {k: v for k, v in schemas.by_name["lifecycle/health"].items() if k != "additionalProperties"}
    check("1.24 policy lint catches a health schema that is no longer closed", lint_schema_policy(broken) != [])


# ── 2. fixture files ───────────────────────────────────────────────────────────────────────────────────────────
def fixtures_section():
    ctx = lint_context()
    suite = load_suite()
    check("2.1 every fixture file loads and lints", len(suite.scenarios) >= 60, f"{len(suite.scenarios)} fixtures")
    check("2.2 fixtures carry the contract version", all(json.loads(p.read_text())["contract_version"] == suite.version for p in ROOT.glob("scenarios/*/*.json") if p.parent.name != "_shared"))
    ids = [s.id for s in suite.scenarios]
    check("2.3 fixture ids are unique", len(ids) == len(set(ids)))
    check("2.4 every fixture names a contract section and an invariant", all(s.traces and all("section" in t and "invariant" in t for t in s.traces) for s in suite.scenarios))
    base = json.loads((ROOT / "scenarios" / "p1" / "unlock.json").read_text())

    def lint(mutate, label):
        doc = copy.deepcopy(base)
        mutate(doc)
        problems, _ = lint_scenario_doc(doc, "typo.json", ctx, {})
        check(f"2.5 a fixture typo is refused: {label}", problems != [], "the mistake went through")

    first = lambda d: d["scenarios"][0]
    step = lambda d: first(d)["steps"][0]

    def rename(obj, old, new):
        obj[new] = obj.pop(old)
    lint(lambda d: rename(step(d)["expect"], "status", "expected_statuz"), "expected_statuz")
    lint(lambda d: rename(step(d), "expect", "expects"), "step key 'expects'")
    lint(lambda d: rename(step(d)["request"], "method", "metod"), "request key 'metod'")
    lint(lambda d: step(d)["expect"].update(status=200, status_in=[200, 201]), "status and status_in together")
    lint(lambda d: step(d)["expect"].update(must_not_contain=["${no.such.thing}"]), "unknown placeholder")
    lint(lambda d: step(d)["request"].update(path="v1/unlock"), "path without a leading slash")
    lint(lambda d: first(d)["traces"][0].update(invariant="I13"), "invariant I13 does not exist")
    lint(lambda d: first(d)["traces"][0].update(section="99"), "section 99 does not exist")
    lint(lambda d: first(d).update(traces=[]), "no trace at all")
    lint(lambda d: first(d).pop("traces"), "traces missing")
    lint(lambda d: first(d).update(status="pending"), "pending without a reason")
    lint(lambda d: first(d).update(requires=["telepathy"]), "unknown hook")
    lint(lambda d: first(d).update(destructive=True), "destructive without the restart hook")
    lint(lambda d: first(d).update(kind="htpp"), "unknown kind")
    lint(lambda d: first(d)["given"].update(state="unlocked"), "unknown given state")
    lint(lambda d: step(d)["expect"].update(error_code="teapot"), "unknown error code")
    lint(lambda d: step(d)["expect"].update(body_schema="lifecycle/nothing"), "unknown schema name")
    lint(lambda d: step(d)["expect"].update(must_not_contain=["@leaks:nowhere"]), "unknown leak group")
    lint(lambda d: step(d)["expect"].update(same_body_as="later_step"), "same_body_as an unknown step")
    lint(lambda d: step(d)["request"].update(body_raw="{}"), "two body forms")
    lint(lambda d: d["scenarios"].append(copy.deepcopy(first(d))), "duplicate scenario id")
    lint(lambda d: d.update(contract_version="1.0-rc9"), "wrong contract version")
    lint(lambda d: d.update(surprise=1), "unknown top-level key")
    lint(lambda d: d["scenarios"].append({"id": "x.y", "kind": "supervisor"}), "half a supervisor scenario")
    good, _ = lint_scenario_doc(copy.deepcopy(base), "ok.json", ctx, {})
    check("2.6 the untouched fixture file is accepted by the same lint", good == [], "; ".join(good))

    # language neutrality: no product or language specifics in the fixtures and schemas (leak patterns name products on purpose)
    pattern = re.compile(r"python|pytest|\.py\b|pymysql|redis|ollama|9010|9999|fastapi|uvicorn|PET\b", re.I)
    offenders = []
    for p in list(ROOT.glob("scenarios/p1/*.json")) + list(ROOT.glob("schemas/**/*.json")):
        for m in pattern.finditer(p.read_text(encoding="utf-8")):
            offenders.append(f"{p.relative_to(REPO)}: {m.group(0)}")
    check("2.7 fixtures and schemas are language-neutral data (no Python, no product, no live port)", offenders == [], "; ".join(offenders[:5]))
    return suite


# ── 3. coverage ────────────────────────────────────────────────────────────────────────────────────────────────
def coverage_section(suite) -> None:
    check("3.1 coverage.json is consistent with the fixtures", coverage.check(suite) == [], "; ".join(coverage.check(suite)))
    table = coverage.compute(suite)
    declared = coverage.load_declared()
    check("3.2 no invariant is claimed 'covered' while something is left over", all(d["status"] != "covered" for d in declared.values()))
    check("3.3 I3 is not claimed as covered (OS isolation is not testable through /v1)", declared["I3"]["status"] == "partial")
    check("3.4 invariants without a runnable fixture are declared not_yet_testable", all(d["status"] == "not_yet_testable" for i, d in declared.items() if not table[i]["now"]))
    # a declaration that over-claims is caught
    over = copy.deepcopy(declared)
    over["I5"] = {"status": "covered", "covers": "everything"}
    saved = coverage.load_declared
    coverage.load_declared = lambda: over
    try:
        check("3.5 an over-claiming coverage.json is refused", any("I5" in p for p in coverage.check(suite)))
    finally:
        coverage.load_declared = saved
    check("3.6 CONTRACT_VERSION is 1.0-rc1 and the document says so and states the freeze rule",
          (ROOT / "CONTRACT_VERSION").read_text().strip() == "1.0-rc1"
          and "# Node ⇄ Core contract (v1) — 1.0-rc1" in (REPO / "docs" / "NODE_CORE_CONTRACT.md").read_text(encoding="utf-8")
          and "may receive only clarifications and additive corrections unless a conformance contradiction proves an invariant wrong"
          in (REPO / "docs" / "NODE_CORE_CONTRACT.md").read_text(encoding="utf-8").replace("\n", " "))


# ── 4. the double satisfies the fixtures ─────────────────────────────────────────────────────────────────────
def double_section(suite) -> None:
    target = ReferenceTarget()
    try:
        results = run_suite(suite, target)
    finally:
        target.close()
    bad = [f"{r.id}: {r.detail}" for r in results if r.status in ("fail", "error")]
    passed = [r for r in results if r.status == "pass"]
    check("4.1 the test double satisfies every fixture that needs no hook it lacks", bad == [], "; ".join(bad[:4]))
    check("4.2 most of the suite really ran (not skipped)", len(passed) >= 55, f"{len(passed)} passed")
    check("4.3 fixtures needing a missing hook are reported 'unsupported', never 'pass'",
          all(r.status == "unsupported" for r in results if r.id.startswith(("launch_secret.", "key_material.key_is", "key_material.no_decrypted", "isolation."))))
    check("4.4 supervisor fixtures are reported 'pending', never 'pass'", all(r.status == "pending" for r in results if r.id.startswith("supervisor.")))
    dump = json.dumps([r.steps + [r.detail] for r in results])
    check("4.5 no report carries the launch secret or an unlock key", target.launch_secret not in dump and target.unlock_key not in dump)
    # no destructive scenario without the restart hook
    from contract.selftest.reference_double import ReferenceTarget as R2
    t2 = R2()
    t2.hooks = set()
    try:
        r2 = run_suite(suite, t2, only=["shutdown."])
    finally:
        t2.close()
    check("4.6 a target without 'restart' is never asked to shut down", all(r.status in ("unsupported", "pending") or r.id == "shutdown.invalid_requests_are_refused_and_change_nothing" for r in r2))


# ── 5. faults are caught ───────────────────────────────────────────────────────────────────────────────────────
CATCHERS = {
    "no_auth_capabilities": ["auth.missing_secret_is_refused_when_locked"],
    "no_auth_lock": ["auth.missing_secret_is_refused_when_ready"],
    "prefix_auth": ["auth.wrong_secret_is_refused_when_locked"],
    "wrong_key_ready": ["unlock.wrong_key_never_becomes_ready"],
    "wrong_key_partial": ["key_material.a_failed_unlock_leaves_no_decrypted_state"],
    "locked_serves_capabilities": ["locked.refuses_everything_except_health_and_unlock"],
    "locked_serves_shutdown": ["locked.refuses_everything_except_health_and_unlock"],
    "lock_noop": ["lock.ready_becomes_locked_and_can_unlock_again"],
    "unknown_fields_ok": ["unlock.unknown_field_is_rejected"],
    "extensions_bypass": ["unlock.extensions_cannot_change_what_is_allowed"],
    "dup_keys_ok": ["unlock.duplicate_json_key_is_refused"],
    "no_size_limit": ["malformed.oversized_body_is_refused_even_with_a_valid_key"],
    "limit_high": ["malformed.body_one_byte_over_the_limit_is_refused"],
    "limit_low": ["malformed.body_exactly_at_the_limit_is_accepted"],
    "leak_traceback": ["malformed.deeply_nested_body_is_refused_cleanly"],
    "echo_key": ["sanitization.errors_never_echo_credentials"],
    "health_extra": ["health.locked_is_open_and_minimal"],
    "idem_ignored": ["idempotency.a_replay_has_no_second_effect"],
    "idem_conflict_ignored": ["idempotency.unlock_same_key_different_body_conflicts"],
    "no_idem_required": ["idempotency.key_is_required_on_a_locked_core_mutation"],
    "state_before_replay": ["idempotency.lock_same_key_same_body_gives_the_same_answer"],
    "principal_grants": ["auth.principal_and_actor_headers_grant_nothing"],
    "absent_feature_ok": ["capabilities.absent_feature_is_not_found"],
    "shutdown_stays_ready": ["shutdown.goes_through_draining_to_stopped"],
    "wrong_context_ok": ["unlock.wrong_context_is_rejected"],
    "loose_key_check": ["unlock.malformed_key_is_rejected"],
    "error_shape_broken": ["auth.missing_secret_is_refused_when_locked"],
    "caps_no_confinement": ["capabilities.reports_the_egress_confinement_level"],
    "caps_old_contract": ["capabilities.answer_has_the_contract_shape"],
    "plain_404": ["sanitization.unknown_endpoint_is_the_canonical_not_found"],
    "echo_secret_401": ["sanitization.errors_never_echo_credentials"],
    "nan_ok": ["malformed.invalid_json_is_refused"],
    "deadline_loose": ["shutdown.invalid_requests_are_refused_and_change_nothing"],
    "health_needs_auth": ["health.locked_is_open_and_minimal"],
    "validate_before_auth": ["auth.refusal_comes_before_every_other_check"],
    "leak_path": ["sanitization.unknown_endpoint_is_the_canonical_not_found"],
    "non_json_content_type": ["health.locked_is_open_and_minimal"],
    "lock_ready_after_wrong_key": ["unlock.wrong_key_never_becomes_ready"],
}


def faults_section(suite) -> None:
    check("5.1 every fault of the double has a named catcher (and no catcher is orphaned)", set(CATCHERS) == FAULTS, f"unmapped: {sorted(FAULTS - set(CATCHERS))} orphaned: {sorted(set(CATCHERS) - FAULTS)}")
    for fault in sorted(CATCHERS):
        target = ReferenceTarget(faults={fault})
        try:
            results = run_suite(suite, target, only=CATCHERS[fault])
        finally:
            target.close()
        caught = [r.id for r in results if r.status == "fail"]
        check(f"5.2 fault '{fault}' is caught", bool(caught) and set(caught) <= set(CATCHERS[fault]) | {r.id for r in results}, "no fixture noticed")
        check(f"5.3 fault '{fault}' is caught by the fixture named for it", any(c.startswith(tuple(CATCHERS[fault])) for c in caught), f"caught by {caught}")


# ── 6. a system that implements nothing passes nothing ──────────────────────────────────────────────────────
class _Stub(http.server.BaseHTTPRequestHandler):
    status, ctype, payload = 404, "text/html", b"<html>Not Found</html>"

    def log_message(self, *a):
        pass

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self.send_response(self.status)
        self.send_header("Content-Type", self.ctype)
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(self.payload)

    do_GET = do_POST = do_PUT = do_DELETE = _go


def _positive_only(sc) -> bool:
    for step in sc.data["steps"]:
        exp = step.get("expect") or (step.get("parallel") or {}).get("expect_all") or {}
        if step.get("assert_health", "ready") != "ready":
            return False
        if exp.get("status", 200) not in (200, 202) or "error_code" in exp or "error_code_in" in exp or "status_in" in exp:
            return False
        if any(v != "ready" for v in (exp.get("body_pointer") or {}).values() if isinstance(v, str) and v in ("locked", "draining")):
            return False
    return True


def stub_section(suite) -> None:
    variants = {
        "404 HTML to everything": dict(status=404, ctype="text/html", payload=b"<html>Not Found</html>"),
        "403 JSON to everything": dict(status=403, ctype="application/json", payload=b'{"detail":"forbidden"}'),
        "200 {} to everything": dict(status=200, ctype="application/json", payload=b"{}"),
        "200 state ready to everything": dict(status=200, ctype="application/json", payload=b'{"state":"ready"}'),
    }
    for label, attrs in variants.items():
        handler = type("H", (_Stub,), attrs)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        cfg = {"name": "stub", "base_url": f"http://127.0.0.1:{server.server_address[1]}", "launch_secret": "s" * 32, "unlock_key": KEY}
        target = ExternalTarget(cfg)
        try:
            results = run_suite(suite, target, lenient=True)
            strict = run_suite(suite, target, only=["unlock.valid_key"])
        finally:
            server.shutdown()
            server.server_close()
        passed = [r.id for r in results if r.status == "pass"]
        if label.startswith("200 state ready"):
            # a system that says "ready, fine" to everything can only satisfy fixtures that never expect a refusal or another state
            by_id = {sc.id: sc for sc in suite.scenarios}
            unexpected = [i for i in passed if not _positive_only(by_id[i])]
            check(f"6.1 a stub answering '{label}' passes only fixtures that expect nothing but success", unexpected == [], f"passed a fixture that expects a refusal: {unexpected[:5]}")
        else:
            check(f"6.1 a stub answering '{label}' passes no fixture (lenient: every fixture's own assertions run)", passed == [], f"passed: {passed[:5]}")
        check(f"6.2 the same stub fails a fixture in strict mode ({label})", all(r.status in ("fail", "error") for r in strict))


# ── 7. runner safety ───────────────────────────────────────────────────────────────────────────────────────────
def safety_section() -> None:
    for port in (9010, 9999, 6379, 18082, 18083, 3306, 11434):
        try:
            check_target_url(f"http://127.0.0.1:{port}")
            check(f"7.1 port {port} of the live system is refused", False)
        except RefusedTarget:
            check(f"7.1 port {port} of the live system is refused", True)
    for url in ("http://192.168.1.5:40000", "http://example.com:40000", "https://127.0.0.1:40000", "http://127.0.0.1"):
        try:
            check_target_url(url)
            check(f"7.2 target {url} is refused", False)
        except RefusedTarget:
            check(f"7.2 target {url} is refused", True)
    check("7.3 loopback targets on ordinary ports are accepted", check_target_url("http://127.0.0.1:41234") == ("127.0.0.1", 41234) and check_target_url("http://localhost:41234")[1] == 41234)
    try:
        ExternalTarget({"name": "live", "base_url": "http://127.0.0.1:9010", "launch_secret": "s" * 32, "unlock_key": KEY})
        check("7.4 a target file that points at the live PET is refused", False)
    except RefusedTarget:
        check("7.4 a target file that points at the live PET is refused", True)
    try:
        ExternalTarget({"name": "x", "base_url": "http://127.0.0.1:41234", "launch_secret": "short", "unlock_key": KEY})
        check("7.5 a malformed target description is refused", False)
    except ValueError:
        check("7.5 a malformed target description is refused", True)
    check("7.6 the ports the runner refuses include every port of the live system named in the project notes", {9010, 9999, 18082, 18083, 6379, 3306} <= set(LIVE_PORTS))
    # an unreachable target is an 'error', not a pass and not a contract failure
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    if free not in LIVE_PORTS:
        target = ExternalTarget({"name": "nobody home", "base_url": f"http://127.0.0.1:{free}", "launch_secret": "s" * 32, "unlock_key": KEY})
        res = run_suite(load_suite(), target, only=["health.locked"])
        check("7.7 a target nobody listens on gives 'error', never 'pass'", res and all(r.status == "error" for r in res))


def main() -> int:
    for title, fn in (("SCHEMAS", schemas_section),):
        print(f"\n── {title}")
        fn()
    print("\n── FIXTURES")
    suite = fixtures_section()
    print("\n── COVERAGE")
    coverage_section(suite)
    print("\n── THE TEST DOUBLE SATISFIES THE FIXTURES")
    double_section(suite)
    print("\n── DELIBERATELY BROKEN DOUBLES ARE CAUGHT")
    faults_section(suite)
    print("\n── A SYSTEM THAT IMPLEMENTS NOTHING PASSES NOTHING")
    stub_section(suite)
    print("\n── RUNNER SAFETY")
    safety_section()
    print("\n" + "=" * 72)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ContractError as exc:
        print(f"CONTRACT FILES ARE NOT VALID: {exc}")
        sys.exit(2)
