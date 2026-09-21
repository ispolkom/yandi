"""Load and check the contract's own files: schemas, fixtures, leak patterns, coverage.

Everything here is deterministic and offline. A fixture that has a typo, an unknown placeholder, a reference to a section or an
invariant that the contract does not have, or a duplicate id, is an error before any request is sent (fail closed).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import jsonschema
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
except ImportError as exc:  # pragma: no cover - environment problem, said plainly
    raise SystemExit("contract runner needs the 'jsonschema' package (>=4.18): pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parents[1]           # .../contract
REPO = ROOT.parent
CONTRACT_DOC = REPO / "docs" / "NODE_CORE_CONTRACT.md"
SCHEMA_BASE = "https://yandi.invalid/contract/{version}/schemas/"

PLACEHOLDERS = {"secret", "secret.short", "key.valid", "key.wrong", "context", "run", "contract_version"}
_PLACEHOLDER_RE = re.compile(r"\$\{([a-z_.]+)\}")


class ContractError(Exception):
    """A defect in the contract's own files (never a failure of the system under test)."""


def contract_version() -> str:
    return (ROOT / "CONTRACT_VERSION").read_text(encoding="utf-8").strip()


def _read_json(path: Path):
    def no_dupes(pairs):
        keys = [k for k, _ in pairs]
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            raise ContractError(f"{path.relative_to(REPO)}: duplicate JSON key {sorted(dup)}")
        return dict(pairs)
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_dupes)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path.relative_to(REPO)}: not valid JSON: {exc}") from exc


# ── schemas ──────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Schemas:
    version: str
    registry: Registry
    by_name: dict = field(default_factory=dict)    # "lifecycle/health" -> schema document
    files: dict = field(default_factory=dict)      # "lifecycle/health" -> Path

    def validator(self, name: str) -> Draft202012Validator:
        if name not in self.by_name:
            raise ContractError(f"unknown schema '{name}'")
        return Draft202012Validator(self.by_name[name], registry=self.registry)

    def errors(self, name: str, instance) -> list[str]:
        v = self.validator(name)
        return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in sorted(v.iter_errors(instance), key=str)]


def load_schemas() -> Schemas:
    version = contract_version()
    base = SCHEMA_BASE.format(version=version)
    schemas_dir = ROOT / "schemas"
    registry = Registry()
    by_name: dict = {}
    files: dict = {}
    for path in sorted(schemas_dir.rglob("*.schema.json")):
        doc = _read_json(path)
        rel = path.relative_to(schemas_dir).as_posix()
        name = rel[: -len(".schema.json")]
        if doc.get("$id") != base + rel:
            raise ContractError(f"{path.relative_to(REPO)}: $id must be {base + rel}")
        if doc.get("x-contract-version") != version:
            raise ContractError(f"{path.relative_to(REPO)}: x-contract-version must be {version}")
        if doc.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ContractError(f"{path.relative_to(REPO)}: $schema must be draft 2020-12")
        try:
            Draft202012Validator.check_schema(doc)
        except jsonschema.SchemaError as exc:
            raise ContractError(f"{path.relative_to(REPO)}: not a valid JSON Schema: {exc.message}") from exc
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
        by_name[name] = doc
        files[name] = path
    return Schemas(version=version, registry=registry, by_name=by_name, files=files)


def lint_schema_policy(schemas: Schemas) -> list[str]:
    """Request schemas are strict at every object level; responses are additive except the health schema."""
    problems: list[str] = []
    closed_responses = {"lifecycle/health"}

    def walk(node, path, want_closed, name):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node and path[-1:] != ["extensions"]:
                closed = node.get("additionalProperties") is False
                if want_closed and not closed:
                    problems.append(f"{name}: request object at {'/'.join(path) or '<root>'} must set additionalProperties:false")
                if not want_closed and closed and name not in closed_responses:
                    problems.append(f"{name}: response object at {'/'.join(path) or '<root>'} must stay additive (no additionalProperties:false)")
            if want_closed and node.get("additionalProperties") is True:
                problems.append(f"{name}: request schema must not use additionalProperties:true at {'/'.join(path) or '<root>'}")
            for k, v in node.items():
                walk(v, path + [k], want_closed, name)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + [str(i)], want_closed, name)

    for name, doc in schemas.by_name.items():
        if name.startswith(("lifecycle/",)) or name.startswith("common/error"):
            walk(doc, [], name.endswith(".request"), name)
    for name in closed_responses:
        if schemas.by_name.get(name, {}).get("additionalProperties") is not False:
            problems.append(f"{name}: must be closed (additionalProperties:false)")
    return problems


