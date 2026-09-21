"""Plain-text and JSON reports."""
from __future__ import annotations

import time
from collections import Counter

ORDER = ["pass", "fail", "error", "unsupported", "not_applicable", "pending"]


def line(res) -> str:
    tag = res.status.upper()
    return f"{tag:<15} {res.id}" + (f"\n{'':<16}{res.detail}" if res.detail and res.status in ("fail", "error", "unsupported", "not_applicable") else "")


def summary(results) -> str:
    c = Counter(r.status for r in results)
    return "  ".join(f"{k}={c[k]}" for k in ORDER if c[k]) + f"   (total {len(results)})"


def to_json(version: str, target_name: str, mode: str, results, extra: dict | None = None) -> dict:
    c = Counter(r.status for r in results)
    doc = {"contract_version": version, "target": target_name, "mode": mode,
           "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "counts": {k: c[k] for k in ORDER if c[k]},
           "results": [{"id": r.id, "status": r.status, "detail": r.detail, "file": r.file} for r in results]}
    if extra:
        doc.update(extra)
    return doc
