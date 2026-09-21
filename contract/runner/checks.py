"""Compare one HTTP answer with what a fixture expects. Returns a list of plain-language problems (empty = matches)."""
from __future__ import annotations

import re

from .loader import Schemas
from .wire import Response


def substitute(value, ctx: dict):
    """Replace ${name} in every string of a JSON-like value."""
    if isinstance(value, str):
        return re.sub(r"\$\{([a-z_.]+)\}", lambda m: str(ctx[m.group(1)]), value)
    if isinstance(value, dict):
        return {substitute(k, ctx): substitute(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, ctx) for v in value]
    return value


def pointer(doc, ptr: str):
    """RFC 6901 lookup; raises KeyError when absent."""
    if ptr == "":
        return doc
    cur = doc
    for raw in ptr.lstrip("/").split("/"):
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(tok)]
        else:
            cur = cur[tok]
    return cur


def _media_type(resp: Response) -> str:
    return resp.headers.get("content-type", "").split(";")[0].strip().lower()


def check_response(exp: dict, resp: Response, ctx: dict, schemas: Schemas, leaks: dict, earlier: dict) -> list[str]:
    problems: list[str] = []
    if resp.error:
        return [f"no HTTP answer ({resp.error})"]

    if "status" in exp and resp.status != exp["status"]:
        problems.append(f"status {resp.status}, expected {exp['status']}")
    if "status_in" in exp and resp.status not in exp["status_in"]:
        problems.append(f"status {resp.status}, expected one of {exp['status_in']}")

    wants_json = any(k in exp for k in ("error_code", "error_code_in", "body_schema", "body_equals", "body_pointer", "body_pointer_in", "same_body_as"))
    doc = None
    if wants_json:
        if _media_type(resp) != "application/json":
            problems.append(f"Content-Type is '{resp.headers.get('content-type', '')}', expected application/json")
        try:
            doc = resp.json()
        except ValueError as exc:
            problems.append(f"body is not valid JSON ({exc})")
    if doc is not None:
        schema_name = "common/error" if ("error_code" in exp or "error_code_in" in exp) else exp.get("body_schema")
        if schema_name:
            for e in schemas.errors(schema_name, doc):
                problems.append(f"body does not match {schema_name}: {e}")
        code = doc.get("error", {}).get("code") if isinstance(doc, dict) and isinstance(doc.get("error"), dict) else None
        if "error_code" in exp and code != exp["error_code"]:
            problems.append(f"error code '{code}', expected '{exp['error_code']}'")
        if "error_code_in" in exp and code not in exp["error_code_in"]:
            problems.append(f"error code '{code}', expected one of {exp['error_code_in']}")
        if "body_equals" in exp:
            want = substitute(exp["body_equals"], ctx)
            if doc != want:
                problems.append(f"body {_clip(doc)} != expected {_clip(want)}")
        for ptr, want in exp.get("body_pointer", {}).items():
            want = substitute(want, ctx)
            try:
                got = pointer(doc, ptr)
            except (KeyError, IndexError, ValueError, TypeError):
                problems.append(f"body has no {ptr}")
                continue
            if got != want:
                problems.append(f"body{ptr} is {_clip(got)}, expected {_clip(want)}")
        for ptr, allowed in exp.get("body_pointer_in", {}).items():
            try:
                got = pointer(doc, ptr)
            except (KeyError, IndexError, ValueError, TypeError):
                problems.append(f"body has no {ptr}")
                continue
            if got not in allowed:
                problems.append(f"body{ptr} is {_clip(got)}, expected one of {_clip(allowed)}")
        if "same_body_as" in exp:
            other = earlier.get(exp["same_body_as"])
            if other is None or other.status is None:
                problems.append(f"no earlier answer '{exp['same_body_as']}' to compare with")
            else:
                try:
                    if other.json() != doc:
                        problems.append(f"body differs from the answer of step '{exp['same_body_as']}'")
                except ValueError:
                    problems.append(f"the answer of step '{exp['same_body_as']}' was not JSON")

    for name, rule in exp.get("headers", {}).items():
        got = resp.headers.get(name.lower())
        if rule.get("absent"):
            if got is not None:
                problems.append(f"header {name} present ('{got}'), expected absent")
            continue
        if got is None:
            problems.append(f"header {name} missing")
        elif "equals" in rule and got != substitute(rule["equals"], ctx):
            problems.append(f"header {name} is '{got}', expected '{rule['equals']}'")
        elif "starts_with" in rule and not got.startswith(substitute(rule["starts_with"], ctx)):
            problems.append(f"header {name} is '{got}', expected to start with '{rule['starts_with']}'")

    for item in exp.get("must_not_contain", []):
        problems.extend(_leak_problems(item, resp, ctx, leaks))
    return problems


def _leak_problems(item: str, resp: Response, ctx: dict, leaks: dict) -> list[str]:
    body = resp.text
    headers = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
    if item.startswith("@leaks"):
        group = item[len("@leaks"):].lstrip(":")
        groups = [group] if group else sorted(leaks)
        out = []
        for g in groups:
            for pattern in leaks[g]:
                m = re.search(pattern, body)
                if m:
                    out.append(f"body leaks ({g}): '{m.group(0)}'")
        return out
    literal = str(substitute(item, ctx))
    out = []
    if literal in body:
        out.append("body contains a value it must never echo")
    if literal in headers:
        out.append("a response header contains a value it must never echo")
    return out


def _clip(v, n: int = 120) -> str:
    s = repr(v)
    return s if len(s) <= n else s[:n] + "…"
