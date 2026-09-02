"""
agent/integrity.py — "10-year bastion" Layer 4: tamper-EVIDENCE for
YANDI's SQL memory (owner mandate, following Layer 1's OS-identity
separation for yandi_runtime and Layer 3's least-privilege readonly/
migrator hardening).

WHY THIS FILE EXISTS (the honest complement to Layers 1-3): Linux root
— on THIS machine, by anyone who ever gains it — can never be
technically excluded from a database that lives on a disk root
controls. Root can grant itself back any MySQL privilege, restart
mysqld with --skip-grant-tables, or edit the InnoDB files directly,
bypassing SQL and every GRANT/trigger this codebase has built entirely.
Layers 1-3 close every door a non-root actor (the owner's own
interactive login, Claude Code, Codex, any other assistant session)
could walk through — they do NOT, and structurally cannot, stop root
itself. Promising otherwise would be a lie.

What CAN be delivered instead: making any such tampering — whether via
a legitimate-looking UPDATE that bypassed a trigger some other way, a
raw file edit, or a DELETE — cryptographically PROVABLE after the
fact, even to a future, more capable adversary (explicitly: Codex GPT,
per the owner's own stated intent to pentest this). That is what a
hash chain gives you and a live database alone never can: each
journaled row commits to its own content AND to the entry immediately
before it, so altering ANY past entry changes every entry_hash
computed from that point forward — a mismatch verify_chain() will
always find by simply recomputing hashes from the DB's own current
content.

THE ONE HONEST CAVEAT (do not build false confidence on this): a hash
chain stored ONLY in the same database, on the same disk, under the
same root, gives ZERO protection against root ALONE — root can
recompute the entire chain consistently after editing anything, since
the chain is just more data plus a public hash function, both fully
under root's control. Real protection against a root-level adversary
requires an EXTERNAL anchor: periodically publishing the chain's
current head (see anchor_chain_head() below) somewhere outside this
machine's own control — a separate git repository was the owner's own
explicit choice for this. Until that anchor is actually configured and
running on a schedule, this file's chain gives real protection against
non-root actors and accidental/partial corruption, but only PARTIAL
protection against root — sufficient to catch a careless or rushed
tamper attempt (recomputing an entire chain consistently by hand is
real, deliberate work), not a determined, patient one. Never claim more
than that.

SCOPE OF THIS PASS: the hashing/chaining/verification primitives
(append_entry, verify_chain, verify_row_against_journal) and the
external-anchor mechanics (write_anchor_file, commit_anchor) are built
and regression-tested here. Wiring append_entry() into live write
paths (shadow_write.py) and scheduling anchor_chain_head() against a
real, owner-configured git remote are DELIBERATE NEXT STEPS, not done
in this pass — same "design first, wire in as an explicit follow-up"
discipline this mandate's Layers 1/3 already used for deploy/yandi-
orchestrator.service.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# The chain's own root-of-trust sentinel — entry #1's prev_hash. Not a
# real hash of anything; a fixed, well-known value so verify_chain()
# has something concrete to check the very first entry against too
# (an empty/missing prev_hash would be a silent way to forge a NEW
# "genesis" partway through a real chain).
GENESIS_HASH = "0" * 64

HASH_ALGO = "sha256"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_row_json(row: Dict[str, Any]) -> str:
    """Deterministic serialization of a row's own column values —
    sorted keys (dict insertion order is not guaranteed to match a
    fresh SELECT * across MySQL versions/drivers), `default=str` so
    datetime/Decimal/etc. values serialize instead of raising, and
    compact separators so two logically-identical rows always produce
    byte-identical JSON regardless of incidental whitespace."""
    return json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))


def content_hash(row: Dict[str, Any]) -> str:
    """SHA-256 of a chained row's own canonical content. Two calls with
    the same logical row (any key order, any dict identity) always
    produce the same hash; changing even one field's value changes it
    completely (the whole point — an UPDATE that bypassed every trigger
    some other way still can't make this match the original)."""
    return _sha256_hex(canonical_row_json(row))


def compute_entry_hash(prev_hash: str, table_name: str, row_pk: str, row_content_hash: str, seq: int) -> str:
    """The chain link itself: binds THIS entry's identity (table_name,
    row_pk, its own content hash, its own seq) to the PREVIOUS entry's
    hash. Pipe-joined with an explicit seq (not just concatenation) so
    a table_name/row_pk that happens to contain a pipe character can
    never be crafted to collide with a different (table_name, row_pk)
    split — mandate's own identifier-injection discipline extended to
    hash-preimage construction, not just SQL."""
    return _sha256_hex(f"{prev_hash}|{table_name}|{row_pk}|{row_content_hash}|{seq}")


def get_chain_head(conn) -> Optional[Dict[str, Any]]:
    """The current tip of the chain — {"seq": int, "entry_hash": str} —
    or None for a virgin, empty journal."""
    with conn.cursor() as cur:
        cur.execute("SELECT seq, entry_hash FROM integrity_journal ORDER BY seq DESC LIMIT 1")
        return cur.fetchone()


def append_entry(conn, table_name: str, row_pk: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Journals one row's content, chained to whatever the current head
    is. Does NOT commit — same convention as agent/db/sql/repositories.
    py's own write functions; the caller commits as part of its own
    transaction.

    CONCURRENCY: locks the head row with `SELECT ... FOR UPDATE` inside
    the caller's transaction before computing the next seq/prev_hash —
    without this, two concurrent appends could both read the SAME head
    and each compute a prev_hash pointing at it, and only one of the
    resulting rows would actually chain correctly (the other would
    still be inserted — AUTO_INCREMENT guarantees a unique seq either
    way — but its prev_hash would silently skip a real link, which
    verify_chain() would then (correctly, but confusingly) report as
    tamper rather than as the concurrency bug it actually was). This
    matters even though this codebase currently has exactly one writer
    process, because that process serves concurrent requests."""
    with conn.cursor() as cur:
        cur.execute("SELECT seq, entry_hash FROM integrity_journal ORDER BY seq DESC LIMIT 1 FOR UPDATE")
        head = cur.fetchone()
    next_seq = (head["seq"] + 1) if head else 1
    prev_hash = head["entry_hash"] if head else GENESIS_HASH
    row_hash = content_hash(row)
    entry_hash = compute_entry_hash(prev_hash, table_name, row_pk, row_hash, next_seq)
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO integrity_journal "
            "(table_name, row_pk, row_content_hash, prev_hash, entry_hash, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (table_name, row_pk, row_hash, prev_hash, entry_hash, created_at),
        )
    return {
        "seq": next_seq, "table_name": table_name, "row_pk": row_pk,
        "row_content_hash": row_hash, "prev_hash": prev_hash, "entry_hash": entry_hash,
    }


def verify_chain(conn) -> Dict[str, Any]:
    """Walks the ENTIRE journal in seq order and recomputes every
    entry_hash from that row's own stored (prev_hash, table_name,
    row_pk, row_content_hash, seq) — comparing the recomputed value
    against what is actually stored, AND checking each row's prev_hash
    against the PREVIOUS row's actual entry_hash (catches a deleted or
    reordered row, not just an edited one). An empty journal is
    trivially ok=True (nothing to break yet).

    Returns {"ok": bool, "entries_checked": int, "broken_at_seq":
    int|None, "reason": str|None} — `reason` is always one of a small,
    fixed set of strings (never a fragment of row content), so this
    result is always safe to log/print in full."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM integrity_journal ORDER BY seq ASC")
        rows = cur.fetchall()

    expected_prev = GENESIS_HASH
    for i, row in enumerate(rows):
        if row["prev_hash"] != expected_prev:
            return {
                "ok": False, "entries_checked": i, "broken_at_seq": row["seq"],
                "reason": "prev_hash does not match the previous entry's entry_hash "
                          "(a row was edited, deleted, or reordered)",
            }
        recomputed = compute_entry_hash(
            row["prev_hash"], row["table_name"], row["row_pk"], row["row_content_hash"], row["seq"],
        )
        if recomputed != row["entry_hash"]:
            return {
                "ok": False, "entries_checked": i, "broken_at_seq": row["seq"],
                "reason": "stored entry_hash does not match its own recomputed hash "
                          "(this entry's own stored fields were altered in place)",
            }
        expected_prev = row["entry_hash"]

    return {"ok": True, "entries_checked": len(rows), "broken_at_seq": None, "reason": None}