# ── contract document anchors (sections and invariants a fixture may cite) ────────────────────────────────────

def doc_anchors() -> tuple[set[str], set[str]]:
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    sections: set[str] = set()
    for m in re.finditer(r"^#{2,3} (\d+(?:\.\d+)?)[. ]", text, re.M):
        sections.add(m.group(1))
    for m in re.finditer(r"^## Appendix ([A-D])\b", text, re.M):
        sections.add(m.group(1))
    invariants = set(re.findall(r"^\| (I\d+) \|", text, re.M))
    if not sections or not invariants:
        raise ContractError("could not read sections/invariants from docs/NODE_CORE_CONTRACT.md")
    return sections, invariants


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Scenario:
    data: dict
    file: str

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def kind(self) -> str:
        return self.data["kind"]

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def requires(self) -> list[str]:
        return self.data.get("requires", [])

    @property
    def traces(self) -> list[dict]:
        return self.data["traces"]


@dataclass
class Suite:
    version: str
    schemas: Schemas
    scenarios: list[Scenario]
    leaks: dict


def _placeholders_in(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found |= set(_PLACEHOLDER_RE.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            found |= _placeholders_in(v)
    elif isinstance(value, list):
        for v in value:
            found |= _placeholders_in(v)
    return found


def load_leaks() -> dict:
    doc = _read_json(ROOT / "scenarios" / "_shared" / "leak-patterns.json")
    if doc.get("contract_version") != contract_version() or doc.get("kind") != "leak-patterns":
        raise ContractError("leak-patterns.json: wrong kind or contract_version")
    groups = doc.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise ContractError("leak-patterns.json: no groups")
    for g, patterns in groups.items():
        if not isinstance(patterns, list) or not patterns:
            raise ContractError(f"leak-patterns.json: group '{g}' is empty")
        for p in patterns:
            try:
                re.compile(p)
            except re.error as exc:
                raise ContractError(f"leak-patterns.json: bad regex {p!r}: {exc}") from exc
    return groups


def _semantic_lint(sc: dict, where: str, sections: set[str], invariants: set[str], leak_groups: set[str]) -> list[str]:
    problems: list[str] = []
    for t in sc["traces"]:
        if t["invariant"] not in invariants:
            problems.append(f"{where}: cites invariant {t['invariant']} which the contract does not have")
        if t["section"] not in sections:
            problems.append(f"{where}: cites section {t['section']} which the contract does not have")
    unknown = _placeholders_in(sc) - PLACEHOLDERS
    if unknown:
        problems.append(f"{where}: unknown placeholder(s) {sorted(unknown)}")
    if sc["kind"] == "supervisor":
        return problems
    if sc["status"] == "active" and "pending_reason" in sc:
        problems.append(f"{where}: an active fixture must not carry pending_reason")
    if sc.get("destructive") and "restart" not in sc.get("requires", []):
        problems.append(f"{where}: a destructive fixture must require the 'restart' hook")
    seen_ids: set[str] = set()
    applies = sc.get("applies_when", {})
    if "feature_absent" in applies and sc["given"]["state"] != "ready":
        problems.append(f"{where}: applies_when needs capabilities, so given.state must be ready")
    if "egress_confinement_in" in applies and sc["given"]["state"] != "ready":
        problems.append(f"{where}: applies_when needs capabilities, so given.state must be ready")

    def check_request(req: dict, sw: str):
        forms = [k for k in ("body_raw", "body_hex", "body_generated") if k in req]
        if "body" in req:
            forms.append("body")
        if len(forms) > 1:
            problems.append(f"{sw}: more than one body form {forms}")
        if "body_pad_to" in req and "body" not in req:
            problems.append(f"{sw}: body_pad_to needs 'body'")
        if req["method"] == "GET" and forms:
            problems.append(f"{sw}: a GET carries no body")

    def check_expect(exp: dict, sw: str):
        if "status" in exp and "status_in" in exp:
            problems.append(f"{sw}: status and status_in together")
        if "error_code" in exp and "error_code_in" in exp:
            problems.append(f"{sw}: error_code and error_code_in together")
        if ("error_code" in exp or "error_code_in" in exp) and "body_schema" in exp:
            problems.append(f"{sw}: an error expectation already implies the error schema")
        if "body_schema" in exp and exp["body_schema"] not in _SCHEMA_NAMES_CACHE:
            problems.append(f"{sw}: unknown schema '{exp['body_schema']}'")
        if exp.get("same_body_as") and exp["same_body_as"] not in seen_ids:
            problems.append(f"{sw}: same_body_as refers to '{exp['same_body_as']}' which is not an earlier step")
        for item in exp.get("must_not_contain", []):
            if item.startswith("@leaks"):
                group = item[len("@leaks"):].lstrip(":")
                if group and group not in leak_groups:
                    problems.append(f"{sw}: unknown leak group '{group}'")

    for i, step in enumerate(sc["steps"]):
        sw = f"{where} step {i + 1}"
        if "request" in step:
            check_request(step["request"], sw)
            check_expect(step["expect"], sw)
        elif "parallel" in step:
            check_request(step["parallel"]["request"], sw)
            check_expect(step["parallel"]["expect_all"], sw)
        if "id" in step:
            if step["id"] in seen_ids:
                problems.append(f"{sw}: duplicate step id '{step['id']}'")
            seen_ids.add(step["id"])
    return problems


_SCHEMA_NAMES_CACHE: set[str] = set()


@dataclass
class LintContext:
    schemas: Schemas
    meta: Draft202012Validator
    version: str
    sections: set
    invariants: set
    leaks: dict


def lint_context() -> LintContext:
    schemas = load_schemas()
    _SCHEMA_NAMES_CACHE.clear()
    _SCHEMA_NAMES_CACHE.update(schemas.by_name)
    sections, invariants = doc_anchors()
    return LintContext(schemas, schemas.validator("scenario/scenario"), schemas.version, sections, invariants, load_leaks())


def lint_scenario_doc(doc, rel: str, ctx: LintContext, ids: dict) -> tuple[list[str], list[Scenario]]:
    """Everything that can be wrong with one fixture file, decided without sending a request."""
    problems: list[str] = []
    errs = sorted(ctx.meta.iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errs:
        for e in errs[:6]:
            where = "/".join(str(p) for p in e.absolute_path)
            problems.append(f"{rel}: {where}: {_short(e)}")
        return problems, []
    if doc["contract_version"] != ctx.version:
        problems.append(f"{rel}: contract_version {doc['contract_version']} != {ctx.version}")
    found: list[Scenario] = []
    for sc in doc["scenarios"]:
        where = f"{rel}::{sc['id']}"
        if sc["id"] in ids:
            problems.append(f"{where}: duplicate scenario id (also in {ids[sc['id']]})")
        ids[sc["id"]] = rel
        problems.extend(_semantic_lint(sc, where, ctx.sections, ctx.invariants, set(ctx.leaks)))
        found.append(Scenario(sc, rel))
    return problems, found


def load_suite() -> Suite:
    ctx = lint_context()
    problems: list[str] = []
    scenarios: list[Scenario] = []
    ids: dict[str, str] = {}
    files = [f for f in sorted((ROOT / "scenarios").glob("*/*.json")) if f.parent.name != "_shared"]
    if not files:
        raise ContractError("no scenario files found")
    for path in files:
        rel = path.relative_to(REPO).as_posix()
        p, found = lint_scenario_doc(_read_json(path), rel, ctx, ids)
        problems.extend(p)
        scenarios.extend(found)
    if problems:
        raise ContractError("fixture problems:\n  " + "\n  ".join(problems))
    return Suite(version=ctx.version, schemas=ctx.schemas, scenarios=scenarios, leaks=ctx.leaks)


def _short(e) -> str:
    """oneOf failures print the whole instance; keep the message readable."""
    if e.validator in ("oneOf", "anyOf") and e.context:
        best = jsonschema.exceptions.best_match(e.context)
        where = "/".join(str(p) for p in best.absolute_path)
        return f"{best.message} (at {where})"
    msg = e.message
    return msg if len(msg) < 200 else msg[:200] + "…"
