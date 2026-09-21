"""Traceability: which invariant is defended by which fixtures, and how much of it is testable today.

The declared status in coverage.json is checked against the fixtures, so it cannot claim more than the fixtures deliver:
  covered            at least one fixture that runs on any target, and nothing left untestable
  partial            at least one fixture that runs on any target, and a stated remainder (needs a hook or a later chapter)
  not_yet_testable   no fixture that runs on any target yet
A fixture that needs a hook ('requires') or is pending never counts towards "runs on any target".
"""
from __future__ import annotations

import json

from .loader import ROOT, ContractError, Suite, doc_anchors, contract_version

STATUSES = ("covered", "partial", "not_yet_testable")


def compute(suite: Suite) -> dict:
    _, invariants = doc_anchors()
    table = {i: {"now": [], "hook": [], "pending": []} for i in sorted(invariants, key=lambda x: int(x[1:]))}
    for sc in suite.scenarios:
        bucket = "pending" if (sc.status == "pending" or sc.kind == "supervisor") else ("hook" if sc.requires else "now")
        for t in sc.traces:
            if sc.id not in table[t["invariant"]][bucket]:
                table[t["invariant"]][bucket].append(sc.id)
    return table


def load_declared() -> dict:
    path = ROOT / "coverage.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("contract_version") != contract_version():
        raise ContractError("coverage.json: contract_version does not match CONTRACT_VERSION")
    return doc["invariants"]


def check(suite: Suite) -> list[str]:
    problems: list[str] = []
    table = compute(suite)
    declared = load_declared()
    for inv in table:
        d = declared.get(inv)
        if d is None:
            problems.append(f"coverage.json: no entry for {inv}")
            continue
        if set(d) - {"status", "covers", "remaining"}:
            problems.append(f"coverage.json {inv}: unknown keys {sorted(set(d) - {'status', 'covers', 'remaining'})}")
        if d.get("status") not in STATUSES:
            problems.append(f"coverage.json {inv}: status must be one of {STATUSES}")
            continue
        now, other = table[inv]["now"], table[inv]["hook"] + table[inv]["pending"]
        if d["status"] == "covered" and (not now or other or d.get("remaining")):
            problems.append(f"coverage.json {inv}: 'covered' needs a fixture that runs anywhere and nothing left over "
                            f"(now={len(now)}, hook/pending={len(other)}, remaining={'yes' if d.get('remaining') else 'no'})")
        if d["status"] == "partial" and (not now or not d.get("remaining")):
            problems.append(f"coverage.json {inv}: 'partial' needs a fixture that runs anywhere and a stated 'remaining'")
        if d["status"] == "not_yet_testable" and now:
            problems.append(f"coverage.json {inv}: declared not_yet_testable but {now} run anywhere")
        if not d.get("covers") and d["status"] != "not_yet_testable":
            problems.append(f"coverage.json {inv}: say in 'covers' what the fixtures prove")
    for inv in declared:
        if inv not in table:
            problems.append(f"coverage.json: {inv} is not an invariant of the contract")
    return problems


def render(suite: Suite) -> str:
    table, declared = compute(suite), load_declared()
    lines = [f"Coverage of contract {suite.version} by the P1 fixtures", ""]
    for inv, row in table.items():
        d = declared[inv]
        lines.append(f"{inv:<4} {d['status'].upper():<17} runs-anywhere={len(row['now']):<3} needs-hook={len(row['hook']):<2} pending={len(row['pending']):<2}")
        if d.get("covers"):
            lines.append(f"       proves:     {d['covers']}")
        if d.get("remaining"):
            lines.append(f"       not yet:    {d['remaining']}")
    total = len(suite.scenarios)
    active = sum(1 for s in suite.scenarios if s.status == "active" and s.kind == "http")
    lines += ["", f"{total} fixtures: {active} active, {total - active} pending"]
    return "\n".join(lines)
