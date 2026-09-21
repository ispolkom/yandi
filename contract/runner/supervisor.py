"""Executing the supervisor scenarios (contract/scenarios/p1/supervisor.json) against a supervisor implementation.

A supervisor is not an HTTP server, so the runner cannot talk to it. It talks to a HARNESS instead: a program the implementation
provides, which runs its supervisor against a stand-in core and prints what happened. The protocol is language-neutral:

  stdin   one JSON object  {"config": <scenario config>, "launches": [<behaviour of launch 1>, 2, ...],
                            "startup_timeout_ms": <int, optional>}
          launch behaviours: normal | crash_at_start | crash_after_ready | never_answers  (launches after the list behave normally)
  stdout  one JSON object per line, in order, each with "t_ms" and "event":
          core_spawned{launch,pid} awaiting_state{state} health_state{state} unlock_sent ready{launch} core_exited{launch,code}
          launch_failed{launch,category} restart_scheduled{attempt,delay_ms} restart_ceiling_reached
          failure_reported{category,message} transport_tick shutdown_done{graceful,terminated,killed} harness_done
          (`transport_tick` comes from a stand-in for the node's own transport, which must go on whatever the core does)

The scenario's `timeline` says what the core does; this module compiles it to `launches`. The scenario's `expect` says what the
supervisor must be seen doing; `evaluate` checks the trace against it, in order. The expectations are the frozen ones: nothing
here bends them to an implementation.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess

from .engine import Result

FAILURE_CATEGORIES = {"core_start_failed", "core_exited", "core_restart_exhausted", "core_unlock_refused"}


class TimelineError(ValueError):
    pass


def compile_launches(timeline: list[dict]) -> list[str]:
    """Timeline of core behaviour → the behaviour of each launch, in order.

    starts            a launch begins (normal unless something below says otherwise)
    answers_locked / answers_ready   what that launch reports (normal behaviour; `answers_ready` before a crash makes it a crash
                                     AFTER ready)
    exits_crash (times N)            this launch, and N-1 more after it, exit with an error
    never_answers (times N)          this launch, and N-1 more after it, run but never answer
    """
    launches: list[dict] = []
    for item in timeline:
        core, times = item["core"], item.get("times", 1)
        if core == "starts":
            launches.append({"ready": False, "behavior": "normal"})
        elif core == "answers_locked":
            continue
        elif core == "answers_ready":
            launches[-1]["ready"] = True
        elif core in ("exits_crash", "never_answers"):
            if not launches:
                raise TimelineError(f"'{core}' before any 'starts'")
            last = launches[-1]
            behavior = "never_answers" if core == "never_answers" else ("crash_after_ready" if last["ready"] else "crash_at_start")
            last["behavior"] = behavior
            for _ in range(times - 1):
                launches.append({"ready": False, "behavior": behavior})
        else:
            raise TimelineError(f"timeline item '{core}' has no meaning for the harness protocol yet")
    return [launch["behavior"] for launch in launches]


def parse_trace(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            events.append(json.loads(line))
    return events


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def evaluate(expect: list[dict], events: list[dict], leaks: dict, *, check_processes: bool = True) -> list[str]:
    """Problems found in the trace (empty = the supervisor did what the scenario expects)."""
    problems: list[str] = []
    names = [e["event"] for e in events]

    def find(kind: str, start: int, where=lambda e: True) -> int:
        for i in range(start, len(events)):
            if events[i]["event"] == kind and where(events[i]):
                return i
        return -1

    if "harness_done" not in names:
        problems.append("the harness did not finish (no harness_done)")
    if "shutdown_done" not in names:
        problems.append("the supervisor never reported its shutdown")
    if check_processes:
        pids = [e["pid"] for e in events if e["event"] == "core_spawned"]
        left = [p for p in pids if _alive(p)]
        if left:
            problems.append(f"core process(es) left running after the supervisor stopped: {left}")

    cursor = 0
    for n, step in enumerate(expect, 1):
        what = step["node"]
        label = f"expectation {n} ({what})"
        if what == "starts_core":
            i = find("core_spawned", cursor)
            if i < 0:
                problems.append(f"{label}: no core was spawned")
                continue
            cursor = i + 1
        elif what == "polls_health":
            state = step.get("until_state")
            i = find("awaiting_state", cursor, lambda e: state is None or e.get("state") == state)
            if i < 0:
                problems.append(f"{label}: the supervisor never waited for '{state}' (after position {cursor})")
                continue
            cursor = i + 1
        elif what == "unlocks":
            i = find("unlock_sent", cursor)
            if i < 0:
                problems.append(f"{label}: no unlock request was sent (after position {cursor})")
                continue
            cursor = i + 1
        elif what == "restarts_core":
            restarts = [(i, e) for i, e in enumerate(events) if e["event"] == "restart_scheduled"]
            wanted = step.get("attempts")
            if not restarts or (wanted is not None and len(restarts) != wanted):
                problems.append(f"{label}: {len(restarts)} restart(s), expected {wanted if wanted is not None else 'at least one'}")
                continue
            if step.get("delays_non_decreasing"):
                delays = [e["delay_ms"] for _, e in restarts]
                if any(b < a for a, b in zip(delays, delays[1:])):
                    problems.append(f"{label}: the waits between restarts shrank: {delays}")
                if delays and min(delays) <= 0:
                    problems.append(f"{label}: a restart without any wait (busy loop): {delays}")
                spawns = {e["launch"]: e["t_ms"] for e in events if e["event"] == "core_spawned"}
                for (idx, e) in restarts:
                    later = [s for s in events[idx:] if s["event"] == "core_spawned"]
                    if later and later[0]["t_ms"] - e["t_ms"] + 25 < e["delay_ms"]:
                        problems.append(f"{label}: the core was respawned {later[0]['t_ms'] - e['t_ms']} ms after the restart was scheduled, before its {e['delay_ms']} ms wait")
            cursor = max(cursor, restarts[-1][0] + 1)
        elif what == "stops_restarting":
            i = find("restart_ceiling_reached", cursor)
            if i < 0:
                problems.append(f"{label}: the supervisor never stopped restarting")
                continue
            if find("core_spawned", i + 1) >= 0:
                problems.append(f"{label}: it spawned the core again after giving up")
            cursor = i + 1
        elif what == "reports_failure":
            i = find("failure_reported", cursor)
            if i < 0:
                problems.append(f"{label}: no failure was reported (after position {cursor})")
                continue
            event = events[i]
            if event.get("category") not in FAILURE_CATEGORIES:
                problems.append(f"{label}: the failure category '{event.get('category')}' is not a known one")
            text = f"{event.get('category', '')} {event.get('message', '')}"
            for item in step.get("reason_must_not_contain", []):
                groups = sorted(leaks) if item == "@leaks" else ([item[len('@leaks:'):]] if item.startswith("@leaks:") else None)
                if groups is None:
                    if item in text:
                        problems.append(f"{label}: the reason contains '{item}'")
                    continue
                for g in groups:
                    for pattern in leaks[g]:
                        m = re.search(pattern, text)
                        if m:
                            problems.append(f"{label}: the reason leaks ({g}): '{m.group(0)}'")
            cursor = i + 1
        elif what == "keeps_serving_transport":
            ticks = [i for i, name in enumerate(names) if name == "transport_tick"]
            exits = [i for i, name in enumerate(names) if name == "core_exited"]
            if not exits:
                problems.append(f"{label}: the core never exited, so nothing was tested")
            elif len([t for t in ticks if t > exits[0]]) < 3 or len([t for t in ticks if t > exits[-1]]) < 2:
                problems.append(f"{label}: the node's transport stopped ticking after the core died")
        else:
            problems.append(f"{label}: the runner does not know this expectation")
    return problems


class SupervisorHarness:
    """A supervisor implementation, reached through its harness command (a program that follows the protocol above)."""

    def __init__(self, command: str, timeout_s: float = 90.0):
        self.argv = shlex.split(command)
        self.timeout_s = timeout_s
        self.name = f"supervisor harness {os.path.basename(self.argv[0])}"

    def run(self, sc, leaks: dict) -> Result:
        data = sc.data
        res = Result(id=sc.id, status="pass", file=sc.file)
        try:
            launches = compile_launches(data["timeline"])
        except TimelineError as exc:
            res.status, res.detail = "error", f"cannot compile the timeline: {exc}"
            return res
        request = {"config": data["config"], "launches": launches,
                   "startup_timeout_ms": 700 if "never_answers" in launches else 4000}
        try:
            proc = subprocess.run(self.argv, input=json.dumps(request), capture_output=True, text=True, timeout=self.timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            res.status, res.detail = "error", f"the harness could not be run: {type(exc).__name__}"
            return res
        try:
            events = parse_trace(proc.stdout)
        except ValueError:
            res.status, res.detail = "fail", "the harness printed something that is not a JSON trace"
            return res
        problems = evaluate(data["expect"], events, leaks)
        if proc.returncode != 0:
            problems.insert(0, f"the harness (the supervisor's host process) exited with status {proc.returncode}")
        res.steps = [{"step": e["event"], **{k: v for k, v in e.items() if k in ("state", "launch", "delay_ms", "category")}} for e in events if e["event"] != "transport_tick"][:60]
        if problems:
            res.status, res.detail = "fail", "; ".join(problems)
        return res