def verify_row_against_journal(conn, table_name: str, row_pk: str, current_row: Dict[str, Any]) -> Dict[str, Any]:
    """Spot-check: does THIS row's CURRENT live content still match
    what was journaled for it? Only meaningful for rows that should
    never change after being journaled — class A/B tables (see
    agent/db/sql/schema.py's TABLE_CLASSIFICATION) — a legitimate
    class-C/D UPDATE will correctly show as a mismatch here too, so
    this function is not the right tool for those tables' own
    (intentionally mutable) state.

    Returns {"ok": bool, "journaled": bool, "reason": str|None}:
    journaled=False means this (table_name, row_pk) was never
    journaled at all (nothing to compare against, not itself a tamper
    finding); ok=False + journaled=True means a REAL content mismatch."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT row_content_hash FROM integrity_journal "
            "WHERE table_name=%s AND row_pk=%s ORDER BY seq DESC LIMIT 1",
            (table_name, row_pk),
        )
        row = cur.fetchone()
    if not row:
        return {"ok": True, "journaled": False, "reason": None}
    if content_hash(current_row) != row["row_content_hash"]:
        return {
            "ok": False, "journaled": True,
            "reason": f"live content of {table_name}#{row_pk} no longer matches its "
                      "journaled hash — either an out-of-band edit, or this table is "
                      "not actually class A/B (check TABLE_CLASSIFICATION before alarming)",
        }
    return {"ok": True, "journaled": True, "reason": None}


# ============================================================
# External anchor (owner's explicit choice: a separate git repository).
# NOT wired to any live schedule in this pass — see module docstring.
# ============================================================

def anchor_payload(head: Optional[Dict[str, Any]], entries_checked: int) -> Dict[str, Any]:
    """The JSON content that gets committed to the external anchor
    repo — deliberately tiny (a head pointer, not a dump of any actual
    row content, which must never leave this machine's own database).
    """
    return {
        "seq": head["seq"] if head else 0,
        "entry_hash": head["entry_hash"] if head else GENESIS_HASH,
        "entries_checked": entries_checked,
        "hash_algo": HASH_ALGO,
        "anchored_at": datetime.now(timezone.utc).isoformat(),
    }


def write_anchor_file(path: str, payload: Dict[str, Any]) -> None:
    """Writes the anchor payload as pretty JSON — a human/diff-friendly
    format is the point here (an owner or Codex reviewing this repo's
    git log should be able to read what changed at a glance), unlike
    canonical_row_json()'s deliberately compact, machine-only format."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def commit_anchor(repo_dir: str, relative_path: str, message: str) -> str:
    """`git add` + `git commit` the anchor file inside an ALREADY-
    EXISTING git working tree at `repo_dir` (this function does not
    `git init` anything itself, and never touches a remote — pushing
    is a separate, explicit, owner-configured step: which remote, how
    often, and under which credentials are operational decisions this
    module does not make on its own, same discipline as every other
    "designed, not executed against live infra" piece of this mandate).

    Returns the new commit's SHA. Raises CalledProcessError (propagated
    unmodified, never swallowed) if either git command fails — a
    silent anchor failure would be worse than a loud one, since a gap
    in the anchor history is itself something Layer 4 exists to make
    visible."""
    subprocess.run(
        ["git", "-C", repo_dir, "add", relative_path],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", repo_dir, "commit", "-m", message],
        check=True, capture_output=True, text=True,
    )
    result = subprocess.run(
        ["git", "-C", repo_dir, "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def anchor_chain_head(conn, repo_dir: str, relative_path: str = "anchor.json") -> Dict[str, Any]:
    """Convenience wrapper: verify_chain() + write_anchor_file() +
    commit_anchor(), in one call. Returns {"verify": <verify_chain()
    result>, "commit_sha": str, "payload": <anchor_payload() result>}.

    Deliberately does NOT `git push`. Deliberately does NOT get called
    from anywhere in this pass — see module docstring's "SCOPE OF THIS
    PASS". Whoever wires this in on a schedule (cron, systemd timer)
    is also the one who decides how/whether it pushes to a remote —
    this function's own job ends at "a new commit exists locally
    proving this repo's git history itself now contains the chain
    head at this point in time.\""""
    verify_result = verify_chain(conn)
    head = get_chain_head(conn)
    payload = anchor_payload(head, verify_result["entries_checked"])
    anchor_path = os.path.join(repo_dir, relative_path)
    write_anchor_file(anchor_path, payload)
    commit_sha = commit_anchor(
        repo_dir, relative_path,
        f"integrity anchor: seq={payload['seq']} entry_hash={payload['entry_hash'][:16]}...",
    )
    return {"verify": verify_result, "commit_sha": commit_sha, "payload": payload}
