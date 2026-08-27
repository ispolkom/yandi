"""
agent/orch_monitoring_regression_test.py — Foundation Repair regression:
orch_metrics.jsonl flush-on-shutdown
(YANDI_SELF_LEARNING_RECONCILIATION_AUDIT.md P1 "orch_metrics.jsonl
silently near-dead").

Root cause: record() only flushed its in-memory buffer every
_flush_every (10) events. A process exiting with fewer than 10 buffered
events since the last flush lost them silently — proven root cause of
registry/orch_metrics.jsonl going stale since 2026-07-15 despite ongoing
production traffic (frequent process restarts during active
development).

atexit behavior can't be observed by importing the module and calling
sys.exit() in-process (atexit handlers run during real interpreter
teardown, not on a caught SystemExit inside the same test process), so
this spawns a short-lived real subprocess that records a few events
(fewer than _flush_every) against a temp metrics file and exits
normally — then checks the file from the parent process.

IMPORTANT SCOPE NOTE: this proves the atexit fix works for a plain
script/CLI process exiting normally. It does NOT prove (and, verified by
a separate isolated repro during the fix, does NOT hold) that this same
mechanism flushes on the real production server's shutdown path
(pet/council_chat_server.py, run via uvicorn.run(...) with SIGTERM) —
uvicorn's graceful shutdown does not go through a path that triggers
atexit handlers here. See orch_monitoring.py's own comment on
atexit.register(flush) and YANDI_SELF_LEARNING_FOUNDATION_REPAIR_REPORT.md
for why that part of the fix was left as documented, verified-incomplete
debt rather than silently claimed complete.

Run: /home/iam/venv/bin/python3 -m agent.orch_monitoring_regression_test
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


PYTHON = sys.executable

with tempfile.TemporaryDirectory() as tmp_dir:
    metrics_file = Path(tmp_dir) / "orch_metrics.jsonl"

    # Record 3 events (well under _flush_every=10) and exit normally —
    # before the fix, none of these would ever reach disk.
    script = f"""
import sys
sys.path.insert(0, {str(Path(__file__).parent.parent)!r})
import agent.orch_monitoring as m
m.METRICS_FILE = {str(metrics_file)!r}
m.record("intent", 0.1, True)
m.record("enrich", 0.2, True)
m.record("synthesize", 0.3, False, timed_out=True)
"""
    result = subprocess.run([PYTHON, "-c", script], capture_output=True, text=True, timeout=30)

    check(
        "subprocess exits cleanly (no crash from the atexit registration itself)",
        result.returncode == 0,
        f"returncode={result.returncode} stderr={result.stderr[:500]}",
    )

    events = []
    if metrics_file.exists():
        events = [json.loads(l) for l in metrics_file.read_text().splitlines() if l.strip()]

    check(
        "all 3 buffered events (fewer than _flush_every=10) are on disk "
        "after normal process exit — previously: 0 would be written, "
        "silently, no error, no warning",
        len(events) == 3,
        f"events={events}",
    )
    check(
        "the flushed events are the real recorded ones, in order, with "
        "their real step names",
        [e["step"] for e in events] == ["intent", "enrich", "synthesize"],
        f"steps={[e.get('step') for e in events]}",
    )
    check(
        "the timed_out flag survives the buffered-then-atexit-flushed path",
        events and events[2].get("timed_out") is True and events[2].get("success") is False,
        f"last_event={events[2] if len(events) > 2 else None}",
    )

# ── in-process sanity: flush() is idempotent / safe on an empty buffer ──
import agent.orch_monitoring as m

with tempfile.TemporaryDirectory() as tmp_dir2:
    m.METRICS_FILE = Path(tmp_dir2) / "orch_metrics.jsonl"
    m._buffer.clear()
    m.flush()  # empty buffer, should be a silent no-op, not create a file/crash
    check(
        "flush() on an empty buffer does not create a file or raise",
        not m.METRICS_FILE.exists(),
        "",
    )
    m.record("cache", 0.05, True)
    check(
        "a single record() call under the flush threshold stays buffered "
        "in-process (flush is still lazy/batched during normal operation, "
        "not flushed on every single event — this fix only changes what "
        "happens at process exit, not the batching itself)",
        len(m._buffer) == 1 and not m.METRICS_FILE.exists(),
        f"buffer={m._buffer}",
    )
    m.flush()
    m._buffer.clear()

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
